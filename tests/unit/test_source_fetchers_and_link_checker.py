"""Модульные тесты реестра источников, загрузчиков RSS/JSON/HTML и проверщика ссылок."""

from pathlib import Path

import httpx
import pytest

from src.core.configuration import PROJECT_ROOT
from src.core.exceptions import (
    AccessPermissionDeniedError,
    NetworkConnectionTimeoutError,
    ServiceRateLimitExceededError,
    ServiceUnavailableError,
    ValidationException,
    ValidationInputError,
    ValidationSchemaError,
)
from src.core.types import EventCategory, SourceType
from src.domain.entities import SourceDefinition
from src.domain.exceptions import UnsupportedSourceFormatError
from src.infrastructure.sources import (
    CompositeSourceFetcher,
    HtmlSourceFetcher,
    HttpLinkChecker,
    JsonSourceFetcher,
    RssSourceFetcher,
    load_sources_registry,
)


def _make_source(
    *, name: str = "test_source", source_type: SourceType, source_url: str, timeout: float = 2.0
) -> SourceDefinition:
    """Создаёт описание источника для изолированных модульных тестов."""
    return SourceDefinition(
        name=name,
        source_type=source_type,
        source_url=source_url,
        default_city="Уфа",
        default_category=EventCategory.CONCERT,
        is_enabled=True,
        request_timeout_seconds=timeout,
    )


def test_load_sources_registry_real_yaml_and_filtering() -> None:
    """Проверяет строгую схему `configuration/sources.yaml` и фильтрацию `only_enabled`."""
    config_path = PROJECT_ROOT / "configuration" / "sources.yaml"

    all_sources = load_sources_registry(config_path, only_enabled=False)
    enabled_sources = load_sources_registry(config_path, only_enabled=True)

    assert len(all_sources) == 8
    assert len(enabled_sources) == 4
    assert [source.name for source in enabled_sources] == [
        "ufa_culture_rss",
        "sterlitamak_afisha_api",
        "salavat_theaters_html",
        "neftekamsk_philharmonic_api",
    ]
    assert all(source.is_enabled for source in enabled_sources)
    assert any(not source.is_enabled for source in all_sources)


def test_load_sources_registry_rejects_invalid_yaml_schema_and_paths(tmp_path: Path) -> None:
    """Проверяет ошибки валидации YAML, схемы, неподдерживаемого типа и выхода за корень."""
    missing_path = tmp_path / "missing.yaml"
    with pytest.raises(ValidationInputError):
        load_sources_registry(missing_path, project_root=tmp_path)

    broken_yaml_path = tmp_path / "broken.yaml"
    broken_yaml_path.write_text("sources: [\n  - name: bad", encoding="utf-8")
    with pytest.raises(ValidationSchemaError):
        load_sources_registry(broken_yaml_path, project_root=tmp_path)

    extra_field_yaml = tmp_path / "extra_field.yaml"
    extra_field_yaml.write_text(
        """
sources:
  - name: bad_extra
    source_type: rss
    source_url: feed.xml
    unknown_field: true
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValidationSchemaError):
        load_sources_registry(extra_field_yaml, project_root=tmp_path)

    duplicate_name_yaml = tmp_path / "duplicate.yaml"
    duplicate_name_yaml.write_text(
        """
sources:
  - name: same_name
    source_type: rss
    source_url: feed1.xml
  - name: same_name
    source_type: api
    source_url: feed2.json
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValidationSchemaError):
        load_sources_registry(duplicate_name_yaml, project_root=tmp_path)

    unsupported_type_yaml = tmp_path / "unsupported_type.yaml"
    unsupported_type_yaml.write_text(
        """
sources:
  - name: unsupported
    source_type: telegram
    source_url: https://example.org/feed
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(UnsupportedSourceFormatError):
        load_sources_registry(unsupported_type_yaml, project_root=tmp_path)

    traversal_yaml = tmp_path / "traversal.yaml"
    traversal_yaml.write_text(
        """
sources:
  - name: escape
    source_type: api
    source_url: ../outside_secret.json
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(AccessPermissionDeniedError):
        load_sources_registry(traversal_yaml, project_root=tmp_path)


async def test_composite_fetcher_reads_real_local_feeds() -> None:
    """Проверяет чтение всех активных локальных фидов из `data/local_feeds/*`."""
    enabled_sources = load_sources_registry(PROJECT_ROOT / "configuration" / "sources.yaml", only_enabled=True)
    fetcher = CompositeSourceFetcher(project_root=PROJECT_ROOT)
    try:
        items_by_source = {source.name: await fetcher.fetch_items(source=source) for source in enabled_sources}
    finally:
        await fetcher.close()

    ufa_items = items_by_source["ufa_culture_rss"]
    assert len(ufa_items) == 4
    assert ufa_items[0].external_identifier == "ufa-rss-2026-12-05-symphony"
    assert ufa_items[0].raw_city == "Уфа"
    assert ufa_items[0].source_url == "data/local_feeds/cards/ufa_symphony_evening.html"
    assert any("Игнорируй предыдущие инструкции" in (item.raw_description or "") for item in ufa_items)

    str_items = items_by_source["sterlitamak_afisha_api"]
    assert len(str_items) == 4
    canceled = next(item for item in str_items if item.external_identifier == "str-api-2026-12-14-canceled-romances")
    assert canceled.raw_payload.get("status") == "canceled"

    salavat_items = items_by_source["salavat_theaters_html"]
    assert len(salavat_items) == 2
    assert any(item.source_url == "data/local_feeds/missing_event_404.html" for item in salavat_items)

    neftekamsk_items = items_by_source["neftekamsk_philharmonic_api"]
    assert len(neftekamsk_items) == 3
    assert any(item.external_identifier == "nfk-api-2026-12-incomplete-excursion" for item in neftekamsk_items)


async def test_http_fetchers_parse_valid_responses_via_mock_transport() -> None:
    """Проверяет HTTP-загрузку RSS, JSON и HTML (JSON-LD + карточки) через `httpx.MockTransport`."""
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <guid>rss-1</guid>
      <title>Концерт оркестра (6+)</title>
      <link>/events/orchestra</link>
      <date_start>2026-12-15T19:00:00+05:00</date_start>
      <city>Уфа</city>
      <venue>ГКЗ «Башкортостан»</venue>
      <price>600 ₽</price>
      <description>Вечерняя симфоническая программа.</description>
    </item>
  </channel>
</rss>"""
    json_body = """{
  "events": [
    {
      "id": "json-1",
      "title": "Выставка графики",
      "url": "/exhibitions/graphics",
      "date_start": "2026-12-16T11:00:00+05:00",
      "city": "Стерлитамак",
      "venue": "Картинная галерея",
      "is_free": true,
      "age_limit": 0
    }
  ]
}"""
    html_body = """<!DOCTYPE html>
<html lang="ru">
<head>
  <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "MusicEvent",
      "identifier": "html-ld-1",
      "name": "Джазовый фестиваль",
      "url": "/events/jazz-fest",
      "startDate": "2026-12-18T19:30:00+05:00",
      "location": {
        "@type": "Place",
        "name": "Арт-Квадрат",
        "address": {"addressLocality": "Уфа", "streetAddress": "ул. Чернышевского, 88"}
      },
      "offers": {"price": "900", "url": "https://tickets.example.org/jazz"}
    }
  </script>
