"""Контрактные тесты соблюдения доменных `Protocol`-портов конкретными адаптерами."""

import inspect
from collections.abc import Callable
from typing import Any, get_protocol_members, get_type_hints

import pytest

from src.core.configuration import DatabaseSettings, GeolocationSettings, LanguageModelSettings
from src.domain.ports import (
    AdministrationRepositoryPort,
    AgentOrchestratorPort,
    DialogRepositoryPort,
    EventRepositoryPort,
    GeolocationResolverPort,
    LinkCheckerPort,
    RawEventRepositoryPort,
    SourceFetcherPort,
    StructuredExtractorPort,
    SubscriptionNotificationSenderPort,
)
from src.infrastructure.database import (
    DatabaseEngineManager,
    PostgreSqlAdministrationRepository,
    PostgreSqlDialogRepository,
    PostgreSqlEventRepository,
    PostgreSqlRawEventRepository,
)
from src.infrastructure.geolocation import HybridGeolocationResolver
from src.infrastructure.language_model import OpenAiAgentOrchestrator, OpenAiStructuredExtractor
from src.infrastructure.notifications import TelegramSubscriptionNotificationSender
from src.infrastructure.sources import (
    CompositeSourceFetcher,
    HtmlSourceFetcher,
    HttpLinkChecker,
    JsonSourceFetcher,
    RssSourceFetcher,
)


def _build_geolocation_resolver() -> HybridGeolocationResolver:
    return HybridGeolocationResolver(settings=GeolocationSettings(_env_file=None, EXTERNAL_ENABLED=False))


def _build_engine_manager() -> DatabaseEngineManager:
    return DatabaseEngineManager(DatabaseSettings(_env_file=None))


def _build_event_repository() -> PostgreSqlEventRepository:
    return PostgreSqlEventRepository(_build_engine_manager())


def _build_dialog_repository() -> PostgreSqlDialogRepository:
    return PostgreSqlDialogRepository(_build_engine_manager())


def _build_administration_repository() -> PostgreSqlAdministrationRepository:
    return PostgreSqlAdministrationRepository(_build_engine_manager())


def _build_raw_event_repository() -> PostgreSqlRawEventRepository:
    return PostgreSqlRawEventRepository(_build_engine_manager())


def _build_agent_orchestrator() -> OpenAiAgentOrchestrator:
    settings = LanguageModelSettings(_env_file=None, API_KEY="")
    return OpenAiAgentOrchestrator(settings=settings)


def _build_structured_extractor() -> OpenAiStructuredExtractor:
    settings = LanguageModelSettings(_env_file=None, API_KEY="")
    return OpenAiStructuredExtractor(settings=settings)


def _build_rss_source_fetcher() -> RssSourceFetcher:
    return RssSourceFetcher()


def _build_json_source_fetcher() -> JsonSourceFetcher:
    return JsonSourceFetcher()


def _build_html_source_fetcher() -> HtmlSourceFetcher:
    return HtmlSourceFetcher()


def _build_composite_source_fetcher() -> CompositeSourceFetcher:
    return CompositeSourceFetcher()


def _build_http_link_checker() -> HttpLinkChecker:
    return HttpLinkChecker()


def _build_telegram_subscription_notification_sender() -> TelegramSubscriptionNotificationSender:
    return TelegramSubscriptionNotificationSender(bot_token="123456:test-token")


_PORT_IMPLEMENTATION_CASES: tuple[tuple[type[Any], type[Any], Callable[[], Any]], ...] = (
    (GeolocationResolverPort, HybridGeolocationResolver, _build_geolocation_resolver),
    (EventRepositoryPort, PostgreSqlEventRepository, _build_event_repository),
    (DialogRepositoryPort, PostgreSqlDialogRepository, _build_dialog_repository),
    (AgentOrchestratorPort, OpenAiAgentOrchestrator, _build_agent_orchestrator),
    (StructuredExtractorPort, OpenAiStructuredExtractor, _build_structured_extractor),
    (AdministrationRepositoryPort, PostgreSqlAdministrationRepository, _build_administration_repository),
    (RawEventRepositoryPort, PostgreSqlRawEventRepository, _build_raw_event_repository),
    (SourceFetcherPort, RssSourceFetcher, _build_rss_source_fetcher),
    (SourceFetcherPort, JsonSourceFetcher, _build_json_source_fetcher),
    (SourceFetcherPort, HtmlSourceFetcher, _build_html_source_fetcher),
    (SourceFetcherPort, CompositeSourceFetcher, _build_composite_source_fetcher),
    (LinkCheckerPort, HttpLinkChecker, _build_http_link_checker),
    (
        SubscriptionNotificationSenderPort,
        TelegramSubscriptionNotificationSender,
        _build_telegram_subscription_notification_sender,
    ),
)


