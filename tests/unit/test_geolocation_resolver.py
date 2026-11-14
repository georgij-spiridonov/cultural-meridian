"""Модульные тесты гибридного адаптера геолокации `HybridGeolocationResolver`."""

import json
from collections.abc import Callable

import httpx
import pytest
import yaml

from src.core.configuration import PROJECT_ROOT, GeolocationSettings
from src.core.exceptions import NetworkConnectionFailureError
from src.domain.entities import CityReference, Coordinates, VenueReference
from src.domain.services import estimate_urban_travel
from src.infrastructure.geolocation import HybridGeolocationResolver

_VENUES_YAML_PATH = PROJECT_ROOT / "configuration" / "venues_coordinates.yaml"


class _GeoEventRepositoryStub:
    """Минимальный стаб `EventRepositoryPort` для проверки интеграции справочников из БД."""

    def __init__(
        self,
        *,
        cities: tuple[CityReference, ...] = (),
        venues: tuple[VenueReference, ...] = (),
        fail_with: Exception | None = None,
    ) -> None:
        self._cities = cities
        self._venues = venues
        self._fail_with = fail_with
        self.cities_calls = 0
        self.venues_calls = 0

    async def list_cities(self) -> tuple[CityReference, ...]:
        """Возвращает настроенный справочник городов или выбрасывает ошибку."""
        self.cities_calls += 1
        if self._fail_with is not None:
            raise self._fail_with
        return self._cities

    async def list_venues(self, *, city: str | None = None) -> tuple[VenueReference, ...]:
        """Возвращает настроенный справочник площадок или выбрасывает ошибку."""
        del city
        self.venues_calls += 1
        if self._fail_with is not None:
            raise self._fail_with
        return self._venues


def _make_settings(
    *, external_enabled: bool = False, cache_max_entries: int = 128, speed_kmh: float = 25.0, overhead_minutes: int = 7
) -> GeolocationSettings:
    return GeolocationSettings(
        _env_file=None,
        VENUES_CATALOG_PATH=_VENUES_YAML_PATH,
        EXTERNAL_ENABLED=external_enabled,
        CACHE_MAX_ENTRIES=cache_max_entries,
        AVERAGE_URBAN_SPEED_KM_PER_HOUR=speed_kmh,
        TRANSFER_OVERHEAD_MINUTES=overhead_minutes,
    )


async def test_resolve_all_18_cities_and_aliases_from_yaml() -> None:
    """Проверяет разрешение центров всех 18 городов, их падежей, алиасов, регистра и пробелов."""
    payload = yaml.safe_load(_VENUES_YAML_PATH.read_text(encoding="utf-8"))
    cities = payload["cities"]
    assert len(cities) == 18

    resolver = HybridGeolocationResolver(settings=_make_settings(external_enabled=False))

    for city_entry in cities:
        expected_coords = Coordinates(
            latitude=float(city_entry["center"]["latitude"]), longitude=float(city_entry["center"]["longitude"])
        )
        canonical_name: str = city_entry["name"]

        resolved_canonical = await resolver.resolve_city_center(city=canonical_name)
        assert resolved_canonical == expected_coords

        resolved_noisy = await resolver.resolve_city_center(city=f"  {canonical_name.upper()}  ")
        assert resolved_noisy == expected_coords

        for alias in city_entry["aliases"]:
            assert await resolver.resolve_city_center(city=alias) == expected_coords
            assert await resolver.resolve_city_center(city=f" {alias.upper()} ") == expected_coords

    assert await resolver.resolve_city_center(city="Уфе") == Coordinates(latitude=54.7431, longitude=55.9678)
    assert await resolver.resolve_city_center(city="  СТЕРЛИТАМАКЕ  ") == Coordinates(
        latitude=53.6246, longitude=55.9501
    )
    assert await resolver.resolve_city_center(city="   ") is None
    assert await resolver.resolve_city_center(city="НесуществующийГородXYZ") is None


