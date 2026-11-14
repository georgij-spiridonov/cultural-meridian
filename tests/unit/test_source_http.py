"""Модульные тесты вежливого HTTP-клиента, его использования загрузчиками и реестра именных парсеров без сети."""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from src.core.configuration import PROJECT_ROOT
from src.core.exceptions import AccessPermissionDeniedError, ServiceUnavailableError, ValidationSchemaError
from src.core.types import SourceType
from src.domain.entities import SourceDefinition
from src.infrastructure.sources import (
    HtmlSourceFetcher,
    HttpLinkChecker,
    PoliteHttpClient,
    load_parser_configurations,
    load_sources_registry,
)
from src.infrastructure.sources import http_client as http_client_module

type Handler = Callable[[httpx.Request], httpx.Response]

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "sources"


@pytest.fixture(autouse=True)
def _disable_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Убирает паузы между повторами, чтобы тесты шли быстро."""
    monkeypatch.setattr(http_client_module, "_RETRY_BASE_DELAY_SECONDS", 0.0)


def _client(handler: Handler) -> PoliteHttpClient:
    """Создаёт вежливый клиент поверх подменённого транспорта без пауз между запросами."""
    return PoliteHttpClient(
        user_agent="test-agent/1.0", request_delay_seconds=0.0, transport=httpx.MockTransport(handler)
    )


async def test_fetch_text_respects_robots_disallow() -> None:
    """Проверяет, что запрещённый в robots.txt путь не загружается, а robots.txt запрашивается один раз."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /afisha/feed\n")
        return httpx.Response(200, text="<html></html>")

    client = _client(handler)
    try:
        assert await client.fetch_text("https://site.ru/afisha/", timeout_seconds=1.0) == "<html></html>"
        with pytest.raises(AccessPermissionDeniedError):
            await client.fetch_text("https://site.ru/afisha/feed", timeout_seconds=1.0)
    finally:
        await client.close()
    assert requested.count("/robots.txt") == 1
    assert "/afisha/feed" not in requested


async def test_robots_returning_html_allows_crawling() -> None:
    """Проверяет случай bashopera.ru: вместо robots.txt отдаётся HTML главной страницы."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<!DOCTYPE html><html><body>Главная</body></html>")

    client = _client(handler)
    try:
        assert await client.is_allowed("https://site.ru/afisha/") is True
    finally:
        await client.close()


async def test_robots_forbidden_status_disallows_crawling() -> None:
    """Проверяет, что ответ 403 на robots.txt запрещает обход."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    client = _client(handler)
    try:
        assert await client.is_allowed("https://site.ru/afisha/") is False
    finally:
        await client.close()


async def test_request_retries_server_errors_then_raises() -> None:
    """Проверяет два повтора при ответе 5xx и итоговую ошибку доступности."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    client = _client(handler)
    try:
        with pytest.raises(ServiceUnavailableError):
            await client.request("GET", "https://site.ru/afisha/", timeout_seconds=1.0)
    finally:
        await client.close()
    assert attempts == 3


async def test_request_recovers_after_transient_error() -> None:
    """Проверяет успешный ответ после временного сбоя соединения."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("сбой", request=request)
        return httpx.Response(200, text="ok")

    client = _client(handler)
    try:
        response = await client.request("GET", "https://site.ru/", timeout_seconds=1.0)
    finally:
        await client.close()
    assert response.text == "ok"
    assert attempts == 2


async def test_html_fetcher_does_not_load_page_disallowed_by_robots() -> None:
    """Проверяет, что загрузчик источника не открывает страницу, запрещённую robots.txt."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /afisha/\n")
        return httpx.Response(200, text="<html><body></body></html>")

    fetcher = HtmlSourceFetcher(polite_client=_client(handler))
    source = SourceDefinition(name="closed", source_type=SourceType.WEBPAGE, source_url="https://site.ru/afisha/")
    with pytest.raises(AccessPermissionDeniedError):
        await fetcher.fetch_items(source=source)
    assert requested == ["/robots.txt"]


async def test_html_fetcher_routes_page_to_named_site_parser(tmp_path: Path) -> None:
    """Проверяет, что источник с полем `parser` разбирается именным парсером сайта, а не общим разбором."""
    sources_yaml = tmp_path / "sources.yaml"
    sources_yaml.write_text(
        "sources:\n"
        "  - name: rusdramteatr-sterlitamak\n"
        "    source_type: webpage\n"
        "    source_url: https://rusdramteatr.ru/afisha\n"
        "    parser:\n"
        "      name: rusdramteatr\n",
        encoding="utf-8",
    )
    page = (_FIXTURES / "rusdramteatr_afisha.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    fetcher = HtmlSourceFetcher(polite_client=_client(handler), sources_config_path=sources_yaml, project_root=tmp_path)
    source = load_sources_registry(sources_yaml, project_root=tmp_path)[0]
    items = await fetcher.fetch_items(source=source)

    assert len(items) == 21
    assert items[0].external_identifier == "/afisha/makbet-14-11-2026"


@pytest.mark.parametrize(
    ("head_status", "get_status", "expected"),
    [(200, None, True), (403, 200, True), (405, 200, True), (403, 404, False), (404, None, False)],
)
async def test_link_checker_uses_head_and_falls_back_to_get(
    head_status: int, get_status: int | None, expected: bool
) -> None:
    """Проверяет HEAD-запрос, повтор через GET при 403/405 и признак доступности."""
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(head_status)
        assert get_status is not None
        return httpx.Response(get_status)

    checker = HttpLinkChecker(polite_client=_client(handler))
    result = await checker.check_link(target_url="https://site.ru/event/1")

    assert result.is_reachable is expected
    assert methods == (["HEAD"] if get_status is None else ["HEAD", "GET"])


def test_project_registry_declares_named_parsers_for_real_sites() -> None:
    """Проверяет, что у пяти реальных сайтов реестра проекта заданы именные парсеры и площадки по умолчанию."""
    config_path = PROJECT_ROOT / "configuration" / "sources.yaml"
    parsers = load_parser_configurations(config_path)
    sources = {source.name: source for source in load_sources_registry(config_path)}

    assert {configuration.parser for configuration in parsers.values()} == {
        "wordpress_events",
        "quicktickets",
        "rusdramteatr",
        "bashopera",
    }
    assert len(parsers) == 5
    assert all(sources[name].default_venue for name in parsers)


def test_registry_rejects_unknown_parser_and_parser_on_non_html_source(tmp_path: Path) -> None:
    """Проверяет отказ на неизвестном имени парсера и на парсере у RSS-источника."""
    unknown = tmp_path / "unknown.yaml"
    unknown.write_text(
        "sources:\n  - name: x\n    source_type: webpage\n    source_url: https://x.ru/\n"
        "    parser:\n      name: unknown\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationSchemaError):
        load_sources_registry(unknown, project_root=tmp_path)

    wrong_type = tmp_path / "wrong_type.yaml"
    wrong_type.write_text(
        "sources:\n  - name: x\n    source_type: rss\n    source_url: https://x.ru/rss\n"
        "    parser:\n      name: bashopera\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationSchemaError):
        load_sources_registry(wrong_type, project_root=tmp_path)