@pytest.mark.parametrize(
    ("protocol_type", "implementation_type", "instance_factory"),
    _PORT_IMPLEMENTATION_CASES,
    ids=[f"{port.__name__}<-{impl.__name__}" for port, impl, _ in _PORT_IMPLEMENTATION_CASES],
)
def test_implementation_satisfies_runtime_protocol_and_attributes(
    protocol_type: type[Any], implementation_type: type[Any], instance_factory: Callable[[], Any]
) -> None:
    """Проверяет runtime-совместимость `@runtime_checkable` Protocol и наличие всех членов порта."""
    instance = instance_factory()
    assert isinstance(instance, implementation_type)
    assert isinstance(instance, protocol_type)
    assert issubclass(implementation_type, protocol_type)

    protocol_members = get_protocol_members(protocol_type)
    assert protocol_members, f"У протокола {protocol_type.__name__} должны быть объявлены члены контракта."

    for member_name in sorted(protocol_members):
        assert hasattr(implementation_type, member_name), (
            f"Класс {implementation_type.__name__} не объявляет член контракта {member_name}."
        )
        assert hasattr(instance, member_name), (
            f"Экземпляр {implementation_type.__name__} не содержит атрибут {member_name}."
        )
        protocol_attr = getattr(protocol_type, member_name)
        impl_attr = getattr(implementation_type, member_name)
        assert callable(protocol_attr) == callable(impl_attr)
        if callable(impl_attr):
            assert callable(getattr(instance, member_name))


@pytest.mark.parametrize(
    ("protocol_type", "implementation_type", "instance_factory"),
    _PORT_IMPLEMENTATION_CASES,
    ids=[f"{port.__name__}<-{impl.__name__}" for port, impl, _ in _PORT_IMPLEMENTATION_CASES],
)
def test_implementation_method_signatures_and_annotations_match_protocol(
    protocol_type: type[Any], implementation_type: type[Any], instance_factory: Callable[[], Any]
) -> None:
    """Проверяет совпадение асинхронности, параметров, значений по умолчанию и типов методов порта."""
    del instance_factory
    protocol_members = sorted(get_protocol_members(protocol_type))

    for member_name in protocol_members:
        protocol_member = getattr(protocol_type, member_name)
        implementation_member = getattr(implementation_type, member_name)
        if not callable(protocol_member):
            continue

        assert inspect.iscoroutinefunction(implementation_member) == inspect.iscoroutinefunction(protocol_member), (
            f"Несоответствие async/sync в {implementation_type.__name__}.{member_name}."
        )

        protocol_sig = inspect.signature(protocol_member)
        implementation_sig = inspect.signature(implementation_member)

        protocol_params = [param for name, param in protocol_sig.parameters.items() if name != "self"]
        implementation_params = [param for name, param in implementation_sig.parameters.items() if name != "self"]

        assert [param.name for param in implementation_params] == [param.name for param in protocol_params], (
            f"Имена параметров в {implementation_type.__name__}.{member_name} отличаются от протокола "
            f"{protocol_type.__name__}."
        )
        assert [param.kind for param in implementation_params] == [param.kind for param in protocol_params], (
            f"Способ передачи параметров (positional/keyword-only) в {implementation_type.__name__}.{member_name} "
            f"отличается от протокола {protocol_type.__name__}."
        )
        for proto_param, impl_param in zip(protocol_params, implementation_params, strict=True):
            assert impl_param.default == proto_param.default, (
                f"Значение по умолчанию параметра '{proto_param.name}' в "
                f"{implementation_type.__name__}.{member_name} ({impl_param.default!r}) "
                f"отличается от протокола ({proto_param.default!r})."
            )

        protocol_hints = get_type_hints(protocol_member)
        unwrapped_impl = inspect.unwrap(implementation_member)
        implementation_hints = get_type_hints(unwrapped_impl)
        assert implementation_hints == protocol_hints, (
            f"Аннотации типов в {implementation_type.__name__}.{member_name} отличаются от протокола "
            f"{protocol_type.__name__}."
        )
