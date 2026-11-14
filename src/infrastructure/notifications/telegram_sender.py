"""Транспортный адаптер доставки уведомлений по подпискам через Telegram Bot API."""

from datetime import datetime
from html import escape
from typing import Final

import httpx

from src.core.exceptions import ValidationException
from src.core.types import EventCategory
from src.domain.entities import Event, SubscriptionNotificationRecord
from src.domain.ports import NotificationDeliveryOutcome
from src.domain.services import BASHKORTOSTAN_TIMEZONE, normalize_optional_text

__all__ = ["TelegramSubscriptionNotificationSender"]

_MAXIMUM_TELEGRAM_MESSAGE_LENGTH: Final[int] = 4096
_MAXIMUM_NOTIFICATION_DESCRIPTION_LENGTH: Final[int] = 500
_PERMANENT_TELEGRAM_HTTP_STATUSES: Final[frozenset[int]] = frozenset({400, 403, 404})

_CATEGORY_LABELS: Final[dict[EventCategory, str]] = {
    EventCategory.CONCERT: "Концерты",
    EventCategory.SPECTACLE: "Спектакли",
    EventCategory.EXHIBITION: "Выставки",
    EventCategory.FESTIVAL: "Фестивали",
    EventCategory.CINEMA: "Кинопоказы",
    EventCategory.EXCURSION: "Экскурсии",
    EventCategory.OTHER: "Другое",
}

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

_SERVICE_DISCLAIMER_TEXT: Final[str] = (
    "Независимый информационный сервис афиши Республики Башкортостан. "
    "Подборки и ответы формируются исключительно по подтверждённым карточкам базы сервиса и не заменяют "
    "официальный портал Министерства культуры Республики Башкортостан."
)


class TelegramSubscriptionNotificationSender:
    """Адаптер отправки уведомлений подписчикам в Telegram напрямую через `httpx.AsyncClient`.

    Не зависит от `aiogram`, что соответствует матрице зависимостей инфраструктурного слоя.
    Формат HTML-сообщений и `callback_data` полностью совместимы с обработчиками Telegram-бота.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        public_web_base_url: str = "",
        request_timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Инициализирует HTTP-клиент Telegram Bot API для доставки уведомлений.

        Arguments:
            bot_token: Секретный токен Telegram-бота.
            public_web_base_url: Публичный базовый URL веб-каталога для кнопки перехода на сайт.
            request_timeout_seconds: Таймаут одного HTTP-запроса к Telegram Bot API в секундах.
            transport: Опциональный транспорт `httpx` для изолированного тестирования.

        Raises:
            ValidationException: Если `request_timeout_seconds` неположительный.
        """
        if request_timeout_seconds <= 0.0:
            raise ValidationException("Таймаут запроса к Telegram Bot API должен быть положительным числом.")

        cleaned_token = bot_token.strip()
        self._bot_token = cleaned_token
        self._public_web_base_url = _sanitize_public_url(public_web_base_url) or ""
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{cleaned_token}",
            timeout=request_timeout_seconds,
            transport=transport,
        )

    async def close(self) -> None:
        """Закрывает внутренний пул HTTP-соединений `httpx.AsyncClient`."""
        await self._client.aclose()

    async def send_event_notification(
        self, *, notification: SubscriptionNotificationRecord
    ) -> NotificationDeliveryOutcome:
        """Отправляет подписчику в Telegram карточку нового опубликованного мероприятия.

        Arguments:
            notification: Запись уведомления по подписке с карточкой мероприятия.

        Returns:
            Итог попытки доставки (`NotificationDeliveryOutcome`) без выброса сетевых исключений.
        """
        if not self._bot_token:
            return NotificationDeliveryOutcome(
                delivered=False,
                is_permanent_failure=False,
                error_message="Токен Telegram-бота не задан для отправки уведомлений.",
            )

        chat_id = notification.subscriber_identifier.strip()
        if not chat_id:
            return NotificationDeliveryOutcome(
                delivered=False, is_permanent_failure=True, error_message="Идентификатор Telegram-чата подписчика пуст."
            )

        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": _format_notification_html(notification.event),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": _build_inline_keyboard(notification.event, self._public_web_base_url)},
        }

        try:
            response = await self._client.post("/sendMessage", json=payload)
        except httpx.HTTPError as error:
            return NotificationDeliveryOutcome(
                delivered=False,
                is_permanent_failure=False,
                error_message=f"Сетевая ошибка при обращении к Telegram Bot API: {error}",
            )

        return _parse_telegram_response(response)


