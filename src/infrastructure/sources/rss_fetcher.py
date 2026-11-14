"""Адаптер загрузки и первичного разбора мероприятий из RSS и Atom-лент."""

import time
import xml.sax
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import feedparser
import httpx

from src.core.configuration import PROJECT_ROOT
from src.core.exceptions import ValidationSchemaError
from src.core.logger import get_logger
from src.domain.entities import FetchedSourceItem, SourceDefinition
from src.infrastructure.sources.registry import (
    DEFAULT_SOURCE_USER_AGENT,
    extract_age_text,
    extract_scalar_text,
    fetch_source_bytes,
    format_price_text_from_parts,
    normalize_status_marker,
    resolve_external_identifier,
    resolve_item_source_url,
)

__all__ = ["RssSourceFetcher"]

logger = get_logger(__name__)


def _to_json_compatible(value: object) -> object:
    """Преобразует структуры `feedparser` (включая `struct_time`) в JSON-совместимые типы."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, time.struct_time):
        return datetime(*value[:6], tzinfo=UTC).isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _to_json_compatible(item_value) for key, item_value in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [_to_json_compatible(item) for item in value]
    return str(value)


def _first_raw_value(entry: Mapping[str, object], keys: tuple[str, ...]) -> object:
    """Возвращает первое непустое сырое значение по списку возможных ключей."""
    for key in keys:
        if key in entry and entry[key] is not None:
            candidate = entry[key]
            if isinstance(candidate, str) and not candidate.strip():
                continue
            return candidate
    return None


def _first_scalar_text(entry: Mapping[str, object], keys: tuple[str, ...]) -> str | None:
    """Возвращает первую непустую строку по списку возможных ключей записи фида."""
    return extract_scalar_text(_first_raw_value(entry, keys))


def _extract_description(entry: Mapping[str, object]) -> str | None:
    """Извлекает описание записи без удаления текстовых маркеров безопасности."""
    raw_value = _first_raw_value(entry, ("description", "summary"))
    if isinstance(raw_value, str):
        cleaned = raw_value.strip()
        return cleaned or None

    raw_content = entry.get("content")
    if isinstance(raw_content, Sequence) and not isinstance(raw_content, str | bytes):
        for block in raw_content:
            if isinstance(block, Mapping):
                block_value = block.get("value")
                if isinstance(block_value, str) and block_value.strip():
                    return block_value.strip()
    return None


def _extract_category(entry: Mapping[str, object]) -> str | None:
    """Извлекает категорию из прямых полей записи или списка тегов `feedparser`."""
    direct = _first_scalar_text(entry, ("category", "cm_category", "event_type", "genre"))
    if direct is not None:
        return direct
    raw_tags = entry.get("tags")
    if isinstance(raw_tags, Sequence) and not isinstance(raw_tags, str | bytes):
        for tag_entry in raw_tags:
            if isinstance(tag_entry, Mapping):
                term = extract_scalar_text(tag_entry.get("term") or tag_entry.get("label"))
                if term is not None:
                    return term
    return None


class RssSourceFetcher:
    """Асинхронный загрузчик событий из RSS 2.0 и Atom-источников через `feedparser`."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = DEFAULT_SOURCE_USER_AGENT,
        default_timeout_seconds: float = 5.0,
        project_root: Path = PROJECT_ROOT,
        local_feeds_directory: Path | None = None,
    ) -> None:
        """Инициализирует RSS/Atom-загрузчик с опциональным HTTP-клиентом или транспортом.

        Arguments:
            http_client: Внешний экземпляр `httpx.AsyncClient` (не закрывается при `close()`).
            transport: Опциональный транспорт `httpx` для изолированного тестирования.
            user_agent: Значение HTTP-заголовка `User-Agent`.
            default_timeout_seconds: Таймаут HTTP-клиента по умолчанию в секундах.
            project_root: Корневая директория проекта для безопасного чтения локальных файлов.
            local_feeds_directory: Дополнительная директория локальных фидов.
        """
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._transport = transport
        self._user_agent = user_agent
        self._default_timeout_seconds = default_timeout_seconds
        self._project_root = project_root
        self._local_feeds_directory = local_feeds_directory

    def _get_or_create_http_client(self) -> httpx.AsyncClient:
        """Возвращает переданный или лениво созданный асинхронный HTTP-клиент."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._default_timeout_seconds),
                headers={"User-Agent": self._user_agent},
                follow_redirects=True,
                transport=self._transport,
            )
            self._owns_http_client = True
        return self._http_client

    async def close(self) -> None:
        """Закрывает внутренний `httpx.AsyncClient`, если он был создан самим загрузчиком."""
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def fetch_items(self, *, source: SourceDefinition) -> tuple[FetchedSourceItem, ...]:
        """Загружает и разбирает карточки мероприятий из RSS/Atom-ленты.

        Arguments:
            source: Описание RSS-источника из реестра.

        Returns:
            Кортеж сырых элементов мероприятий `FetchedSourceItem`.

        Raises:
            AccessPermissionDeniedError: При отказе доступа к URL или выходе локального пути за пределы проекта.
            NetworkException: При сетевой ошибке или таймауте загрузки.
            ServiceAvailabilityException: При ответах HTTP 429 или 5xx.
            ValidationSchemaError: При пустом или синтаксически повреждённом XML/RSS/Atom-документе.
            ValidationInputError: При отсутствии локального файла или HTTP-ошибке 4xx.
        """
        raw_bytes = await fetch_source_bytes(
            source=source,
            get_http_client=self._get_or_create_http_client,
            user_agent=self._user_agent,
            project_root=self._project_root,
            local_feeds_directory=self._local_feeds_directory,
        )
        if not raw_bytes.strip():
            raise ValidationSchemaError(f"RSS/Atom-источник '{source.name}' вернул пустое содержимое.")

        parsed_obj: object = feedparser.parse(raw_bytes)
        if not isinstance(parsed_obj, Mapping):
            raise ValidationSchemaError(f"Неожиданный результат разбора RSS/Atom-ленты '{source.name}'.")

        raw_entries = parsed_obj.get("entries")
        entries: Sequence[object] = (
            raw_entries if isinstance(raw_entries, Sequence) and not isinstance(raw_entries, str | bytes) else ()
        )
        bozo = bool(parsed_obj.get("bozo"))
        bozo_exception = parsed_obj.get("bozo_exception")
        version = extract_scalar_text(parsed_obj.get("version"))

        if bozo and (not entries or version is None or isinstance(bozo_exception, xml.sax.SAXException)):
            detail = f": {bozo_exception}" if bozo_exception is not None else ""
            if isinstance(bozo_exception, Exception):
                raise ValidationSchemaError(
                    f"Повреждённый XML в RSS/Atom-источнике '{source.name}'{detail}"
                ) from bozo_exception
            raise ValidationSchemaError(f"Повреждённый XML в RSS/Atom-источнике '{source.name}'{detail}")

        items: list[FetchedSourceItem] = []
        for raw_entry in entries:
            if not isinstance(raw_entry, Mapping):
                continue

            entry_mapping = {str(key): value for key, value in raw_entry.items()}
            title = _first_scalar_text(entry_mapping, ("title",))
            raw_link = _first_scalar_text(entry_mapping, ("link",))
            if title is None and raw_link is None:
                logger.warning("Пропущена пустая запись RSS без заголовка и ссылки в источнике '%s'.", source.name)
                continue

            resolved_url = resolve_item_source_url(
                raw_link, base_source_url=source.source_url, project_root=self._project_root
            )
            explicit_id = _first_scalar_text(entry_mapping, ("id", "guid", "external_id", "external_identifier"))
            payload_dict = _to_json_compatible(entry_mapping)
            normalized_payload: dict[str, object] = (
                dict(payload_dict) if isinstance(payload_dict, Mapping) else {"raw": str(payload_dict)}
            )

            normalized_status = normalize_status_marker(
                _first_raw_value(entry_mapping, ("status", "cm_status", "event_status"))
            )
            if normalized_status is not None:
                normalized_payload["status"] = normalized_status

            external_identifier = resolve_external_identifier(
                explicit_id, source_url=resolved_url, fallback_payload=normalized_payload
            )
            raw_date_text = _first_scalar_text(
                entry_mapping,
                (
                    "date_start",
                    "cm_date_start",
                    "start_date",
                    "date_text",
                    "cm_date_text",
                    "event_date",
                    "start_at",
                    "date",
                    "when",
                    "published",
                    "updated",
                ),
            )
            raw_price_text = format_price_text_from_parts(
                raw_price=_first_raw_value(entry_mapping, ("price_text", "cm_price_text", "price", "cm_price", "cost")),
                price_minimum=_first_raw_value(entry_mapping, ("price_minimum", "price_min")),
                price_maximum=_first_raw_value(entry_mapping, ("price_maximum", "price_max")),
                is_free=entry_mapping.get("is_free"),
            )
            raw_org_link = _first_scalar_text(
                entry_mapping, ("organization_link", "org_link", "cm_org_link", "organizer_url", "tickets_url")
            )
            resolved_org_link = (
                resolve_item_source_url(
                    raw_org_link, base_source_url=source.source_url, project_root=self._project_root
                )
                if raw_org_link is not None
                else None
            )

            items.append(
                FetchedSourceItem(
                    source_name=source.name,
                    source_type=source.source_type,
                    external_identifier=external_identifier,
                    source_url=resolved_url,
                    raw_title=title or "",
                    raw_description=_extract_description(entry_mapping),
                    raw_date_text=raw_date_text,
                    raw_price_text=raw_price_text,
                    raw_city=_first_scalar_text(entry_mapping, ("city", "cm_city", "event_city", "town", "locality")),
                    raw_venue=_first_scalar_text(entry_mapping, ("venue", "cm_venue", "place", "location", "hall")),
                    raw_address=_first_scalar_text(entry_mapping, ("address", "cm_address", "street_address")),
                    raw_category=_extract_category(entry_mapping),
                    raw_age_text=extract_age_text(
                        _first_raw_value(entry_mapping, ("age_limit", "cm_age_limit", "age", "age_restriction"))
                    ),
                    raw_language=_first_scalar_text(entry_mapping, ("language", "cm_language", "lang")),
                    organization_link=resolved_org_link,
                    raw_payload=normalized_payload,
                )
            )

        return tuple(items)
