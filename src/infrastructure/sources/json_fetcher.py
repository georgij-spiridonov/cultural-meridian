"""Адаптер загрузки и первичного разбора мероприятий из JSON API и локальных JSON-фидов."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx

from src.core.configuration import PROJECT_ROOT
from src.core.exceptions import ValidationSchemaError
from src.domain.entities import FetchedSourceItem, SourceDefinition
from src.infrastructure.sources.http_client import PoliteHttpClient
from src.infrastructure.sources.registry import (
    DEFAULT_SOURCE_USER_AGENT,
    create_standalone_polite_client,
    decode_utf8_content,
    extract_age_text,
    extract_scalar_text,
    fetch_source_bytes,
    format_price_text_from_parts,
    normalize_status_marker,
    resolve_external_identifier,
    resolve_item_source_url,
)

__all__ = ["JsonSourceFetcher"]

_CONTAINER_KEYS: tuple[str, ...] = ("items", "events", "results", "data")


def _first_raw_value(item: Mapping[str, object], keys: tuple[str, ...]) -> object:
    """Возвращает первое непустое сырое значение из словаря по приоритетному списку ключей."""
    for key in keys:
        if key in item and item[key] is not None:
            candidate = item[key]
            if isinstance(candidate, str) and not candidate.strip():
                continue
            return candidate
    return None


def _first_scalar_text(item: Mapping[str, object], keys: tuple[str, ...]) -> str | None:
    """Возвращает первую непустую строку из словаря по списку ключей."""
    return extract_scalar_text(_first_raw_value(item, keys))


def _extract_json_items(payload: object, *, source_name: str) -> list[object]:
    """Извлекает список записей мероприятий из корневого массива или контейнера JSON."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in _CONTAINER_KEYS:
            if key in payload:
                candidate = payload[key]
                if isinstance(candidate, list):
                    return candidate
                raise ValidationSchemaError(
                    f"Поле '{key}' в JSON-источнике '{source_name}' должно быть массивом объектов."
                )
        raise ValidationSchemaError(
            f"JSON-объект источника '{source_name}' не содержит поддерживаемого массива ('items' или 'events')."
        )
    raise ValidationSchemaError(f"Корневая структура JSON-источника '{source_name}' должна быть массивом или объектом.")


