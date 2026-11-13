"""Идемпотентная начальная загрузка справочников и событий Республики Башкортостан."""

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final
from urllib.parse import urlparse
from uuid import UUID, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.configuration import PROJECT_ROOT, get_database_settings, get_logging_settings
from src.core.exceptions import ValidationException
from src.core.logger import get_logger, setup_logger
from src.core.types import EventCategory, EventStatus, RealCollection, SourceType
from src.domain.entities import CityReference, Event, EventSearchFilter, EventSourceReference, VenueReference
from src.domain.exceptions import EventNotFoundError
from src.domain.services.deduplication import (
    are_events_duplicates,
    compute_deduplication_key,
    compute_payload_hash,
    merge_event_sources,
)
from src.domain.services.normalization import (
    BASHKORTOSTAN_TIMEZONE,
    extract_event_tags,
    normalize_city,
    normalize_optional_text,
    normalize_text_for_matching,
    normalize_title,
    normalize_venue,
)
from src.infrastructure.database import DatabaseEngineManager, PostgreSqlEventRepository
from src.infrastructure.database.errors import handle_database_errors
from src.infrastructure.database.tables import CityAliasRow, CityRow, VenueAliasRow, VenueRow

__all__ = [
    "DEFAULT_SEED_EVENTS_PATH",
    "DEFAULT_VENUES_CATALOG_PATH",
    "CitySeedSchema",
    "CoordinatesSeedSchema",
    "EventSourceSeedSchema",
    "SeedEventItemSchema",
    "SeedEventsDocumentSchema",
    "SeedExecutionReport",
    "VenueSeedSchema",
    "VenuesCatalogSeedSchema",
    "load_seed_events",
    "load_venues_catalog",
    "main",
    "run_database_seed",
    "seed_events_from_file",
    "seed_reference_catalog",
]

logger = get_logger(__name__)

DEFAULT_VENUES_CATALOG_PATH: Final[Path] = PROJECT_ROOT / "configuration" / "venues_coordinates.yaml"
DEFAULT_SEED_EVENTS_PATH: Final[Path] = PROJECT_ROOT / "data" / "seed_events.json"

_ALLOWED_AGE_LIMITS: Final[frozenset[int]] = frozenset({0, 6, 12, 16, 18})
_SEED_EVENT_UUID_NAMESPACE: Final[UUID] = UUID("7b2b7d44-6e3f-4c9a-9f57-6f2a3e8b1234")


class CoordinatesSeedSchema(BaseModel):
    """Схема валидации географических координат в YAML-справочнике площадок.

    Attributes:
        latitude: Географическая широта точки в градусах (`-90..90`).
        longitude: Географическая долгота точки в градусах (`-180..180`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)


class VenueSeedSchema(BaseModel):
    """Схема валидации площадки культурного мероприятия из `venues_coordinates.yaml`.

    Attributes:
        name: Каноническое название площадки.
        address: Почтовый адрес площадки в пределах города.
        aliases: Кортеж альтернативных написаний и сокращений.
        coordinates: Точные координаты площадки, если известны.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    address: str = Field(default="", max_length=500)
    aliases: tuple[str, ...] = ()
    coordinates: CoordinatesSeedSchema | None = None

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, raw_value: object) -> str:
        """Проверяет непустоту названия площадки."""
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ValueError("Название площадки должно быть непустой строкой.")
        return raw_value.strip()


class CitySeedSchema(BaseModel):
    """Схема валидации города Республики Башкортостан и его площадок.

    Attributes:
        name: Каноническое название города.
        aliases: Словоформы и распространённые опечатки названия города.
        center: Географические координаты центра города.
        venues: Кортеж ключевых культурных площадок города.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    aliases: tuple[str, ...] = ()
    center: CoordinatesSeedSchema | None = None
    venues: tuple[VenueSeedSchema, ...] = ()

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, raw_value: object) -> str:
        """Проверяет непустоту названия города."""
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ValueError("Название города должно быть непустой строкой.")
        return raw_value.strip()


class VenuesCatalogSeedSchema(BaseModel):
    """Корневая схема YAML-справочника городов и площадок Башкортостана.

    Attributes:
        cities: Кортеж городов и привязанных к ним культурных площадок.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cities: tuple[CitySeedSchema, ...] = Field(min_length=1)


