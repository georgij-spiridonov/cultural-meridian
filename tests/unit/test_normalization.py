"""Модульные тесты нормализации дат и городов из `src/domain/services/normalization.py`."""

from datetime import date, datetime

import pytest

from src.domain.services import BASHKORTOSTAN_TIMEZONE
from src.domain.services.normalization import normalize_city, parse_event_datetime

_REFERENCE_DATETIME = datetime(2026, 11, 14, 12, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)


@pytest.mark.parametrize(
    ("raw_date_text", "expected_date"),
    [
        ("15 января в 19:00", date(2027, 1, 15)),
        ("15.01 в 19:00", date(2027, 1, 15)),
        ("16 ноября в 19:00", date(2026, 11, 16)),
        ("10 ноября", date(2026, 11, 10)),
        ("20 октября", date(2026, 10, 20)),
        ("13 октября", date(2027, 10, 13)),
        ("15 января 2026", date(2026, 1, 15)),
        ("15.01.2026", date(2026, 1, 15)),
    ],
)
def test_parse_event_datetime_resolves_year_without_explicit_year(raw_date_text: str, expected_date: date) -> None:
    """Проверяет перенос даты без года на следующий год, если она ушла в прошлое больше чем на 30 дней."""
    date_start, _, _ = parse_event_datetime(raw_date_text, reference_datetime=_REFERENCE_DATETIME)

    assert date_start is not None
    assert date_start.date() == expected_date


def test_parse_event_datetime_moves_whole_range_to_next_year() -> None:
    """Проверяет, что диапазон без года переносится на следующий год целиком."""
    date_start, date_end, _ = parse_event_datetime("15–17 января", reference_datetime=_REFERENCE_DATETIME)

    assert date_start is not None
    assert date_end is not None
    assert date_start.date() == date(2027, 1, 15)
    assert date_end.date() == date(2027, 1, 17)


def test_parse_event_datetime_keeps_current_behavior_for_february_29_in_common_year() -> None:
    """Фиксирует текущее поведение: «29 февраля» без года в невисокосном году не разбирается."""
    assert parse_event_datetime("29 февраля", reference_datetime=_REFERENCE_DATETIME) == (None, None, None)


@pytest.mark.parametrize(
    ("raw_city", "expected_city"),
    [("kumertau", "Кумертау"), ("sterlitamak", "Стерлитамак"), ("zirgan", "Зирган"), ("в Зиргане", "Зирган")],
)
def test_normalize_city_resolves_latin_aliases_and_zirgan(raw_city: str, expected_city: str) -> None:
    """Проверяет латинские алиасы городов и новый населённый пункт Зирган."""
    assert normalize_city(raw_city) == expected_city
