"""Общие фикстуры изолированной тестовой базы данных PostgreSQL 17."""

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.configuration import PROJECT_ROOT, DatabaseSettings, get_database_settings
from src.infrastructure.database import (
    DatabaseEngineManager,
    PostgreSqlAdministrationRepository,
    PostgreSqlDialogRepository,
    PostgreSqlEventRepository,
    PostgreSqlRawEventRepository,
)
from src.infrastructure.database.tables import DatabaseBase

_STATIC_REFERENCE_TABLES: frozenset[str] = frozenset({"categories"})


def _build_test_database_settings() -> DatabaseSettings:
    """Формирует конфигурацию отдельной тестовой базы данных `_test`."""
    base_settings = get_database_settings()
    target_name = base_settings.NAME if base_settings.NAME.endswith("_test") else f"{base_settings.NAME}_test"
    return base_settings.model_copy(update={"NAME": target_name})


async def _ensure_test_database_exists(settings: DatabaseSettings) -> None:
    """Создаёт изолированную тестовую базу данных в PostgreSQL, если она ещё не существует."""
    admin_settings = settings.model_copy(update={"NAME": "postgres"})
    engine = create_async_engine(admin_settings.connection_url, isolation_level="AUTOCOMMIT", poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            exists = (
                await connection.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :database_name"), {"database_name": settings.NAME}
                )
            ).scalar_one_or_none()
            if not exists:
                escaped_identifier = settings.NAME.replace('"', '""')
                await connection.execute(text(f'CREATE DATABASE "{escaped_identifier}"'))
    finally:
        await engine.dispose()


def _apply_alembic_migrations(settings: DatabaseSettings) -> None:
    """Применяет все миграции Alembic до актуальной ревизии `head`."""
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    escaped_url = settings.connection_url.replace("%", "%%")
    alembic_config.set_main_option("sqlalchemy.url", escaped_url)
    command.upgrade(alembic_config, "head")


@pytest.fixture(scope="session")
def test_database_settings() -> DatabaseSettings:
    """Готовит изолированную тестовую базу данных и применяет схему Alembic один раз за прогон."""
    settings = _build_test_database_settings()
    asyncio.run(_ensure_test_database_exists(settings))
    _apply_alembic_migrations(settings)
    return settings


@pytest_asyncio.fixture
async def database_engine_manager(test_database_settings: DatabaseSettings) -> AsyncIterator[DatabaseEngineManager]:
    """Создаёт чистый `DatabaseEngineManager` и очищает изменяемые таблицы до и после теста."""
    mutable_tables = [
        table.name for table in DatabaseBase.metadata.sorted_tables if table.name not in _STATIC_REFERENCE_TABLES
    ]
    truncate_sql = f"TRUNCATE TABLE {', '.join(mutable_tables)} RESTART IDENTITY CASCADE"

    async with DatabaseEngineManager(test_database_settings) as manager:
        async with manager.session() as session, session.begin():
            await session.execute(text(truncate_sql))
        try:
            yield manager
        finally:
            async with manager.session() as session, session.begin():
                await session.execute(text(truncate_sql))


@pytest_asyncio.fixture
async def event_repository(database_engine_manager: DatabaseEngineManager) -> PostgreSqlEventRepository:
    """Возвращает экземпляр `PostgreSqlEventRepository` поверх тестового пула подключений."""
    return PostgreSqlEventRepository(database_engine_manager)


@pytest_asyncio.fixture
async def raw_event_repository(database_engine_manager: DatabaseEngineManager) -> PostgreSqlRawEventRepository:
    """Возвращает экземпляр `PostgreSqlRawEventRepository` поверх тестового пула подключений."""
    return PostgreSqlRawEventRepository(database_engine_manager)


@pytest_asyncio.fixture
async def dialog_repository(database_engine_manager: DatabaseEngineManager) -> PostgreSqlDialogRepository:
    """Возвращает экземпляр `PostgreSqlDialogRepository` поверх тестового пула подключений."""
    return PostgreSqlDialogRepository(database_engine_manager)


@pytest_asyncio.fixture
async def administration_repository(
    database_engine_manager: DatabaseEngineManager,
) -> PostgreSqlAdministrationRepository:
    """Возвращает экземпляр `PostgreSqlAdministrationRepository` поверх тестового пула подключений."""
    return PostgreSqlAdministrationRepository(database_engine_manager)
