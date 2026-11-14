"""Адаптер загрузки и первичного разбора мероприятий из HTML-страниц (JSON-LD и карточки)."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, Tag

from src.core.configuration import PROJECT_ROOT
from src.core.exceptions import ValidationSchemaError
from src.core.logger import get_logger
from src.domain.entities import FetchedSourceItem, SourceDefinition
from src.domain.exceptions import UnsupportedSourceFormatError
from src.infrastructure.sources.registry import (
    DEFAULT_SOURCE_USER_AGENT,
    decode_utf8_content,
    extract_age_text,
    extract_scalar_text,
    fetch_source_bytes,
    format_price_text_from_parts,
    normalize_status_marker,
    resolve_external_identifier,
    resolve_item_source_url,
)

__all__ = ["HtmlSourceFetcher"]

logger = get_logger(__name__)

_EVENT_CARD_SELECTOR = "article.event-card, .event-item, [data-event]"
_CANCELED_CSS_CLASSES: frozenset[str] = frozenset({"canceled", "cancelled", "event-canceled", "event-cancelled"})


def _get_tag_attr_text(tag: Tag, attribute_name: str) -> str | None:
    """Извлекает строковое значение атрибута HTML-тега `BeautifulSoup`."""
    raw_value: object = tag.get(attribute_name)
    if isinstance(raw_value, str):
        cleaned = " ".join(raw_value.split()).strip()
        return cleaned or None
    if isinstance(raw_value, Sequence) and not isinstance(raw_value, bytes):
        joined = " ".join(str(part).strip() for part in raw_value if str(part).strip())
        return joined or None
    return None


def _get_tag_classes(tag: Tag) -> set[str]:
    """Возвращает множество CSS-классов HTML-тега в нижнем регистре."""
    raw_classes: object = tag.get("class")
    if isinstance(raw_classes, str):
        return {part.strip().lower() for part in raw_classes.split() if part.strip()}
    if isinstance(raw_classes, Sequence) and not isinstance(raw_classes, bytes):
        return {str(part).strip().lower() for part in raw_classes if str(part).strip()}
    return set()


def _select_first_text(root: Tag, selector: str) -> str | None:
    """Находит первый элемент по CSS-селектору и возвращает его очищенный текст."""
    matched = root.select_one(selector)
    if matched is None:
        return None
    cleaned = " ".join(matched.get_text(" ", strip=True).split()).strip()
    return cleaned or None


def _select_first_href(root: Tag, selector: str) -> str | None:
    """Находит первый элемент по CSS-селектору и возвращает атрибут `href`."""
    matched = root.select_one(selector)
    if matched is None:
        return None
    return _get_tag_attr_text(matched, "href")


def _extract_data_attributes(tag: Tag) -> dict[str, str]:
    """Извлекает все `data-*` атрибуты HTML-элемента в словарь с ключами в snake_case."""
    result: dict[str, str] = {}
    for raw_key, raw_value in tag.attrs.items():
        key_str = str(raw_key).lower()
        if not key_str.startswith("data-"):
            continue
        clean_key = key_str[5:].replace("-", "_")
        if not clean_key:
            continue
        if isinstance(raw_value, str):
            text = raw_value.strip()
        elif isinstance(raw_value, Sequence) and not isinstance(raw_value, bytes):
            text = " ".join(str(item).strip() for item in raw_value if str(item).strip())
        else:
            text = str(raw_value).strip()
        if text:
            result[clean_key] = text
    return result


def _is_ld_json_script(tag: Tag) -> bool:
    """Проверяет, является ли тег `<script>` блоком `application/ld+json`."""
    if tag.name != "script":
        return False
    script_type = _get_tag_attr_text(tag, "type")
    return script_type is not None and script_type.lower().split(";")[0].strip() == "application/ld+json"


def _is_schema_event_type(raw_type: object) -> bool:
    """Проверяет, описывает ли `@type` в JSON-LD культурное событие schema.org."""
    if isinstance(raw_type, str):
        normalized = raw_type.strip().rsplit("/", 1)[-1]
        return normalized == "Festival" or normalized.endswith("Event")
    if isinstance(raw_type, Sequence) and not isinstance(raw_type, str | bytes):
        return any(_is_schema_event_type(item) for item in raw_type)
    return False


def _collect_ld_json_event_dicts(node: object) -> list[dict[str, object]]:
    """Рекурсивно собирает объекты `Event` из произвольной структуры JSON-LD."""
    collected: list[dict[str, object]] = []
    if isinstance(node, list):
        for element in node:
            collected.extend(_collect_ld_json_event_dicts(element))
        return collected
    if isinstance(node, Mapping):
        if "@graph" in node:
            collected.extend(_collect_ld_json_event_dicts(node["@graph"]))
        if _is_schema_event_type(node.get("@type")):
            collected.append({str(key): value for key, value in node.items()})
    return collected


def _extract_location_from_ld_json(raw_location: object) -> tuple[str | None, str | None, str | None]:
    """Извлекает `(city, venue, address)` из поля `location` объекта schema.org Event."""
    location_node = raw_location[0] if isinstance(raw_location, list) and raw_location else raw_location
    if isinstance(location_node, str):
        return None, extract_scalar_text(location_node), None
    if not isinstance(location_node, Mapping):
        return None, None, None

    venue = extract_scalar_text(location_node.get("name"))
    raw_address = location_node.get("address")
    if isinstance(raw_address, str):
        return None, venue, extract_scalar_text(raw_address)
    if isinstance(raw_address, Mapping):
        city = extract_scalar_text(raw_address.get("addressLocality"))
        street = extract_scalar_text(raw_address.get("streetAddress"))
        return city, venue, street
    return None, venue, None


def _extract_offers_from_ld_json(raw_offers: object) -> tuple[str | None, str | None, str | None]:
    """Извлекает `(price_text, org_link, status_hint)` из поля `offers` schema.org Event."""
    offer_node = raw_offers[0] if isinstance(raw_offers, list) and raw_offers else raw_offers
    if not isinstance(offer_node, Mapping):
        return None, None, None
    price_text = format_price_text_from_parts(
        raw_price=offer_node.get("price"),
        price_minimum=offer_node.get("lowPrice"),
        price_maximum=offer_node.get("highPrice"),
    )
    offer_url = extract_scalar_text(offer_node.get("url"))
    availability = normalize_status_marker(offer_node.get("availability"))
    status_hint = availability if availability == "canceled" else None
    return price_text, offer_url, status_hint


class HtmlSourceFetcher:
    """Асинхронный загрузчик событий из HTML-страниц (schema.org JSON-LD и DOM-карточки)."""

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
        """Инициализирует HTML-загрузчик с опциональным HTTP-клиентом или транспортом.

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

    def _parse_ld_json_items(
        self, soup: BeautifulSoup, *, source: SourceDefinition
    ) -> tuple[list[FetchedSourceItem], json.JSONDecodeError | None]:
        """Извлекает мероприятия из блоков `<script type="application/ld+json">`."""
        items: list[FetchedSourceItem] = []
        last_json_error: json.JSONDecodeError | None = None

        for script_tag in soup.find_all("script"):
            if not _is_ld_json_script(script_tag):
                continue
            script_text = (script_tag.string or script_tag.get_text() or "").strip()
            if not script_text:
                continue
            try:
                parsed_json: object = json.loads(script_text)
            except json.JSONDecodeError as exc:
                last_json_error = exc
                logger.warning("Ошибка разбора JSON-LD в источнике '%s': %s", source.name, exc)
                continue

            for event_dict in _collect_ld_json_event_dicts(parsed_json):
                title = extract_scalar_text(event_dict.get("name") or event_dict.get("title"))
                if title is None:
                    continue

                raw_url = extract_scalar_text(event_dict.get("url"))
                resolved_url = resolve_item_source_url(
                    raw_url, base_source_url=source.source_url, project_root=self._project_root
                )
                city, venue, address = _extract_location_from_ld_json(event_dict.get("location"))
                price_text, offer_url, offer_status = _extract_offers_from_ld_json(event_dict.get("offers"))

                payload = dict(event_dict)
                normalized_status = (
                    normalize_status_marker(event_dict.get("eventStatus") or event_dict.get("status")) or offer_status
                )
                if normalized_status is not None:
                    payload["status"] = normalized_status

                explicit_id = extract_scalar_text(event_dict.get("identifier") or event_dict.get("@id"))
                external_identifier = resolve_external_identifier(
                    explicit_id, source_url=resolved_url, fallback_payload=payload
                )

                raw_description_obj = event_dict.get("description")
                raw_description = raw_description_obj.strip() if isinstance(raw_description_obj, str) else None

                raw_category_obj = event_dict.get("genre") or event_dict.get("category")
                if isinstance(raw_category_obj, Sequence) and not isinstance(raw_category_obj, str | bytes):
                    raw_category = extract_scalar_text(raw_category_obj[0]) if raw_category_obj else None
                else:
                    raw_category = extract_scalar_text(raw_category_obj)

                resolved_org_link = (
                    resolve_item_source_url(
                        offer_url, base_source_url=source.source_url, project_root=self._project_root
                    )
                    if offer_url is not None
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
                        raw_date_text=extract_scalar_text(event_dict.get("startDate") or event_dict.get("date_text")),
                        raw_price_text=price_text,
                        raw_city=city,
                        raw_venue=venue,
                        raw_address=address,
                        raw_category=raw_category,
                        raw_age_text=extract_age_text(event_dict.get("typicalAgeRange") or event_dict.get("age_limit")),
                        raw_language=extract_scalar_text(event_dict.get("inLanguage") or event_dict.get("language")),
                        organization_link=resolved_org_link,
                        raw_payload=payload,
                    )
                )

        return items, last_json_error

    def _parse_card_items(self, soup: BeautifulSoup, *, source: SourceDefinition) -> list[FetchedSourceItem]:
        """Извлекает мероприятия из HTML-карточек `article.event-card`, `.event-item`, `[data-event]`."""
        items: list[FetchedSourceItem] = []
        seen_tags: set[int] = set()

        for card in soup.select(_EVENT_CARD_SELECTOR):
            if id(card) in seen_tags:
                continue
            seen_tags.add(id(card))

            data_attrs = _extract_data_attributes(card)
            title = (
                extract_scalar_text(data_attrs.get("title"))
                or _select_first_text(card, "h1, h2, h3, h4, .event-title, .title, [itemprop='name']")
                or _select_first_text(card, "a[href]")
            )
            if title is None:
                continue

            card_dom_id = _get_tag_attr_text(card, "id")
            raw_link = (
                data_attrs.get("url")
                or data_attrs.get("source_url")
                or data_attrs.get("href")
                or _select_first_href(card, "a.event-link[href], h1 a[href], h2 a[href], h3 a[href], a[href]")
                or (f"#{card_dom_id}" if card_dom_id else None)
            )
            resolved_url = resolve_item_source_url(
                raw_link, base_source_url=source.source_url, project_root=self._project_root
            )

            time_tag = card.select_one("time")
            time_datetime = _get_tag_attr_text(time_tag, "datetime") if time_tag is not None else None
            time_text = " ".join(time_tag.get_text(" ", strip=True).split()).strip() if time_tag is not None else None
            raw_date_text = (
                extract_scalar_text(
                    data_attrs.get("date_start") or data_attrs.get("date") or data_attrs.get("date_text")
                )
                or time_datetime
                or (time_text or None)
                or _select_first_text(card, ".event-date, .date, [itemprop='startDate']")
            )

            raw_city = extract_scalar_text(data_attrs.get("city")) or _select_first_text(
                card, ".event-city, .city, [itemprop='addressLocality']"
            )
            raw_venue = extract_scalar_text(data_attrs.get("venue") or data_attrs.get("place")) or _select_first_text(
                card, ".event-venue, .venue, .location, [itemprop='location']"
            )
            raw_address = extract_scalar_text(data_attrs.get("address")) or _select_first_text(
                card, ".event-address, .address, [itemprop='streetAddress']"
            )
            raw_price_text = format_price_text_from_parts(
                raw_price=data_attrs.get("price_text")
                or data_attrs.get("price")
                or _select_first_text(card, ".event-price, .price, [itemprop='price']"),
                price_minimum=data_attrs.get("price_minimum") or data_attrs.get("price_min"),
                price_maximum=data_attrs.get("price_maximum") or data_attrs.get("price_max"),
                is_free=data_attrs.get("is_free") in {"true", "1", "yes"} if "is_free" in data_attrs else None,
            )
            raw_category = extract_scalar_text(data_attrs.get("category")) or _select_first_text(
                card, ".event-category, .category"
            )
            raw_age_text = extract_age_text(
                data_attrs.get("age_limit")
                or data_attrs.get("age")
                or _select_first_text(card, ".event-age, .age-limit, .age")
            )
            raw_language = extract_scalar_text(
                data_attrs.get("language") or data_attrs.get("lang")
            ) or _select_first_text(card, ".event-language, .language")
            raw_description = data_attrs.get("description") or _select_first_text(
                card, ".event-description, .description, .summary, p"
            )
            raw_org_link = (
                data_attrs.get("organization_link")
                or data_attrs.get("org_link")
                or _select_first_href(card, "a.org-link[href], a.tickets-link[href], a.ticket-link[href]")
            )
            resolved_org_link = (
                resolve_item_source_url(
                    raw_org_link, base_source_url=source.source_url, project_root=self._project_root
                )
                if raw_org_link is not None
                else None
            )

            card_classes = _get_tag_classes(card)
            raw_status: object = data_attrs.get("status") or _select_first_text(card, ".event-status, .status")
            if raw_status is None and (card_classes & _CANCELED_CSS_CLASSES):
                raw_status = "canceled"
            normalized_status = normalize_status_marker(raw_status)

            payload: dict[str, object] = {
                **data_attrs,
                "title": title,
                "url": resolved_url,
                "date_text": raw_date_text,
                "city": raw_city,
                "venue": raw_venue,
                "address": raw_address,
                "price_text": raw_price_text,
                "category": raw_category,
                "age_limit": raw_age_text,
                "language": raw_language,
                "description": raw_description,
                "organization_link": resolved_org_link,
            }
            if normalized_status is not None:
                payload["status"] = normalized_status

            explicit_id = extract_scalar_text(
                data_attrs.get("external_id") or data_attrs.get("event_id") or data_attrs.get("id") or card_dom_id
            )
            external_identifier = resolve_external_identifier(
                explicit_id, source_url=resolved_url, fallback_payload=payload
            )

            items.append(
                FetchedSourceItem(
                    source_name=source.name,
                    source_type=source.source_type,
                    external_identifier=external_identifier,
                    source_url=resolved_url,
                    raw_title=title,
                    raw_description=raw_description,
                    raw_date_text=raw_date_text,
                    raw_price_text=raw_price_text,
                    raw_city=raw_city,
                    raw_venue=raw_venue,
                    raw_address=raw_address,
                    raw_category=raw_category,
                    raw_age_text=raw_age_text,
                    raw_language=raw_language,
                    organization_link=resolved_org_link,
                    raw_payload=payload,
                )
            )

        return items

    async def fetch_items(self, *, source: SourceDefinition) -> tuple[FetchedSourceItem, ...]:
        """Загружает HTML-документ и извлекает мероприятия из JSON-LD и/или карточек.

        Arguments:
            source: Описание HTML-источника из реестра.

        Returns:
            Кортеж сырых элементов мероприятий `FetchedSourceItem`.

        Raises:
            AccessPermissionDeniedError: При отказе доступа к URL или выходе локального пути за пределы проекта.
            NetworkException: При сетевой ошибке или таймауте загрузки.
            ServiceAvailabilityException: При ответах HTTP 429 или 5xx.
            UnsupportedSourceFormatError: Если вместо HTML получен чистый JSON-документ.
            ValidationSchemaError: При пустом документе, отсутствии HTML-разметки или повреждённом JSON-LD.
            ValidationInputError: При отсутствии локального файла или HTTP-ошибке 4xx.
        """
        raw_bytes = await fetch_source_bytes(
            source=source,
            get_http_client=self._get_or_create_http_client,
            user_agent=self._user_agent,
            project_root=self._project_root,
            local_feeds_directory=self._local_feeds_directory,
        )
        raw_text = decode_utf8_content(raw_bytes, source_name=source.name)
        stripped = raw_text.strip()
        if not stripped:
            raise ValidationSchemaError(f"HTML-источник '{source.name}' вернул пустое содержимое.")

        if stripped.startswith(("{", "[")):
            raise UnsupportedSourceFormatError(
                f"Источник '{source.name}' вернул JSON вместо HTML-документа.",
                format_name="json",
                source_name=source.name,
            )

        soup = BeautifulSoup(raw_text, "html.parser")
        if soup.find() is None:
            raise ValidationSchemaError(f"Документ источника '{source.name}' не содержит HTML-разметки.")

        ld_json_items, last_json_error = self._parse_ld_json_items(soup, source=source)
        card_items = self._parse_card_items(soup, source=source)

        if last_json_error is not None and not ld_json_items and not card_items:
            raise ValidationSchemaError(
                f"Повреждённый блок JSON-LD в HTML-источнике '{source.name}': {last_json_error}"
            ) from last_json_error

        merged: list[FetchedSourceItem] = list(ld_json_items)
        seen_signatures: set[tuple[str, str]] = {(item.source_url, item.raw_title.casefold()) for item in ld_json_items}
        seen_identifiers: set[str] = {item.external_identifier for item in ld_json_items}

        for card_item in card_items:
            signature = (card_item.source_url, card_item.raw_title.casefold())
            if signature in seen_signatures or card_item.external_identifier in seen_identifiers:
                continue
            seen_signatures.add(signature)
            seen_identifiers.add(card_item.external_identifier)
            merged.append(card_item)

        return tuple(merged)
