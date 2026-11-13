"""Управление асинхронным движком, пулом подключений и сессиями PostgreSQL."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Self

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.core.configuration import DatabaseSettings, get_database_settings
from src.core.exceptions import (
    LifecycleClientClosedError,
    LifecycleClientNotInitializedError,
    NetworkConnectionFailureError,
    NetworkConnectionTimeoutError,
    ServiceUnavailableError,
)
from src.core.logger import get_logger

__all__ = ["DatabaseEngineManager", "DatabaseLifecycleState"]

logger = get_logger(__name__)


class DatabaseLifecycleState(StrEnum):
    """Состояние жизненного цикла менеджера подключений к базе данных.

    Attributes:
        UNINITIALIZED: Менеджер создан, движок и пул подключений ещё не инициализированы.
        ACTIVE: Движок и фабрика сессий инициализированы и готовы к работе.
        CLOSED: Менеджер закрыт, пул подключений освобождён, повторное открытие запрещено.
    """

    UNINITIALIZED = "uninitialized"
    ACTIVE = "active"
    CLOSED = "closed"


class DatabaseEngineManager:
    """Менеджер асинхронного движка SQLAlchemy и фабрики сессий для PostgreSQL.

    Гарантирует явный жизненный цикл (`initialize` -> `session` / `check_health` -> `close`),
    идемпотентное открытие и закрытие, а также единую трансляцию ошибок подключения
    в инфраструктурные исключения ядра.
    """

    def __init__(self, settings: DatabaseSettings | None = None) -> None:
        """Создаёт экземпляр менеджера движка базы данных.

        Arguments:
            settings: Необязательные настройки базы данных; при отсутствии загружаются из конфигурации ядра.
        """
        self._settings = settings if settings is not None else get_database_settings()
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._lifecycle_state = DatabaseLifecycleState.UNINITIALIZED

    @property
    def settings(self) -> DatabaseSettings:
        """Возвращает используемую конфигурацию базы данных.

        Returns:
            Неизменяемый объект настроек базы данных.
        """
        return self._settings

    @property
    def lifecycle_state(self) -> DatabaseLifecycleState:
        """Возвращает текущее состояние жизненного цикла менеджера.

        Returns:
            Текущее состояние (`UNINITIALIZED`, `ACTIVE` или `CLOSED`).
        """
        return self._lifecycle_state

    @property
    def engine(self) -> AsyncEngine:
        """Возвращает активный асинхронный движок SQLAlchemy.

        Returns:
            Инициализированный экземпляр `AsyncEngine`.

        Raises:
            LifecycleClientNotInitializedError: Если метод `initialize` ещё не вызывался.
            LifecycleClientClosedError: Если менеджер уже закрыт.
        """
        engine, _ = self._ensure_active()
        return engine

    async def initialize(self) -> None:
        """Инициализирует асинхронный движок и фабрику сессий базы данных.

        Повторный вызов в активном состоянии является безопасной идемпотентной операцией.

        Raises:
            LifecycleClientClosedError: При попытке повторно открыть уже закрытый менеджер.
        """
        if self._lifecycle_state is DatabaseLifecycleState.CLOSED:
            raise LifecycleClientClosedError("Нельзя инициализировать менеджер базы данных после его закрытия.")
        if self._lifecycle_state is DatabaseLifecycleState.ACTIVE:
            return

        engine = create_async_engine(
            url=self._settings.connection_url,
            pool_size=self._settings.POOL_SIZE,
            max_overflow=self._settings.MAXIMUM_OVERFLOW,
            pool_timeout=self._settings.POOL_TIMEOUT_SECONDS,
            pool_pre_ping=True,
            echo=self._settings.ECHO_SQL,
        )
        self._engine = engine
        self._session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        self._lifecycle_state = DatabaseLifecycleState.ACTIVE
        logger.info(
            "Инициализирован движок PostgreSQL (%s:%s/%s, размер пула=%s, резерв=%s).",
            self._settings.HOST,
            self._settings.PORT,
            self._settings.NAME,
            self._settings.POOL_SIZE,
            self._settings.MAXIMUM_OVERFLOW,
        )

    async def close(self) -> None:
        """Закрывает пул подключений и переводит менеджер в закрытое состояние.

        Повторный вызов является безопасной идемпотентной операцией.
        """
        if self._lifecycle_state is DatabaseLifecycleState.CLOSED:
            return

        engine = self._engine
        self._engine = None
        self._session_factory = None
        self._lifecycle_state = DatabaseLifecycleState.CLOSED

        if engine is not None:
            await engine.dispose()
        logger.info("Движок и пул подключений PostgreSQL закрыты.")

    async def __aenter__(self) -> Self:
        """Инициализирует менеджер при входе в асинхронный контекстный менеджер.

        Returns:
            Текущий инициализированный экземпляр менеджера.
        """
        await self.initialize()
        return self

    async def __aexit__(self, *_: object) -> None:
        """Закрывает менеджер при выходе из асинхронного контекстного менеджера."""
        await self.close()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Предоставляет асинхронную сессию SQLAlchemy с откатом при исключении.

        Yields:
            Асинхронная сессия `AsyncSession`.

        Raises:
            LifecycleClientNotInitializedError: Если менеджер не инициализирован.
            LifecycleClientClosedError: Если менеджер уже закрыт.
        """
        _, session_factory = self._ensure_active()
        async with session_factory() as active_session:
            try:
                yield active_session
            except Exception:
                await active_session.rollback()
                raise

    async def check_health(self) -> bool:
        """Проверяет доступность PostgreSQL выполнением пробного запроса `SELECT 1`.

        Returns:
            `True`, если база данных ответила корректно.

        Raises:
            LifecycleClientNotInitializedError: Если менеджер не инициализирован.
            LifecycleClientClosedError: Если менеджер уже закрыт.
            NetworkConnectionTimeoutError: При превышении времени ожидания ответа базы данных.
            NetworkConnectionFailureError: При сетевом отказе подключения к серверу.
            ServiceUnavailableError: При ошибке SQLAlchemy или отказе сервера базы данных.
        """
        self._ensure_active()
        try:
            async with self.session() as active_session:
                result = await active_session.execute(text("SELECT 1"))
                return bool(result.scalar_one() == 1)
        except TimeoutError as error:
            raise NetworkConnectionTimeoutError("Превышено время ожидания проверки доступности PostgreSQL.") from error
        except OSError as error:
            raise NetworkConnectionFailureError("Сетевая ошибка при проверке доступности PostgreSQL.") from error
        except SQLAlchemyError as error:
            raise ServiceUnavailableError("База данных PostgreSQL недоступна при проверке состояния.") from error

    def _ensure_active(self) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
        if self._lifecycle_state is DatabaseLifecycleState.UNINITIALIZED:
            raise LifecycleClientNotInitializedError(
                "Менеджер базы данных не инициализирован; вызовите initialize() перед использованием."
            )
        if self._lifecycle_state is DatabaseLifecycleState.CLOSED:
            raise LifecycleClientClosedError("Менеджер базы данных уже закрыт; операции с сессиями невозможны.")
        if self._session_factory is None or self._engine is None:
            raise LifecycleClientNotInitializedError("Фабрика сессий базы данных не готова к работе.")
        return self._engine, self._session_factory
