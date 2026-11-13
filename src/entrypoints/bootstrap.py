"""Единый корень композиции (Composition Root) и управления жизненным циклом приложения."""

from dataclasses import dataclass

from src.application import AdministrationService, EventImportService, EventSearchService, SubscriptionService
from src.core.configuration import (
    ApplicationSettings,
    DatabaseSettings,
    LoggingSettings,
    WebSettings,
    get_application_settings,
    get_database_settings,
    get_logging_settings,
    get_web_settings,
)
from src.core.logger import get_logger, setup_logger
from src.infrastructure.database import (
    DatabaseEngineManager,
    PostgreSqlAdministrationRepository,
    PostgreSqlDialogRepository,
    PostgreSqlEventRepository,
    PostgreSqlRawEventRepository,
)

__all__ = ["ApplicationContainer", "build_application_container"]

logger = get_logger(__name__)


@dataclass(slots=True, kw_only=True)
class ApplicationContainer:
    """Контейнер собранных зависимостей конфигурации, БД, репозиториев и прикладных сервисов.

    Attributes:
        application_settings: Базовые настройки сервиса и среды запуска.
        database_settings: Параметры подключения к PostgreSQL 17.
        logging_settings: Настройки структурного логирования.
        web_settings: Настройки веб-сервера, REST API и сессий модератора.
        database_engine_manager: Менеджер пула подключений и жизненного цикла SQLAlchemy.
        event_repository: Репозиторий каталога мероприятий и справочников.
        raw_event_repository: Репозиторий сырых записей внешних источников.
        dialog_repository: Репозиторий истории диалогов, подтверждений и трассировки.
        administration_repository: Репозиторий модерации, аудита, подписок и обращений.
        search_service: Прикладной сервис поиска, фильтрации и объяснения пустой выдачи.
        import_service: Прикладной сервис идемпотентного импорта CSV и JSON.
        subscription_service: Прикладной сервис подписок и обратной связи.
        administration_service: Прикладной сервис модерации, ручного управления и аудита.
    """

    application_settings: ApplicationSettings
    database_settings: DatabaseSettings
    logging_settings: LoggingSettings
    web_settings: WebSettings
    database_engine_manager: DatabaseEngineManager
    event_repository: PostgreSqlEventRepository
    raw_event_repository: PostgreSqlRawEventRepository
    dialog_repository: PostgreSqlDialogRepository
    administration_repository: PostgreSqlAdministrationRepository
    search_service: EventSearchService
    import_service: EventImportService
    subscription_service: SubscriptionService
    administration_service: AdministrationService

    async def startup(self) -> None:
        """Инициализирует инфраструктурные подключения при старте точки входа."""
        setup_logger(self.logging_settings)
        await self.database_engine_manager.initialize()
        logger.info("Контейнер зависимостей приложения запущен.")

    async def shutdown(self) -> None:
        """Корректно закрывает подключения к базе данных и освобождает ресурсы."""
        await self.database_engine_manager.close()
        logger.info("Ресурсы контейнера зависимостей корректно освобождены.")


def build_application_container(
    *,
    application_settings: ApplicationSettings | None = None,
    database_settings: DatabaseSettings | None = None,
    logging_settings: LoggingSettings | None = None,
    web_settings: WebSettings | None = None,
) -> ApplicationContainer:
    """Собирает граф зависимостей приложения поверх настроек окружения.

    Arguments:
        application_settings: Явная переопределённая конфигурация приложения.
        database_settings: Явная переопределённая конфигурация PostgreSQL.
        logging_settings: Явная переопределённая конфигурация логирования.
        web_settings: Явная переопределённая конфигурация веб-интерфейса.

    Returns:
        Полностью настроенный экземпляр `ApplicationContainer`.
    """
    resolved_application_settings = application_settings or get_application_settings()
    resolved_database_settings = database_settings or get_database_settings()
    resolved_logging_settings = logging_settings or get_logging_settings()
    resolved_web_settings = web_settings or get_web_settings()

    engine_manager = DatabaseEngineManager(resolved_database_settings)

    event_repository = PostgreSqlEventRepository(engine_manager)
    raw_event_repository = PostgreSqlRawEventRepository(engine_manager)
    dialog_repository = PostgreSqlDialogRepository(engine_manager)
    administration_repository = PostgreSqlAdministrationRepository(engine_manager)

    search_service = EventSearchService(event_repository=event_repository)
    import_service = EventImportService(
        event_repository=event_repository, administration_repository=administration_repository
    )
    subscription_service = SubscriptionService(
        administration_repository=administration_repository, event_repository=event_repository
    )
    administration_service = AdministrationService(
        event_repository=event_repository, administration_repository=administration_repository
    )

    return ApplicationContainer(
        application_settings=resolved_application_settings,
        database_settings=resolved_database_settings,
        logging_settings=resolved_logging_settings,
        web_settings=resolved_web_settings,
        database_engine_manager=engine_manager,
        event_repository=event_repository,
        raw_event_repository=raw_event_repository,
        dialog_repository=dialog_repository,
        administration_repository=administration_repository,
        search_service=search_service,
        import_service=import_service,
        subscription_service=subscription_service,
        administration_service=administration_service,
    )