class EventSourceSeedSchema(BaseModel):
    """Схема подтверждённого источника события внутри JSON-файла сидера.

    Attributes:
        source_name: Человекочитаемое наименование источника.
        source_type: Тип протокола или канала источника.
        source_url: Прямая ссылка на страницу или запись источника.
        source_hash: Необязательная контрольная сумма записи для идемпотентности.
        verified_at: Временная метка последней проверки ссылки.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_name: str = Field(min_length=1, max_length=120)
    source_type: SourceType
    source_url: str = Field(min_length=1, max_length=1024)
    source_hash: str | None = Field(default=None, max_length=128)
    verified_at: datetime | None = None


class SeedEventItemSchema(BaseModel):
    """Схема отдельной карточки мероприятия из `data/seed_events.json`.

    Поддерживает явную передачу списка `sources` либо одиночных полей `source_url` /
    `source_name` / `source_type` и автоматически дозаполняет вычисляемые поля.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    identifier: UUID | None = None
    title: str = Field(min_length=1, max_length=500)
    normalized_title: str | None = Field(default=None, max_length=500)
    category: EventCategory
    tags: tuple[str, ...] = ()
    date_start: datetime
    date_end: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=1)
    city: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    description: str = ""
    price_minimum: int | None = Field(default=None, ge=0)
    price_maximum: int | None = Field(default=None, ge=0)
    is_free: bool = False
    age_limit: int = 0
    language: str | None = Field(default=None, max_length=64)
    organization_link: str | None = Field(default=None, max_length=1024)
    source_name: str | None = Field(default=None, max_length=120)
    source_type: SourceType = SourceType.API
    source_url: str | None = Field(default=None, max_length=1024)
    source_hash: str | None = Field(default=None, max_length=128)
    verified_at: datetime | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    status: EventStatus = EventStatus.PUBLISHED
    deduplication_key: str | None = Field(default=None, max_length=128)
    sources: tuple[EventSourceSeedSchema, ...] = ()

    @field_validator("age_limit")
    @classmethod
    def validate_age_limit(cls, value: int) -> int:
        """Проверяет соответствие возрастного ценза стандартной шкале (`0, 6, 12, 16, 18`)."""
        if value not in _ALLOWED_AGE_LIMITS:
            raise ValueError(f"Допустимые значения age_limit: {sorted(_ALLOWED_AGE_LIMITS)}, получено: {value}.")
        return value

    @model_validator(mode="after")
    def validate_cross_field_invariants(self) -> SeedEventItemSchema:
        """Проверяет согласованность координат, дат, ценовых диапазонов и источников."""
        if not self.title.strip():
            raise ValueError("Название мероприятия не может состоять только из пробелов.")
        if not self.city.strip():
            raise ValueError("Город мероприятия не может быть пустым.")
        if not self.venue.strip():
            raise ValueError("Площадка мероприятия не может быть пустой.")

        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Координаты latitude и longitude должны указываться только парой.")

        start_local = _ensure_timezone(self.date_start)
        if self.date_end is not None and _ensure_timezone(self.date_end) < start_local:
            raise ValueError("Время окончания date_end не может быть раньше date_start.")

        if (
            self.price_minimum is not None
            and self.price_maximum is not None
            and self.price_maximum < self.price_minimum
        ):
            raise ValueError("Максимальная цена price_maximum не может быть меньше price_minimum.")

        if self.is_free and (
            (self.price_minimum is not None and self.price_minimum > 0)
            or (self.price_maximum is not None and self.price_maximum > 0)
        ):
            raise ValueError("Для бесплатного события (is_free=true) цены не могут превышать 0.")

        has_sources_list = len(self.sources) > 0
        has_single_source = normalize_optional_text(self.source_url) is not None
        if not has_sources_list and not has_single_source:
            raise ValueError("Необходимо указать source_url либо непустой список sources.")

        return self