</head>
<body>
  <article class="event-card canceled" data-id="html-card-2" data-url="/events/canceled-play">
    <h2>Отменённый спектакль</h2>
    <time datetime="2026-12-19T18:00:00+05:00">19 декабря</time>
    <span class="city">Салават</span>
    <span class="venue">Драмтеатр</span>
    <span class="price">500 ₽</span>
  </article>
</body>
</html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rss.xml":
            return httpx.Response(200, text=rss_xml, headers={"content-type": "application/xml"})
        if request.url.path == "/events.json":
            return httpx.Response(200, text=json_body, headers={"content-type": "application/json"})
        if request.url.path == "/afisha.html":
            return httpx.Response(200, text=html_body, headers={"content-type": "text/html"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    composite = CompositeSourceFetcher(transport=transport)
    try:
        rss_items = await composite.fetch_items(
            source=_make_source(source_type=SourceType.RSS, source_url="https://afisha.example.org/rss.xml")
        )
        json_items = await composite.fetch_items(
            source=_make_source(source_type=SourceType.API, source_url="https://afisha.example.org/events.json")
        )
        html_items = await composite.fetch_items(
            source=_make_source(source_type=SourceType.WEBPAGE, source_url="https://afisha.example.org/afisha.html")
        )
    finally:
        await composite.close()

    assert len(rss_items) == 1
    assert rss_items[0].source_url == "https://afisha.example.org/events/orchestra"
    assert rss_items[0].raw_price_text == "600 ₽"

    assert len(json_items) == 1
    assert json_items[0].source_url == "https://afisha.example.org/exhibitions/graphics"
    assert json_items[0].raw_price_text == "Бесплатно"
    assert json_items[0].raw_age_text == "0+"

    assert len(html_items) == 2
    assert html_items[0].external_identifier == "html-ld-1"
    assert html_items[0].raw_city == "Уфа"
    assert html_items[0].organization_link == "https://tickets.example.org/jazz"
    assert html_items[1].external_identifier == "html-card-2"
    assert html_items[1].raw_payload.get("status") == "canceled"


@pytest.mark.parametrize(
    ("fetcher_factory", "source_type"),
    [(RssSourceFetcher, SourceType.RSS), (JsonSourceFetcher, SourceType.API), (HtmlSourceFetcher, SourceType.WEBPAGE)],
)
async def test_http_fetchers_map_timeout_and_http_errors(
    fetcher_factory: type[RssSourceFetcher | JsonSourceFetcher | HtmlSourceFetcher], source_type: SourceType
) -> None:
    """Проверяет маппинг таймаута, HTTP 500 и HTTP 429 в доменные исключения."""
    source = _make_source(source_type=source_type, source_url="https://afisha.example.org/feed")

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Timed out", request=request)

    timeout_fetcher = fetcher_factory(transport=httpx.MockTransport(timeout_handler))
    try:
        with pytest.raises(NetworkConnectionTimeoutError):
            await timeout_fetcher.fetch_items(source=source)
    finally:
        await timeout_fetcher.close()

    server_error_fetcher = fetcher_factory(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500, text="Internal Error"))
    )
    try:
        with pytest.raises(ServiceUnavailableError):
            await server_error_fetcher.fetch_items(source=source)
    finally:
        await server_error_fetcher.close()

    rate_limit_fetcher = fetcher_factory(
        transport=httpx.MockTransport(lambda _request: httpx.Response(429, text="Too Many Requests"))
    )
    try:
        with pytest.raises(ServiceRateLimitExceededError):
            await rate_limit_fetcher.fetch_items(source=source)
    finally:
        await rate_limit_fetcher.close()


