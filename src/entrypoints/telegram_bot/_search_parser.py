"""Детерминированный разбор текстовых запросов Уровня 1–2 и хранение фильтров сессии."""

import hashlib
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, time, timedelta
from difflib import SequenceMatcher
from types import MappingProxyType
from typing import Final

from src.core.types import EventCategory, RealCollection
from src.domain.entities import CityReference, EventSearchFilter
from src.domain.services import (
    BASHKORTOSTAN_TIMEZONE,
    DEFAULT_BASHKORTOSTAN_CITIES,
    normalize_optional_text,
    normalize_text_for_matching,
    parse_event_datetime,
)
from src.entrypoints.web._formatting import compute_period_range

__all__ = [
    "TelegramSessionFilterStore",
    "has_active_search_constraints",
    "parse_search_query_to_filter",
    "relax_search_filter",
]

_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "а",
        "афиша",
        "без",
        "билет",
        "билеты",
        "ближайший",
        "будет",
        "в",
        "во",
        "все",
        "где",
        "год",
        "года",
        "город",
        "городе",
        "двоих",
        "детей",
        "детям",
        "для",
        "до",
        "ехать",
        "есть",
        "за",
        "и",
        "из",
        "или",
        "интересное",
        "интересного",
        "к",
        "какие",
        "какой",
        "куда",
        "лет",
        "ли",
        "мероприятие",
        "мероприятия",
        "мне",
        "можно",
        "на",
        "найди",
        "найти",
        "не",
        "недорого",
        "но",
        "нужно",
        "о",
        "от",
        "по",
        "подбери",
        "пойти",
        "покажи",
        "показать",
        "посмотреть",
        "посоветуй",
        "похожее",
        "при",
        "про",
        "руб",
        "рублей",
        "рубля",
        "с",
        "со",
        "событие",
        "события",
        "стоимость",
        "сходить",
        "у",
        "цена",
        "что",
        "что-нибудь",
        "чтобы",
        "эти",
        "это",
        "эту",
    }
)

_CATEGORY_STEMS: Final[Mapping[EventCategory, tuple[str, ...]]] = MappingProxyType(
    {
        EventCategory.CONCERT: (
            "концерт",
            "музыкальн",
            "филармони",
            "симфоническ",
            "оркестр",
            "вокал",
            "квартет",
            "акустик",
        ),
        EventCategory.SPECTACLE: (
            "спектакл",
            "театр",
            "постановк",
            "премьер",
            "опер",
            "балет",
            "мюзикл",
            "драм",
            "комеди",
            "кукол",
        ),
        EventCategory.EXHIBITION: ("выставк", "экспозиц", "музей", "галере", "вернисаж", "живопис", "фотовыстав"),
        EventCategory.FESTIVAL: ("фестивал", "сабантуй", "ярмарк", "форум", "фест", "праздник"),
        EventCategory.CINEMA: ("кинопоказ", "киновечер", "кинотеатр", "кино", "фильм", "мультфильм"),
        EventCategory.EXCURSION: ("экскурс", "прогулк", "квест", "пешеходн", "тур"),
    }
)

_WEEKDAY_WORDS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "понедельник": 0,
        "вторник": 1,
        "среда": 2,
        "среду": 2,
        "четверг": 3,
        "пятница": 4,
        "пятницу": 4,
        "суббота": 5,
        "субботу": 5,
        "воскресенье": 6,
    }
)

