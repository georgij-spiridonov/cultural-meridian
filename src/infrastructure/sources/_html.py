"""Общие помощники разбора HTML-страниц сайтов источников на `BeautifulSoup`."""

import re
from collections.abc import Mapping, Sequence
from typing import Final
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from src.core.exceptions import ValidationSchemaError
from src.core.logger import get_logger
from src.domain.entities import FetchedSourceItem, SourceDefinition

__all__ = [
    "MAXIMUM_DESCRIPTION_LENGTH",
    "absolute_url",
    "attribute",
    "build_item",
    "describe_with_hall",
    "extract_settlement",
    "log_skipped_card",
    "node_text",
    "parse_html",
    "require_cards",
    "url_path",
]

logger = get_logger(__name__)

MAXIMUM_DESCRIPTION_LENGTH: Final[int] = 2000
_SETTLEMENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?:^|[\s,])(?:г|с|пос|п|д|рп)\.\s*([А-ЯЁ][А-Яа-яЁё-]+)")
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")


def parse_html(html: str) -> BeautifulSoup:
    """Разбирает HTML встроенным парсером `html.parser`, как и общий HTML-загрузчик."""
    return BeautifulSoup(html, "html.parser")


def describe_with_hall(*, hall: str | None, description: str | None) -> str | None:
    """Добавляет название зала или коллектива в начало описания: «Большой зал. Джузеппе Верди»."""
    parts = [part for part in (hall, description) if part]
    return ". ".join(parts) or None


def node_text(node: Tag | None) -> str | None:
    """Возвращает текст узла со схлопнутыми пробелами либо `None` для пустого или отсутствующего узла."""
    if node is None:
        return None
    text = _WHITESPACE_PATTERN.sub(" ", node.get_text(" ", strip=True)).strip()
    return text or None


def attribute(node: Tag | None, name: str) -> str | None:
    """Возвращает непустое значение атрибута узла либо `None`; многозначный `class` склеивается пробелом."""
    if node is None:
        return None
    value = node.get(name)
    if value is None:
        return None
    text = value if isinstance(value, str) else " ".join(value)
    stripped = text.strip()
    return stripped or None


def absolute_url(base_url: str, href: str | None) -> str | None:
    """Превращает относительную ссылку в абсолютную относительно адреса страницы."""
    if not href or href.startswith(("javascript:", "#")):
        return None
    return urljoin(base_url, href)


def url_path(url: str) -> str:
    """Возвращает путь адреса без схемы и хоста."""
    return urlsplit(url).path


def extract_settlement(address: str | None) -> str | None:
    """Извлекает название населённого пункта из адреса с префиксом `г.`, `с.`, `пос.` и т. п."""
    if not address:
        return None
    match = _SETTLEMENT_PATTERN.search(address)
    return match.group(1) if match else None


def require_cards(cards: Sequence[Tag], *, source: SourceDefinition, selector: str) -> Sequence[Tag]:
    """Проверяет, что на странице найдены карточки мероприятий.

    Пустая выдача почти всегда означает изменение разметки сайта, поэтому она считается ошибкой схемы.

    Raises:
        ValidationSchemaError: Если ни одной карточки не найдено.
    """
    if not cards:
        raise ValidationSchemaError(
            f"На странице источника {source.name} не найдено карточек по селектору {selector!r}."
        )
    return cards


def build_item(
    *,
    source: SourceDefinition,
    external_identifier: str,
    source_url: str,
    raw_title: str,
    payload: Mapping[str, object],
    raw_description: str | None = None,
    raw_date_text: str | None = None,
    raw_price_text: str | None = None,
    raw_city: str | None = None,
    raw_venue: str | None = None,
    raw_address: str | None = None,
    raw_category: str | None = None,
    raw_age_text: str | None = None,
    organization_link: str | None = None,
) -> FetchedSourceItem:
    """Собирает сырую запись источника; пустые поля полезной нагрузки отбрасываются."""
    description = raw_description[:MAXIMUM_DESCRIPTION_LENGTH] if raw_description else None
    return FetchedSourceItem(
        source_name=source.name,
        source_type=source.source_type,
        external_identifier=external_identifier,
        source_url=source_url,
        raw_title=raw_title,
        raw_description=description,
        raw_date_text=raw_date_text,
        raw_price_text=raw_price_text,
        raw_city=raw_city,
        raw_venue=raw_venue,
        raw_address=raw_address,
        raw_category=raw_category,
        raw_age_text=raw_age_text,
        organization_link=organization_link,
        raw_payload={key: value for key, value in payload.items() if value not in (None, "")},
    )


def log_skipped_card(*, source: SourceDefinition, reason: str) -> None:
    """Пишет в лог предупреждение о пропущенной карточке."""
    logger.warning("Источник %s: карточка пропущена (%s).", source.name, reason)
