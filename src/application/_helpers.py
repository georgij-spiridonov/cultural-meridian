"""Общие вспомогательные функции для прикладных сценариев."""

from datetime import datetime
from urllib.parse import urlsplit

from src.domain.exceptions import InvalidEventDataError
from src.domain.ports import EventRepositoryPort
from src.domain.services import BASHKORTOSTAN_TIMEZONE, normalize_city, normalize_optional_text

__all__ = ["attach_default_timezone", "resolve_duration_minutes", "resolve_normalized_city", "validate_http_url"]


def attach_default_timezone(target_datetime: datetime) -> datetime:
    """Назначает часовой пояс Республики Башкортостан наивным датам."""
    if target_datetime.tzinfo is None:
        return target_datetime.replace(tzinfo=BASHKORTOSTAN_TIMEZONE)
    return target_datetime


def validate_http_url(raw_url: str, *, field_name: str) -> str:
    """Проверяет, что строка является корректной ссылкой протокола HTTP или HTTPS."""
    cleaned_url = normalize_optional_text(raw_url)
    if cleaned_url is None:
        raise InvalidEventDataError(f"Поле «{field_name}» не может быть пустым.", field_name=field_name)
    parsed = urlsplit(cleaned_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise InvalidEventDataError(
            f"Поле «{field_name}» должно содержать корректную ссылку HTTP или HTTPS.", field_name=field_name
        )
    return cleaned_url


def resolve_duration_minutes(
    *, date_start: datetime, date_end: datetime | None, explicit_duration_minutes: int | None
) -> int | None:
    """Проверяет явную длительность или вычисляет её по интервалу дат начала и окончания."""
    if explicit_duration_minutes is not None:
        if explicit_duration_minutes <= 0:
            raise InvalidEventDataError(
                "Длительность мероприятия должна быть положительным числом минут.", field_name="duration_minutes"
            )
        return explicit_duration_minutes

    if date_end is not None and date_end > date_start:
        computed_minutes = int((date_end - date_start).total_seconds() // 60)
        if 0 < computed_minutes <= 720:
            return computed_minutes
    return None


async def resolve_normalized_city(raw_city: str, *, event_repository: EventRepositoryPort) -> str:
    """Приводит название города к каноническому виду по справочнику каталога."""
    cities = await event_repository.list_cities()
    return normalize_city(raw_city, cities=cities) or raw_city
