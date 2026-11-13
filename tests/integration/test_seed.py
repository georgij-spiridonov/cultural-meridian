"""Интеграционные тесты схемы сидинга, YAML-справочника и загрузки случайного набора событий."""

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.core.types import EventCategory, EventStatus, SourceType
from src.domain.entities import EventSearchFilter
from src.domain.services.normalization import BASHKORTOSTAN_TIMEZONE, normalize_title
from src.entrypoints.command_line.seed import (
    DEFAULT_VENUES_CATALOG_PATH,
    SeedEventItemSchema,
    SeedEventsDocumentSchema,
    load_venues_catalog,
    seed_events_from_file,
    seed_reference_catalog,
)
from src.infrastructure.database import DatabaseEngineManager, PostgreSqlEventRepository

pytestmark = pytest.mark.integration

_SEED_SCHEMA_PATH: Path = DEFAULT_VENUES_CATALOG_PATH.parent.parent / "data" / "seed_events.schema.json"


def _build_random_synthetic_seed_payload(seed: int = 20261114, count: int = 24) -> dict[str, object]:
    """Генерирует детерминированный случайный набор событий в формате схемы сидера.

    Включает случайные мероприятия по городам и площадкам Башкортостана из `venues_coordinates.yaml`,
    а также контрольные кейсы: детское бесплатное событие на башкирском языке, отменённое событие
    и пару дублей из двух разных источников на одну дату и площадку.
    """
    generator = random.Random(seed)
    catalog = load_venues_catalog(DEFAULT_VENUES_CATALOG_PATH)

    venue_pool: list[tuple[str, str]] = [(city.name, venue.name) for city in catalog.cities for venue in city.venues]
    categories = tuple(EventCategory)
    languages = ("русский", "башкирский", "татарский", None)
    base_date = datetime(2026, 11, 14, 10, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)

    generated_events: list[dict[str, object]] = []
    for index in range(count):
        city_name, venue_name = generator.choice(venue_pool)
        category = generator.choice(categories)
        day_offset = generator.randint(0, 10)
        hour_start = generator.choice((11, 14, 17, 19, 20))
        start_at = (base_date + timedelta(days=day_offset)).replace(hour=hour_start, minute=0)
        is_free = generator.random() < 0.3
        price_minimum = 0 if is_free else generator.choice((300, 500, 800, 1200))
        price_maximum = 0 if is_free else price_minimum + generator.choice((0, 400, 900))
        age_limit = generator.choice((0, 6, 12, 16, 18))

        generated_events.append(
            {
                "title": f"Культурная программа «Меридиан-{index + 1}» ({age_limit}+)",
                "category": category.value,
                "date_start": start_at.isoformat(),
                "duration_minutes": generator.choice((60, 90, 120)),
                "city": city_name,
                "venue": venue_name,
                "description": f"Случайная синтетическая карточка #{index + 1} для проверки сидера и поиска.",
                "price_minimum": price_minimum,
                "price_maximum": price_maximum,
                "is_free": is_free,
                "age_limit": age_limit,
                "language": generator.choice(languages),
                "source_name": "Публичная Афиша РБ",
                "source_type": SourceType.API.value,
                "source_url": f"https://example.org/afisha/random-{index + 1}",
            }
        )

    # Специальное детское бесплатное событие на башкирском языке 14 ноября (переданное через алиас площадки)
    generated_events.append(
        {
            "title": "Сказка «Урал-батыр» для всей семьи 6+",
            "category": EventCategory.SPECTACLE.value,
            "tags": ["дети", "башкирский язык", "бесплатно"],
            "date_start": "2026-11-14T11:30:00+05:00",
            "duration_minutes": 75,
            "city": "Уфа",
            "venue": "БГАТД им. М. Гафури",
            "is_free": True,
            "age_limit": 0,
            "language": "башкирский",
            "source_name": "Культура.РФ",
            "source_type": SourceType.API.value,
            "source_url": "https://example.org/culture/ural-batyr-kids",
        }
    )

    # Дубликат одного спектакля из двух разных источников (должен склеиться в 1 карточку)
    generated_events.append(
        {
            "title": "Щелкунчик (премьера, 6+)",
            "category": EventCategory.SPECTACLE.value,
            "date_start": "2026-11-14T19:00:00+05:00",
            "duration_minutes": 140,
            "city": "Уфа",
            "venue": "Башопера",
            "price_minimum": 800,
            "price_maximum": 2500,
            "age_limit": 6,
            "source_name": "Культура.РФ",
            "source_type": SourceType.API.value,
            "source_url": "https://example.org/culture/nutcracker",
        }
    )
    generated_events.append(
        {
            "title": "Спектакль «Щелкунчик» 6+",
            "category": EventCategory.SPECTACLE.value,
            "date_start": "2026-11-14T19:00:00+05:00",
            "city": "Уфа",
            "venue": "Башкирский театр оперы и балета",
            "price_minimum": 800,
            "price_maximum": 2500,
            "age_limit": 6,
            "source_name": "Яндекс Афиша",
            "source_type": SourceType.WEBPAGE.value,
            "source_url": "https://example.org/yandex/nutcracker",
        }
    )

    # Отменённый концерт (исключается из публичного поиска)
    generated_events.append(
        {
            "title": "Вечер романсов (отменён)",
            "category": EventCategory.CONCERT.value,
            "date_start": "2026-11-14T20:00:00+05:00",
            "city": "Стерлитамак",
            "venue": "СГТКО",
            "status": EventStatus.CANCELED.value,
            "source_name": "Касса Стерлитамака",
            "source_type": SourceType.WEBPAGE.value,
            "source_url": "https://example.org/sterlitamak/canceled-romance",
        }
    )

    return {"$schema": "./seed_events.schema.json", "events": generated_events}


