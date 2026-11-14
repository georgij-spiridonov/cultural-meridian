"""Интеграционные тесты конвейера сбора источников и фонового воркера поверх PostgreSQL 17."""

from collections.abc import AsyncIterator
from datetime import datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from src.core.configuration import (
    PROJECT_ROOT,
    DatabaseSettings,
    GeolocationSettings,
    IngestionSettings,
    LanguageModelSettings,
)
from src.core.types import EventStatus, SourceType
from src.domain.entities import RawEventRecord, SourceDefinition
from src.domain.services import BASHKORTOSTAN_TIMEZONE
from src.entrypoints.background_worker.runner import BackgroundWorkerRunner
from src.entrypoints.bootstrap import ApplicationContainer, build_application_container
from src.entrypoints.command_line.seed import DEFAULT_VENUES_CATALOG_PATH, seed_reference_catalog
from src.infrastructure.database import DatabaseEngineManager
from src.infrastructure.database.tables import EventRow, EventSourceRow, ModerationQueueRow, RawEventRow
from src.infrastructure.sources import load_sources_registry

pytestmark = pytest.mark.integration

_INGESTION_REFERENCE_TIME = datetime(2026, 11, 14, 12, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)


@pytest_asyncio.fixture
async def ingestion_container(
    test_database_settings: DatabaseSettings, database_engine_manager: DatabaseEngineManager
) -> AsyncIterator[ApplicationContainer]:
    """Собирает `ApplicationContainer` с выключенной LLM поверх чистой тестовой БД."""
    del database_engine_manager
    container = build_application_container(
        database_settings=test_database_settings,
        language_model_settings=LanguageModelSettings(_env_file=None, API_KEY=""),
        geolocation_settings=GeolocationSettings(
            _env_file=None, EXTERNAL_ENABLED=False, VENUES_CATALOG_PATH=DEFAULT_VENUES_CATALOG_PATH
        ),
        ingestion_settings=IngestionSettings(
            _env_file=None,
            SOURCES_CONFIG_PATH=PROJECT_ROOT / "configuration" / "sources.yaml",
            LOCAL_FEEDS_DIRECTORY=PROJECT_ROOT / "data" / "local_feeds",
            HYBRID_LLM_ENABLED=False,
            VERIFY_LOCAL_FILE_LINKS=True,
        ),
    )
    await container.startup()
    try:
        await seed_reference_catalog(container.database_engine_manager, catalog_path=DEFAULT_VENUES_CATALOG_PATH)
        yield container
    finally:
        await container.shutdown()


