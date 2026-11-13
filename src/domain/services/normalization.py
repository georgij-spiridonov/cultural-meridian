"""Чистая детерминированная нормализация сведений о культурных мероприятиях."""

import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from difflib import SequenceMatcher
from types import MappingProxyType
from typing import Final

from src.core.types import EventCategory, RealCollection
from src.domain.entities import CityReference, ExtractedEventFields, FetchedSourceItem, VenueReference

__all__ = [
    "BASHKORTOSTAN_TIMEZONE",
    "DEFAULT_BASHKORTOSTAN_CITIES",
    "extract_event_tags",
    "normalize_city",
    "normalize_optional_text",
    "normalize_source_item",
    "normalize_text_for_matching",
    "normalize_title",
    "normalize_venue",
    "parse_age_limit",
    "parse_category",
    "parse_event_datetime",
    "parse_language",
    "parse_price_range",
]

BASHKORTOSTAN_TIMEZONE: Final[timezone] = timezone(timedelta(hours=5))

DEFAULT_BASHKORTOSTAN_CITIES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "Уфа": ("уфа", "уфе", "уфы", "уфу", "уфой", "ufa"),
        "Стерлитамак": (
            "стерлитамак",
            "стерлитамаке",
            "стерлитамака",
            "стерлитамаку",
            "стерлитамаком",
            "стерлитакмак",
            "стерлитакмае",
            "стерлитомак",
        ),
        "Салават": ("салават", "салавате", "салавата", "салавату"),
        "Нефтекамск": ("нефтекамск", "нефтекамске", "нефтекамска", "нефтикамск"),
        "Октябрьский": ("октябрьский", "октябрьском", "октябрьского", "октябрьскому"),
        "Белорецк": ("белорецк", "белорецке", "белорецка"),
        "Туймазы": ("туймазы", "туймазах", "туймазов"),
        "Ишимбай": ("ишимбай", "ишимбае", "ишимбая"),
        "Кумертау": ("кумертау",),
        "Сибай": ("сибай", "сибае", "сибая"),
        "Мелеуз": ("мелеуз", "мелеузе", "мелеуза"),
        "Белебей": ("белебей", "белебее", "белебея"),
        "Бирск": ("бирск", "бирске", "бирска"),
        "Благовещенск": ("благовещенск", "благовещенске", "благовещенска"),
        "Учалы": ("учалы", "учалах", "учалов"),
        "Давлеканово": ("давлеканово",),
        "Дюртюли": ("дюртюли", "дюртюлях"),
        "Янаул": ("янаул", "янауле", "янаула"),
    }
)

_CATEGORY_KEYWORDS: Final[Mapping[EventCategory, tuple[str, ...]]] = MappingProxyType(
    {
        EventCategory.CONCERT: (
            "концерт",
            "музыкальный вечер",
            "филармони",
            "джаз",
            "симфоническ",
            "оркестр",
            "камерн",
            "романс",
            "вокал",
            "квартет",
            "акустик",
            "рок",
        ),
        EventCategory.SPECTACLE: (
            "спектакль",
            "театр",
            "постановк",
            "премьер",
            "опера",
            "балет",
            "мюзикл",
            "драма",
            "комеди",
            "кукол",
        ),
        EventCategory.EXHIBITION: (
            "выставк",
            "экспозиц",
            "музей",
            "галере",
            "вернисаж",
            "живопис",
            "фотовыстав",
            "арт-пространств",
        ),
        EventCategory.FESTIVAL: ("фестивал", "сабантуй", "ярмарк", "форум", "фест", "праздник"),
        EventCategory.CINEMA: ("кинопоказ", "киновечер", "кинотеатр", "кино", "фильм", "мультфильм", "ретроспектив"),
        EventCategory.EXCURSION: ("экскурс", "прогулк", "квест", "обзорн", "пешеходн", "тур"),
    }
)

_RUSSIAN_MONTHS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "январ": 1,
        "феврал": 2,
        "март": 3,
        "апрел": 4,
        "мая": 5,
        "май": 5,
        "июн": 6,
        "июл": 7,
        "август": 8,
        "сентябр": 9,
        "октябр": 10,
        "ноябр": 11,
        "декабр": 12,
    }
)