async def test_fetchers_raise_validation_or_format_errors_on_corrupted_payloads() -> None:
    """Проверяет реакцию RSS/JSON/HTML-загрузчиков на повреждённый XML, JSON и HTML."""
    broken_rss = RssSourceFetcher(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="<rss><channel><item><title>broken"))
    )
    try:
        with pytest.raises(ValidationException):
            await broken_rss.fetch_items(
                source=_make_source(source_type=SourceType.RSS, source_url="https://example.org/bad.xml")
            )
    finally:
        await broken_rss.close()

    broken_json = JsonSourceFetcher(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="{broken json"))
    )
    try:
        with pytest.raises(ValidationException):
            await broken_json.fetch_items(
                source=_make_source(source_type=SourceType.API, source_url="https://example.org/bad.json")
            )
    finally:
        await broken_json.close()

    json_instead_of_html = HtmlSourceFetcher(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text='{"items": []}'))
    )
    try:
        with pytest.raises(UnsupportedSourceFormatError):
            await json_instead_of_html.fetch_items(
                source=_make_source(source_type=SourceType.WEBPAGE, source_url="https://example.org/wrong.html")
            )
    finally:
        await json_instead_of_html.close()

    plain_text_html = HtmlSourceFetcher(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="plain text without tags"))
    )
    try:
        with pytest.raises(ValidationException):
            await plain_text_html.fetch_items(
                source=_make_source(source_type=SourceType.WEBPAGE, source_url="https://example.org/plain.html")
            )
    finally:
        await plain_text_html.close()

    broken_ld_json_html = HtmlSourceFetcher(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, text='<html><head><script type="application/ld+json">{bad</script></head><body></body></html>'
            )
        )
    )
    try:
        with pytest.raises(ValidationException):
            await broken_ld_json_html.fetch_items(
                source=_make_source(source_type=SourceType.WEBPAGE, source_url="https://example.org/bad-ld.html")
            )
    finally:
        await broken_ld_json_html.close()


async def test_http_link_checker_http_and_local_scenarios() -> None:
    """Проверяет 200, 405->GET fallback, 404, таймаут и локальные пути с `#fragment`."""
    recorded_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_methods.append(request.method)
        path = request.url.path
        if path == "/ok":
            return httpx.Response(200)
        if path == "/head-405":
            if request.method == "HEAD":
                return httpx.Response(405)
            return httpx.Response(200)
        if path == "/not-found":
            return httpx.Response(404)
        if path == "/timeout":
            raise httpx.ReadTimeout("Link check timeout", request=request)
        return httpx.Response(500)

    checker = HttpLinkChecker(transport=httpx.MockTransport(handler), project_root=PROJECT_ROOT)
    try:
        ok_result = await checker.check_link(target_url="https://afisha.example.org/ok")
        assert ok_result.is_reachable is True
        assert ok_result.http_status_code == 200
        assert ok_result.error_reason is None

        recorded_methods.clear()
        fallback_result = await checker.check_link(target_url="https://afisha.example.org/head-405")
        assert fallback_result.is_reachable is True
        assert fallback_result.http_status_code == 200
        assert recorded_methods == ["HEAD", "GET"]

        not_found_result = await checker.check_link(target_url="https://afisha.example.org/not-found")
        assert not_found_result.is_reachable is False
        assert not_found_result.http_status_code == 404
        assert not_found_result.error_reason is not None

        timeout_result = await checker.check_link(target_url="https://afisha.example.org/timeout")
        assert timeout_result.is_reachable is False
        assert timeout_result.http_status_code is None
        assert timeout_result.error_reason is not None

        local_ok = await checker.check_link(target_url="data/local_feeds/cards/ufa_symphony_evening.html#details")
        assert local_ok.is_reachable is True
        assert local_ok.http_status_code == 200

        local_missing = await checker.check_link(target_url="data/local_feeds/missing_event_404.html")
        assert local_missing.is_reachable is False
        assert local_missing.http_status_code == 404

        local_traversal = await checker.check_link(target_url="../../outside_project.html")
        assert local_traversal.is_reachable is False
        assert local_traversal.http_status_code == 403
    finally:
        await checker.close()