async def test_resolve_venue_coordinates_from_yaml_and_fuzzy_aliases() -> None:
    """Проверяет поиск координат площадок в YAML по имени, алиасам, нечёткому и скобочному виду."""
    resolver = HybridGeolocationResolver(settings=_make_settings(external_enabled=False))

    bashopera_coords = await resolver.resolve_venue_coordinates(
        city="Уфа", venue="Башкирский государственный театр оперы и балета"
    )
    assert bashopera_coords == Coordinates(latitude=54.722222, longitude=55.945)

    assert await resolver.resolve_venue_coordinates(city="уфе", venue="  бгтоиб ") == bashopera_coords
    assert await resolver.resolve_venue_coordinates(city="Уфа", venue="Башопера") == bashopera_coords
    assert (
        await resolver.resolve_venue_coordinates(
            city="Уфа", venue="Башкирский государственный театр оперы и балета (Большой зал)"
        )
        == bashopera_coords
    )

    sterlitamak_coords = await resolver.resolve_venue_coordinates(city="Стерлитамаке", venue="СГТКО")
    assert sterlitamak_coords == Coordinates(latitude=53.6339, longitude=55.9404)

    beloretsk_coords = await resolver.resolve_venue_coordinates(city="Белорецк", venue="ГДК Белорецк")
    assert beloretsk_coords == Coordinates(latitude=53.9668, longitude=58.3979)

    assert await resolver.resolve_venue_coordinates(city="Уфа", venue="   ") is None
    assert await resolver.resolve_venue_coordinates(city="Уфа", venue="Совершенно Неизвестная Площадка 404") is None


async def test_resolve_coordinates_via_repository_mock_with_ttl_cache_and_failure_fallback() -> None:
    """Проверяет приоритет БД-справочника, TTL-кэш репозитория и деградацию при ошибке БД."""
    db_city = CityReference(
        name="Агидель",
        normalized_name="агидель",
        aliases=("агидели",),
        center_coordinates=Coordinates(latitude=55.8992, longitude=53.9367),
    )
    db_venue = VenueReference(
        name="Дворец культуры Агидель",
        normalized_name="дворец культуры агидель",
        city="Агидель",
        address="ул. Академика Курчатова, 7",
        aliases=("ДК Агидель",),
        coordinates=Coordinates(latitude=55.9011, longitude=53.9388),
    )
    override_ufa_venue = VenueReference(
        name="Кинотеатр «Родина»",
        normalized_name="кинотеатр родина",
        city="Уфа",
        address="ул. Ленина, 42",
        aliases=(),
        coordinates=Coordinates(latitude=54.7285, longitude=55.9499),
    )

    repo = _GeoEventRepositoryStub(cities=(db_city,), venues=(db_venue, override_ufa_venue))
    resolver = HybridGeolocationResolver(
        settings=_make_settings(external_enabled=False),
        event_repository=repo,  # type: ignore[arg-type]
        repository_cache_ttl_seconds=60.0,
    )

    assert await resolver.resolve_city_center(city="в Агидели") == db_city.center_coordinates
    assert await resolver.resolve_venue_coordinates(city="Агидель", venue="ДК Агидель") == db_venue.coordinates
    assert await resolver.resolve_venue_coordinates(city="Уфа", venue="Родина") == override_ufa_venue.coordinates
    # Повторные вызовы в пределах TTL не должны повторно опрашивать репозиторий
    assert repo.cities_calls == 1
    assert repo.venues_calls == 1

    # При ошибке БД адаптер безопасно возвращается к YAML без исключения
    failing_repo = _GeoEventRepositoryStub(fail_with=NetworkConnectionFailureError("PostgreSQL недоступен"))
    resilient_resolver = HybridGeolocationResolver(
        settings=_make_settings(external_enabled=False),
        event_repository=failing_repo,  # type: ignore[arg-type]
    )
    assert await resilient_resolver.resolve_city_center(city="Уфа") == Coordinates(latitude=54.7431, longitude=55.9678)
    assert await resilient_resolver.resolve_venue_coordinates(city="Уфа", venue="Башопера") == Coordinates(
        latitude=54.722222, longitude=55.945
    )


async def test_external_geocoder_success_and_lru_cache_hit() -> None:
    """Проверяет успешное внешнее геокодирование через `MockTransport` и кэширование повторных вызовов."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/search":
            query = request.url.params.get("q", "")
            if "Приютово" in query:
                return httpx.Response(200, json=[{"lat": "53.9167", "lon": "54.0500"}])
            return httpx.Response(200, json=[{"lat": "54.7401", "lon": "55.9802"}])
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = HybridGeolocationResolver(settings=_make_settings(external_enabled=True), http_client=client)

        city_coords_first = await resolver.resolve_city_center(city="Приютово")
        city_coords_second = await resolver.resolve_city_center(city="  приютово ")
        assert city_coords_first == Coordinates(latitude=53.9167, longitude=54.05)
        assert city_coords_second == city_coords_first
        assert len(calls) == 1

        venue_coords_first = await resolver.resolve_venue_coordinates(
            city="Уфа", venue="Арт-центр Маяк", address="ул. Энтузиастов, 10"
        )
        venue_coords_second = await resolver.resolve_venue_coordinates(
            city="Уфе", venue="  АРТ-ЦЕНТР   МАЯК ", address="УЛ. ЭНТУЗИАСТОВ, 10"
        )
        assert venue_coords_first == Coordinates(latitude=54.7401, longitude=55.9802)
        assert venue_coords_second == venue_coords_first
        assert len(calls) == 2


async def test_external_router_success_cache_hit_and_same_point_shortcut() -> None:
    """Проверяет расчёт маршрута через OSRM `MockTransport`, кэш и обход сети для одинаковой точки."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 4200.0, "duration": 600.0}]})

    origin = Coordinates(latitude=54.722222, longitude=55.945)
    destination = Coordinates(latitude=54.757017, longitude=56.00164)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = HybridGeolocationResolver(settings=_make_settings(external_enabled=True), http_client=client)

        same_point = await resolver.estimate_travel(origin=origin, destination=origin)
        assert same_point.travel_minutes == 0
        assert same_point.used_external_service is False
        assert len(calls) == 0

        first = await resolver.estimate_travel(origin=origin, destination=destination)
        second = await resolver.estimate_travel(origin=origin, destination=destination)
        assert first.distance_kilometers == 4.2
        assert first.travel_minutes == 10
        assert first.used_external_service is True
        assert second == first
        assert len(calls) == 1


