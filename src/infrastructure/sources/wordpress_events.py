"""Разбор афиши WordPress-плагина событий (bashgf.ru, sgtko.ru)."""

import re
from collections.abc import Mapping
from typing import Final

from src.domain.entities import FetchedSourceItem, SourceDefinition
from src.infrastructure.sources._html import (
    absolute_url,
    attribute,
    build_item,
    describe_with_hall,
    log_skipped_card,
    node_text,
    parse_html,
    require_cards,
)

__all__ = ["parse_wordpress_events_page"]

_CARD_SELECTOR: Final[str] = "article"
_TITLE_SELECTOR: Final[str] = ".entry-title a, .post-creative-title a"
_DESCRIPTION_SELECTOR: Final[str] = ".post-modern-text, .my-posters-content"
_TICKET_SELECTOR: Final[str] = "a.widget-tr, a.a_quicktickets"
_OPEN_PRICE_RANGE_PATTERN: Final[re.Pattern[str]] = re.compile(r"(\d[\d\s]*?)\s*-\s*₽\s*$")


def _normalize_open_price_range(price: str | None) -> str | None:
    """Переписывает диапазон без верхней границы («Цена: 400 - ₽») в нижнюю границу («от 400 ₽»)."""
    if price is None:
        return None
    match = _OPEN_PRICE_RANGE_PATTERN.search(price)
    if match is None:
        return price
    return f"от {match.group(1).strip()} ₽"


def parse_wordpress_events_page(
    html: str, *, source: SourceDefinition, page_url: str, options: Mapping[str, str]
) -> tuple[FetchedSourceItem, ...]:
    """Извлекает мероприятия со страницы афиши WordPress-плагина событий.

    Дата берётся из машиночитаемого `abbr.dtstart[title]` (ISO с годом), цена из `.event-ticket`.
    Рубрика события обозначает зал или коллектив и добавляется в начало описания;
    площадку задаёт `default_venue` источника.

    Arguments:
        html: Текст страницы.
        source: Описание источника.
        page_url: Адрес загруженной страницы для разрешения относительных ссылок.
        options: Параметры парсера из конфигурации (не используются).

    Returns:
        Кортеж сырых записей.

    Raises:
        ValidationSchemaError: Если на странице нет ни одной карточки.
    """
    del options
    cards = require_cards(parse_html(html).select(_CARD_SELECTOR), source=source, selector=_CARD_SELECTOR)
    items: list[FetchedSourceItem] = []
    for card in cards:
        title_node = card.select_one(_TITLE_SELECTOR)
        title = attribute(title_node, "title") or node_text(title_node)
        event_url = absolute_url(page_url, attribute(title_node, "href"))
        date_start = attribute(card.select_one("abbr.dtstart"), "title")
        if not title or not event_url or not date_start:
            log_skipped_card(source=source, reason="нет названия, ссылки или даты")
            continue

        description = node_text(card.select_one(_DESCRIPTION_SELECTOR))
        if description and description.startswith(title):
            description = description[len(title) :].strip() or None
        price = _normalize_open_price_range(node_text(card.select_one(".event-ticket")))
        hall = node_text(card.select_one(".event-category a"))
        ticket_link = absolute_url(page_url, attribute(card.select_one(_TICKET_SELECTOR), "href"))

        items.append(
            build_item(
                source=source,
                external_identifier=f"{event_url}#{date_start}",
                source_url=event_url,
                raw_title=title,
                raw_description=describe_with_hall(hall=hall, description=description),
                raw_date_text=date_start,
                raw_price_text=price,
                organization_link=ticket_link,
                payload={
                    "title": title,
                    "date_start": date_start,
                    "price": price,
                    "hall": hall,
                    "event_url": event_url,
                    "ticket_url": ticket_link,
                },
            )
        )
    return tuple(items)