_LANGUAGE_MARKERS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "башкирский": ("башкирск", "башкорт", "BA"),
        "татарский": ("татарск",),
        "русский": ("русский язык", "на русском", "RU"),
        "английский": ("английский язык", "на английском", "EN"),
    }
)

_TAG_KEYWORDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "джаз": ("джаз", "jazz"),
        "дети": ("дет", "ребенк", "ребёнк", "семейн", "кукол", "сказк"),
        "классика": ("классич", "симфон", "филарм", "камерн", "опера", "балет"),
        "фолк": ("фолк", "этно", "народн", "курай"),
        "рок": ("рок",),
        "музей": ("музей", "выставк", "экспозиц", "галере"),
    }
)

_PUNCTUATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^\w\s-]+", flags=re.UNICODE)
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")
_CITY_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:республика\s+башкортостан|башкортостан|рб|г\.?|город|в|во|по|из)\s+", flags=re.IGNORECASE
)
_THOUSANDS_SPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<=\d)[\s\u00a0]+(?=\d{3}\b)")
_FREE_PRICE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:бесплатн\w*|вход\s+свободн\w*|без\s+оплат\w*|\bfree\b)", flags=re.IGNORECASE
)
_PRICE_RANGE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:от\s*)?(\d{1,6})\s*(?:до|[-–—/])\s*(\d{1,6})\s*(?:₽|руб\.?|рублей|рубля|р\.?)?", flags=re.IGNORECASE
)
_PRICE_FROM_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"от\s*(\d{1,6})\s*(?:₽|руб\.?|рублей|рубля|р\.?)?", flags=re.IGNORECASE
)
_PRICE_UP_TO_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"до\s*(\d{1,6})\s*(?:₽|руб\.?|рублей|рубля|р\.?)?", flags=re.IGNORECASE
)
_PRICE_SINGLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(\d{1,6})\s*(?:₽|руб\.?|рублей|рубля|р\.?\b)", flags=re.IGNORECASE
)
_PLAIN_INTEGER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s*(\d{1,6})\s*$")
_AGE_PLUS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\D)(\d{1,2})\s*\+|(?:от\s+)?(\d{1,2})\s*(?:лет|года)(?:\s+и\s+старше)?", flags=re.IGNORECASE
)
_TIME_RANGE_PATTERN: Final[re.Pattern[str]] = re.compile(r"(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})")
_TIME_SINGLE_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?:^|[\s,в])(\d{1,2})[:.](\d{2})(?!\d)")
_NUMERIC_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?")
_RUSSIAN_DATE_RANGE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(\d{1,2})\s*[-–—]\s*(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", flags=re.IGNORECASE
)
_RUSSIAN_DATE_SINGLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", flags=re.IGNORECASE
)
_ISO_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
_PARENTHETICAL_BLOCK_PATTERN: Final[re.Pattern[str]] = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_TRAILING_AGE_MARKER_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?:^|\s)(?:0|6|12|16|18)\s*\+\s*$")


def normalize_optional_text(value: str | None) -> str | None:
    """Очищает строковое поле от краевых и повторных пробелов, сводя пустоту к `None`.

    Arguments:
        value: Исходная строка или `None`.

    Returns:
        Очищенная непустая строка либо `None`.
    """
    if value is None:
        return None
    cleaned = _WHITESPACE_PATTERN.sub(" ", value).strip()
    return cleaned or None


def normalize_text_for_matching(value: str) -> str:
    """Приводит произвольный текст к каноническому виду для нечёткого сравнения.

    Переводит в нижний регистр, заменяет «ё» на «е», убирает знаки пунктуации
    и схлопывает повторяющиеся пробелы.

    Arguments:
        value: Исходная строка.

    Returns:
        Очищенная строка в нижнем регистре.
    """
    lowered = value.lower().replace("ё", "е").replace("—", " ").replace("–", " ")
    without_punctuation = _PUNCTUATION_PATTERN.sub(" ", lowered)
    return _WHITESPACE_PATTERN.sub(" ", without_punctuation).strip()


