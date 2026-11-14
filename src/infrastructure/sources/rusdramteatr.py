"""Разбор афиши Русского драматического театра Стерлитамака (rusdramteatr.ru, Joomla)."""

import re
from collections.abc import Mapping
from typing import Final

from src.domain.entities import FetchedSourceItem, SourceDefinition
from src.infrastructure.sources._html import (
    absolute_url,
    attribute,
    build_item,
    log_skipped_card,
    node_text,
    parse_html,
    require_cards,
    url_path,
)

__all__ = ["parse_rusdramteatr_page"]

_CARD_SELECTOR: Final[str] = ".nspArt"
_SLUG_PATTERN: Final[re.Pattern[str]] = re.compile(r"-(\d{2})-(\d{2})-(\d{4})(?:-(?:g-)?([a-z]+))?/?$")
_TIME_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(\d{1,2}:\d{2})\b")


def parse_rusdramteatr_page(
    html: str, *, source: SourceDefinition, page_url: str, options: Mapping[str, str]
) -> tuple[FetchedSourceItem, ...]:
    """Извлекает спектакли со страницы афиши rusdramteatr.ru.

    Дата с годом и город гастроли берутся из слага ссылки (`/afisha/groza-18-11-2026-kumertau`),
    время из строки `.nspInfo`. Если в слаге нет даты, используется строка `.nspInfo` целиком.
    Без суффикса города в слаге город остаётся пустым, и применяется город источника по умолчанию.

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
        event_url = absolute_url(page_url, attribute(card.select_one("a.nspImageWrapper"), "href"))
        title = node_text(card.select_one("h2"))
        schedule_text = node_text(card.select_one(".nspInfo"))
        if not event_url or not title or not schedule_text:
            log_skipped_card(source=source, reason="нет ссылки, названия или даты")
            continue

        path = url_path(event_url)
        slug_match = _SLUG_PATTERN.search(path)
        city_slug: str | None = None
        date_text = schedule_text
        if slug_match is not None:
            day, month, year, city_slug = slug_match.groups()
            time_match = _TIME_PATTERN.search(schedule_text)
            date_text = f"{day}.{month}.{year} {time_match.group(1)}" if time_match else f"{day}.{month}.{year}"

        text_blocks = [
            text
            for paragraph in card.select("p")
            if paragraph.select_one("button, img") is None
            and "nspInfo" not in (attribute(paragraph, "class") or "")
            and (text := node_text(paragraph))
        ]
        genre = text_blocks[0] if text_blocks else None
        description = " ".join(text_blocks[1:]) or None

        items.append(
            build_item(
                source=source,
                external_identifier=path,
                source_url=event_url,
                raw_title=title,
                raw_description=description,
                raw_date_text=date_text,
                raw_city=city_slug,
                raw_category=genre,
                raw_age_text=node_text(card.select_one("h5")),
                organization_link=event_url,
                payload={
                    "title": title,
                    "schedule": schedule_text,
                    "path": path,
                    "genre": genre,
                    "kassy_event_identifier": attribute(card.select_one("button.button-buy"), "data-kassy-event_id"),
                },
            )
        )
    return tuple(items)