def _parse_telegram_response(response: httpx.Response) -> NotificationDeliveryOutcome:
    """Преобразует HTTP-ответ Telegram Bot API в доменный результат доставки."""
    parsed_json: object | None
    try:
        parsed_json = response.json()
    except ValueError:
        parsed_json = None

    description: str | None = None
    retry_after: int | None = None
    ok_flag: object = None
    error_code: int | None = None

    if isinstance(parsed_json, dict):
        ok_flag = parsed_json.get("ok")
        raw_description = parsed_json.get("description")
        if isinstance(raw_description, str):
            description = normalize_optional_text(raw_description)
        raw_error_code = parsed_json.get("error_code")
        if isinstance(raw_error_code, int) and not isinstance(raw_error_code, bool):
            error_code = raw_error_code
        parameters = parsed_json.get("parameters")
        if isinstance(parameters, dict):
            raw_retry = parameters.get("retry_after")
            if isinstance(raw_retry, int) and not isinstance(raw_retry, bool) and raw_retry > 0:
                retry_after = raw_retry

    if retry_after is None:
        header_retry = response.headers.get("Retry-After")
        if header_retry is not None and header_retry.strip().isdigit():
            parsed_header_retry = int(header_retry.strip())
            if parsed_header_retry > 0:
                retry_after = parsed_header_retry

    status_code = response.status_code
    if status_code == 200 and ok_flag is True:
        return NotificationDeliveryOutcome(delivered=True)

    if status_code == 429 or error_code == 429 or retry_after is not None:
        return NotificationDeliveryOutcome(
            delivered=False,
            is_permanent_failure=False,
            retry_after_seconds=retry_after,
            error_message=description or "Превышен лимит запросов Telegram Bot API (HTTP 429).",
        )

    if status_code in _PERMANENT_TELEGRAM_HTTP_STATUSES or (
        status_code == 200 and error_code in _PERMANENT_TELEGRAM_HTTP_STATUSES
    ):
        effective_code = error_code or status_code
        return NotificationDeliveryOutcome(
            delivered=False,
            is_permanent_failure=True,
            error_message=description or f"Постоянный отказ Telegram Bot API (HTTP {effective_code}).",
        )

    if not isinstance(parsed_json, dict):
        return NotificationDeliveryOutcome(
            delivered=False,
            is_permanent_failure=False,
            error_message=f"Некорректный JSON-ответ Telegram Bot API (HTTP {status_code}).",
        )

    return NotificationDeliveryOutcome(
        delivered=False,
        is_permanent_failure=False,
        error_message=description or f"Временная ошибка Telegram Bot API (HTTP {status_code}).",
    )


def _build_inline_keyboard(event: Event, public_web_base_url: str) -> list[list[dict[str, str]]]:
    """Формирует инлайн-клавиатуру, совместимую с `EventCardCallback` и `MenuCallback`."""
    event_identifier = str(event.identifier)
    first_row: list[dict[str, str]] = [{"text": "🎟 Подробнее", "callback_data": f"event:{event_identifier}"}]
    if public_web_base_url:
        first_row.append(
            {"text": "🌐 Открыть на сайте", "url": f"{public_web_base_url.rstrip('/')}/events/{event_identifier}"}
        )
    return [first_row, [{"text": "🔔 Мои подписки", "callback_data": "m:subscriptions"}]]


