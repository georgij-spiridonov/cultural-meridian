"""Общий прикладной поиск дублей мероприятий для импорта, начального наполнения и сбора из источников."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final, Self
from uuid import UUID

from src.core.types import EventStatus
from src.domain.entities import Event, EventSearchFilter, EventSourceReference
from src.domain.exceptions import EventNotFoundError
from src.domain.ports import EventRepositoryPort
from src.domain.services import BASHKORTOSTAN_TIMEZONE, are_events_duplicates

__all__ = [
    "DuplicateCandidate",
    "find_duplicate_event",
    "find_duplicate_in_catalog",
    "find_similar_event",
    "is_duplicate_of",
]

_MAXIMUM_SEARCH_CANDIDATES: Final[int] = 500
_SEARCHABLE_STATUSES: Final[tuple[EventStatus, ...]] = (EventStatus.PUBLISHED, EventStatus.DRAFT, EventStatus.CANCELED)


@dataclass(frozen=True, slots=True, kw_only=True)
class DuplicateCandidate:
    """Поля поступающей записи, по которым ищется дубль.

    Attributes:
        normalized_title: Нормализованное название.
        date_start: Дата и время начала.
        city: Нормализованный город.
        venue: Площадка, если известна.
        deduplication_key: Ключ дедупликации.
        sources: Упоминания источников поступающей записи.
    """

    normalized_title: str
    date_start: datetime
    city: str
    venue: str | None
    deduplication_key: str
    sources: tuple[EventSourceReference, ...]

    @classmethod
    def from_event(cls, event: Event) -> Self:
        """Строит кандидата по собранному мероприятию."""
        return cls(
            normalized_title=event.normalized_title,
            date_start=event.date_start,
            city=event.city,
            venue=event.venue,
            deduplication_key=event.deduplication_key,
            sources=event.sources,
        )


def _is_separate_session_of_same_source(existing: Event, *, candidate: DuplicateCandidate) -> bool:
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


def is_duplicate_of(existing: Event, *, candidate: Event | DuplicateCandidate) -> bool:
    """Проверяет, описывает ли поступающая запись уже существующее мероприятие.

    Решение принимает доменное правило `are_events_duplicates`: тот же город,
    тот же локальный календарный день, допуск по времени начала и сходство названий и площадок.
    Правило не применяется к сеансам, которые один и тот же источник публикует на разных страницах.

    Arguments:
        existing: Мероприятие, уже сохранённое в каталоге или в текущем пакете.
        candidate: Поступающее мероприятие или его поля.

    Returns:
        `True`, если поступающая запись является дублем существующего мероприятия.
    """
    fields = candidate if isinstance(candidate, DuplicateCandidate) else DuplicateCandidate.from_event(candidate)
    if _is_separate_session_of_same_source(existing, candidate=fields):
        return False
    verdict = are_events_duplicates(
        existing,
        normalized_title=fields.normalized_title,
        date_start=fields.date_start,
        city=fields.city,
        venue=fields.venue,
        deduplication_key=fields.deduplication_key,
    )
    return verdict.is_duplicate


async def find_similar_event(
    *, candidate: DuplicateCandidate, event_repository: EventRepositoryPort, batch_events: Iterable[Event] = ()
) -> Event | None:
    """Ищет дубль среди мероприятий текущего пакета, затем среди мероприятий каталога того же города и дня.

    Окно запроса к каталогу (локальный календарный день) лишь ограничивает выборку кандидатов
    и не заменяет доменное правило дубля из `are_events_duplicates`.

    Arguments:
        candidate: Поля поступающей записи.
        event_repository: Порт каталога мероприятий.
        batch_events: Мероприятия, уже сохранённые в текущем пакете.

    Returns:
        Найденное мероприятие-дубль либо `None`.
    """
    for batch_event in batch_events:
        if is_duplicate_of(batch_event, candidate=candidate):
            return batch_event

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


async def find_duplicate_in_catalog(*, candidate: Event, event_repository: EventRepositoryPort) -> Event | None:
    """Ищет дубль поступающего мероприятия среди мероприятий каталога того же города и дня.

    Arguments:
        candidate: Поступающее мероприятие.
        event_repository: Порт каталога мероприятий.

    Returns:
        Найденное мероприятие-дубль либо `None`.
    """
    return await find_similar_event(
        candidate=DuplicateCandidate.from_event(candidate), event_repository=event_repository
    )


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

    return await find_similar_event(
        candidate=DuplicateCandidate.from_event(candidate),
        event_repository=event_repository,
        batch_events=batch_events_by_identifier.values(),
    )