async def test_local_feeds_ingestion_deduplication_moderation_and_idempotency_postgres(
    ingestion_container: ApplicationContainer,
) -> None:
    """Проверяет сбор `sources.yaml` + `data/local_feeds/*` в PostgreSQL 17 и идемпотентность."""
    runner = BackgroundWorkerRunner(ingestion_container)
    first_report = await runner.run_once(current_time=_INGESTION_REFERENCE_TIME)

    assert first_report.sources_processed == 4
    assert first_report.sources_failed == 0
    assert first_report.items_fetched == 13
    assert first_report.items_deduplicated == 1
    assert first_report.items_published == 7
    assert first_report.items_sent_to_moderation == 4
    assert first_report.errors == ()

    async with ingestion_container.database_engine_manager.session() as session:
        raw_rows = (await session.execute(select(RawEventRow))).scalars().all()
        event_rows = (await session.execute(select(EventRow))).scalars().all()
        source_rows = (await session.execute(select(EventSourceRow))).scalars().all()
        moderation_rows = (await session.execute(select(ModerationQueueRow))).scalars().all()

    assert len(raw_rows) == 13
    assert all(row.normalization_status == "normalized" for row in raw_rows)
    assert len(event_rows) == 12
    assert len(source_rows) == 13
    assert len(moderation_rows) == 4

    events_by_title = {row.title: row for row in event_rows}
    published_rows = [row for row in event_rows if row.status == EventStatus.PUBLISHED.value]
    draft_rows = [row for row in event_rows if row.status == EventStatus.DRAFT.value]
    canceled_rows = [row for row in event_rows if row.status == EventStatus.CANCELED.value]
    assert len(published_rows) == 7
    assert len(draft_rows) == 4
    assert len(canceled_rows) == 1
    assert "романсов" in canceled_rows[0].title.lower()

    winter_tale = next(row for title, row in events_by_title.items() if "Зимняя сказка Урала" in title)
    winter_tale_sources = [row for row in source_rows if row.event_identifier == winter_tale.identifier]
    assert len(winter_tale_sources) == 2
    assert {row.source_name for row in winter_tale_sources} == {"ufa_culture_rss", "sterlitamak_afisha_api"}

    suspicious_mod_items = [row for row in moderation_rows if row.has_suspicious_instructions]
    assert len(suspicious_mod_items) == 2
    assert all(len(row.matched_injection_patterns) > 0 for row in suspicious_mod_items)

    draft_titles = " | ".join(row.title for row in draft_rows)
    assert "ДК «Нефтехимик»" in draft_titles
    assert "Огни зимнего Нефтекамска" in draft_titles

    # Повторный прогон не должен создавать дубли в БД и должен фиксировать `items_unchanged`.
    second_report = await runner.run_once(current_time=_INGESTION_REFERENCE_TIME)
    assert second_report.sources_processed == 4
    assert second_report.items_fetched == 13
    assert second_report.items_unchanged == 13
    assert second_report.items_published == 0
    assert second_report.items_sent_to_moderation == 0
    assert second_report.items_deduplicated == 0

    async with ingestion_container.database_engine_manager.session() as session:
        assert len((await session.execute(select(RawEventRow))).scalars().all()) == 13
        assert len((await session.execute(select(EventRow))).scalars().all()) == 12
        assert len((await session.execute(select(EventSourceRow))).scalars().all()) == 13
        assert len((await session.execute(select(ModerationQueueRow))).scalars().all()) == 4


async def test_reprocess_raw_events_postgres(ingestion_container: ApplicationContainer) -> None:
    """Проверяет переигрывание `pending`-записей из таблицы `raw_events` в PostgreSQL 17."""
    assert ingestion_container.event_ingestion_service is not None
    raw_identifier = uuid4()
    raw_record = RawEventRecord(
        identifier=raw_identifier,
        source_name="ufa_culture_rss",
        source_type=SourceType.RSS,
        external_identifier="pg-reprocess-001",
        source_url="data/local_feeds/cards/ufa_symphony_evening.html",
        source_hash="pg-reprocess-hash-001",
        raw_payload={
            "raw_title": "Повторно нормализованный концерт в Уфе (6+)",
            "raw_description": "Концерт симфонической музыки из сохранённой сырой записи.",
            "raw_date_text": "2026-12-27T19:00:00+05:00",
            "raw_price_text": "от 500 до 1200 ₽",
            "raw_city": "Уфа",
            "raw_venue": "Башкирский государственный театр оперы и балета",
            "raw_category": "concert",
            "raw_age_text": "6+",
        },
        fetched_at=_INGESTION_REFERENCE_TIME,
        normalization_status="pending",
    )
    _, created = await ingestion_container.raw_event_repository.save_raw_event(raw_event=raw_record)
    assert created is True

    enabled_sources = load_sources_registry(PROJECT_ROOT / "configuration" / "sources.yaml", only_enabled=True)
    sources_by_name: dict[str, SourceDefinition] = {source.name: source for source in enabled_sources}
    report = await ingestion_container.event_ingestion_service.reprocess_raw_events(
        normalization_status="pending",
        limit=20,
        current_time=_INGESTION_REFERENCE_TIME,
        sources_by_name=sources_by_name,
    )

    assert report.sources_processed == 1
    assert report.items_fetched == 1
    assert report.items_published == 1

    async with ingestion_container.database_engine_manager.session() as session:
        raw_row = (
            await session.execute(select(RawEventRow).where(RawEventRow.identifier == raw_identifier))
        ).scalar_one()
        event_rows = (await session.execute(select(EventRow))).scalars().all()

    assert raw_row.normalization_status == "normalized"
    assert len(event_rows) == 1
    assert event_rows[0].status == EventStatus.PUBLISHED.value
    assert event_rows[0].title == "Повторно нормализованный концерт в Уфе (6+)"
