"""Интеграционные тесты сбора с реальных сайтов: площадка по умолчанию, гастроли, прошедшие показы и сеансы."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest

from src.application import EventIngestionService
from src.core.types import EventCategory, EventStatus, SourceType
from src.domain.entities import Event, EventSearchFilter, FetchedSourceItem, LinkCheckResult, SourceDefinition
from src.domain.services import BASHKORTOSTAN_TIMEZONE
from src.entrypoints.command_line.seed import DEFAULT_VENUES_CATALOG_PATH, seed_reference_catalog
from src.infrastructure.database import (
    DatabaseEngineManager,
    PostgreSqlAdministrationRepository,
    PostgreSqlEventRepository,
    PostgreSqlRawEventRepository,
)

pytestmark = pytest.mark.integration

_RUN_TIME = datetime(2026, 11, 14, 12, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)
_ALL_STATUSES = (EventStatus.PUBLISHED, EventStatus.DRAFT, EventStatus.CANCELED)

_OPERA = SourceDefinition(
    name="bashopera-ufa",
    source_type=SourceType.WEBPAGE,
    source_url="https://bashopera.ru/",
    default_city="Уфа",
    default_category=EventCategory.SPECTACLE,
    default_venue="Башкирский государственный театр оперы и балета",
)
_DRAMA = SourceDefinition(
    name="rusdramteatr-sterlitamak",
    source_type=SourceType.WEBPAGE,
    source_url="https://rusdramteatr.ru/afisha",
    default_city="Стерлитамак",
    default_category=EventCategory.SPECTACLE,
    default_venue="Стерлитамакский государственный русский драматический театр",
)


class _FetcherBySource:
    """Загрузчик, возвращающий заранее заданные записи по имени источника."""

    def __init__(self, items_by_source: Mapping[str, Sequence[FetchedSourceItem]]) -> None:
        """Запоминает записи."""
        self._items_by_source = {name: tuple(items) for name, items in items_by_source.items()}

    async def fetch_items(self, *, source: SourceDefinition) -> tuple[FetchedSourceItem, ...]:
        """Возвращает записи источника."""
        return self._items_by_source.get(source.name, ())


class _ReachableLinkChecker:
    """Проверка ссылок без сети: все ссылки доступны."""

    async def check_link(self, *, target_url: str, timeout_seconds: float = 4.0) -> LinkCheckResult:
        """Возвращает доступную ссылку."""
        return LinkCheckResult(
            target_url=target_url,
            is_reachable=True,
            http_status_code=200,
            final_url=target_url,
            checked_at=datetime.now(tz=UTC),
        )


def _item(
    source: SourceDefinition, *, identifier: str, title: str, date_text: str, city: str | None = None
) -> FetchedSourceItem:
    """Создаёт сырую запись источника с ценой и возрастом, чтобы уверенность нормализации была высокой."""
    return FetchedSourceItem(
        source_name=source.name,
        source_type=source.source_type,
        external_identifier=identifier,
        source_url=f"{source.source_url.rstrip('/')}/{identifier}",
        raw_title=title,
        raw_date_text=date_text,
        raw_price_text="500 ₽",
        raw_city=city,
        raw_age_text="12+",
        raw_payload={"title": title, "date": date_text, "city": city},
    )


async def _run(
    engine_manager: DatabaseEngineManager, *, source: SourceDefinition, items: Sequence[FetchedSourceItem]
) -> tuple[list[Event], PostgreSqlAdministrationRepository]:
    """Загружает справочники, выполняет один прогон источника и возвращает мероприятия каталога."""
    await seed_reference_catalog(engine_manager, catalog_path=DEFAULT_VENUES_CATALOG_PATH)
    event_repository = PostgreSqlEventRepository(engine_manager)
    administration_repository = PostgreSqlAdministrationRepository(engine_manager)
    service = EventIngestionService(
        source_fetcher=_FetcherBySource({source.name: items}),
        link_checker=_ReachableLinkChecker(),
        raw_event_repository=PostgreSqlRawEventRepository(engine_manager),
        event_repository=event_repository,
        administration_repository=administration_repository,
        confidence_threshold=0.75,
    )
    await service.ingest_sources(sources=[source], current_time=_RUN_TIME)
    page = await event_repository.search_events(
        search_filter=EventSearchFilter(page=1, page_size=50), statuses=_ALL_STATUSES
    )
    return list(page.items), administration_repository


async def test_home_venue_is_published_and_touring_goes_to_moderation(
    database_engine_manager: DatabaseEngineManager,
) -> None:
    """Проверяет площадку по умолчанию на домашней сцене, модерацию гастроли и пропуск прошедшего показа."""
    events, administration_repository = await _run(
        database_engine_manager,
        source=_DRAMA,
        items=[
            _item(_DRAMA, identifier="makbet-05-12-2026", title="Макбет", date_text="05.12.2026 18:00"),
            _item(
                _DRAMA,
                identifier="groza-08-12-2026-kumertau",
                title="Гроза",
                date_text="08.12.2026 19:00",
                city="kumertau",
            ),
            _item(_DRAMA, identifier="maskarad-10-11-2026", title="Маскарад", date_text="10.11.2026 18:00"),
        ],
    )
    by_title = {event.title: event for event in events}

    assert set(by_title) == {"Макбет", "Гроза"}

    home = by_title["Макбет"]
    assert home.status == EventStatus.PUBLISHED
    assert home.venue == "Стерлитамакский государственный русский драматический театр"
    assert home.address == "ул. Худайбердина, 18"

    touring = by_title["Гроза"]
    assert touring.status == EventStatus.DRAFT
    assert touring.city == "Кумертау"
    assert touring.address == "Кумертау"
    moderation = await administration_repository.list_moderation_items(status="pending")
    assert [item.event_identifier for item in moderation] == [touring.identifier]
    assert "Площадка не определена" in moderation[0].reason


async def test_consecutive_sessions_of_same_source_stay_separate(
    database_engine_manager: DatabaseEngineManager,
) -> None:
    """Проверяет, что два сеанса одного спектакля с разницей в час остаются двумя мероприятиями."""
    events, _ = await _run(
        database_engine_manager,
        source=_OPERA,
        items=[
            _item(_OPERA, identifier="s550", title="Щелкунчик", date_text="15 декабря в 14:00"),
            _item(_OPERA, identifier="s551", title="Щелкунчик", date_text="15 декабря в 15:00"),
        ],
    )

    assert len(events) == 2
    assert {event.date_start.astimezone(BASHKORTOSTAN_TIMEZONE).hour for event in events} == {14, 15}
    assert all(event.status == EventStatus.PUBLISHED for event in events)
    assert all(event.address == "ул. Ленина, 5/1" for event in events)