@pytest.mark.parametrize(
    "failure_handler",
    [
        lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("Таймаут внешнего сервиса", request=req)),
        lambda req: (_ for _ in ()).throw(httpx.ConnectError("Сеть недоступна", request=req)),
        lambda _req: httpx.Response(500, text="Internal Server Error"),
        lambda _req: httpx.Response(200, content=b"{invalid-json", headers={"Content-Type": "application/json"}),
        lambda _req: httpx.Response(200, json={"code": "NoRoute", "routes": []}),
    ],
    ids=["read_timeout", "connect_error", "http_500", "invalid_json", "malformed_osrm_payload"],
)
async def test_external_service_failures_degrade_safely_without_exceptions(
    failure_handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Проверяет изоляцию всех отказов геосервиса и гарантированный откат на гаверсинус/YAML."""
    origin = Coordinates(latitude=54.722222, longitude=55.945)
    destination = Coordinates(latitude=54.799722, longitude=56.033333)
    expected_haversine = estimate_urban_travel(
        origin=origin, destination=destination, average_speed_kilometers_per_hour=25.0, transfer_overhead_minutes=7
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(failure_handler)) as client:
        resolver = HybridGeolocationResolver(settings=_make_settings(external_enabled=True), http_client=client)

        # Известная площадка из YAML продолжает определяться даже при полном отказе внешнего геокодера
        known_coords = await resolver.resolve_venue_coordinates(city="Уфа", venue="Башопера")
        assert known_coords == Coordinates(latitude=54.722222, longitude=55.945)

        # Неизвестная площадка и город не выбрасывают исключений
        assert await resolver.resolve_venue_coordinates(city="Уфа", venue="Неизвестная точка 999") is None
        assert await resolver.resolve_city_center(city="НеизвестныйПосёлок999") is None

        # Маршрутизатор откатывается на детерминированный гаверсинус с used_external_service=False
        estimate = await resolver.estimate_travel(origin=origin, destination=destination)
        assert estimate == expected_haversine
        assert estimate.used_external_service is False
        assert estimate.travel_minutes > 0


async def test_resolver_close_lifecycle_for_owned_and_external_http_client() -> None:
    """Проверяет корректное и идемпотентное закрытие только собственного `httpx.AsyncClient`."""
    external_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, content=json.dumps([])))
    )
    try:
        resolver_with_external = HybridGeolocationResolver(
            settings=_make_settings(external_enabled=True), http_client=external_client
        )
        await resolver_with_external.resolve_city_center(city="НеизвестныйГород")
        await resolver_with_external.close()
        assert external_client.is_closed is False
    finally:
        await external_client.aclose()

    resolver_owned = HybridGeolocationResolver(settings=_make_settings(external_enabled=True))
    await resolver_owned.close()  # до ленивого создания клиента — без ошибок

    def _mock_transport(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=[{"lat": "54.75", "lon": "55.97"}])

    # Инициализируем внутренний клиент и подменяем его транспорт на MockTransport без выхода в сеть
    internal_client = resolver_owned._get_or_create_http_client()
    await internal_client.aclose()
    resolver_owned._http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_transport))
    resolver_owned._owns_http_client = True

    resolved = await resolver_owned.resolve_city_center(city="НовыйГород")
    assert resolved == Coordinates(latitude=54.75, longitude=55.97)
    created_client = resolver_owned._http_client
    assert created_client is not None and not created_client.is_closed

    await resolver_owned.close()
    assert created_client.is_closed is True
    assert resolver_owned._http_client is None
    await resolver_owned.close()  # повторный close() идемпотентен
