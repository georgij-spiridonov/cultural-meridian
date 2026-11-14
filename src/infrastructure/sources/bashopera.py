"""Разбор блока «Афиша» на главной странице Башкирского театра оперы и балета (bashopera.ru, Bitrix)."""

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
    url_path,
)

__all__ = ["parse_bashopera_page"]

_CARD_SELECTOR: Final[str] = ".b-posters .item"


def parse_bashopera_page(
    html: str, *, source: SourceDefinition, page_url: str, options: Mapping[str, str]
) -> tuple[FetchedSourceItem, ...]:
    """Извлекает спектакли из блока `.b-posters` главной страницы bashopera.ru.

    Дата указана без года («16 ноября в 19:00»); год определяет нормализация.
    Внешний идентификатор — путь ссылки на билеты конкретного показа. Зал не входит в название
    площадки, а добавляется в начало описания; площадку задаёт `default_venue` источника.

    Arguments:
        html: Текст страницы.
        source: Описание источника.
        page_url: Адрес загруженной страницы.
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
        title = node_text(card.select_one(".title"))
        date_text = node_text(card.select_one(".date"))
        performance_url = absolute_url(page_url, attribute(card.select_one(".buy a"), "href"))
        production_url = absolute_url(page_url, attribute(card.select_one("a.image"), "href"))
        event_url = performance_url or production_url
        if not title or not date_text or not event_url:
            log_skipped_card(source=source, reason="нет названия, даты или ссылки")
            continue

        hall = node_text(card.select_one(".hall"))
        authors = node_text(card.select_one(".tags"))

        items.append(
            build_item(
                source=source,
                external_identifier=f"{url_path(event_url)}#{date_text}",
                source_url=production_url or event_url,
                raw_title=title,
                raw_description=describe_with_hall(hall=hall, description=authors),
                raw_date_text=date_text,
                raw_age_text=node_text(card.select_one(".age")),
                organization_link=performance_url,
                payload={
                    "title": title,
                    "date": date_text,
                    "hall": hall,
                    "tags": authors,
                    "production_url": production_url,
                    "performance_url": performance_url,
                },
            )
        )
    return tuple(items)