def normalize_title(title: str) -> str:
    """Нормализует название мероприятия для поиска и вычисления ключа дедупликации.

    Удаляет скобочные пояснения (`(...)`, `[...]`) и хвостовые возрастные метки (`6+`, `12+`),
    сохраняя исходный текст как резерв, если после очистки строка оказалась пустой.

    Arguments:
        title: Название мероприятия из источника.

    Returns:
        Каноническое название в нижнем регистре без лишней пунктуации и скобочных вставок.
    """
    without_parentheses = _PARENTHETICAL_BLOCK_PATTERN.sub(" ", title)
    without_age_marker = _TRAILING_AGE_MARKER_PATTERN.sub(" ", without_parentheses)
    normalized = normalize_text_for_matching(without_age_marker)
    return normalized or normalize_text_for_matching(title)


def _strip_city_prefixes(raw_value: str) -> str:
    """Удаляет типовые предлоги и обозначения города перед сопоставлением."""
    cleaned = normalize_text_for_matching(raw_value)
    previous = ""
    while cleaned and cleaned != previous:
        previous = cleaned
        cleaned = _CITY_PREFIX_PATTERN.sub("", cleaned).strip()
    return cleaned


def _build_city_lookup(cities: RealCollection[CityReference]) -> dict[str, str]:
    """Собирает таблицу сопоставления нормализованных алиасов к каноническому названию города."""
    lookup: dict[str, str] = {}
    for canonical_name, aliases in DEFAULT_BASHKORTOSTAN_CITIES.items():
        lookup[normalize_text_for_matching(canonical_name)] = canonical_name
        for alias in aliases:
            lookup[normalize_text_for_matching(alias)] = canonical_name

    for city_reference in cities:
        lookup[normalize_text_for_matching(city_reference.name)] = city_reference.name
        lookup[normalize_text_for_matching(city_reference.normalized_name)] = city_reference.name
        for alias in city_reference.aliases:
            lookup[normalize_text_for_matching(alias)] = city_reference.name

    return lookup


def normalize_city(
    raw_city: str | None, *, cities: RealCollection[CityReference] = (), minimum_similarity: float = 0.78
) -> str | None:
    """Приводит название города или населённого пункта РБ к справочному виду.

    Поддерживает падежные формы (*«в Уфе»*, *«в Стерлитамаке»*) и исправление опечаток
    (*«стерлитакмае»* → *«Стерлитамак»*).

    Arguments:
        raw_city: Исходное название города или фрагмент пользовательского запроса.
        cities: Дополнительный список записей справочника городов из базы.
        minimum_similarity: Порог сходства для нечёткого сопоставления опечаток.

    Returns:
        Каноническое название города или очищенная строка; `None`, если вход пуст.
    """
    cleaned_input = normalize_optional_text(raw_city)
    if cleaned_input is None:
        return None

    candidate = _strip_city_prefixes(cleaned_input)
    if not candidate:
        return None

    lookup = _build_city_lookup(cities)
    if candidate in lookup:
        return lookup[candidate]

    if len(candidate) >= 5:
        best_canonical: str | None = None
        best_ratio = 0.0
        for alias_key, canonical_name in lookup.items():
            if abs(len(alias_key) - len(candidate)) > 3:
                continue
            ratio = SequenceMatcher(None, candidate, alias_key).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_canonical = canonical_name
        if best_canonical is not None and best_ratio >= minimum_similarity:
            return best_canonical

    return cleaned_input


def normalize_venue(
    raw_venue: str | None, *, venues: RealCollection[VenueReference] = (), city: str | None = None
) -> VenueReference | str | None:
    """Сопоставляет название площадки со справочником или очищает исходную строку.

    Arguments:
        raw_venue: Исходное название площадки.
        venues: Записи справочника площадок.
        city: Город для уточнения совпадений при одинаковых названиях площадок.

    Returns:
        Найденный объект `VenueReference`, очищенная строка названия или `None`.
    """
    cleaned_venue = normalize_optional_text(raw_venue)
    if cleaned_venue is None:
        return None

    normalized_target = normalize_text_for_matching(cleaned_venue)
    normalized_city = normalize_text_for_matching(city) if city else None

    for venue_reference in venues:
        if normalized_city and normalize_text_for_matching(venue_reference.city) != normalized_city:
            continue
        variants = (venue_reference.name, venue_reference.normalized_name, *venue_reference.aliases)
        if any(normalize_text_for_matching(variant) == normalized_target for variant in variants):
            return venue_reference

    return cleaned_venue