def _format_notification_html(event: Event) -> str:
    """Форматирует карточку уведомления о новом событии в стиле HTML-сообщений Telegram-бота."""
    category_label = _CATEGORY_LABELS.get(event.category, event.category.value)
    datetime_label = _format_telegram_datetime(event)
    price_label = _format_price_label(event)

    lines: list[str] = [
        "🔔 <b>Новое мероприятие по вашей подписке</b>",
        "",
        f"<b>{_escape_html(event.title)}</b>",
        f"🎭 Категория: {_escape_html(category_label)}",
        f"📅 Дата и время: {_escape_html(datetime_label)} (<code>Asia/Yekaterinburg</code>)",
        f"📍 Место: {_escape_html(event.city)}, {_escape_html(event.venue)}",
    ]

    cleaned_address = normalize_optional_text(event.address)
    if cleaned_address is not None:
        lines.append(f"🗺 Адрес: {_escape_html(cleaned_address)}")

    lines.append(f"💳 Стоимость: <b>{_escape_html(price_label)}</b> · 🔞 Возраст: <b>{event.age_limit}+</b>")

    cleaned_description = normalize_optional_text(event.description)
    if cleaned_description is not None:
        short_description = _truncate_plain_text(
            cleaned_description, maximum_length=_MAXIMUM_NOTIFICATION_DESCRIPTION_LENGTH
        )
        lines.extend(["", _escape_html(short_description)])

    lines.extend(["", f"<i>ℹ️ {_escape_html(_SERVICE_DISCLAIMER_TEXT)}</i>"])
    return _fit_telegram_message("\n".join(lines))


def _escape_html(value: str) -> str:
    return escape(value, quote=True)


def _sanitize_public_url(raw_url: str | None) -> str | None:
    cleaned = normalize_optional_text(raw_url)
    if cleaned is None:
        return None
    normalized = cleaned.rstrip("/")
    if normalized.lower().startswith(("https://", "http://")):
        return normalized
    return None


def _truncate_plain_text(value: str, *, maximum_length: int) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= maximum_length:
        return cleaned
    if maximum_length <= 1:
        return cleaned[:maximum_length]
    truncated = cleaned[: maximum_length - 1].rsplit(" ", maxsplit=1)[0].strip()
    if not truncated:
        truncated = cleaned[: maximum_length - 1].strip()
    return f"{truncated}…"


def _to_bashkortostan_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BASHKORTOSTAN_TIMEZONE)
    return value.astimezone(BASHKORTOSTAN_TIMEZONE)


def _format_telegram_datetime(event: Event) -> str:
    local_start = _to_bashkortostan_datetime(event.date_start)
    weekday_label = _RUSSIAN_WEEKDAYS_SHORT[local_start.weekday()]
    month_label = _RUSSIAN_MONTHS_GENITIVE[local_start.month]
    date_part = f"{weekday_label}, {local_start.day} {month_label} {local_start.year}"
    time_part = f"{local_start:%H:%M}"
    if event.date_end is not None:
        local_end = _to_bashkortostan_datetime(event.date_end)
        if local_end.date() == local_start.date():
            range_label = f"{date_part} · {time_part}–{local_end:%H:%M}"
        else:
            end_month = _RUSSIAN_MONTHS_GENITIVE[local_end.month]
            range_label = f"{date_part} {time_part} — {local_end.day} {end_month} {local_end:%H:%M}"
    elif event.duration_minutes is not None:
        range_label = f"{date_part} · {time_part} ({event.duration_minutes} мин.)"
    else:
        range_label = f"{date_part} · {time_part}"
    return f"{range_label} (+05:00)"


def _format_price_label(event: Event) -> str:
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


def _fit_telegram_message(text: str) -> str:
    if len(text) <= _MAXIMUM_TELEGRAM_MESSAGE_LENGTH:
        return text
    lines = text.splitlines()
    while len("\n".join(lines)) > _MAXIMUM_TELEGRAM_MESSAGE_LENGTH - 32 and len(lines) > 3:
        lines.pop(-2)
    fitted = "\n".join(lines)
    if len(fitted) <= _MAXIMUM_TELEGRAM_MESSAGE_LENGTH:
        return fitted
    return fitted[: _MAXIMUM_TELEGRAM_MESSAGE_LENGTH - 1] + "…"
