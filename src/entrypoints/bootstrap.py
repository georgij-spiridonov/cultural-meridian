"""Единый корень композиции (Composition Root) и управления жизненным циклом приложения."""

from dataclasses import dataclass, field

from openai import AsyncOpenAI

from src.application import (
    AdministrationService,
    AgentDialogService,
    EventImportService,
    EventIngestionService,
    EventSearchService,
    FallbackSearchService,
    SubscriptionService,
    WeekendPlannerService,
)
from src.core.configuration import (
    PROJECT_ROOT,
    ApplicationSettings,
    DatabaseSettings,
    GeolocationSettings,
    IngestionSettings,
    LanguageModelSettings,
    LoggingSettings,
    TelegramSettings,
    WebSettings,
    get_application_settings,
    get_database_settings,
    get_geolocation_settings,
    get_ingestion_settings,
    get_language_model_settings,
    get_logging_settings,
    get_telegram_settings,
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
from src.infrastructure.geolocation import HybridGeolocationResolver
from src.infrastructure.language_model import OpenAiAgentOrchestrator, OpenAiStructuredExtractor, create_openai_client
from src.infrastructure.notifications import TelegramSubscriptionNotificationSender
from src.infrastructure.sources import CompositeSourceFetcher, HttpLinkChecker, PoliteHttpClient

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
        telegram_settings: Настройки Telegram-бота и параметров диалоговой выдачи.
        language_model_settings: Настройки языковой модели, провайдера и суточных бюджетов.
        database_engine_manager: Менеджер пула подключений и жизненного цикла SQLAlchemy.
        event_repository: Репозиторий каталога мероприятий и справочников.
        raw_event_repository: Репозиторий сырых записей внешних источников.
        dialog_repository: Репозиторий истории диалогов, подтверждений и трассировки.
        administration_repository: Репозиторий модерации, аудита, подписок и обращений.
        structured_extractor: Адаптер извлечения структурированных полей на быстрой модели.
        agent_orchestrator: Адаптер ИИ-агента (или `None`, если не задан `LANGUAGE_MODEL_API_KEY`).
        search_service: Прикладной сервис поиска, фильтрации и объяснения пустой выдачи.
        import_service: Прикладной сервис идемпотентного импорта CSV и JSON.
        subscription_service: Прикладной сервис подписок и обратной связи.
        administration_service: Прикладной сервис модерации, ручного управления и аудита.
        weekend_planner: Прикладной планировщик маршрута «Выходной день».
        fallback_search: Детерминированный резервный поиск без языковой модели.
        agent_dialog_service: Оркестратор многошагового диалога с ИИ-агентом и фоллбэком.
        geolocation_settings: Настройки геолокации, справочника координат и оценки переездов.
        geolocation_resolver: Гибридный адаптер геокодирования и расчёта времени в пути.
        ingestion_settings: Настройки агента сбора данных, фонового воркера и валидации фидов.
        source_fetcher: Маршрутизирующий загрузчик внешних и локальных источников (RSS, JSON, HTML).
        link_checker: Адаптер проверки доступности ссылок на первоисточники.
        event_ingestion_service: Прикладной конвейер сбора, гибридной нормализации и дедупликации.
        telegram_notification_sender: Адаптер доставки уведомлений по подпискам в Telegram (или `None`).
        source_http_client: Общий вежливый HTTP-клиент загрузчиков и проверки ссылок.
    """

    application_settings: ApplicationSettings
    database_settings: DatabaseSettings
    logging_settings: LoggingSettings
    web_settings: WebSettings
    telegram_settings: TelegramSettings
    language_model_settings: LanguageModelSettings
    database_engine_manager: DatabaseEngineManager
    event_repository: PostgreSqlEventRepository
    raw_event_repository: PostgreSqlRawEventRepository
    dialog_repository: PostgreSqlDialogRepository
    administration_repository: PostgreSqlAdministrationRepository
    structured_extractor: OpenAiStructuredExtractor
    agent_orchestrator: OpenAiAgentOrchestrator | None
    search_service: EventSearchService
    import_service: EventImportService
    subscription_service: SubscriptionService
    administration_service: AdministrationService
    weekend_planner: WeekendPlannerService
    fallback_search: FallbackSearchService
    agent_dialog_service: AgentDialogService
    geolocation_settings: GeolocationSettings = field(default_factory=get_geolocation_settings)
    geolocation_resolver: HybridGeolocationResolver | None = None
    ingestion_settings: IngestionSettings = field(default_factory=get_ingestion_settings)
    source_fetcher: CompositeSourceFetcher | None = None
    link_checker: HttpLinkChecker | None = None
    event_ingestion_service: EventIngestionService | None = None
    telegram_notification_sender: TelegramSubscriptionNotificationSender | None = None
    source_http_client: PoliteHttpClient | None = None
    _openai_client: AsyncOpenAI | None = field(default=None, repr=False)

    async def startup(self) -> None:
        """Инициализирует инфраструктурные подключения при старте точки входа."""
        setup_logger(self.logging_settings)
        await self.database_engine_manager.initialize()
        logger.info("Контейнер зависимостей приложения запущен.")

    async def shutdown(self) -> None:
        """Корректно закрывает подключения к БД, сборщику, проверщику ссылок, уведомлениям, геокодеру и LLM."""
        if self.telegram_notification_sender is not None:
            await self.telegram_notification_sender.close()
        if self.source_fetcher is not None:
            await self.source_fetcher.close()
        if self.link_checker is not None:
            await self.link_checker.close()
        if self.source_http_client is not None:
            await self.source_http_client.close()
        if self.geolocation_resolver is not None:
            await self.geolocation_resolver.close()
        if self._openai_client is not None:
            await self._openai_client.close()
        await self.database_engine_manager.close()
        logger.info("Ресурсы контейнера зависимостей корректно освобождены.")


def build_application_container(
    *,
    application_settings: ApplicationSettings | None = None,
    database_settings: DatabaseSettings | None = None,
    logging_settings: LoggingSettings | None = None,
    web_settings: WebSettings | None = None,
    telegram_settings: TelegramSettings | None = None,
    language_model_settings: LanguageModelSettings | None = None,
    geolocation_settings: GeolocationSettings | None = None,
    ingestion_settings: IngestionSettings | None = None,
) -> ApplicationContainer:
    """Собирает граф зависимостей приложения поверх настроек окружения.

    Arguments:
        application_settings: Явная переопределённая конфигурация приложения.
        database_settings: Явная переопределённая конфигурация PostgreSQL.
        logging_settings: Явная переопределённая конфигурация логирования.
        web_settings: Явная переопределённая конфигурация веб-интерфейса.
        telegram_settings: Явная переопределённая конфигурация Telegram-бота.
        language_model_settings: Явная переопределённая конфигурация языковой модели.
        geolocation_settings: Явная переопределённая конфигурация геолокации.
        ingestion_settings: Явная переопределённая конфигурация фонового сбора и проверки источников.

    Returns:
        Полностью настроенный экземпляр `ApplicationContainer`.
    """
    resolved_application_settings = application_settings or get_application_settings()
    resolved_database_settings = database_settings or get_database_settings()
    resolved_logging_settings = logging_settings or get_logging_settings()
    resolved_web_settings = web_settings or get_web_settings()
    resolved_telegram_settings = telegram_settings or get_telegram_settings()
    resolved_language_model_settings = language_model_settings or get_language_model_settings()
    resolved_geolocation_settings = geolocation_settings or get_geolocation_settings()
    resolved_ingestion_settings = ingestion_settings or get_ingestion_settings()

    engine_manager = DatabaseEngineManager(resolved_database_settings)

    event_repository = PostgreSqlEventRepository(engine_manager)
    raw_event_repository = PostgreSqlRawEventRepository(engine_manager)
    dialog_repository = PostgreSqlDialogRepository(engine_manager)
    administration_repository = PostgreSqlAdministrationRepository(engine_manager)

    openai_client: AsyncOpenAI | None = None
    agent_orchestrator: OpenAiAgentOrchestrator | None = None
    if resolved_language_model_settings.API_KEY.strip():
        openai_client = create_openai_client(resolved_language_model_settings)
        agent_orchestrator = OpenAiAgentOrchestrator(
            settings=resolved_language_model_settings, openai_client=openai_client
        )

    structured_extractor = OpenAiStructuredExtractor(
        settings=resolved_language_model_settings, openai_client=openai_client
    )

    geolocation_resolver = HybridGeolocationResolver(
        settings=resolved_geolocation_settings, event_repository=event_repository
    )

    source_http_client = PoliteHttpClient(
        user_agent=resolved_ingestion_settings.USER_AGENT,
        max_concurrent_requests_per_host=resolved_ingestion_settings.MAX_CONCURRENT_REQUESTS_PER_HOST,
        request_delay_seconds=resolved_ingestion_settings.REQUEST_DELAY_SECONDS,
    )
    source_fetcher = CompositeSourceFetcher(
        project_root=PROJECT_ROOT,
        local_feeds_directory=resolved_ingestion_settings.LOCAL_FEEDS_DIRECTORY,
        user_agent=resolved_ingestion_settings.USER_AGENT,
        polite_client=source_http_client,
        sources_config_path=resolved_ingestion_settings.SOURCES_CONFIG_PATH,
    )
    link_checker = HttpLinkChecker(
        user_agent=resolved_ingestion_settings.USER_AGENT,
        default_timeout_seconds=resolved_ingestion_settings.LINK_CHECK_TIMEOUT_SECONDS,
        project_root=PROJECT_ROOT,
        local_feeds_directory=resolved_ingestion_settings.LOCAL_FEEDS_DIRECTORY,
        verify_local_file_links=resolved_ingestion_settings.VERIFY_LOCAL_FILE_LINKS,
        polite_client=source_http_client,
    )
    telegram_notification_sender: TelegramSubscriptionNotificationSender | None = None
    bot_token = resolved_telegram_settings.BOT_TOKEN.strip()
    if bot_token:
        telegram_notification_sender = TelegramSubscriptionNotificationSender(
            bot_token=bot_token,
            public_web_base_url=resolved_telegram_settings.PUBLIC_WEB_BASE_URL,
            request_timeout_seconds=resolved_telegram_settings.NOTIFICATION_REQUEST_TIMEOUT_SECONDS,
        )

    subscription_service = SubscriptionService(
        administration_repository=administration_repository,
        event_repository=event_repository,
        telegram_notification_sender=telegram_notification_sender,
        notification_batch_size=resolved_telegram_settings.NOTIFICATION_BATCH_SIZE,
        notification_max_attempts=resolved_telegram_settings.NOTIFICATION_MAX_ATTEMPTS,
    )

    event_ingestion_service = EventIngestionService(
        source_fetcher=source_fetcher,
        link_checker=link_checker,
        raw_event_repository=raw_event_repository,
        event_repository=event_repository,
        administration_repository=administration_repository,
        structured_extractor=structured_extractor if resolved_ingestion_settings.HYBRID_LLM_ENABLED else None,
        subscription_service=subscription_service,
        confidence_threshold=resolved_ingestion_settings.CONFIDENCE_THRESHOLD,
        link_check_timeout_seconds=resolved_ingestion_settings.LINK_CHECK_TIMEOUT_SECONDS,
    )

    search_service = EventSearchService(event_repository=event_repository)
    import_service = EventImportService(
        event_repository=event_repository,
        administration_repository=administration_repository,
        subscription_service=subscription_service,
    )
    administration_service = AdministrationService(
        event_repository=event_repository,
        administration_repository=administration_repository,
        subscription_service=subscription_service,
    )
    weekend_planner = WeekendPlannerService(
        event_repository=event_repository, geolocation_resolver=geolocation_resolver
    )
    fallback_search = FallbackSearchService(event_repository=event_repository, weekend_planner=weekend_planner)
    agent_dialog_service = AgentDialogService(
        event_repository=event_repository,
        dialog_repository=dialog_repository,
        administration_repository=administration_repository,
        search_service=search_service,
        subscription_service=subscription_service,
        weekend_planner=weekend_planner,
        fallback_search=fallback_search,
        agent_orchestrator=agent_orchestrator,
        language_model_settings=resolved_language_model_settings,
        pending_action_ttl_seconds=resolved_telegram_settings.PENDING_ACTION_LIFETIME_SECONDS,
        dialog_history_limit=resolved_telegram_settings.DIALOG_HISTORY_LIMIT,
    )

    return ApplicationContainer(
        application_settings=resolved_application_settings,
        database_settings=resolved_database_settings,
        logging_settings=resolved_logging_settings,
        web_settings=resolved_web_settings,
        telegram_settings=resolved_telegram_settings,
        language_model_settings=resolved_language_model_settings,
        database_engine_manager=engine_manager,
        event_repository=event_repository,
        raw_event_repository=raw_event_repository,
        dialog_repository=dialog_repository,
        administration_repository=administration_repository,
        structured_extractor=structured_extractor,
        agent_orchestrator=agent_orchestrator,
        search_service=search_service,
        import_service=import_service,
        subscription_service=subscription_service,
        administration_service=administration_service,
        weekend_planner=weekend_planner,
        fallback_search=fallback_search,
        agent_dialog_service=agent_dialog_service,
        geolocation_settings=resolved_geolocation_settings,
        geolocation_resolver=geolocation_resolver,
        ingestion_settings=resolved_ingestion_settings,
        source_fetcher=source_fetcher,
        link_checker=link_checker,
        event_ingestion_service=event_ingestion_service,
        telegram_notification_sender=telegram_notification_sender,
        source_http_client=source_http_client,
        _openai_client=openai_client,
    )
