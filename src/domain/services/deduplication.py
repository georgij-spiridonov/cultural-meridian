"""Чистые доменные правила идемпотентности, дедупликации и объединения источников."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Final

from src.core.types import EventStatus
from src.domain.entities import Event, EventSourceReference, ExtractedEventFields
from src.domain.services.normalization import BASHKORTOSTAN_TIMEZONE, normalize_text_for_matching, normalize_title

__all__ = [
    "DuplicateMatchResult",
    "are_events_duplicates",
    "compute_deduplication_key",
    "compute_payload_hash",
    "merge_event_sources",
]

_GENERIC_TITLE_PREFIXES: Final[tuple[str, ...]] = (
    "концерт",
    "спектакль",
    "выставка",
    "фестиваль",
    "кинопоказ",
    "экскурсия",
    "музыкальный вечер",
    "творческий вечер",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class DuplicateMatchResult:
    """Результат проверки двух мероприятий на совпадение при дедупликации.

    Attributes:
        is_duplicate: Признак того, что карточки описывают одно и то же событие.
        similarity_score: Числовая оценка сходства в диапазоне `0.0..1.0`.
        reason: Понятное пояснение основания совпадения или различия.
    """

    is_duplicate: bool
    similarity_score: float
    reason: str


def compute_payload_hash(payload: Mapping[str, object]) -> str:
    """Вычисляет детерминированный SHA-256 хэш сырой записи для идемпотентности.

    Arguments:
        payload: Словарь исходных полей записи из файла импорта или внешнего источника.

    Returns:
        Шестнадцатеричная строка длиной 64 символа.
    """
    canonical_json = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _strip_generic_prefixes(normalized_title: str) -> str:
    """Убирает общие жанровые префиксы в начале заголовка для точного сравнения названий."""
    result = normalized_title.strip()
    changed = True
    while changed and result:
        changed = False
        for prefix in _GENERIC_TITLE_PREFIXES:
            prefix_with_space = f"{prefix} "
            if result.startswith(prefix_with_space):
                result = result[len(prefix_with_space) :].strip()
                changed = True
                break
    return result or normalized_title.strip()


def _to_local_datetime(value: datetime) -> datetime:
    """Приводит дату и время к единому часовому поясу Республики Башкортостан."""
    if value.tzinfo is None:
        return value.replace(tzinfo=BASHKORTOSTAN_TIMEZONE)
    return value.astimezone(BASHKORTOSTAN_TIMEZONE)


def compute_deduplication_key(
    *, normalized_title: str, date_start: datetime, city: str, venue: str | None = None
) -> str:
    """Формирует устойчивый ключ дедупликации по названию, дате, городу и площадке.

    Arguments:
        normalized_title: Нормализованное название события.
        date_start: Дата и время начала мероприятия.
        city: Нормализованное название города.
        venue: Название площадки проведения, если известно.

    Returns:
        Детерминированный 32-символьный ключ дедупликации.
    """
    core_title = _strip_generic_prefixes(normalize_title(normalized_title))
    normalized_city = normalize_text_for_matching(city)
    normalized_venue = normalize_text_for_matching(venue) if venue else ""
    date_bucket = _to_local_datetime(date_start).strftime("%Y-%m-%d %H:%M")
    raw_signature = f"{normalized_city}|{date_bucket}|{normalized_venue}|{core_title}"
    return hashlib.sha256(raw_signature.encode("utf-8")).hexdigest()[:32]


def _calculate_title_similarity(first_title: str, second_title: str) -> float:
    """Комбинирует посимвольное сходство и пересечение слов без жанровых префиксов."""
    left = _strip_generic_prefixes(normalize_title(first_title))
    right = _strip_generic_prefixes(normalize_title(second_title))
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    sequence_ratio = SequenceMatcher(None, left, right).ratio()
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    union_tokens = left_tokens | right_tokens
    jaccard_ratio = len(left_tokens & right_tokens) / len(union_tokens) if union_tokens else 0.0
    return round(0.6 * sequence_ratio + 0.4 * jaccard_ratio, 3)


def _are_dates_compatible(first_start: datetime, second_start: datetime) -> bool:
    """Проверяет совпадение календарного дня и допустимую разницу во времени старта."""
    local_first = _to_local_datetime(first_start)
    local_second = _to_local_datetime(second_start)
    if local_first.date() != local_second.date():
        return False
    # Если у одного из источников время не указано (ровно 00:00), достаточно совпадения дня.
    if (local_first.hour == 0 and local_first.minute == 0) or (local_second.hour == 0 and local_second.minute == 0):
        return True
    return abs((local_first - local_second).total_seconds()) <= 7200


def _calculate_venue_similarity(first_venue: str | None, second_venue: str | None) -> float | None:
    """Оценивает совпадение площадок; возвращает `None`, если одна из площадок не указана."""
    normalized_first = normalize_text_for_matching(first_venue or "")
    normalized_second = normalize_text_for_matching(second_venue or "")
    if not normalized_first or not normalized_second:
        return None
    if normalized_first in normalized_second or normalized_second in normalized_first:
        return 1.0
    return round(SequenceMatcher(None, normalized_first, normalized_second).ratio(), 3)


def are_events_duplicates(
    candidate: Event,
    *,
    normalized_title: str,
    date_start: datetime,
    city: str,
    venue: str | None = None,
    deduplication_key: str | None = None,
) -> DuplicateMatchResult:
    """Определяет, является ли поступающее описание дублем существующей карточки.

    Arguments:
        candidate: Существующая карточка мероприятия из каталога.
        normalized_title: Нормализованное название поступающего события.
        date_start: Дата и время начала поступающего события.
        city: Нормализованный город проведения.
        venue: Название площадки поступающего события.
        deduplication_key: Предварительно вычисленный ключ дедупликации.

    Returns:
        Структурированный вердикт со степенью сходства и причиной.
    """
    if deduplication_key is not None and candidate.deduplication_key == deduplication_key:
        return DuplicateMatchResult(
            is_duplicate=True, similarity_score=1.0, reason="Точное совпадение ключа дедупликации."
        )

    if normalize_text_for_matching(candidate.city) != normalize_text_for_matching(city):
        return DuplicateMatchResult(is_duplicate=False, similarity_score=0.0, reason="Разные города.")

    if not _are_dates_compatible(candidate.date_start, date_start):
        return DuplicateMatchResult(is_duplicate=False, similarity_score=0.0, reason="Несовместимые дата или время.")

    title_similarity = _calculate_title_similarity(candidate.normalized_title, normalized_title)
    venue_similarity = _calculate_venue_similarity(candidate.venue, venue)

    if venue_similarity is None:
        is_duplicate = title_similarity >= 0.82
        reason = (
            "Совпадение названия и даты в одном городе."
            if is_duplicate
            else "Недостаточное сходство названий без подтверждения площадки."
        )
        return DuplicateMatchResult(is_duplicate=is_duplicate, similarity_score=title_similarity, reason=reason)

    combined_score = round(0.75 * title_similarity + 0.25 * venue_similarity, 3)
    if venue_similarity >= 0.65 and title_similarity >= 0.72:
        return DuplicateMatchResult(
            is_duplicate=True, similarity_score=combined_score, reason="Совпадение названия, даты и площадки."
        )

    if title_similarity >= 0.92:
        return DuplicateMatchResult(
            is_duplicate=True,
            similarity_score=combined_score,
            reason="Высокое совпадение названия на ту же дату в одном городе.",
        )

    return DuplicateMatchResult(
        is_duplicate=False,
        similarity_score=combined_score,
        reason="Названия или площадки различаются ниже порога дедупликации.",
    )


def merge_event_sources(
    existing_event: Event,
    *,
    new_source: EventSourceReference,
    updated_fields: ExtractedEventFields | None = None,
    updated_at: datetime,
    new_status: EventStatus | None = None,
) -> Event:
    """Объединяет подтверждённый источник и недостающие поля в новую иммутабельную карточку.

    Arguments:
        existing_event: Существующая карточка в каталоге.
        new_source: Подтверждённая ссылка на дополнительный или обновлённый источник.
        updated_fields: Дополнительно извлечённые поля для обогащения пустых атрибутов.
        updated_at: Временная метка обновления карточки.
        new_status: Необязательное обновление статуса публикации.

    Returns:
        Новая неизменяемая сущность `Event`, готовая к сохранению через `EventRepositoryPort.save_event`.
    """
    existing_sources = list(existing_event.sources)
    if not existing_sources:
        existing_sources.append(
            EventSourceReference(
                source_name="primary",
                source_type=existing_event.source_type,
                source_url=existing_event.source_url,
                source_hash=existing_event.source_hash,
                verified_at=existing_event.verified_at,
            )
        )

    replaced = False
    for index, source_item in enumerate(existing_sources):
        if source_item.source_url == new_source.source_url or (
            source_item.source_name == new_source.source_name and source_item.source_hash == new_source.source_hash
        ):
            existing_sources[index] = new_source
            replaced = True
            break
    if not replaced:
        existing_sources.append(new_source)

    merged_tags = list(existing_event.tags)
    if updated_fields is not None:
        for tag in updated_fields.tags:
            if tag not in merged_tags:
                merged_tags.append(tag)

    date_end = existing_event.date_end or (updated_fields.date_end if updated_fields else None)
    duration_minutes = existing_event.duration_minutes or (updated_fields.duration_minutes if updated_fields else None)
    price_minimum = (
        existing_event.price_minimum
        if existing_event.price_minimum is not None
        else (updated_fields.price_minimum if updated_fields else None)
    )
    price_maximum = (
        existing_event.price_maximum
        if existing_event.price_maximum is not None
        else (updated_fields.price_maximum if updated_fields else None)
    )
    is_free = existing_event.is_free or (
        existing_event.price_minimum is None
        and existing_event.price_maximum is None
        and updated_fields is not None
        and bool(updated_fields.is_free)
    )
    language = existing_event.language or (updated_fields.language if updated_fields else None)
    confidence = max(existing_event.confidence, updated_fields.confidence if updated_fields else 0.0)
    verified_at = new_source.verified_at or existing_event.verified_at

    return Event(
        identifier=existing_event.identifier,
        title=existing_event.title,
        normalized_title=existing_event.normalized_title,
        category=existing_event.category,
        tags=tuple(merged_tags),
        date_start=existing_event.date_start,
        date_end=date_end,
        duration_minutes=duration_minutes,
        city=existing_event.city,
        venue=existing_event.venue,
        address=existing_event.address,
        latitude=existing_event.latitude,
        longitude=existing_event.longitude,
        description=existing_event.description,
        price_minimum=price_minimum,
        price_maximum=price_maximum,
        is_free=is_free,
        age_limit=existing_event.age_limit,
        language=language,
        organization_link=existing_event.organization_link,
        source_url=existing_event.source_url,
        source_type=existing_event.source_type,
        source_hash=existing_event.source_hash,
        verified_at=verified_at,
        confidence=confidence,
        status=new_status or existing_event.status,
        deduplication_key=existing_event.deduplication_key,
        created_at=existing_event.created_at,
        updated_at=updated_at,
        sources=tuple(existing_sources),
    )
