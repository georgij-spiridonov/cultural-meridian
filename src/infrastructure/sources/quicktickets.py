"""Разбор городской афиши quicktickets.ru: одна сырая запись на каждый сеанс."""

import re
from collections.abc import Mapping
from typing import Final

from src.domain.entities import FetchedSourceItem, SourceDefinition
from src.infrastructure.sources._html import (
    absolute_url,
    attribute,
    build_item,
    extract_settlement,
    log_skipped_card,
    node_text,
    parse_html,
    require_cards,
    url_path,
)

__all__ = [
    "MULTIPLE_ADDRESSES_MARKER",
    "is_multiple_addresses",
    "parse_quicktickets_page",
    "parse_quicktickets_session_address",
]

MULTIPLE_ADDRESSES_MARKER: Final[str] = "разные адреса"
_CARD_SELECTOR: Final[str] = ".quick-tickets-event-card"
_ADDRESS_SELECTOR: Final[str] = ".quick-tickets-event-location__address .quick-tickets-event-location__value"
_ORGANISER_SELECTOR: Final[str] = ".quick-tickets-event-location__organiser .quick-tickets-event-location__value"
_DUPLICATED_TITLE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?P<outer>.+?)\s*\((?P<inner>.+)\)$")
_QUOTES: Final[str] = "\"'«»“”„ "


def is_multiple_addresses(address: str | None) -> bool:
    """Проверяет, что вместо адреса указана пометка «Разные адреса проведения»."""
    return address is not None and MULTIPLE_ADDRESSES_MARKER in address.lower()


def _clean_title(raw_title: str) -> str:
    """Убирает повтор названия в скобках: `"Сваты" ("Сваты")` превращается в `Сваты`."""
    match = _DUPLICATED_TITLE_PATTERN.match(raw_title)
    if match is None:
        return raw_title.strip(_QUOTES)
    outer = match.group("outer").strip(_QUOTES)
    inner = match.group("inner").strip(_QUOTES)
    if outer.casefold() == inner.casefold():
        return outer
    return raw_title


def parse_quicktickets_page(
    html: str, *, source: SourceDefinition, page_url: str, options: Mapping[str, str]
) -> tuple[FetchedSourceItem, ...]:
    """Извлекает сеансы со страницы городской афиши quicktickets.ru (`/<город>/afisha`).

    Каждый сеанс становится отдельной записью. Город берётся из адреса карточки (`с.Исянгулово`);
    если вместо адреса указано «Разные адреса проведения», адрес и город остаются пустыми,
    а в полезной нагрузке ставится признак `address_scope=multiple` для дозагрузки страницы сеанса.
    Организатор — коллектив, а не площадка, поэтому он хранится только в полезной нагрузке.

    Arguments:
        html: Текст страницы.
        source: Описание источника.
        page_url: Адрес загруженной страницы.
        options: Параметры парсера из конфигурации (не используются).

    Returns:
        Кортеж сырых записей, по одной на сеанс.

    Raises:
        ValidationSchemaError: Если на странице нет ни одной карточки.
    """
    del options
    cards = require_cards(parse_html(html).select(_CARD_SELECTOR), source=source, selector=_CARD_SELECTOR)
    items: list[FetchedSourceItem] = []
    for card in cards:
        raw_title = node_text(card.select_one(".event-card__title .underline"))
        if not raw_title:
            log_skipped_card(source=source, reason="нет названия")
            continue
        title = _clean_title(raw_title)
        address = node_text(card.select_one(_ADDRESS_SELECTOR))
        has_multiple_addresses = is_multiple_addresses(address)
        card_address = None if has_multiple_addresses else address
        organiser = node_text(card.select_one(_ORGANISER_SELECTOR))
        description = node_text(card.select_one(".quick-tickets-event-card__description"))
        age = node_text(card.select_one(".event-card__title .ageRestriction"))
        event_url = absolute_url(page_url, attribute(card.select_one(".event-card__title a"), "href"))
        has_pushkin_card = card.select_one(".qtf-pushkinCard") is not None

        sessions = card.select(".sessions__session")
        if not sessions:
            log_skipped_card(source=source, reason=f"нет сеансов у «{title}»")
            continue
        for session in sessions:
            session_url = absolute_url(page_url, attribute(session.select_one("a"), "href"))
            date_text = node_text(session.select_one(".quick-tickets-session-option__label"))
            if not session_url or not date_text:
                log_skipped_card(source=source, reason=f"сеанс «{title}» без ссылки или даты")
                continue
            items.append(
                build_item(
                    source=source,
                    external_identifier=url_path(session_url),
                    source_url=session_url,
                    raw_title=title,
                    raw_description=description,
                    raw_date_text=date_text,
                    raw_city=extract_settlement(card_address),
                    raw_address=card_address,
                    raw_age_text=age,
                    organization_link=session_url,
                    payload={
                        "title": title,
                        "date": date_text,
                        "organiser": organiser,
                        "address": card_address,
                        "address_scope": "multiple" if has_multiple_addresses else "single",
                        "event_url": event_url,
                        "pushkin_card": has_pushkin_card,
                    },
                )
            )
    return tuple(items)


def parse_quicktickets_session_address(html: str) -> str | None:
    """Извлекает адрес проведения со страницы конкретного сеанса quicktickets.ru.

    Arguments:
        html: Текст страницы сеанса.

    Returns:
        Адрес сеанса либо `None`, если адреса нет или он снова указан как «Разные адреса».
    """
    address = node_text(parse_html(html).select_one(_ADDRESS_SELECTOR))
    if is_multiple_addresses(address):
        return None
    return address
