"""Общие функции форматирования, разбора фильтров, сессий и кэш-тегов веб-слоя."""

import base64
import calendar
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as datetime_time
from typing import Final
from urllib.parse import urlencode
from uuid import uuid4

from src.core.types import EventCategory, EventStatus, SourceType
from src.domain.entities import Event, EventSearchFilter, EventSortField, SortDirection
from src.domain.exceptions import InvalidSearchFilterError
from src.domain.services import BASHKORTOSTAN_TIMEZONE, normalize_optional_text

__all__ = [
    "ADMIN_SESSION_COOKIE_NAME",
    "ALLOWED_AGE_OPTIONS",
    "CATEGORY_LABELS",
    "PERIOD_LABELS",
    "SERVICE_DISCLAIMER_TEXT",
    "SOURCE_TYPE_LABELS",
    "VISITOR_COOKIE_NAME",
    "CatalogQueryParameters",
    "build_catalog_url_query",
    "build_entity_etag",
    "build_search_filter_from_parameters",
    "compute_period_range",
    "format_date_range_label",
    "format_price_label",
    "format_status_label",
    "generate_visitor_identifier",
    "parse_date_boundary",
    "parse_time_value",
    "sign_admin_session",
    "verify_admin_session",
]

SERVICE_DISCLAIMER_TEXT: Final[str] = (
    "Независимый информационный сервис афиши Республики Башкортостан. "
    "Подборки и ответы формируются исключительно по подтверждённым карточкам базы сервиса и не заменяют "
    "официальный портал Министерства культуры Республики Башкортостан."
)

VISITOR_COOKIE_NAME: Final[str] = "cultural_meridian_visitor"
ADMIN_SESSION_COOKIE_NAME: Final[str] = "cultural_meridian_moderator"

CATEGORY_LABELS: Final[dict[EventCategory, str]] = {
    EventCategory.CONCERT: "Концерты",
    EventCategory.SPECTACLE: "Спектакли",
    EventCategory.EXHIBITION: "Выставки",
    EventCategory.FESTIVAL: "Фестивали",
    EventCategory.CINEMA: "Кинопоказы",
    EventCategory.EXCURSION: "Экскурсии",
    EventCategory.OTHER: "Другое",
}

PERIOD_LABELS: Final[dict[str, str]] = {
    "today": "Сегодня",
    "tomorrow": "Завтра",
    "weekend": "Выходные",
    "week": "На неделе",
    "month": "До конца месяца",
}

SOURCE_TYPE_LABELS: Final[dict[SourceType, str]] = {
    SourceType.MANUAL: "Ручной ввод",
    SourceType.IMPORT_FILE: "Файловый импорт",
    SourceType.RSS: "RSS-лента",
    SourceType.API: "Внешний API",
    SourceType.WEBPAGE: "Веб-страница",
}

ALLOWED_AGE_OPTIONS: Final[tuple[int, ...]] = (0, 6, 12, 16, 18)

_RUSSIAN_MONTHS_GENITIVE: Final[tuple[str, ...]] = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

_RUSSIAN_WEEKDAYS_SHORT: Final[tuple[str, ...]] = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogQueryParameters:
    """Сырые параметры фильтров каталога, синхронизируемые с адресной строкой URL.

    Attributes:
        query: Свободный поисковый текст.
        city: Выбранный город.
        category: Строковый код категории.
        period: Предустановленный период (`today`, `tomorrow`, `weekend`, `week`, `month`).
        date_from: Явная начальная дата (`YYYY-MM-DD` или ISO-8601).
        date_to: Явная конечная дата (`YYYY-MM-DD` или ISO-8601).
        maximum_price: Максимальная цена билета в рублях.
        only_free: Показывать только бесплатные события.
        maximum_age_limit: Максимальный возрастной ценз ребёнка/зрителя.
        language: Язык проведения мероприятия.
        venue: Название площадки.
        near_center: Приоритизировать площадки в центре города.
        sort_field: Поле сортировки выдачи.
        sort_direction: Направление сортировки.
        page: Номер страницы выдачи.
        page_size: Размер страницы выдачи.
    """

    query: str | None = None
    city: str | None = None
    category: str | None = None
    period: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    maximum_price: int | None = None
    only_free: bool = False
    maximum_age_limit: int | None = None
    language: str | None = None
    venue: str | None = None
    near_center: bool = False
    sort_field: str = EventSortField.DATE_START.value
    sort_direction: str = SortDirection.ASCENDING.value
    page: int = 1
    page_size: int = 9