def parse_category(
    raw_category: str | None, *, fallback_text: str | None = None, default_category: EventCategory | None = None
) -> EventCategory:
    """Определяет категорию мероприятия по названию категории или тексту афиши.

    Arguments:
        raw_category: Исходное наименование категории в источнике.
        fallback_text: Заголовок или описание для уточнения при отсутствии точной категории.
        default_category: Категория источника по умолчанию.

    Returns:
        Значение перечисления `EventCategory`.
    """
    for candidate_text in (raw_category, fallback_text):
        cleaned = normalize_optional_text(candidate_text)
        if cleaned is None:
            continue
        normalized = normalize_text_for_matching(cleaned)
        for category, keywords in _CATEGORY_KEYWORDS.items():
            if normalized == category.value or any(keyword in normalized for keyword in keywords):
                return category

    if default_category is not None:
        return default_category
    return EventCategory.OTHER


def parse_price_range(raw_price_text: str | None) -> tuple[int | None, int | None, bool | None]:
    """Извлекает минимальную и максимальную цену в рублях и признак бесплатного входа.

    Arguments:
        raw_price_text: Текстовое описание стоимости (*«вход свободный»*, *«от 300 до 800 ₽»* и др.).

    Returns:
        Кортеж `(price_minimum, price_maximum, is_free)`. Если стоимость не распознана —
        `(None, None, None)`.
    """
    cleaned = normalize_optional_text(raw_price_text)
    if cleaned is None:
        return None, None, None

    if _FREE_PRICE_PATTERN.search(cleaned):
        return 0, 0, True

    compact_numbers = _THOUSANDS_SPACE_PATTERN.sub("", cleaned)

    range_match = _PRICE_RANGE_PATTERN.search(compact_numbers)
    if range_match is not None:
        first = int(range_match.group(1))
        second = int(range_match.group(2))
        minimum, maximum = (first, second) if first <= second else (second, first)
        return minimum, maximum, minimum == 0

    from_match = _PRICE_FROM_PATTERN.search(compact_numbers)
    if from_match is not None:
        minimum = int(from_match.group(1))
        return minimum, None, minimum == 0

    up_to_match = _PRICE_UP_TO_PATTERN.search(compact_numbers)
    if up_to_match is not None:
        maximum = int(up_to_match.group(1))
        return 0 if maximum == 0 else None, maximum, maximum == 0

    single_matches = [int(match) for match in _PRICE_SINGLE_PATTERN.findall(compact_numbers)]
    if single_matches:
        minimum = min(single_matches)
        maximum = max(single_matches)
        return minimum, maximum, minimum == 0

    plain_match = _PLAIN_INTEGER_PATTERN.match(compact_numbers)
    if plain_match is not None:
        price = int(plain_match.group(1))
        return price, price, price == 0

    return None, None, None


def _snap_to_standard_age_limit(raw_age: int) -> int:
    """Приводит произвольное числовое возрастное ограничение к шкале 0/6/12/16/18."""
    if raw_age <= 0:
        return 0
    if raw_age <= 6:
        return 6
    if raw_age <= 12:
        return 12
    if raw_age <= 16:
        return 16
    return 18


def parse_age_limit(raw_age_text: str | None, *, fallback_text: str | None = None) -> int | None:
    """Извлекает возрастной ценз (`0`, `6`, `12`, `16`, `18`) из строки или заголовка.

    Arguments:
        raw_age_text: Поле возрастного ограничения из источника.
        fallback_text: Заголовок или описание для поиска маркировки вида `6+`.

    Returns:
        Стандартное возрастное ограничение либо `None`, если оно не указано.
    """
    for candidate_text in (raw_age_text, fallback_text):
        cleaned = normalize_optional_text(candidate_text)
        if cleaned is None:
            continue
        lowered = cleaned.lower()
        if "без возрастных ограничений" in lowered:
            return 0
        match = _AGE_PLUS_PATTERN.search(cleaned)
        if match is not None:
            digits = match.group(1) or match.group(2)
            return _snap_to_standard_age_limit(int(digits))
        if candidate_text is raw_age_text and ("для детей" in lowered or "детск" in lowered):
            return 0
        if candidate_text is raw_age_text and "для взрослых" in lowered:
            return 18
    return None


