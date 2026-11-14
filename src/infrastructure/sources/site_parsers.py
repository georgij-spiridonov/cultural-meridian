"""Разбор HTML-страниц конкретных сайтов именными парсерами и дозагрузка адресов сеансов."""

import dataclasses
from collections.abc import Callable, Mapping
from typing import Final

from src.core.exceptions import ApplicationException
from src.core.logger import get_logger
from src.domain.entities import FetchedSourceItem, SourceDefinition
from src.infrastructure.sources._html import extract_settlement
from src.infrastructure.sources.bashopera import parse_bashopera_page
from src.infrastructure.sources.http_client import PoliteHttpClient
from src.infrastructure.sources.quicktickets import parse_quicktickets_page, parse_quicktickets_session_address
from src.infrastructure.sources.registry import ParserConfiguration
from src.infrastructure.sources.rusdramteatr import parse_rusdramteatr_page
from src.infrastructure.sources.wordpress_events import parse_wordpress_events_page

__all__ = ["parse_site_page"]

logger = get_logger(__name__)

type PageParser = Callable[..., tuple[FetchedSourceItem, ...]]

_PARSERS: Final[Mapping[str, PageParser]] = {
    "wordpress_events": parse_wordpress_events_page,
    "quicktickets": parse_quicktickets_page,
    "rusdramteatr": parse_rusdramteatr_page,
    "bashopera": parse_bashopera_page,
}
_PAGE_CITY_OPTION: Final[str] = "city"


async def parse_site_page(
    *, html: str, source: SourceDefinition, parser_configuration: ParserConfiguration, http_client: PoliteHttpClient
) -> tuple[FetchedSourceItem, ...]:
    """Разбирает страницу афиши именным парсером сайта.

    Arguments:
        html: Текст страницы.
        source: Описание источника.
        parser_configuration: Имя парсера и его параметры.
        http_client: Вежливый HTTP-клиент для дозагрузки страниц сеансов.

    Returns:
        Кортеж сырых записей.

    Raises:
        ValidationSchemaError: Если на странице не найдено ни одной карточки.
    """
    parse_page = _PARSERS[parser_configuration.parser]
    items = parse_page(html, source=source, page_url=source.source_url, options=parser_configuration.options)
    if parser_configuration.parser == "quicktickets":
        items = await _resolve_quicktickets_locations(
            items, source=source, page_city=parser_configuration.options.get(_PAGE_CITY_OPTION), http_client=http_client
        )
    logger.info("Источник %s: разобрано записей %d.", source.name, len(items))
    return items


async def _resolve_quicktickets_locations(
    items: tuple[FetchedSourceItem, ...],
    *,
    source: SourceDefinition,
    page_city: str | None,
    http_client: PoliteHttpClient,
) -> tuple[FetchedSourceItem, ...]:
    """Уточняет адрес и город сеансов quicktickets.

    Для карточек с «Разными адресами проведения» открывается страница сеанса. Адрес без префикса
    населённого пункта относится к городу страницы из опции `city`. Если город так и не найден,
    он остаётся пустым, и конвейер отправит запись в модерацию.
    """
    resolved: list[FetchedSourceItem] = []
    for item in items:
        address = item.raw_address
        if item.raw_payload.get("address_scope") == "multiple":
            address = await _fetch_session_address(item, source=source, http_client=http_client)
        city = extract_settlement(address) or (page_city if address else None)
        if address == item.raw_address and city == item.raw_city:
            resolved.append(item)
            continue
        resolved.append(
            dataclasses.replace(
                item,
                raw_address=address,
                raw_city=city,
                raw_payload={**item.raw_payload, "address": address, "city": city},
            )
        )
    return tuple(resolved)


async def _fetch_session_address(
    item: FetchedSourceItem, *, source: SourceDefinition, http_client: PoliteHttpClient
) -> str | None:
    """Загружает страницу сеанса и извлекает из неё адрес; при сбое возвращает `None`."""
    try:
        html = await http_client.fetch_text(item.source_url, timeout_seconds=source.request_timeout_seconds)
    except ApplicationException as error:
        logger.warning("Источник %s: адрес сеанса %s не получен (%s).", source.name, item.source_url, error)
        return None
    return parse_quicktickets_session_address(html)
