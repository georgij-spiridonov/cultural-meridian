"""Модульные тесты правила дубля с учётом сеансов одного источника из `src/application/event_merging.py`."""

from datetime import datetime
from uuid import uuid4

from src.application.event_merging import is_duplicate_of
from src.core.types import EventCategory, SourceType
from src.domain.entities import Event, EventSourceReference
from src.domain.services import BASHKORTOSTAN_TIMEZONE, compute_deduplication_key, normalize_title


def _event(*, source_name: str, source_url: str, hour: int) -> Event:
    """Создаёт мероприятие с одним упоминанием источника."""
    title = "Дети дома Павлова"
    normalized_title = normalize_title(title)
    date_start = datetime(2026, 11, 16, hour, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)
    venue = "Салаватский государственный башкирский драматический театр"
    return Event(
        identifier=uuid4(),
        title=title,
        normalized_title=normalized_title,
        category=EventCategory.SPECTACLE,
        date_start=date_start,
        city="Салават",
        venue=venue,
        address="ул. 30 лет Победы, 5",
        source_url=source_url,
        source_type=SourceType.WEBPAGE,
        source_hash=source_url,
        deduplication_key=compute_deduplication_key(
            normalized_title=normalized_title, date_start=date_start, city="Салават", venue=venue
        ),
        created_at=date_start,
        updated_at=date_start,
        sources=(
            EventSourceReference(
                source_name=source_name, source_type=SourceType.WEBPAGE, source_url=source_url, source_hash=source_url
            ),
        ),
    )


def test_sessions_of_same_source_on_different_pages_are_not_duplicates() -> None:
    """Проверяет, что сеансы в 14:00 и 15:00 одного источника остаются отдельными мероприятиями."""
    first = _event(source_name="quicktickets-salavat", source_url="https://quicktickets.ru/s550", hour=14)
    second = _event(source_name="quicktickets-salavat", source_url="https://quicktickets.ru/s551", hour=15)

    assert is_duplicate_of(first, candidate=second) is False


def test_same_page_of_same_source_is_duplicate() -> None:
    """Проверяет, что повторная запись той же страницы источника считается тем же мероприятием."""
    first = _event(source_name="quicktickets-salavat", source_url="https://quicktickets.ru/s550", hour=14)
    repeated = _event(source_name="quicktickets-salavat", source_url="https://quicktickets.ru/s550", hour=14)

    assert is_duplicate_of(first, candidate=repeated) is True


def test_same_event_from_another_source_is_duplicate() -> None:
    """Проверяет, что то же мероприятие из другого источника с расхождением во времени объединяется."""
    first = _event(source_name="quicktickets-salavat", source_url="https://quicktickets.ru/s550", hour=14)
    other_source = _event(source_name="theater-site", source_url="https://theater.example.ru/pavlov", hour=15)

    assert is_duplicate_of(first, candidate=other_source) is True