def _resolve_russian_month(token: str) -> int | None:
    """Определяет номер месяца по русской основе слова."""
    normalized = token.lower().replace("ё", "е")
    for prefix, month_number in _RUSSIAN_MONTHS.items():
        if normalized.startswith(prefix):
            return month_number
    return None


def _extract_times(text: str) -> tuple[time | None, time | None]:
    """Извлекает время начала и окончания из строки даты."""
    range_match = _TIME_RANGE_PATTERN.search(text)
    if range_match is not None:
        start_hour, start_minute, end_hour, end_minute = (int(group) for group in range_match.groups())
        if 0 <= start_hour <= 23 and 0 <= start_minute <= 59 and 0 <= end_hour <= 23 and 0 <= end_minute <= 59:
            return time(start_hour, start_minute), time(end_hour, end_minute)

    single_match = _TIME_SINGLE_PATTERN.search(text)
    if single_match is not None:
        hour = int(single_match.group(1))
        minute = int(single_match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute), None

    return None, None


def _combine_with_timezone(target_date: date, target_time: time | None, target_tz: tzinfo) -> datetime:
    """Собирает дату и время с фиксированным часовым поясом."""
    effective_time = target_time or time(0, 0)
    return datetime.combine(target_date, effective_time, tzinfo=target_tz)