def compute_period_range(
    period: str | None, *, reference_now: datetime | None = None
) -> tuple[datetime | None, datetime | None]:
    """Вычисляет границы периода (`today`, `tomorrow`, `weekend`, `week`, `month`) в часовом поясе РБ.

    Arguments:
        period: Код предустановленного периода.
        reference_now: Опорная метка текущего времени (по умолчанию системное время Уфы).

    Returns:
        Кортеж `(date_from, date_to)` или `(None, None)`, если период не задан.
    """
    cleaned_period = normalize_optional_text(period)
    if cleaned_period is None:
        return None, None

    now = reference_now or datetime.now(tz=BASHKORTOSTAN_TIMEZONE)
    now = now.replace(tzinfo=BASHKORTOSTAN_TIMEZONE) if now.tzinfo is None else now.astimezone(BASHKORTOSTAN_TIMEZONE)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if cleaned_period == "today":
        return today_start, today_start.replace(hour=23, minute=59, second=59)

    if cleaned_period == "tomorrow":
        tomorrow_start = today_start + timedelta(days=1)
        return tomorrow_start, tomorrow_start.replace(hour=23, minute=59, second=59)

    if cleaned_period == "weekend":
        weekday = today_start.weekday()
        if weekday == 6:
            weekend_start = today_start
            weekend_end = today_start
        elif weekday == 5:
            weekend_start = today_start
            weekend_end = today_start + timedelta(days=1)
        else:
            days_until_saturday = 5 - weekday
            weekend_start = today_start + timedelta(days=days_until_saturday)
            weekend_end = weekend_start + timedelta(days=1)
        return weekend_start, weekend_end.replace(hour=23, minute=59, second=59)

    if cleaned_period == "week":
        return today_start, (today_start + timedelta(days=6)).replace(hour=23, minute=59, second=59)

    if cleaned_period == "month":
        _, last_day = calendar.monthrange(today_start.year, today_start.month)
        month_end = today_start.replace(day=last_day, hour=23, minute=59, second=59)
        return today_start, month_end

    return None, None


def parse_date_boundary(raw_value: str | None, *, end_of_day: bool = False) -> datetime | None:
    """Разбирает строку даты (`YYYY-MM-DD`) или ISO-метку с часовым поясом РБ.

    Arguments:
        raw_value: Входная строка даты.
        end_of_day: Если указана только календарная дата, установить `23:59:59` вместо `00:00:00`.

    Returns:
        Объект `datetime` с часовым поясом либо `None`, если вход пуст.

    Raises:
        ValueError: Если формат даты невалиден.
    """
    cleaned = normalize_optional_text(raw_value)
    if cleaned is None:
        return None

    if len(cleaned) == 10 and cleaned[4] == "-" and cleaned[7] == "-":
        parsed_date = datetime.fromisoformat(cleaned)
        target_time = datetime_time(23, 59, 59) if end_of_day else datetime_time(0, 0, 0)
        return datetime.combine(parsed_date.date(), target_time, tzinfo=BASHKORTOSTAN_TIMEZONE)

    parsed_datetime = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    if parsed_datetime.tzinfo is None:
        return parsed_datetime.replace(tzinfo=BASHKORTOSTAN_TIMEZONE)
    return parsed_datetime


def parse_time_value(raw_value: str | None) -> datetime_time | None:
    """Разбирает строку времени формата `HH:MM` или `HH:MM:SS`."""
    cleaned = normalize_optional_text(raw_value)
    if cleaned is None:
        return None
    return datetime_time.fromisoformat(cleaned)