class SeedEventsDocumentSchema(BaseModel):
    """Корневая объектная обёртка файла `data/seed_events.json`.

    Attributes:
        schema_reference: Необязательная ссылка `$schema` на JSON Schema.
        events: Кортеж валидированных карточек мероприятий.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_reference: str | None = Field(default=None, alias="$schema")
    events: tuple[SeedEventItemSchema, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SeedExecutionReport:
    """Итоговая сводка выполнения идемпотентного сидера базы данных.

    Attributes:
        cities_processed: Количество городов, прочитанных из YAML-справочника.
        cities_created: Количество впервые добавленных городов.
        cities_updated: Количество обновлённых городов.
        venues_processed: Количество площадок, прочитанных из YAML-справочника.
        venues_created: Количество впервые добавленных площадок.
        venues_updated: Количество обновлённых площадок.
        events_file_found: Признак наличия файла событий для загрузки.
        events_processed: Количество записей событий, прочитанных из JSON-файла.
        events_created: Количество впервые созданных карточек мероприятий.
        events_updated: Количество обновлённых карточек мероприятий.
        events_merged: Количество дублей, объединённых в существующие карточки.
        events_skipped_unchanged: Количество записей без изменений (идемпотентный пропуск).
    """

    cities_processed: int = 0
    cities_created: int = 0
    cities_updated: int = 0
    venues_processed: int = 0
    venues_created: int = 0
    venues_updated: int = 0
    events_file_found: bool = False
    events_processed: int = 0
    events_created: int = 0
    events_updated: int = 0
    events_merged: int = 0
    events_skipped_unchanged: int = 0


def _ensure_timezone(value: datetime) -> datetime:
    """Приводит дату и время к явному часовому поясу Башкортостана при отсутствии смещения."""
    if value.tzinfo is None:
        return value.replace(tzinfo=BASHKORTOSTAN_TIMEZONE)
    return value


def _derive_source_name(source_url: str, explicit_name: str | None) -> str:
    """Определяет читаемое имя источника по явному полю или домену ссылки."""
    cleaned = normalize_optional_text(explicit_name)
    if cleaned is not None:
        return cleaned[:120]
    parsed_host = urlparse(source_url).netloc.strip()
    if parsed_host:
        return parsed_host[:120]
    return "Подготовленный набор"


def load_venues_catalog(catalog_path: Path = DEFAULT_VENUES_CATALOG_PATH) -> VenuesCatalogSeedSchema:
    """Читает и валидирует YAML-справочник городов и площадок Республики Башкортостан.

    Arguments:
        catalog_path: Путь к файлу `venues_coordinates.yaml`.

    Returns:
        Провалидированная неизменяемая структура справочника.

    Raises:
        ValidationException: Если файл отсутствует, содержит невалидный YAML или нарушает схему.
    """
    if not catalog_path.is_file():
        raise ValidationException(f"Файл справочника площадок не найден: {catalog_path}.")

    try:
        raw_content = catalog_path.read_text(encoding="utf-8")
        parsed_yaml: object = yaml.safe_load(raw_content)
        return VenuesCatalogSeedSchema.model_validate(parsed_yaml)
    except Exception as error:
        raise ValidationException(f"Ошибка разбора справочника площадок {catalog_path}: {error}.") from error


def load_seed_events(events_path: Path = DEFAULT_SEED_EVENTS_PATH) -> tuple[SeedEventItemSchema, ...]:
    """Читает и валидирует JSON-файл стартового набора культурных событий.

    Поддерживает как объектный формат `{"$schema": ..., "events": [...]}`,
    так и плоский массив карточек `[...]`.

    Arguments:
        events_path: Путь к файлу `seed_events.json`.

    Returns:
        Кортеж валидированных записей событий для загрузки.

    Raises:
        ValidationException: Если файл отсутствует, содержит невалидный JSON или нарушает схему.
    """
    if not events_path.is_file():
        raise ValidationException(f"Файл стартового набора событий не найден: {events_path}.")

    try:
        raw_json: object = json.loads(events_path.read_text(encoding="utf-8"))
        if isinstance(raw_json, list):
            return tuple(SeedEventItemSchema.model_validate(item) for item in raw_json)
        document = SeedEventsDocumentSchema.model_validate(raw_json)
        return document.events
    except Exception as error:
        raise ValidationException(f"Ошибка валидации файла событий {events_path}: {error}.") from error


async def _upsert_city(session: AsyncSession, *, city_schema: CitySeedSchema) -> tuple[CityRow, bool, bool]:
    """Идемпотентно создаёт или обновляет запись города и возвращает `(row, created, updated)`."""
    canonical_name = normalize_city(city_schema.name) or city_schema.name.strip()
    normalized_name = normalize_text_for_matching(canonical_name)
    existing_city = (
        await session.execute(select(CityRow).where(CityRow.normalized_name == normalized_name))
    ).scalar_one_or_none()

    target_latitude = city_schema.center.latitude if city_schema.center is not None else None
    target_longitude = city_schema.center.longitude if city_schema.center is not None else None

    if existing_city is None:
        city_row = CityRow(
            name=canonical_name,
            normalized_name=normalized_name,
            center_latitude=target_latitude,
            center_longitude=target_longitude,
        )
        session.add(city_row)
        await session.flush()
        return city_row, True, False

    was_updated = False
    if (
        target_latitude is not None
        and target_longitude is not None
        and (existing_city.center_latitude != target_latitude or existing_city.center_longitude != target_longitude)
    ):
        existing_city.center_latitude = target_latitude
        existing_city.center_longitude = target_longitude
        was_updated = True
    return existing_city, False, was_updated


async def _sync_city_aliases(session: AsyncSession, *, city_row: CityRow, aliases: Sequence[str]) -> None:
    """Добавляет отсутствующие алиасы города с дедупликацией по нормализованному ключу."""
    normalized_to_alias: dict[str, str] = {}
    for raw_alias in (city_row.name, *aliases):
        cleaned = normalize_optional_text(raw_alias)
        if cleaned is None:
            continue
        normalized_alias = normalize_text_for_matching(cleaned)
        if normalized_alias and normalized_alias not in normalized_to_alias:
            normalized_to_alias[normalized_alias] = cleaned[:120]

    if not normalized_to_alias:
        return

    values = [
        {"normalized_alias": normalized, "city_identifier": city_row.identifier, "alias": display}
        for normalized, display in normalized_to_alias.items()
    ]
    await session.execute(
        postgresql_insert(CityAliasRow).values(values).on_conflict_do_nothing(index_elements=["normalized_alias"])
    )


async def _upsert_venue(
    session: AsyncSession, *, city_row: CityRow, venue_schema: VenueSeedSchema
) -> tuple[VenueRow, bool, bool]:
    """Идемпотентно создаёт или обновляет площадку в рамках города."""
    cleaned_name = normalize_optional_text(venue_schema.name) or venue_schema.name.strip()
    normalized_name = normalize_text_for_matching(cleaned_name)
    cleaned_address = normalize_optional_text(venue_schema.address) or ""
    target_latitude = venue_schema.coordinates.latitude if venue_schema.coordinates is not None else None
    target_longitude = venue_schema.coordinates.longitude if venue_schema.coordinates is not None else None

    existing_venue = (
        await session.execute(
            select(VenueRow).where(
                (VenueRow.city_identifier == city_row.identifier) & (VenueRow.normalized_name == normalized_name)
            )
        )
    ).scalar_one_or_none()

    if existing_venue is None:
        venue_row = VenueRow(
            city_identifier=city_row.identifier,
            name=cleaned_name,
            normalized_name=normalized_name,
            address=cleaned_address,
            latitude=target_latitude,
            longitude=target_longitude,
        )
        session.add(venue_row)
        await session.flush()
        return venue_row, True, False

    was_updated = False
    if cleaned_address and existing_venue.address != cleaned_address:
        existing_venue.address = cleaned_address
        was_updated = True
    if (
        target_latitude is not None
        and target_longitude is not None
        and (existing_venue.latitude != target_latitude or existing_venue.longitude != target_longitude)
    ):
        existing_venue.latitude = target_latitude
        existing_venue.longitude = target_longitude
        was_updated = True
    return existing_venue, False, was_updated


async def _sync_venue_aliases(session: AsyncSession, *, venue_row: VenueRow, aliases: Sequence[str]) -> None:
    """Добавляет отсутствующие алиасы площадки с дедупликацией по нормализованному ключу."""
    normalized_to_alias: dict[str, str] = {}
    for raw_alias in aliases:
        cleaned = normalize_optional_text(raw_alias)
        if cleaned is None:
            continue
        normalized_alias = normalize_text_for_matching(cleaned)
        if normalized_alias and normalized_alias not in normalized_to_alias:
            normalized_to_alias[normalized_alias] = cleaned[:255]

    if not normalized_to_alias:
        return

    values = [
        {"venue_identifier": venue_row.identifier, "alias": display, "normalized_alias": normalized}
        for normalized, display in normalized_to_alias.items()
    ]
    await session.execute(postgresql_insert(VenueAliasRow).values(values).on_conflict_do_nothing())


async def seed_reference_catalog(
    engine_manager: DatabaseEngineManager, *, catalog_path: Path = DEFAULT_VENUES_CATALOG_PATH
) -> tuple[int, int, int, int, int, int]:
    """Идемпотентно загружает города, их алиасы, площадки и алиасы площадок из YAML.

    Arguments:
        engine_manager: Активный менеджер подключений к PostgreSQL 17.
        catalog_path: Путь к YAML-файлу справочника городов и площадок.

    Returns:
        Кортеж счётчиков: `(cities_processed, cities_created, cities_updated,
        venues_processed, venues_created, venues_updated)`.
    """
    catalog = load_venues_catalog(catalog_path)
    cities_processed = 0
    cities_created = 0
    cities_updated = 0
    venues_processed = 0
    venues_created = 0
    venues_updated = 0

    async with (
        handle_database_errors(operation="загрузка справочников городов и площадок"),
        engine_manager.session() as session,
        session.begin(),
    ):
        for city_schema in catalog.cities:
            cities_processed += 1
            city_row, city_created, city_updated = await _upsert_city(session, city_schema=city_schema)
            cities_created += int(city_created)
            cities_updated += int(city_updated)
            await _sync_city_aliases(session, city_row=city_row, aliases=city_schema.aliases)

            for venue_schema in city_schema.venues:
                venues_processed += 1
                venue_row, venue_created, venue_updated = await _upsert_venue(
                    session, city_row=city_row, venue_schema=venue_schema
                )
                venues_created += int(venue_created)
                venues_updated += int(venue_updated)
                await _sync_venue_aliases(session, venue_row=venue_row, aliases=venue_schema.aliases)

    logger.info(
        "Справочники загружены: городов=%s (создано=%s, обновлено=%s), площадок=%s (создано=%s, обновлено=%s).",
        cities_processed,
        cities_created,
        cities_updated,
        venues_processed,
        venues_created,
        venues_updated,
    )
    return (cities_processed, cities_created, cities_updated, venues_processed, venues_created, venues_updated)


def _resolve_seed_sources(item: SeedEventItemSchema) -> tuple[EventSourceReference, ...]:
    """Преобразует источники записи сидера в кортеж `EventSourceReference` с устойчивыми хэшами."""
    resolved: list[EventSourceReference] = []
    if item.sources:
        for source_entry in item.sources:
            cleaned_url = source_entry.source_url.strip()
            cleaned_name = source_entry.source_name.strip()
            resolved_hash = normalize_optional_text(source_entry.source_hash) or compute_payload_hash(
                {"source_type": source_entry.source_type.value, "source_url": cleaned_url, "title": item.title.strip()}
            )
            resolved.append(
                EventSourceReference(
                    source_name=cleaned_name,
                    source_type=source_entry.source_type,
                    source_url=cleaned_url,
                    source_hash=resolved_hash,
                    verified_at=_ensure_timezone(source_entry.verified_at) if source_entry.verified_at else None,
                )
            )
        return tuple(resolved)

    assert item.source_url is not None
    cleaned_url = item.source_url.strip()
    source_name = _derive_source_name(cleaned_url, item.source_name)
    resolved_hash = normalize_optional_text(item.source_hash) or compute_payload_hash(
        {
            "source_type": item.source_type.value,
            "source_url": cleaned_url,
            "title": item.title.strip(),
            "date_start": _ensure_timezone(item.date_start).isoformat(),
        }
    )
    return (
        EventSourceReference(
            source_name=source_name,
            source_type=item.source_type,
            source_url=cleaned_url,
            source_hash=resolved_hash,
            verified_at=_ensure_timezone(item.verified_at) if item.verified_at else None,
        ),
    )


def _normalize_tags_tuple(raw_tags: Sequence[str]) -> tuple[str, ...]:
    """Удаляет пустые и дублирующиеся метки, упорядочивая их как в PostgreSQL."""
    normalized_to_display: dict[str, str] = {}
    for raw_tag in raw_tags:
        cleaned = normalize_optional_text(raw_tag)
        if cleaned is None:
            continue
        normalized_key = normalize_text_for_matching(cleaned)
        if normalized_key and normalized_key not in normalized_to_display:
            normalized_to_display[normalized_key] = cleaned[:64]
    return tuple(normalized_to_display[key] for key in sorted(normalized_to_display))


def _build_domain_event(
    item: SeedEventItemSchema,
    *,
    cities: RealCollection[CityReference],
    venues: RealCollection[VenueReference],
    timestamp_now: datetime,
) -> Event:
    """Строит нормализованную доменную карточку `Event` с обогащением по справочникам."""
    cleaned_title = normalize_optional_text(item.title) or item.title.strip()
    effective_normalized_title = normalize_title(item.normalized_title or cleaned_title)
    resolved_city = normalize_city(item.city, cities=cities) or item.city.strip()

    venue_resolution = normalize_venue(item.venue, venues=venues, city=resolved_city)
    if isinstance(venue_resolution, VenueReference):
        resolved_venue_name = venue_resolution.name
        fallback_address = venue_resolution.address
        fallback_coordinates = venue_resolution.coordinates
    else:
        resolved_venue_name = venue_resolution or item.venue.strip()
        fallback_address = ""
        fallback_coordinates = None

    resolved_address = normalize_optional_text(item.address) or fallback_address
    resolved_latitude = (
        item.latitude
        if item.latitude is not None
        else (fallback_coordinates.latitude if fallback_coordinates else None)
    )
    resolved_longitude = (
        item.longitude
        if item.longitude is not None
        else (fallback_coordinates.longitude if fallback_coordinates else None)
    )

    date_start = _ensure_timezone(item.date_start)
    date_end = _ensure_timezone(item.date_end) if item.date_end is not None else None
    duration_minutes = item.duration_minutes
    if duration_minutes is None and date_end is not None:
        computed_minutes = round((date_end - date_start).total_seconds() / 60.0)
        if computed_minutes > 0:
            duration_minutes = computed_minutes

    cleaned_language = normalize_optional_text(item.language)
    normalized_language = cleaned_language.lower() if cleaned_language is not None else None
    is_free = item.is_free or (item.price_minimum == 0 and item.price_maximum in (None, 0))
    price_minimum = 0 if is_free and item.price_minimum is None else item.price_minimum
    price_maximum = 0 if is_free and item.price_maximum is None else item.price_maximum

    raw_tags = _normalize_tags_tuple(item.tags)
    tags = (
        raw_tags
        if raw_tags
        else _normalize_tags_tuple(
            extract_event_tags(
                raw_title=cleaned_title,
                raw_description=item.description,
                category=item.category,
                is_free=is_free,
                language=normalized_language,
                age_limit=item.age_limit,
            )
        )
    )

    sources = _resolve_seed_sources(item)
    primary_source = sources[0]
    deduplication_key = (
        normalize_optional_text(item.deduplication_key)
        or compute_deduplication_key(
            normalized_title=effective_normalized_title,
            date_start=date_start,
            city=resolved_city,
            venue=resolved_venue_name,
        )
    ).lower()

    deterministic_identifier = item.identifier or uuid5(
        _SEED_EVENT_UUID_NAMESPACE, f"{primary_source.source_type.value}:{primary_source.source_hash}"
    )

    return Event(
        identifier=deterministic_identifier,
        title=cleaned_title,
        normalized_title=effective_normalized_title,
        category=item.category,
        tags=tags,
        date_start=date_start,
        date_end=date_end,
        duration_minutes=duration_minutes,
        city=resolved_city,
        venue=resolved_venue_name,
        address=resolved_address,
        latitude=resolved_latitude,
        longitude=resolved_longitude,
        description=item.description.strip(),
        price_minimum=price_minimum,
        price_maximum=price_maximum,
        is_free=is_free,
        age_limit=item.age_limit,
        language=normalized_language,
        organization_link=normalize_optional_text(item.organization_link),
        source_url=primary_source.source_url,
        source_type=primary_source.source_type,
        source_hash=primary_source.source_hash,
        verified_at=primary_source.verified_at,
        confidence=item.confidence,
        status=item.status,
        deduplication_key=deduplication_key,
        created_at=timestamp_now,
        updated_at=timestamp_now,
        sources=sources,
    )


def _events_semantically_equal(existing: Event, incoming: Event) -> bool:
    """Сравнивает две карточки мероприятий без учёта технических временных меток."""
    existing_sources = {
        (source.source_type, source.source_hash, source.source_url, source.source_name) for source in existing.sources
    }
    incoming_sources = {
        (source.source_type, source.source_hash, source.source_url, source.source_name) for source in incoming.sources
    }
    existing_tag_keys = {normalize_text_for_matching(tag) for tag in existing.tags}
    incoming_tag_keys = {normalize_text_for_matching(tag) for tag in incoming.tags}
    return (
        existing.title == incoming.title
        and existing.category == incoming.category
        and existing_tag_keys == incoming_tag_keys
        and existing.date_start == incoming.date_start
        and existing.date_end == incoming.date_end
        and existing.duration_minutes == incoming.duration_minutes
        and existing.city == incoming.city
        and existing.venue == incoming.venue
        and existing.address == incoming.address
        and existing.latitude == incoming.latitude
        and existing.longitude == incoming.longitude
        and existing.description == incoming.description
        and existing.price_minimum == incoming.price_minimum
        and existing.price_maximum == incoming.price_maximum
        and existing.is_free == incoming.is_free
        and existing.age_limit == incoming.age_limit
        and existing.language == incoming.language
        and existing.organization_link == incoming.organization_link
        and existing.status == incoming.status
        and existing.deduplication_key == incoming.deduplication_key
        and existing_sources == incoming_sources
    )


async def _find_existing_event(event_repository: PostgreSqlEventRepository, *, candidate_event: Event) -> Event | None:
    """Ищет ранее сохранённую карточку по хэшу источника или детерминированному UUID."""
    for source in candidate_event.sources:
        matched = await event_repository.find_by_source_hash(
            source_type=source.source_type, source_hash=source.source_hash
        )
        if matched is not None:
            return matched

    try:
        return await event_repository.get_event_by_identifier(event_identifier=candidate_event.identifier)
    except EventNotFoundError:
        return None


async def _find_duplicate_candidate(
    event_repository: PostgreSqlEventRepository, *, candidate_event: Event
) -> Event | None:
    """Ищет существующий дубль карточки в каталоге по городу и календарному дню."""
    local_start = candidate_event.date_start.astimezone(BASHKORTOSTAN_TIMEZONE)
    day_start = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = local_start.replace(hour=23, minute=59, second=59, microsecond=999999)
    page = await event_repository.search_events(
        search_filter=EventSearchFilter(
            city=candidate_event.city, date_from=day_start, date_to=day_end, page=1, page_size=500
        ),
        statuses=(EventStatus.PUBLISHED, EventStatus.DRAFT, EventStatus.CANCELED),
    )
    for existing_candidate in page.items:
        verdict = are_events_duplicates(
            existing_candidate,
            normalized_title=candidate_event.normalized_title,
            date_start=candidate_event.date_start,
            city=candidate_event.city,
            venue=candidate_event.venue,
            deduplication_key=candidate_event.deduplication_key,
        )
        if verdict.is_duplicate:
            return existing_candidate
    return None


def _preserve_existing_secondary_sources(
    existing_sources: tuple[EventSourceReference, ...], incoming_sources: tuple[EventSourceReference, ...]
) -> tuple[EventSourceReference, ...]:
    """Сохраняет дополнительные подтверждённые источники при повторном обновлении основной карточки."""
    by_key: dict[tuple[SourceType, str], EventSourceReference] = {}
    for source in incoming_sources:
        by_key[(source.source_type, source.source_hash)] = source
    for source in existing_sources:
        key = (source.source_type, source.source_hash)
        if key not in by_key:
            by_key[key] = source
    return tuple(by_key.values())


def _is_matching_primary_source(existing: Event, candidate: Event) -> bool:
    """Проверяет, совпадает ли основной источник существующей и поступающей карточек."""
    return (existing.source_type, existing.source_hash) == (candidate.source_type, candidate.source_hash)


async def seed_events_from_file(
    event_repository: PostgreSqlEventRepository, *, events_path: Path = DEFAULT_SEED_EVENTS_PATH
) -> tuple[int, int, int, int, int]:
    """Идемпотентно загружает мероприятия из JSON-файла в каталог PostgreSQL.

    Arguments:
        event_repository: Экземпляр репозитория событий каталога.
        events_path: Путь к файлу подготовленных событий.

    Returns:
        Кортеж счётчиков: `(processed, created, updated, merged, skipped_unchanged)`.
    """
    seed_items = load_seed_events(events_path)
    cities = await event_repository.list_cities()
    venues = await event_repository.list_venues()

    processed = 0
    created = 0
    updated = 0
    merged = 0
    skipped_unchanged = 0
    timestamp_now = datetime.now(tz=BASHKORTOSTAN_TIMEZONE)

    for item in seed_items:
        processed += 1
        candidate = _build_domain_event(item, cities=cities, venues=venues, timestamp_now=timestamp_now)

        existing = await _find_existing_event(event_repository, candidate_event=candidate)
        if existing is not None:
            if not _is_matching_primary_source(existing, candidate) and existing.identifier != candidate.identifier:
                existing_sources_map = {(source.source_type, source.source_hash): source for source in existing.sources}
                needs_source_refresh = any(
                    existing_sources_map.get((source.source_type, source.source_hash)) != source
                    for source in candidate.sources
                )
                if not needs_source_refresh:
                    skipped_unchanged += 1
                    continue
                merged_event = existing
                for source_reference in candidate.sources:
                    merged_event = merge_event_sources(
                        merged_event,
                        new_source=source_reference,
                        updated_at=timestamp_now,
                        new_status=candidate.status if candidate.status == EventStatus.CANCELED else None,
                    )
                await event_repository.save_event(event=merged_event)
                merged += 1
                continue

            combined_sources = _preserve_existing_secondary_sources(existing.sources, candidate.sources)
            aligned_incoming = Event(
                identifier=existing.identifier,
                title=candidate.title,
                normalized_title=candidate.normalized_title,
                category=candidate.category,
                tags=candidate.tags,
                date_start=candidate.date_start,
                date_end=candidate.date_end,
                duration_minutes=candidate.duration_minutes,
                city=candidate.city,
                venue=candidate.venue,
                address=candidate.address,
                latitude=candidate.latitude,
                longitude=candidate.longitude,
                description=candidate.description,
                price_minimum=candidate.price_minimum,
                price_maximum=candidate.price_maximum,
                is_free=candidate.is_free,
                age_limit=candidate.age_limit,
                language=candidate.language,
                organization_link=candidate.organization_link,
                source_url=candidate.source_url,
                source_type=candidate.source_type,
                source_hash=candidate.source_hash,
                verified_at=candidate.verified_at,
                confidence=candidate.confidence,
                status=candidate.status,
                deduplication_key=candidate.deduplication_key,
                created_at=existing.created_at,
                updated_at=timestamp_now,
                sources=combined_sources,
            )
            if _events_semantically_equal(existing, aligned_incoming):
                skipped_unchanged += 1
                continue
            await event_repository.save_event(event=aligned_incoming)
            updated += 1
            continue

        duplicate = await _find_duplicate_candidate(event_repository, candidate_event=candidate)
        if duplicate is not None:
            merged_event = duplicate
            for source_reference in candidate.sources:
                merged_event = merge_event_sources(
                    merged_event,
                    new_source=source_reference,
                    updated_at=timestamp_now,
                    new_status=candidate.status if candidate.status == EventStatus.CANCELED else None,
                )
            existing_keys = {(source.source_type, source.source_hash) for source in duplicate.sources}
            merged_keys = {(source.source_type, source.source_hash) for source in merged_event.sources}
            if existing_keys == merged_keys and duplicate.status == merged_event.status:
                skipped_unchanged += 1
                continue
            await event_repository.save_event(event=merged_event)
            merged += 1
            continue

        await event_repository.save_event(event=candidate)
        created += 1

    logger.info(
        "Сидинг событий завершён: обработано=%s, создано=%s, обновлено=%s, объединено=%s, без_изменений=%s.",
        processed,
        created,
        updated,
        merged,
        skipped_unchanged,
    )
    return processed, created, updated, merged, skipped_unchanged


async def run_database_seed(
    *,
    venues_catalog_path: Path = DEFAULT_VENUES_CATALOG_PATH,
    seed_events_path: Path | None = None,
    references_only: bool = False,
) -> SeedExecutionReport:
    """Выполняет полный цикл идемпотентной инициализации справочников и событий.

    Arguments:
        venues_catalog_path: Путь к YAML-справочнику городов и площадок.
        seed_events_path: Явный путь к JSON-файлу событий; при `None` проверяется `data/seed_events.json`.
        references_only: Флаг загрузки только справочников без чтения файла событий.

    Returns:
        Подробная сводка выполненных операций сидинга.
    """
    explicit_events_requested = seed_events_path is not None
    effective_events_path = seed_events_path if seed_events_path is not None else DEFAULT_SEED_EVENTS_PATH

    async with DatabaseEngineManager(get_database_settings()) as engine_manager:
        (
            cities_processed,
            cities_created,
            cities_updated,
            venues_processed,
            venues_created,
            venues_updated,
        ) = await seed_reference_catalog(engine_manager, catalog_path=venues_catalog_path)

        if references_only:
            return SeedExecutionReport(
                cities_processed=cities_processed,
                cities_created=cities_created,
                cities_updated=cities_updated,
                venues_processed=venues_processed,
                venues_created=venues_created,
                venues_updated=venues_updated,
                events_file_found=False,
            )

        if not effective_events_path.is_file():
            if explicit_events_requested:
                raise ValidationException(f"Запрошенный файл событий не найден: {effective_events_path}.")
            logger.info(
                "Файл событий по умолчанию %s пока отсутствует — загружены только справочники.", effective_events_path
            )
            return SeedExecutionReport(
                cities_processed=cities_processed,
                cities_created=cities_created,
                cities_updated=cities_updated,
                venues_processed=venues_processed,
                venues_created=venues_created,
                venues_updated=venues_updated,
                events_file_found=False,
            )

        event_repository = PostgreSqlEventRepository(engine_manager)
        processed, created, updated, merged, skipped = await seed_events_from_file(
            event_repository, events_path=effective_events_path
        )

    return SeedExecutionReport(
        cities_processed=cities_processed,
        cities_created=cities_created,
        cities_updated=cities_updated,
        venues_processed=venues_processed,
        venues_created=venues_created,
        venues_updated=venues_updated,
        events_file_found=True,
        events_processed=processed,
        events_created=created,
        events_updated=updated,
        events_merged=merged,
        events_skipped_unchanged=skipped,
    )


def _build_argument_parser() -> argparse.ArgumentParser:
    """Создаёт парсер аргументов командной строки для скрипта инициализации."""
    parser = argparse.ArgumentParser(description="Идемпотентный сидер справочников и каталога событий Башкортостана.")
    parser.add_argument(
        "--venues-file",
        type=Path,
        default=DEFAULT_VENUES_CATALOG_PATH,
        help="Путь к YAML-файлу справочника городов и площадок.",
    )
    parser.add_argument(
        "--events-file",
        type=Path,
        default=None,
        help="Путь к JSON-файлу событий (по умолчанию data/seed_events.json, если существует).",
    )
    parser.add_argument(
        "--references-only",
        action="store_true",
        help="Загрузить только справочники городов и площадок без чтения файла событий.",
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    """Точка входа CLI-команды `python -m src.entrypoints.command_line.seed`.

    Arguments:
        arguments: Необязательный список аргументов командной строки для программного запуска.

    Returns:
        Код завершения процесса (`0` при успехе).
    """
    setup_logger(get_logging_settings())
    parsed = _build_argument_parser().parse_args(list(arguments) if arguments is not None else None)
    report = asyncio.run(
        run_database_seed(
            venues_catalog_path=parsed.venues_file,
            seed_events_path=parsed.events_file,
            references_only=bool(parsed.references_only),
        )
    )
    logger.info("Итоговый отчёт сидера: %s", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