def _compute_duration_minutes(date_start: datetime, date_end: datetime | None) -> int | None:
    """Вычисляет положительную длительность события в минутах."""
    if date_end is None or date_end <= date_start:
        return None
    total_minutes = int((date_end - date_start).total_seconds() // 60)
    return total_minutes if 0 < total_minutes <= 720 else None


def _build_datetime_tuple(
    *, start_date: date, end_date: date | None = None, text_without_date: str, target_tz: tzinfo
) -> tuple[datetime, datetime | None, int | None]:
    """Извлекает время из остатка строки и формирует кортеж дат и длительности."""
    start_time, end_time = _extract_times(text_without_date)
    date_start = _combine_with_timezone(start_date, start_time, target_tz)
    if end_date is not None:
        date_end: datetime | None = _combine_with_timezone(end_date, end_time or time(23, 59), target_tz)
    elif end_time is not None:
        date_end = _combine_with_timezone(start_date, end_time, target_tz)
    else:
        date_end = None
    return date_start, date_end, _compute_duration_minutes(date_start, date_end)


def parse_event_datetime(
    raw_date_text: str | None, *, reference_datetime: datetime
) -> tuple[datetime | None, datetime | None, int | None]:
    """Разбирает дату, время начала, окончания и длительность события из текста.

    Поддерживает ISO 8601, числовые форматы `DD.MM.YYYY`, русские названия месяцев,
    диапазоны дат и времени, а также относительные формулировки (*«сегодня»*, *«завтра»*).

    Arguments:
        raw_date_text: Исходная текстовая строка даты и времени.
        reference_datetime: Опорное время с часовым поясом для относительных дат.

    Returns:
        Кортеж `(date_start, date_end, duration_minutes)`.
    """
    cleaned = normalize_optional_text(raw_date_text)
    if cleaned is None:
        return None, None, None

    target_tz: tzinfo = reference_datetime.tzinfo or BASHKORTOSTAN_TIMEZONE

    iso_match = _ISO_DATE_PATTERN.search(cleaned)
    if iso_match is not None:
        iso_candidate = iso_match.group(0).replace("Z", "+00:00")
        try:
            parsed_iso = datetime.fromisoformat(iso_candidate)
            if parsed_iso.tzinfo is None:
                parsed_iso = parsed_iso.replace(tzinfo=target_tz)
            _, end_time = _extract_times(cleaned[iso_match.end() :])
            date_end = _combine_with_timezone(parsed_iso.date(), end_time, target_tz) if end_time else None
            return parsed_iso, date_end, _compute_duration_minutes(parsed_iso, date_end)
        except ValueError:
            pass

    lowered = cleaned.lower()
    for keyword, day_offset in (("послезавтра", 2), ("завтра", 1), ("сегодня", 0)):
        if keyword in lowered:
            resolved_date = (reference_datetime + timedelta(days=day_offset)).date()
            return _build_datetime_tuple(start_date=resolved_date, text_without_date=cleaned, target_tz=target_tz)

    for numeric_match in _NUMERIC_DATE_PATTERN.finditer(cleaned):
        day_number = int(numeric_match.group(1))
        numeric_month = int(numeric_match.group(2))
        year_number = int(numeric_match.group(3)) if numeric_match.group(3) else reference_datetime.year
        remaining_text = f"{cleaned[: numeric_match.start()]} {cleaned[numeric_match.end() :]}"
        try:
            resolved_date = date(year_number, numeric_month, day_number)
        except ValueError:
            continue
        return _build_datetime_tuple(start_date=resolved_date, text_without_date=remaining_text, target_tz=target_tz)

    russian_range_match = _RUSSIAN_DATE_RANGE_PATTERN.search(cleaned)
    if russian_range_match is not None:
        day_start = int(russian_range_match.group(1))
        day_end = int(russian_range_match.group(2))
        range_month = _resolve_russian_month(russian_range_match.group(3))
        year_number = int(russian_range_match.group(4)) if russian_range_match.group(4) else reference_datetime.year
        remaining_text = f"{cleaned[: russian_range_match.start()]} {cleaned[russian_range_match.end() :]}"
        if range_month is not None:
            try:
                start_date = date(year_number, range_month, day_start)
                end_date = date(year_number, range_month, day_end)
            except ValueError:
                return None, None, None
            return _build_datetime_tuple(
                start_date=start_date, end_date=end_date, text_without_date=remaining_text, target_tz=target_tz
            )

    russian_match = _RUSSIAN_DATE_SINGLE_PATTERN.search(cleaned)
    if russian_match is not None:
        day_number = int(russian_match.group(1))
        single_month = _resolve_russian_month(russian_match.group(2))
        year_number = int(russian_match.group(3)) if russian_match.group(3) else reference_datetime.year
        remaining_text = f"{cleaned[: russian_match.start()]} {cleaned[russian_match.end() :]}"
        if single_month is not None:
            try:
                resolved_date = date(year_number, single_month, day_number)
            except ValueError:
                return None, None, None
            return _build_datetime_tuple(
                start_date=resolved_date, text_without_date=remaining_text, target_tz=target_tz
            )

    return None, None, None


def parse_language(raw_language: str | None, *, fallback_text: str | None = None) -> str | None:
    """Определяет язык проведения мероприятия по метаданным или тексту описания.

    Arguments:
        raw_language: Явное поле языка из источника.
        fallback_text: Заголовок и описание для поиска указания языка.

    Returns:
        Каноническое название языка либо `None`, если язык не указан явно.
    """
    for candidate in (raw_language, fallback_text):
        cleaned = normalize_optional_text(candidate)
        if cleaned is None:
            continue
        lowered = cleaned.lower()
        for canonical_language, markers in _LANGUAGE_MARKERS.items():
            if any(marker in lowered for marker in markers):
                return canonical_language
        if candidate is raw_language:
            return cleaned.lower()
    return None


def extract_event_tags(
    *,
    raw_title: str,
    raw_description: str | None,
    category: EventCategory,
    is_free: bool | None,
    language: str | None,
    age_limit: int | None,
) -> tuple[str, ...]:
    """Извлекает неизменяемый кортеж тематических меток для фильтрации и объяснений.

    Arguments:
        raw_title: Название мероприятия.
        raw_description: Описание мероприятия.
        category: Определённая категория.
        is_free: Признак бесплатного посещения.
        language: Определённый язык проведения.
        age_limit: Возрастное ограничение.

    Returns:
        Упорядоченный кортеж уникальных меток.
    """
    combined = normalize_text_for_matching(f"{raw_title} {raw_description or ''}")
    tags: list[str] = [category.value]

    for tag, keywords in _TAG_KEYWORDS.items():
        if any(keyword in combined for keyword in keywords) and tag not in tags:
            tags.append(tag)

    if is_free and "бесплатно" not in tags:
        tags.append("бесплатно")
    if language == "башкирский" and "башкирский язык" not in tags:
        tags.append("башкирский язык")
    if age_limit is not None and age_limit <= 6 and "дети" not in tags:
        tags.append("дети")

    return tuple(tags)


def _calculate_extraction_confidence(
    *,
    has_title: bool,
    date_start: datetime | None,
    city: str | None,
    venue: str | None,
    price_minimum: int | None,
    price_maximum: int | None,
    is_free: bool | None,
    category: EventCategory,
    age_limit: int | None,
) -> float:
    """Оценивает полноту и уверенность детерминированной нормализации (0.0..1.0)."""
    score = 0.0
    if has_title:
        score += 0.2
    if date_start is not None:
        score += 0.25
        if date_start.hour != 0 or date_start.minute != 0:
            score += 0.05
    if city is not None:
        score += 0.15
    if venue is not None:
        score += 0.1
    if is_free is not None or price_minimum is not None or price_maximum is not None:
        score += 0.1
    if category != EventCategory.OTHER:
        score += 0.1
    if age_limit is not None:
        score += 0.05
    return round(min(score, 1.0), 2)


def normalize_source_item(
    item: FetchedSourceItem,
    *,
    reference_datetime: datetime,
    cities: RealCollection[CityReference] = (),
    venues: RealCollection[VenueReference] = (),
    default_city: str | None = None,
    default_category: EventCategory | None = None,
) -> ExtractedEventFields:
    """Выполняет первичную детерминированную нормализацию сырого элемента источника.

    Если ключевые поля (`date_start`, `city`, стоимость) не распознаны или `confidence`
    ниже целевого порога, конвейер сбора может дополнительно вызвать `StructuredExtractorPort`.

    Arguments:
        item: Сырой элемент, загруженный из внешнего или локального источника.
        reference_datetime: Опорные дата и время для разрешения относительных дат.
        cities: Справочник городов.
        venues: Справочник площадок.
        default_city: Город по умолчанию из конфигурации источника.
        default_category: Категория по умолчанию из конфигурации источника.

    Returns:
        Нормализованные поля карточки с оценкой уверенности извлечения.
    """
    cleaned_title = normalize_optional_text(item.raw_title)
    combined_text = " ".join(part for part in (item.raw_title, item.raw_description) if part)

    city = normalize_city(item.raw_city, cities=cities) or normalize_city(default_city, cities=cities)
    venue_match = normalize_venue(item.raw_venue, venues=venues, city=city)
    if isinstance(venue_match, VenueReference):
        venue_name: str | None = venue_match.name
        address: str | None = normalize_optional_text(item.raw_address) or venue_match.address
        city = city or venue_match.city
    else:
        venue_name = venue_match
        address = normalize_optional_text(item.raw_address)

    date_start, date_end, duration_minutes = parse_event_datetime(
        item.raw_date_text, reference_datetime=reference_datetime
    )
    price_minimum, price_maximum, is_free = parse_price_range(item.raw_price_text)
    category = parse_category(item.raw_category, fallback_text=combined_text, default_category=default_category)
    age_limit = parse_age_limit(item.raw_age_text, fallback_text=combined_text)
    language = parse_language(item.raw_language, fallback_text=combined_text)
    tags = extract_event_tags(
        raw_title=cleaned_title or "",
        raw_description=item.raw_description,
        category=category,
        is_free=is_free,
        language=language,
        age_limit=age_limit,
    )
    confidence = _calculate_extraction_confidence(
        has_title=cleaned_title is not None,
        date_start=date_start,
        city=city,
        venue=venue_name,
        price_minimum=price_minimum,
        price_maximum=price_maximum,
        is_free=is_free,
        category=category,
        age_limit=age_limit,
    )

    return ExtractedEventFields(
        title=cleaned_title,
        category=category,
        date_start=date_start,
        date_end=date_end,
        duration_minutes=duration_minutes,
        city=city,
        venue=venue_name,
        address=address,
        price_minimum=price_minimum,
        price_maximum=price_maximum,
        is_free=is_free,
        age_limit=age_limit,
        language=language,
        tags=tags,
        confidence=confidence,
    )
