"""Модульные тесты парсеров HTML-страниц источников на сохранённых снимках `tests/fixtures/sources`."""

from datetime import date, datetime
from pathlib import Path

import pytest

from src.core.exceptions import ValidationSchemaError
from src.core.types import SourceType
from src.domain.entities import SourceDefinition
from src.domain.services import BASHKORTOSTAN_TIMEZONE
from src.domain.services.normalization import normalize_city, parse_event_datetime, parse_price_range
from src.infrastructure.sources.bashopera import parse_bashopera_page
from src.infrastructure.sources.quicktickets import parse_quicktickets_page, parse_quicktickets_session_address
from src.infrastructure.sources.rusdramteatr import parse_rusdramteatr_page
from src.infrastructure.sources.wordpress_events import parse_wordpress_events_page

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "sources"
_REFERENCE_DATETIME = datetime(2026, 11, 14, 12, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)


def _read_fixture(name: str) -> str:
    """Читает снимок страницы источника."""
    return (_FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def _source(name: str, url: str) -> SourceDefinition:
    """Создаёт описание источника для теста."""
    return SourceDefinition(name=name, source_type=SourceType.WEBPAGE, source_url=url)


def _parse_date(raw_date_text: str | None) -> datetime | None:
    """Разбирает дату относительно опорной даты снимков."""
    return parse_event_datetime(raw_date_text, reference_datetime=_REFERENCE_DATETIME)[0]


@pytest.mark.parametrize(
    ("fixture", "url", "expected_count"),
    [("bashgf_afisha", "https://bashgf.ru/afisha/", 12), ("sgtko_afisha", "https://sgtko.ru/afisha/", 12)],
)
def test_wordpress_events_parser_extracts_all_cards(fixture: str, url: str, expected_count: int) -> None:
    """Проверяет разбор обоих сайтов на WordPress-плагине событий одним парсером."""
    items = parse_wordpress_events_page(_read_fixture(fixture), source=_source(fixture, url), page_url=url, options={})

    assert len(items) == expected_count
    assert len({item.external_identifier for item in items}) == expected_count
    assert all(_parse_date(item.raw_date_text) is not None for item in items)
    assert all(item.source_url.startswith(url) for item in items)


def test_wordpress_events_parser_reads_iso_date_price_and_hall() -> None:
    """Проверяет поля первой карточки bashgf.ru."""
    url = "https://bashgf.ru/afisha/"
    first = parse_wordpress_events_page(
        _read_fixture("bashgf_afisha"), source=_source("bashgf", url), page_url=url, options={}
    )[0]

    assert first.raw_title == "Освобождение звука"
    assert _parse_date(first.raw_date_text) == datetime(2026, 11, 14, 18, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)
    assert parse_price_range(first.raw_price_text) == (600, 800, False)
    assert first.raw_venue is None
    assert first.raw_description is not None
    assert first.raw_description.startswith("Малый (органный) зал. ")
    assert first.organization_link is not None
    assert first.organization_link.startswith("https://widget.kassir.ru/")


def test_wordpress_events_parser_rewrites_open_price_range_as_lower_bound() -> None:
    """Проверяет, что «Цена: 400 - ₽» превращается в нижнюю границу цены."""
    url = "https://sgtko.ru/afisha/"
    items = parse_wordpress_events_page(
        _read_fixture("sgtko_afisha"), source=_source("sgtko", url), page_url=url, options={}
    )
    open_range = next(item for item in items if item.raw_title == "Музыкальная программа “По дороге с облаками”")

    assert open_range.raw_price_text == "от 400 ₽"
    assert parse_price_range(open_range.raw_price_text) == (400, None, False)


def test_quicktickets_parser_creates_one_item_per_session() -> None:
    """Проверяет, что каждый сеанс становится отдельной записью с собственной ссылкой."""
    url = "https://quicktickets.ru/salavat/afisha"
    items = parse_quicktickets_page(
        _read_fixture("quicktickets_salavat"), source=_source("quicktickets", url), page_url=url, options={}
    )

    assert len(items) == 32
    assert len({item.external_identifier for item in items}) == 32
    assert all(_parse_date(item.raw_date_text) is not None for item in items)
    first = items[0]
    assert first.raw_title == "Дети дома Павлова"
    assert first.external_identifier == "/salavat-teatr-dramy/s550"
    assert first.raw_age_text == "12+"


def test_quicktickets_parser_marks_multiple_addresses_and_reads_settlement() -> None:
    """Проверяет признак «Разные адреса» и город из адреса карточки с префиксом «с.»."""
    url = "https://quicktickets.ru/salavat/afisha"
    items = parse_quicktickets_page(
        _read_fixture("quicktickets_salavat"), source=_source("quicktickets", url), page_url=url, options={}
    )
    by_title = {item.raw_title: item for item in items}

    multiple = by_title["Дети дома Павлова"]
    assert multiple.raw_address is None
    assert multiple.raw_city is None
    assert multiple.raw_payload["address_scope"] == "multiple"

    touring = by_title["Волшебная лампа Аладдина"]
    assert touring.raw_city == "Исянгулово"
    assert touring.raw_payload["address_scope"] == "single"


def test_quicktickets_session_page_provides_address_with_settlement() -> None:
    """Проверяет адрес со страницы сеанса и его сопоставление со справочником городов."""
    address = parse_quicktickets_session_address(_read_fixture("quicktickets_session"))

    assert address == "Мелеузовский район с.Зирган"


def test_rusdramteatr_parser_takes_date_and_city_from_slug() -> None:
    """Проверяет дату с годом и город гастроли из слага ссылки."""
    url = "https://rusdramteatr.ru/afisha"
    items = parse_rusdramteatr_page(
        _read_fixture("rusdramteatr_afisha"), source=_source("rusdramteatr", url), page_url=url, options={}
    )
    by_identifier = {item.external_identifier: item for item in items}

    assert len(items) == 21
    assert len(by_identifier) == 21
    touring = by_identifier["/afisha/groza-18-11-2026-kumertau"]
    assert touring.raw_title == "Гроза"
    assert normalize_city(touring.raw_city) == "Кумертау"
    assert _parse_date(touring.raw_date_text) == datetime(2026, 11, 18, 19, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)
    assert normalize_city(by_identifier["/afisha/gorgony-17-11-2026-g-meleuz"].raw_city) == "Мелеуз"
    assert by_identifier["/afisha/makbet-14-11-2026"].raw_city is None


def test_rusdramteatr_parser_falls_back_to_schedule_text_without_date_in_slug() -> None:
    """Проверяет слаг без даты: дата берётся из строки расписания и год определяет нормализация."""
    url = "https://rusdramteatr.ru/afisha"
    items = parse_rusdramteatr_page(
        _read_fixture("rusdramteatr_afisha"), source=_source("rusdramteatr", url), page_url=url, options={}
    )
    item = next(
        item
        for item in items
        if item.external_identifier == "/afisha/my-khotim-chtoby-v-etom-mire-bylo-bolshe-schastlivykh-zhenshchin"
    )

    parsed = _parse_date(item.raw_date_text)
    assert parsed is not None
    assert parsed.date() == date(2026, 12, 4)


def test_bashopera_parser_reads_playbill_block_without_year() -> None:
    """Проверяет блок «Афиша» главной страницы и перенос января на следующий год."""
    url = "https://bashopera.ru/"
    items = parse_bashopera_page(
        _read_fixture("bashopera_afisha"), source=_source("bashopera", url), page_url=url, options={}
    )

    assert len(items) == 22
    assert len({item.external_identifier for item in items}) == 22
    first = items[0]
    assert first.raw_title == "Травиата"
    assert first.raw_venue is None
    assert first.raw_description == "Большой зал. Премьера Джузеппе Верди"
    assert first.raw_age_text == "16+"
    assert _parse_date(first.raw_date_text) == datetime(2026, 11, 14, 18, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)
    january = next(item for item in items if item.raw_title == "Аттила")
    parsed = _parse_date(january.raw_date_text)
    assert parsed is not None
    assert parsed.year == 2027


@pytest.mark.parametrize(
    "parser", [parse_wordpress_events_page, parse_quicktickets_page, parse_rusdramteatr_page, parse_bashopera_page]
)
def test_parsers_reject_page_without_cards(parser: object) -> None:
    """Проверяет, что изменившаяся разметка без карточек считается ошибкой схемы, а не пустой афишей."""
    url = "https://example.ru/"
    with pytest.raises(ValidationSchemaError):
        parser("<html><body><p>Нет афиши</p></body></html>", source=_source("broken", url), page_url=url, options={})  # type: ignore[operator]