_FREE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:бесплатн\w*|вход\s+свободн\w*|свободный\s+вход|даром)\b", flags=re.IGNORECASE
)
_PRICE_LIMIT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:до|не\s+дороже|бюджет\s+до|меньше|не\s+больше)\s*(\d[\d\s]{0,7})\s*(?:₽|руб\.?|рублей|рубля|р\.?\b)?",
    flags=re.IGNORECASE,
)
_AGE_EXPLICIT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:с\s+реб[её]нком\s+(\d{1,2})\s*(?:лет|года)?|реб[её]нку\s+(\d{1,2})\s*(?:лет|года)?|"
    r"для\s+детей\s+(\d{1,2})\+?|(\d{1,2})\s*\+|возраст\s*(?:до\s*)?(\d{1,2}))",
    flags=re.IGNORECASE,
)
_CHILDREN_MARKER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:для\s+детей|детск\w*|с\s+реб[её]нком|семейн\w*)\b", flags=re.IGNORECASE
)
_TIME_AFTER_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?:после|с)\s*(\d{1,2})[:.](\d{2})", flags=re.IGNORECASE)
_TIME_BEFORE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:до|не\s+позже)\s*(\d{1,2})[:.](\d{2})", flags=re.IGNORECASE
)
_DURATION_MINUTES_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:не\s+дольше|до|не\s+более)\s*(\d{1,3})\s*(?:мин(?:ут)?\.?)", flags=re.IGNORECASE
)
_DURATION_HOURS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:не\s+дольше|до|не\s+более)\s*(?:(\d{1,2})|(одного|полутора|двух|тр[её]х|четыр[её]х))\s*(?:час\w*)",
    flags=re.IGNORECASE,
)
_LANGUAGE_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("башкирский", re.compile(r"\b(?:на\s+)?башкирск\w*(?:\s+язык\w*)?\b", flags=re.IGNORECASE)),
    ("татарский", re.compile(r"\b(?:на\s+)?татарск\w*(?:\s+язык\w*)?\b", flags=re.IGNORECASE)),
    ("русский", re.compile(r"\b(?:на\s+)?русск\w*(?:\s+язык\w*)?\b", flags=re.IGNORECASE)),
)
_NUMERIC_OR_TEXT_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\b|"
    r"\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
    r"(?:\s+\d{4})?\b)",
    flags=re.IGNORECASE,
)
_PERIOD_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("next_week", re.compile(r"\bна\s+следующей\s+неделе\b", flags=re.IGNORECASE)),
    ("today", re.compile(r"\bсегодня\b", flags=re.IGNORECASE)),
    ("tomorrow", re.compile(r"\bзавтра\b", flags=re.IGNORECASE)),
    ("weekend", re.compile(r"\b(?:в\s+эти\s+выходные|на\s+выходных|выходные)\b", flags=re.IGNORECASE)),
    ("week", re.compile(r"\b(?:на\s+этой\s+неделе|на\s+неделе|ближайшую\s+неделю)\b", flags=re.IGNORECASE)),
    ("month", re.compile(r"\b(?:до\s+конца\s+месяца|в\s+этом\s+месяце|за\s+месяц)\b", flags=re.IGNORECASE)),
)
_EVENING_PATTERN: Final[re.Pattern[str]] = re.compile(r"\bвечером\b", flags=re.IGNORECASE)
_MORNING_PATTERN: Final[re.Pattern[str]] = re.compile(r"\bутром\b", flags=re.IGNORECASE)

_WORD_HOURS_TO_MINUTES: Final[Mapping[str, int]] = MappingProxyType(
    {"одного": 60, "полутора": 90, "двух": 120, "трех": 180, "трёх": 180, "четырех": 240, "четырёх": 240}
)