def build_catalog_url_query(
    parameters: CatalogQueryParameters, *, overrides: Mapping[str, str | int | bool | None] | None = None
) -> str:
    """Собирает каноническую строку query-параметров для адресной строки и ссылок.

    Arguments:
        parameters: Текущие параметры фильтра каталога.
        overrides: Словарь точечных переопределений параметров (передайте `None`, чтобы сбросить ключ).

    Returns:
        Строка запроса URL вида `?city=...&only_free=true` или пустая строка.
    """
    raw_values: dict[str, str | int | bool | None] = {
        "query": parameters.query,
        "city": parameters.city,
        "category": parameters.category,
        "period": parameters.period,
        "date_from": parameters.date_from,
        "date_to": parameters.date_to,
        "maximum_price": parameters.maximum_price,
        "only_free": parameters.only_free if parameters.only_free else None,
        "maximum_age_limit": parameters.maximum_age_limit,
        "language": parameters.language,
        "venue": parameters.venue,
        "near_center": parameters.near_center if parameters.near_center else None,
        "sort_field": (parameters.sort_field if parameters.sort_field != EventSortField.DATE_START.value else None),
        "sort_direction": (
            parameters.sort_direction if parameters.sort_direction != SortDirection.ASCENDING.value else None
        ),
        "page": parameters.page if parameters.page > 1 else None,
        "page_size": parameters.page_size if parameters.page_size != 9 else None,
    }
    if overrides:
        raw_values.update(overrides)

    encoded_pairs: list[tuple[str, str]] = []
    for key, value in raw_values.items():
        if value is None or value == "" or value is False:
            continue
        if isinstance(value, bool):
            encoded_pairs.append((key, "true"))
        else:
            encoded_pairs.append((key, str(value)))
    if not encoded_pairs:
        return ""
    return f"?{urlencode(encoded_pairs)}"


def build_search_filter_from_parameters(parameters: CatalogQueryParameters) -> EventSearchFilter:
    """Преобразует параметры веб-запроса каталога в доменный фильтр поиска.

    Arguments:
        parameters: Структурированные параметры строки запроса.

    Returns:
        Доменная структура `EventSearchFilter`.

    Raises:
        InvalidSearchFilterError: При некорректном коде категории, дате или способе сортировки.
    """
    resolved_category: EventCategory | None = None
    cleaned_category = normalize_optional_text(parameters.category)
    if cleaned_category is not None:
        try:
            resolved_category = EventCategory(cleaned_category.lower())
        except ValueError as error:
            raise InvalidSearchFilterError(
                f"Неизвестная категория мероприятий «{cleaned_category}».",
                parameter_name="category",
                reason="Неподдерживаемое значение категории",
            ) from error

    period_start, period_end = compute_period_range(parameters.period)

    try:
        explicit_start = parse_date_boundary(parameters.date_from, end_of_day=False)
    except ValueError as error:
        raise InvalidSearchFilterError(
            "Некорректный формат начальной даты (ожидается ГГГГ-ММ-ДД).",
            parameter_name="date_from",
            reason="Ошибка разбора даты",
        ) from error

    try:
        explicit_end = parse_date_boundary(parameters.date_to, end_of_day=True)
    except ValueError as error:
        raise InvalidSearchFilterError(
            "Некорректный формат конечной даты (ожидается ГГГГ-ММ-ДД).",
            parameter_name="date_to",
            reason="Ошибка разбора даты",
        ) from error

    try:
        sort_field = EventSortField(parameters.sort_field)
    except ValueError as error:
        raise InvalidSearchFilterError(
            f"Неподдерживаемое поле сортировки «{parameters.sort_field}».",
            parameter_name="sort_field",
            reason="Некорректное поле сортировки",
        ) from error

    try:
        sort_direction = SortDirection(parameters.sort_direction)
    except ValueError as error:
        raise InvalidSearchFilterError(
            f"Неподдерживаемое направление сортировки «{parameters.sort_direction}».",
            parameter_name="sort_direction",
            reason="Некорректное направление сортировки",
        ) from error

    return EventSearchFilter(
        query=normalize_optional_text(parameters.query),
        city=normalize_optional_text(parameters.city),
        category=resolved_category,
        date_from=explicit_start or period_start,
        date_to=explicit_end or period_end,
        maximum_price=parameters.maximum_price,
        only_free=parameters.only_free,
        maximum_age_limit=parameters.maximum_age_limit,
        language=normalize_optional_text(parameters.language),
        venue=normalize_optional_text(parameters.venue),
        near_center=parameters.near_center,
        sort_field=sort_field,
        sort_direction=sort_direction,
        page=parameters.page,
        page_size=parameters.page_size,
    )


