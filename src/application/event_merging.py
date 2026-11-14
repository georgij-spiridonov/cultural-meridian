"""Общий прикладной поиск дублей мероприятий для импорта, начального наполнения и сбора из источников."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final
from uuid import UUID

from src.core.types import EventStatus
from src.domain.entities import Event, EventSearchFilter
from src.domain.exceptions import EventNotFoundError
from src.domain.ports import EventRepositoryPort
from src.domain.services import BASHKORTOSTAN_TIMEZONE, are_events_duplicates

__all__ = ["find_duplicate_event", "find_duplicate_in_catalog", "is_duplicate_of"]

_MAXIMUM_SEARCH_CANDIDATES: Final[int] = 500
_SEARCHABLE_STATUSES: Final[tuple[EventStatus, ...]] = (EventStatus.PUBLISHED, EventStatus.DRAFT, EventStatus.CANCELED)


def _is_separate_session_of_same_source(existing: Event, *, candidate: Event) -> bool:
    """Проверяет, что поступающая запись — другой сеанс, который тот же источник перечислил отдельно.

    Если у существующего мероприятия уже есть упоминание того же источника, но с другим адресом
    страницы, источник сам считает эти записи разными показами (например, сеансы в 14:00 и 15:00).
    """
    for incoming in candidate.sources:
        same_source_urls = {
            reference.source_url for reference in existing.sources if reference.source_name == incoming.source_name
        }
        if same_source_urls and incoming.source_url not in same_source_urls:
            return True
    return False


def is_duplicate_of(existing: Event, *, candidate: Event) -> bool:
    """Проверяет, описывает ли поступающее мероприятие уже существующее.

    Решение принимает доменное правило `are_events_duplicates`: тот же город,
    тот же локальный календарный день, допуск по времени начала и сходство названий и площадок.
    Правило не применяется к сеансам, которые один и тот же источник публикует на разных страницах.

    Arguments:
        existing: Мероприятие, уже сохранённое в каталоге или в текущем пакете.
        candidate: Поступающее мероприятие.

    Returns:
        `True`, если поступающее мероприятие является дублем существующего.
    """
    if _is_separate_session_of_same_source(existing, candidate=candidate):
        return False
    verdict = are_events_duplicates(
        existing,
        normalized_title=candidate.normalized_title,
        date_start=candidate.date_start,
        city=candidate.city,
        venue=candidate.venue,
        deduplication_key=candidate.deduplication_key,
    )
    return verdict.is_duplicate


async def find_duplicate_in_catalog(*, candidate: Event, event_repository: EventRepositoryPort) -> Event | None:
    """Ищет дубль поступающего мероприятия среди мероприятий каталога того же города и дня.

    Окно запроса к каталогу (локальный календарный день) лишь ограничивает выборку кандидатов
    и не заменяет доменное правило дубля из `are_events_duplicates`.

    Arguments:
        candidate: Поступающее мероприятие.
        event_repository: Порт каталога мероприятий.

    Returns:
        Найденное мероприятие-дубль либо `None`.
    """
    local_start = candidate.date_start.astimezone(BASHKORTOSTAN_TIMEZONE)
    day_start = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = local_start.replace(hour=23, minute=59, second=59, microsecond=999999)
    page = await event_repository.search_events(
        search_filter=EventSearchFilter(
            city=candidate.city, date_from=day_start, date_to=day_end, page=1, page_size=_MAXIMUM_SEARCH_CANDIDATES
        ),
        statuses=_SEARCHABLE_STATUSES,
    )
    for existing in page.items:
        if is_duplicate_of(existing, candidate=candidate):
            return existing
    return None


async def find_duplicate_event(
    *,
    candidate: Event,
    event_repository: EventRepositoryPort,
    batch_events_by_identifier: Mapping[UUID, Event] = MappingProxyType({}),
    batch_events_by_hash: Mapping[str, Event] = MappingProxyType({}),
    explicit_identifier: UUID | None = None,
) -> Event | None:
    """Ищет существующее мероприятие, которое описывает поступающая запись.

    Порядок поиска: хэш источника в текущем пакете, явный идентификатор в пакете и в каталоге,
    хэш источника в каталоге, сходство с мероприятиями пакета, сходство с мероприятиями каталога.

    Arguments:
        candidate: Поступающее мероприятие.
        event_repository: Порт каталога мероприятий.
        batch_events_by_identifier: Мероприятия, уже сохранённые в текущем пакете, по идентификатору.
        batch_events_by_hash: Мероприятия, уже сохранённые в текущем пакете, по хэшу источника.
        explicit_identifier: Идентификатор, явно указанный во входных данных.

    Returns:
        Найденное мероприятие либо `None`, если поступающая запись описывает новое мероприятие.
    """
    in_batch_by_hash = batch_events_by_hash.get(candidate.source_hash)
    if in_batch_by_hash is not None:
        return in_batch_by_hash

    if explicit_identifier is not None:
        in_batch_by_identifier = batch_events_by_identifier.get(explicit_identifier)
        if in_batch_by_identifier is not None:
            return in_batch_by_identifier
        try:
            return await event_repository.get_event_by_identifier(
                event_identifier=explicit_identifier, include_non_published=True
            )
        except EventNotFoundError:
            pass

    matched_by_hash = await event_repository.find_by_source_hash(
        source_type=candidate.source_type, source_hash=candidate.source_hash
    )
    if matched_by_hash is not None:
        return matched_by_hash

    for batch_event in batch_events_by_identifier.values():
        if is_duplicate_of(batch_event, candidate=candidate):
            return batch_event

    return await find_duplicate_in_catalog(candidate=candidate, event_repository=event_repository)