class JsonSourceFetcher:
    """Асинхронный загрузчик событий из JSON API и локальных JSON-фидов."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = DEFAULT_SOURCE_USER_AGENT,
        default_timeout_seconds: float = 5.0,
        project_root: Path = PROJECT_ROOT,
        local_feeds_directory: Path | None = None,
        polite_client: PoliteHttpClient | None = None,
    ) -> None:
        """Инициализирует JSON-загрузчик.

        Arguments:
            http_client: Внешний экземпляр `httpx.AsyncClient` (не закрывается при `close()`).
            transport: Опциональный транспорт `httpx` для изолированного тестирования.
            user_agent: Значение HTTP-заголовка `User-Agent`.
            default_timeout_seconds: Устаревший параметр; таймаут задаётся в описании источника.
            project_root: Корневая директория проекта для безопасного чтения локальных файлов.
            local_feeds_directory: Дополнительная директория локальных фидов.
            polite_client: Общий вежливый HTTP-клиент сборщика; без него создаётся собственный.
        """
        del default_timeout_seconds
        self._owns_polite_client = polite_client is None
        self._polite_client = polite_client or create_standalone_polite_client(
            user_agent=user_agent, http_client=http_client, transport=transport
        )
        self._project_root = project_root
        self._local_feeds_directory = local_feeds_directory

    async def close(self) -> None:
        """Закрывает собственный HTTP-клиент, если он был создан самим загрузчиком."""
        if self._owns_polite_client:
            await self._polite_client.close()

    async def fetch_items(self, *, source: SourceDefinition) -> tuple[FetchedSourceItem, ...]:
        """Загружает, валидирует и преобразует JSON-фид в кортеж `FetchedSourceItem`.

        Arguments:
            source: Описание JSON/API-источника из реестра.

        Returns:
            Кортеж сырых элементов мероприятий.

        Raises:
            AccessPermissionDeniedError: При отказе доступа к URL или выходе локального пути за пределы проекта.
            NetworkException: При сетевой ошибке или таймауте загрузки.
            ServiceAvailabilityException: При ответах HTTP 429 или 5xx.
            ValidationSchemaError: При невалидном JSON или нарушении структуры записей.
            ValidationInputError: При отсутствии локального файла или HTTP-ошибке 4xx.
        """
        raw_bytes = await fetch_source_bytes(
            source=source,
            http_client=self._polite_client,
            project_root=self._project_root,
            local_feeds_directory=self._local_feeds_directory,
        )
        raw_text = decode_utf8_content(raw_bytes, source_name=source.name)
        if not raw_text.strip():
            raise ValidationSchemaError(f"JSON-источник '{source.name}' вернул пустое содержимое.")

        try:
            parsed_payload: object = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValidationSchemaError(f"Некорректный JSON в источнике '{source.name}': {exc}") from exc

        raw_items = _extract_json_items(parsed_payload, source_name=source.name)
        items: list[FetchedSourceItem] = []

        for index, raw_entry in enumerate(raw_items, start=1):
            if not isinstance(raw_entry, Mapping):
                raise ValidationSchemaError(
                    f"Элемент #{index} в JSON-источнике '{source.name}' должен быть JSON-объектом."
                )

            item_dict: dict[str, object] = {str(key): value for key, value in raw_entry.items()}
            title = _first_scalar_text(item_dict, ("title", "name", "event_title", "heading"))
            if title is None:
                raise ValidationSchemaError(
                    f"В элементе #{index} JSON-источника '{source.name}' отсутствует обязательный заголовок."
                )

            raw_link = _first_scalar_text(item_dict, ("source_url", "url", "link", "event_url", "href"))
            resolved_url = resolve_item_source_url(
                raw_link, base_source_url=source.source_url, project_root=self._project_root
            )
            explicit_id = _first_scalar_text(item_dict, ("external_identifier", "external_id", "id", "uid", "guid"))

            normalized_status = normalize_status_marker(item_dict.get("status"))
            if normalized_status is not None:
                item_dict["status"] = normalized_status

            external_identifier = resolve_external_identifier(
                explicit_id, source_url=resolved_url, fallback_payload=item_dict
            )

            raw_description_value = _first_raw_value(item_dict, ("description", "summary", "text", "body", "details"))
            raw_description = raw_description_value.strip() if isinstance(raw_description_value, str) else None

            raw_date_text = _first_scalar_text(
                item_dict, ("date_text", "date_start", "start_at", "start_date", "date", "when")
            )
            raw_price_text = format_price_text_from_parts(
                raw_price=_first_raw_value(item_dict, ("price_text", "price", "cost", "ticket_price")),
                price_minimum=_first_raw_value(item_dict, ("price_minimum", "price_min")),
                price_maximum=_first_raw_value(item_dict, ("price_maximum", "price_max")),
                is_free=item_dict.get("is_free"),
            )

            raw_category_value = _first_raw_value(item_dict, ("category", "event_type", "type", "genre"))
            if isinstance(raw_category_value, Sequence) and not isinstance(raw_category_value, str | bytes):
                raw_category = extract_scalar_text(raw_category_value[0]) if raw_category_value else None
            else:
                raw_category = extract_scalar_text(raw_category_value)

            raw_org_link = _first_scalar_text(
                item_dict, ("organization_link", "org_link", "organizer_url", "tickets_url", "ticket_url")
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
                    raw_title=title,
                    raw_description=raw_description or None,
                    raw_date_text=raw_date_text,
                    raw_price_text=raw_price_text,
                    raw_city=_first_scalar_text(item_dict, ("city", "town", "locality")),
                    raw_venue=_first_scalar_text(item_dict, ("venue", "place", "location_name", "hall")),
                    raw_address=_first_scalar_text(item_dict, ("address", "street_address", "location_address")),
                    raw_category=raw_category,
                    raw_age_text=extract_age_text(_first_raw_value(item_dict, ("age_limit", "age", "age_restriction"))),
                    raw_language=_first_scalar_text(item_dict, ("language", "lang")),
                    organization_link=resolved_org_link,
                    raw_payload=item_dict,
                )
            )

        return tuple(items)