def format_price_label(event: Event) -> str:
    """Форматирует стоимость билета карточки для отображения пользователю."""
    if event.is_free or (event.price_minimum == 0 and (event.price_maximum is None or event.price_maximum == 0)):
        return "Бесплатно"
    if event.price_minimum is not None and event.price_maximum is not None:
        if event.price_minimum == event.price_maximum:
            return f"{event.price_minimum:,} ₽".replace(",", " ")
        return f"от {event.price_minimum:,} до {event.price_maximum:,} ₽".replace(",", " ")
    if event.price_minimum is not None:
        return f"от {event.price_minimum:,} ₽".replace(",", " ")
    if event.price_maximum is not None:
        return f"до {event.price_maximum:,} ₽".replace(",", " ")
    return "Цена уточняется у организатора"


def format_date_range_label(event: Event) -> str:
    """Форматирует дату и время мероприятия на русском языке в часовом поясе Башкортостана."""
    local_start = event.date_start.astimezone(BASHKORTOSTAN_TIMEZONE)
    weekday_label = _RUSSIAN_WEEKDAYS_SHORT[local_start.weekday()]
    month_label = _RUSSIAN_MONTHS_GENITIVE[local_start.month]
    date_part = f"{weekday_label}, {local_start.day} {month_label} {local_start.year}"
    time_part = f"{local_start:%H:%M}"
    if event.date_end is not None:
        local_end = event.date_end.astimezone(BASHKORTOSTAN_TIMEZONE)
        if local_end.date() == local_start.date():
            return f"{date_part} · {time_part}–{local_end:%H:%M}"
        end_month = _RUSSIAN_MONTHS_GENITIVE[local_end.month]
        return f"{date_part} {time_part} — {local_end.day} {end_month} {local_end:%H:%M}"
    if event.duration_minutes is not None:
        return f"{date_part} · {time_part} ({event.duration_minutes} мин.)"
    return f"{date_part} · {time_part}"


def format_status_label(status: EventStatus) -> str:
    """Возвращает человекочитаемый статус публикации карточки."""
    if status is EventStatus.PUBLISHED:
        return "Опубликовано"
    if status is EventStatus.DRAFT:
        return "На проверке (черновик)"
    return "Отменено"


def build_entity_etag(prefix: str, payload: object) -> str:
    """Вычисляет детерминированный HTTP-заголовок ETag для ресурса или справочника."""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{prefix}:{serialized}".encode()).hexdigest()[:24]
    return f'"{prefix}-{digest}"'


def generate_visitor_identifier() -> str:
    """Создаёт случайный технический идентификатор браузерного посетителя без ПДн."""
    return f"web-{uuid4()}"


def sign_admin_session(*, username: str, secret: str, ttl_seconds: int) -> str:
    """Формирует подписанный HMAC-токен сессии модератора с ограничением срока действия."""
    expires_at = int(time.time()) + ttl_seconds
    payload_bytes = json.dumps({"username": username, "expires_at": expires_at}, separators=(",", ":")).encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(payload_bytes).decode("ascii")
    signature = hmac.new(secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded_payload}.{signature}"


def verify_admin_session(*, token: str | None, secret: str) -> str | None:
    """Проверяет подпись и срок действия токена сессии модератора.

    Arguments:
        token: Значение cookie сессии модератора.
        secret: Секретный ключ из `WebSettings`.

    Returns:
        Логин модератора при валидной сессии или `None`.
    """
    if not token or "." not in token:
        return None
    encoded_payload, provided_signature = token.rsplit(".", maxsplit=1)
    expected_signature = hmac.new(secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided_signature, expected_signature):
        return None

    try:
        decoded_bytes = base64.urlsafe_b64decode(encoded_payload.encode("ascii"))
        payload: object = json.loads(decoded_bytes.decode("utf-8"))
    except ValueError, UnicodeDecodeError:
        return None

    if not isinstance(payload, dict):
        return None
    username = payload.get("username")
    expires_at = payload.get("expires_at")
    if not isinstance(username, str) or not isinstance(expires_at, int):
        return None
    if int(time.time()) > expires_at:
        return None
    return username