def test_seed_schema_and_venues_catalog_valid() -> None:
    """Проверяет валидность YAML-справочника площадок и JSON Schema сидера."""
    assert _SEED_SCHEMA_PATH.is_file()
    raw_schema = json.loads(_SEED_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert raw_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "SeedEventItem" in raw_schema["$defs"]

    catalog = load_venues_catalog(DEFAULT_VENUES_CATALOG_PATH)
    city_names = {city.name for city in catalog.cities}
    assert {"Уфа", "Стерлитамак", "Салават", "Нефтекамск", "Октябрьский"}.issubset(city_names)
    assert len(catalog.cities) >= 18

    payload = _build_random_synthetic_seed_payload()
    document = SeedEventsDocumentSchema.model_validate(payload)
    assert len(document.events) == 28
    assert all(isinstance(item, SeedEventItemSchema) for item in document.events)


async def test_seed_references_and_random_events_end_to_end_and_idempotency(
    database_engine_manager: DatabaseEngineManager, event_repository: PostgreSqlEventRepository, tmp_path: Path
) -> None:
    """Проверяет идемпотентный сидинг справочников, случайных событий, дедупликацию и поиск."""
    # Шаг 1: первичная и повторная загрузка справочника configuration/venues_coordinates.yaml
    (
        cities_processed,
        cities_created,
        cities_updated,
        venues_processed,
        venues_created,
        venues_updated,
    ) = await seed_reference_catalog(database_engine_manager, catalog_path=DEFAULT_VENUES_CATALOG_PATH)
    assert cities_processed >= 18
    assert cities_created == cities_processed
    assert cities_updated == 0
    assert venues_processed >= 40
    assert venues_created == venues_processed
    assert venues_updated == 0

    (
        _cities_processed_second,
        cities_created_second,
        cities_updated_second,
        _venues_processed_second,
        venues_created_second,
        venues_updated_second,
    ) = await seed_reference_catalog(database_engine_manager, catalog_path=DEFAULT_VENUES_CATALOG_PATH)
    assert (cities_created_second, cities_updated_second, venues_created_second, venues_updated_second) == (0, 0, 0, 0)

    cities = await event_repository.list_cities()
    venues = await event_repository.list_venues(city="Уфа")
    assert len(cities) >= 18
    assert len(venues) >= 20
    assert all(venue.coordinates is not None and venue.address for venue in venues)

    # Шаг 2: загрузка случайного синтетического набора событий + дубликатов + отмен
    seed_file = tmp_path / "random_seed_events.json"
    seed_file.write_text(
        json.dumps(_build_random_synthetic_seed_payload(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    processed, created, updated, merged, skipped = await seed_events_from_file(event_repository, events_path=seed_file)
    assert processed == 28
    assert created == 27
    assert updated == 0
    assert merged == 1
    assert skipped == 0

    # Повторный прогон по тем же данным обязан быть полностью идемпотентным
    processed_2, created_2, updated_2, merged_2, skipped_2 = await seed_events_from_file(
        event_repository, events_path=seed_file
    )
    assert (processed_2, created_2, updated_2, merged_2, skipped_2) == (28, 0, 0, 0, 28)

    # Проверка склейки дублей, обогащения адреса/координат из YAML и триграммного поиска
    nutcracker_page = await event_repository.search_events(
        search_filter=EventSearchFilter(city="Уфа", query="шилкунчик", near_center=True)
    )
    assert nutcracker_page.total_count == 1
    nutcracker = nutcracker_page.items[0]
    assert nutcracker.venue == "Башкирский государственный театр оперы и балета"
    assert nutcracker.address == "ул. Ленина, 5/1"
    assert nutcracker.latitude == pytest.approx(54.722222)
    assert len(nutcracker.sources) == 2

    # Проверка детского бесплатного башкирского события 14 ноября с обогащением площадки
    kids_page = await event_repository.search_events(
        search_filter=EventSearchFilter(city="Уфа", only_free=True, maximum_age_limit=6, language="башкирский")
    )
    assert kids_page.total_count >= 1
    ural_batyr_events = [item for item in kids_page.items if "Урал-батыр" in item.title]
    assert len(ural_batyr_events) == 1
    assert ural_batyr_events[0].venue == "Башкирский государственный академический театр драмы имени Мажита Гафури"
    assert ural_batyr_events[0].normalized_title == normalize_title("Сказка «Урал-батыр» для всей семьи 6+")

    # Отменённое событие в Стерлитамаке не должно выдаваться в публичном поиске
    canceled_in_public = await event_repository.search_events(
        search_filter=EventSearchFilter(city="Стерлитамак", query="Вечер романсов")
    )
    assert canceled_in_public.total_count == 0