class TelegramSessionFilterStore:
    """Ограниченный по памяти кэш активных фильтров поиска для пагинации и ослабления без БД."""

    def __init__(self, *, maximum_sessions: int = 2048, maximum_tokens: int = 4096) -> None:
        """Инициализирует хранилище фильтров сессий.

        Arguments:
            maximum_sessions: Максимальное количество хранимых активных сессий.
            maximum_tokens: Максимальное количество хранимых снимков фильтров по токену.
        """
        self._maximum_sessions = maximum_sessions
        self._maximum_tokens = maximum_tokens
        self._session_filters: OrderedDict[str, EventSearchFilter] = OrderedDict()
        self._token_entries: OrderedDict[str, tuple[str, EventSearchFilter, int]] = OrderedDict()

    def get_session_filter(self, session_identifier: str, *, default_page_size: int) -> EventSearchFilter:
        """Возвращает текущий фильтр пользователя или новый фильтр по умолчанию."""
        existing = self._session_filters.get(session_identifier)
        if existing is not None:
            self._session_filters.move_to_end(session_identifier)
            return existing
        return EventSearchFilter(page_size=default_page_size)

    def save_session_filter(
        self, session_identifier: str, search_filter: EventSearchFilter, *, current_page: int | None = None
    ) -> str:
        """Сохраняет активный фильтр сессии и возвращает компактный 8-символьный токен.

        Arguments:
            session_identifier: Идентификатор сессии пользователя.
            search_filter: Активный фильтр поиска.
            current_page: Номер текущей страницы (для возврата из карточки).

        Returns:
            Короткий 8-символьный ASCII-токен для `callback_data`.
        """
        self._session_filters[session_identifier] = search_filter
        self._session_filters.move_to_end(session_identifier)
        while len(self._session_filters) > self._maximum_sessions:
            self._session_filters.popitem(last=False)

        token_payload = f"{session_identifier}:{search_filter!r}"
        token = hashlib.blake2b(token_payload.encode("utf-8"), digest_size=4).hexdigest()
        effective_page = current_page if current_page is not None else search_filter.page
        self._token_entries[token] = (session_identifier, search_filter, effective_page)
        self._token_entries.move_to_end(token)
        while len(self._token_entries) > self._maximum_tokens:
            self._token_entries.popitem(last=False)
        return token

    def resolve_token(self, session_identifier: str, filter_token: str) -> tuple[EventSearchFilter, int] | None:
        """Извлекает фильтр и номер страницы по токену только для владельца сессии.

        Arguments:
            session_identifier: Идентификатор текущей сессии пользователя.
            filter_token: Токен фильтра из callback-кнопки.

        Returns:
            Кортеж `(search_filter, page_number)` либо `None`, если кнопка устарела.
        """
        entry = self._token_entries.get(filter_token)
        if entry is None or entry[0] != session_identifier:
            return None
        self._token_entries.move_to_end(filter_token)
        return entry[1], entry[2]

    def reset_session(self, session_identifier: str) -> None:
        """Удаляет активный фильтр сессии при `/cancel` или явном сбросе."""
        self._session_filters.pop(session_identifier, None)


def has_active_search_constraints(search_filter: EventSearchFilter) -> bool:
    """Проверяет, задано ли в фильтре хотя бы одно условие поиска."""
    return any(
        [
            search_filter.query is not None,
            search_filter.city is not None,
            search_filter.category is not None,
            search_filter.date_from is not None,
            search_filter.date_to is not None,
            search_filter.time_from is not None,
            search_filter.time_to is not None,
            search_filter.maximum_price is not None,
            search_filter.only_free,
            search_filter.maximum_age_limit is not None,
            search_filter.maximum_duration_minutes is not None,
            search_filter.language is not None,
            search_filter.venue is not None,
        ]
    )


def relax_search_filter(search_filter: EventSearchFilter, relaxation_kind: str) -> EventSearchFilter:
    """Снимает выбранное пользователем ограничение и сбрасывает страницу на первую.

    Arguments:
        search_filter: Исходный фильтр поиска.
        relaxation_kind: Код снимаемого ограничения.

    Returns:
        Новый фильтр с ослабленным ограничением.
    """
    base_filter = replace(search_filter, page=1)
    if relaxation_kind == "free":
        return replace(base_filter, only_free=False)
    if relaxation_kind == "price":
        return replace(base_filter, maximum_price=None)
    if relaxation_kind == "age":
        return replace(base_filter, maximum_age_limit=None)
    if relaxation_kind == "duration":
        return replace(base_filter, maximum_duration_minutes=None)
    if relaxation_kind == "time":
        return replace(base_filter, time_from=None, time_to=None)
    if relaxation_kind == "venue":
        return replace(base_filter, venue=None)
    if relaxation_kind == "language":
        return replace(base_filter, language=None)
    if relaxation_kind == "category":
        return replace(base_filter, category=None)
    if relaxation_kind == "date":
        return replace(base_filter, date_from=None, date_to=None)
    if relaxation_kind == "query":
        return replace(base_filter, query=None)
    if relaxation_kind == "city":
        return replace(base_filter, city=None)
    return EventSearchFilter(page=1, page_size=search_filter.page_size)


def parse_search_query_to_filter(
    raw_text: str,
    *,
    cities: RealCollection[CityReference] = (),
    page_size: int = 5,
    reference_now: datetime | None = None,
) -> EventSearchFilter:
    """Детерминированно преобразует текстовый запрос пользователя в `EventSearchFilter`.

    Arguments:
        raw_text: Очищенный пользовательский запрос на русском языке.
        cities: Справочник городов каталога для сопоставления и исправления опечаток.
        page_size: Размер страницы выдачи.
        reference_now: Опорное время для относительных дат (по умолчанию текущее время РБ).

    Returns:
        Структурированный доменный фильтр `EventSearchFilter`.
    """
    now = reference_now or datetime.now(tz=BASHKORTOSTAN_TIMEZONE)
    now = now.replace(tzinfo=BASHKORTOSTAN_TIMEZONE) if now.tzinfo is None else now.astimezone(BASHKORTOSTAN_TIMEZONE)

    working_text = normalize_optional_text(raw_text) or ""

    only_free = False
    if _FREE_PATTERN.search(working_text):
        only_free = True
        working_text = _FREE_PATTERN.sub(" ", working_text)

    maximum_price: int | None = None
    price_match = _PRICE_LIMIT_PATTERN.search(working_text)
    if price_match is not None:
        digits_only = re.sub(r"\s+", "", price_match.group(1))
        if digits_only:
            maximum_price = int(digits_only)
            if maximum_price == 0:
                only_free = True
        working_text = f"{working_text[: price_match.start()]} {working_text[price_match.end() :]}"

    maximum_age_limit: int | None = None
    age_match = _AGE_EXPLICIT_PATTERN.search(working_text)
    if age_match is not None:
        age_digits = next((group for group in age_match.groups() if group is not None), None)
        if age_digits is not None:
            maximum_age_limit = int(age_digits)
        working_text = f"{working_text[: age_match.start()]} {working_text[age_match.end() :]}"
    elif _CHILDREN_MARKER_PATTERN.search(working_text):
        maximum_age_limit = 12
        working_text = _CHILDREN_MARKER_PATTERN.sub(" ", working_text)

    maximum_duration_minutes: int | None = None
    duration_minutes_match = _DURATION_MINUTES_PATTERN.search(working_text)
    if duration_minutes_match is not None:
        maximum_duration_minutes = int(duration_minutes_match.group(1))
        working_text = (
            f"{working_text[: duration_minutes_match.start()]} {working_text[duration_minutes_match.end() :]}"
        )
    else:
        duration_hours_match = _DURATION_HOURS_PATTERN.search(working_text)
        if duration_hours_match is not None:
            if duration_hours_match.group(1):
                maximum_duration_minutes = int(duration_hours_match.group(1)) * 60
            elif duration_hours_match.group(2):
                maximum_duration_minutes = _WORD_HOURS_TO_MINUTES.get(duration_hours_match.group(2).lower())
            working_text = (
                f"{working_text[: duration_hours_match.start()]} {working_text[duration_hours_match.end() :]}"
            )

    time_from: time | None = None
    time_to: time | None = None
    time_after_match = _TIME_AFTER_PATTERN.search(working_text)
    if time_after_match is not None:
        hour, minute = int(time_after_match.group(1)), int(time_after_match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            time_from = time(hour, minute)
            working_text = f"{working_text[: time_after_match.start()]} {working_text[time_after_match.end() :]}"

    time_before_match = _TIME_BEFORE_PATTERN.search(working_text)
    if time_before_match is not None:
        hour, minute = int(time_before_match.group(1)), int(time_before_match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            time_to = time(hour, minute)
            working_text = f"{working_text[: time_before_match.start()]} {working_text[time_before_match.end() :]}"

    if time_from is None and _EVENING_PATTERN.search(working_text):
        time_from = time(18, 0)
        working_text = _EVENING_PATTERN.sub(" ", working_text)
    elif time_from is None and time_to is None and _MORNING_PATTERN.search(working_text):
        time_from = time(7, 0)
        time_to = time(12, 0)
        working_text = _MORNING_PATTERN.sub(" ", working_text)

    language: str | None = None
    for canonical_language, language_pattern in _LANGUAGE_PATTERNS:
        if language_pattern.search(working_text):
            language = canonical_language
            working_text = language_pattern.sub(" ", working_text)
            break

    date_from, date_to, working_text = _extract_date_range(working_text, reference_now=now)
    city, working_text = _extract_city(working_text, cities=cities)
    category, working_text = _extract_category(working_text)

    remaining_tokens = [
        token
        for token in normalize_text_for_matching(working_text).split()
        if token and token not in _STOPWORDS and len(token) >= 2
    ]
    residual_query = " ".join(remaining_tokens) if remaining_tokens else None

    return EventSearchFilter(
        query=residual_query,
        city=city,
        category=category,
        date_from=date_from,
        date_to=date_to,
        time_from=time_from,
        time_to=time_to,
        maximum_price=maximum_price,
        only_free=only_free,
        maximum_age_limit=maximum_age_limit,
        maximum_duration_minutes=maximum_duration_minutes,
        language=language,
        page=1,
        page_size=page_size,
    )


def _extract_date_range(text: str, *, reference_now: datetime) -> tuple[datetime | None, datetime | None, str]:
    explicit_date_match = _NUMERIC_OR_TEXT_DATE_PATTERN.search(text)
    if explicit_date_match is not None:
        parsed_start, parsed_end, _ = parse_event_datetime(
            explicit_date_match.group(0), reference_datetime=reference_now
        )
        if parsed_start is not None:
            day_start = parsed_start.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = (parsed_end or day_start).replace(hour=23, minute=59, second=59, microsecond=0)
            cleaned = f"{text[: explicit_date_match.start()]} {text[explicit_date_match.end() :]}"
            return day_start, day_end, cleaned

    for period_code, period_pattern in _PERIOD_PATTERNS:
        period_match = period_pattern.search(text)
        if period_match is None:
            continue
        cleaned = f"{text[: period_match.start()]} {text[period_match.end() :]}"
        if period_code == "next_week":
            today_start = reference_now.replace(hour=0, minute=0, second=0, microsecond=0)
            days_until_next_monday = 7 - today_start.weekday()
            monday = today_start + timedelta(days=days_until_next_monday)
            sunday = (monday + timedelta(days=6)).replace(hour=23, minute=59, second=59)
            return monday, sunday, cleaned
        period_start, period_end = compute_period_range(period_code, reference_now=reference_now)
        return period_start, period_end, cleaned

    lowered = text.lower()
    for weekday_word, target_weekday in _WEEKDAY_WORDS.items():
        weekday_pattern = re.compile(rf"\b(?:в\s+|во\s+|на\s+)?{re.escape(weekday_word)}\b", flags=re.IGNORECASE)
        weekday_match = weekday_pattern.search(lowered)
        if weekday_match is None:
            continue
        today_start = reference_now.replace(hour=0, minute=0, second=0, microsecond=0)
        days_ahead = (target_weekday - today_start.weekday()) % 7
        target_day = today_start + timedelta(days=days_ahead)
        cleaned = f"{text[: weekday_match.start()]} {text[weekday_match.end() :]}"
        return target_day, target_day.replace(hour=23, minute=59, second=59), cleaned

    return None, None, text


def _build_known_city_lookup(cities: RealCollection[CityReference]) -> dict[str, str]:
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


def _extract_city(text: str, *, cities: RealCollection[CityReference]) -> tuple[str | None, str]:
    lookup = _build_known_city_lookup(cities)
    tokens = text.split()
    for index, raw_token in enumerate(tokens):
        cleaned_token = normalize_text_for_matching(raw_token)
        if not cleaned_token or cleaned_token in _STOPWORDS:
            continue
        if cleaned_token in lookup:
            remaining = [*tokens[:index], *tokens[index + 1 :]]
            return lookup[cleaned_token], " ".join(remaining)
        if len(cleaned_token) >= 5:
            best_city: str | None = None
            best_ratio = 0.0
            for alias_key, canonical_name in lookup.items():
                if abs(len(alias_key) - len(cleaned_token)) > 3:
                    continue
                ratio = SequenceMatcher(None, cleaned_token, alias_key).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_city = canonical_name
            if best_city is not None and best_ratio >= 0.8:
                remaining = [*tokens[:index], *tokens[index + 1 :]]
                return best_city, " ".join(remaining)
    return None, text


def _extract_category(text: str) -> tuple[EventCategory | None, str]:
    tokens = text.split()
    for index, raw_token in enumerate(tokens):
        normalized_token = normalize_text_for_matching(raw_token)
        if not normalized_token:
            continue
        for category, stems in _CATEGORY_STEMS.items():
            if any(stem in normalized_token for stem in stems):
                remaining = [*tokens[:index], *tokens[index + 1 :]]
                return category, " ".join(remaining)
    return None, text
