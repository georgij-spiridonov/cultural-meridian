"""Гибридный адаптер геокодирования площадок и расчёта времени перемещения.

Архитектурный выбор и границы применимости:
- Локальный YAML-справочник (`configuration/venues_coordinates.yaml`) и справочники БД
  обеспечивают детерминированное определение координат без сетевых зависимостей.
- При включённом `GEOLOCATION_EXTERNAL_ENABLED=True` адаптер дополняет справочник
  короткими по таймауту запросами к Nominatim и OSRM с LRU-кэшированием в памяти.
- Любой сбой сети, таймаут или некорректный ответ внешнего геосервиса изолируется:
  адаптер логирует предупреждение и деградирует до локальных данных или гаверсинуса.
"""

import asyncio
import math
import re
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import Final

import httpx
import yaml

from src.core.configuration import GeolocationSettings, get_geolocation_settings
from src.core.exceptions import ApplicationException
from src.core.logger import get_logger
from src.domain.entities import CityReference, Coordinates, TravelEstimate, VenueReference
from src.domain.ports import EventRepositoryPort
from src.domain.services import (
    estimate_urban_travel,
    normalize_city,
    normalize_optional_text,
    normalize_text_for_matching,
    normalize_venue,
)

__all__ = ["HybridGeolocationResolver"]

logger = get_logger(__name__)

_DEFAULT_REPOSITORY_CACHE_TTL_SECONDS: Final[float] = 300.0
_PARENTHETICAL_SUFFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_FUZZY_VENUE_MIN_SIMILARITY: Final[float] = 0.82
_MIN_FUZZY_TEXT_LENGTH: Final[int] = 6

type _GeocodeCacheKey = tuple[str, ...]
type _TravelCacheKey = tuple[float, float, float, float]


def _parse_non_negative_finite_float(value: object) -> float | None:
    """Преобразует числовое значение (исключая `bool`) в конечное неотрицательное число."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0.0:
        return None
    return numeric


def _parse_coordinates(raw_value: object) -> Coordinates | None:
    """Преобразует словарь или строковые поля координат в доменную сущность `Coordinates`."""
    if not isinstance(raw_value, Mapping):
        return None
    raw_latitude = raw_value.get("latitude", raw_value.get("lat"))
    raw_longitude = raw_value.get("longitude", raw_value.get("lon"))

    try:
        latitude = float(raw_latitude) if not isinstance(raw_latitude, bool) and raw_latitude is not None else math.nan
        longitude = (
            float(raw_longitude) if not isinstance(raw_longitude, bool) and raw_longitude is not None else math.nan
        )
    except ValueError, TypeError:
        return None

    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    return Coordinates(latitude=latitude, longitude=longitude)


def _extract_string_tuple(raw_aliases: object) -> tuple[str, ...]:
    """Извлекает кортеж непустых строковых алиасов из YAML-узла."""
    if not isinstance(raw_aliases, Sequence) or isinstance(raw_aliases, str | bytes):
        return ()
    cleaned_aliases: list[str] = []
    for item in raw_aliases:
        if isinstance(item, str):
            cleaned = normalize_optional_text(item)
            if cleaned is not None:
                cleaned_aliases.append(cleaned)
    return tuple(cleaned_aliases)


def _load_yaml_catalog(catalog_path: Path) -> tuple[tuple[CityReference, ...], tuple[VenueReference, ...]]:
    """Загружает и индексирует локальный YAML-справочник городов и площадок.

    Arguments:
        catalog_path: Путь к YAML-файлу справочника координат.

    Returns:
        Кортеж `(cities, venues)` из валидных записей справочника.
    """
    if not catalog_path.is_file():
        logger.warning("Локальный справочник координат не найден: %s", catalog_path)
        return (), ()

    try:
        raw_content = catalog_path.read_text(encoding="utf-8")
        payload = yaml.safe_load(raw_content)
    except (OSError, yaml.YAMLError, ValueError, TypeError) as error:
        logger.warning("Не удалось прочитать YAML-справочник координат %s: %s", catalog_path, error)
        return (), ()

    if not isinstance(payload, Mapping):
        logger.warning("Некорректная корневая структура YAML-справочника координат: %s", catalog_path)
        return (), ()

    raw_cities = payload.get("cities")
    if not isinstance(raw_cities, Sequence) or isinstance(raw_cities, str | bytes):
        return (), ()

    cities: list[CityReference] = []
    venues: list[VenueReference] = []

    for raw_city in raw_cities:
        if not isinstance(raw_city, Mapping):
            continue
        city_name = normalize_optional_text(raw_city.get("name")) if isinstance(raw_city.get("name"), str) else None
        if city_name is None:
            continue
        city_aliases = _extract_string_tuple(raw_city.get("aliases"))
        city_center = _parse_coordinates(raw_city.get("center"))
        cities.append(
            CityReference(
                name=city_name,
                normalized_name=normalize_text_for_matching(city_name),
                aliases=city_aliases,
                center_coordinates=city_center,
            )
        )

        raw_venues = raw_city.get("venues")
        if not isinstance(raw_venues, Sequence) or isinstance(raw_venues, str | bytes):
            continue
        for raw_venue in raw_venues:
            if not isinstance(raw_venue, Mapping):
                continue
            venue_name = (
                normalize_optional_text(raw_venue.get("name")) if isinstance(raw_venue.get("name"), str) else None
            )
            if venue_name is None:
                continue
            raw_address = raw_venue.get("address")
            venue_address = normalize_optional_text(raw_address) if isinstance(raw_address, str) else None
            venue_aliases = _extract_string_tuple(raw_venue.get("aliases"))
            venue_coordinates = _parse_coordinates(raw_venue.get("coordinates"))
            venues.append(
                VenueReference(
                    name=venue_name,
                    normalized_name=normalize_text_for_matching(venue_name),
                    city=city_name,
                    address=venue_address or "",
                    aliases=venue_aliases,
                    coordinates=venue_coordinates,
                )
            )

    return tuple(cities), tuple(venues)


def _merge_city_references(
    primary: Sequence[CityReference], fallback: Sequence[CityReference]
) -> tuple[CityReference, ...]:
    """Объединяет справочники городов из БД и YAML, сохраняя приоритет координат БД."""
    merged: dict[str, CityReference] = {}
    for item in primary:
        key = normalize_text_for_matching(item.name)
        if key:
            merged[key] = item

    for item in fallback:
        key = normalize_text_for_matching(item.name)
        if not key:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            continue
        combined_aliases = tuple(dict.fromkeys((*existing.aliases, *item.aliases)))
        merged[key] = CityReference(
            name=existing.name,
            normalized_name=existing.normalized_name,
            aliases=combined_aliases,
            center_coordinates=existing.center_coordinates or item.center_coordinates,
        )

    return tuple(merged.values())


def _merge_venue_references(
    primary: Sequence[VenueReference], fallback: Sequence[VenueReference]
) -> tuple[VenueReference, ...]:
    """Объединяет справочники площадок из БД и YAML с сохранением приоритета БД."""
    merged: dict[tuple[str, str], VenueReference] = {}
    for item in primary:
        key = (normalize_text_for_matching(item.city), normalize_text_for_matching(item.name))
        if key[1]:
            merged[key] = item

    for item in fallback:
        key = (normalize_text_for_matching(item.city), normalize_text_for_matching(item.name))
        if not key[1]:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            continue
        combined_aliases = tuple(dict.fromkeys((*existing.aliases, *item.aliases)))
        merged[key] = VenueReference(
            name=existing.name,
            normalized_name=existing.normalized_name,
            city=existing.city,
            address=existing.address or item.address,
            aliases=combined_aliases,
            coordinates=existing.coordinates or item.coordinates,
        )

    return tuple(merged.values())


def _find_fuzzy_venue_match(query_venue: str, venues: tuple[VenueReference, ...]) -> VenueReference | None:
    """Выполняет нечёткий и частичный поиск площадки по каноническому названию и алиасам."""
    normalized_query = normalize_text_for_matching(query_venue)
    if not normalized_query:
        return None

    stripped_parens = normalize_text_for_matching(_PARENTHETICAL_SUFFIX_PATTERN.sub(" ", query_venue))
    if stripped_parens and stripped_parens != normalized_query:
        exact_without_parens = normalize_venue(stripped_parens, venues=venues)
        if isinstance(exact_without_parens, VenueReference):
            return exact_without_parens

    query_tokens = set(normalized_query.split())
    best_venue: VenueReference | None = None
    best_score = 0.0

    for venue_ref in venues:
        if venue_ref.coordinates is None:
            continue
        variants = (venue_ref.name, venue_ref.normalized_name, *venue_ref.aliases)
        for variant in variants:
            normalized_variant = normalize_text_for_matching(variant)
            if not normalized_variant:
                continue

            variant_tokens = set(normalized_variant.split())
            if (
                len(variant_tokens) >= 2
                and len(query_tokens) >= 2
                and (variant_tokens.issubset(query_tokens) or query_tokens.issubset(variant_tokens))
            ):
                common_count = len(variant_tokens & query_tokens)
                max_count = max(len(variant_tokens), len(query_tokens))
                containment_ratio = common_count / max_count
                if containment_ratio >= 0.5:
                    score = 0.85 + 0.15 * containment_ratio
                    if score > best_score:
                        best_score = score
                        best_venue = venue_ref
                    continue

            if len(normalized_query) >= _MIN_FUZZY_TEXT_LENGTH and len(normalized_variant) >= _MIN_FUZZY_TEXT_LENGTH:
                similarity = SequenceMatcher(None, normalized_query, normalized_variant).ratio()
                if similarity >= _FUZZY_VENUE_MIN_SIMILARITY and similarity > best_score:
                    best_score = similarity
                    best_venue = venue_ref

    return best_venue


class HybridGeolocationResolver:
    """Гибридный резолвер координат и времени перемещения с локальным справочником и фоллбэком."""

    def __init__(
        self,
        settings: GeolocationSettings | None = None,
        event_repository: EventRepositoryPort | None = None,
        http_client: httpx.AsyncClient | None = None,
        *,
        repository_cache_ttl_seconds: float = _DEFAULT_REPOSITORY_CACHE_TTL_SECONDS,
    ) -> None:
        """Инициализирует адаптер геолокации, загружая локальный YAML-справочник.

        Arguments:
            settings: Настройки геолокации (если не переданы, берутся кэшированные настройки).
            event_repository: Необязательный репозиторий каталога для подхвата справочников из БД.
            http_client: Необязательный внешний экземпляр `httpx.AsyncClient` (например, для тестов).
            repository_cache_ttl_seconds: Время жизни кэша справочников БД в секундах.
        """
        self._settings = settings or get_geolocation_settings()
        self._event_repository = event_repository
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._repository_cache_ttl_seconds = max(0.0, repository_cache_ttl_seconds)

        self._yaml_cities, self._yaml_venues = _load_yaml_catalog(self._settings.VENUES_CATALOG_PATH)

        self._db_cities: tuple[CityReference, ...] = ()
        self._db_venues: tuple[VenueReference, ...] = ()
        self._db_loaded_at_monotonic: float | None = None
        self._db_refresh_lock = asyncio.Lock()

        self._geocode_cache: OrderedDict[_GeocodeCacheKey, Coordinates] = OrderedDict()
        self._travel_cache: OrderedDict[_TravelCacheKey, TravelEstimate] = OrderedDict()

    async def close(self) -> None:
        """Закрывает внутренний `httpx.AsyncClient`, если он был создан самим адаптером."""
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _get_or_create_http_client(self) -> httpx.AsyncClient:
        """Возвращает переданный или лениво созданный асинхронный HTTP-клиент."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._settings.REQUEST_TIMEOUT_SECONDS),
                headers={"User-Agent": self._settings.USER_AGENT},
                follow_redirects=True,
            )
            self._owns_http_client = True
        return self._http_client

    async def _get_reference_catalogs(self) -> tuple[tuple[CityReference, ...], tuple[VenueReference, ...]]:
        """Возвращает объединённые справочники городов и площадок из БД (с TTL-кэшем) и YAML."""
        if self._event_repository is None:
            return self._yaml_cities, self._yaml_venues

        now = time.monotonic()
        if (
            self._db_loaded_at_monotonic is not None
            and (now - self._db_loaded_at_monotonic) < self._repository_cache_ttl_seconds
        ):
            return (
                _merge_city_references(self._db_cities, self._yaml_cities),
                _merge_venue_references(self._db_venues, self._yaml_venues),
            )

        async with self._db_refresh_lock:
            now = time.monotonic()
            if (
                self._db_loaded_at_monotonic is not None
                and (now - self._db_loaded_at_monotonic) < self._repository_cache_ttl_seconds
            ):
                return (
                    _merge_city_references(self._db_cities, self._yaml_cities),
                    _merge_venue_references(self._db_venues, self._yaml_venues),
                )

            try:
                loaded_cities = await self._event_repository.list_cities()
                loaded_venues = await self._event_repository.list_venues()
            except (ApplicationException, RuntimeError, ValueError, OSError) as error:
                logger.warning("Не удалось обновить геосправочники из БД, используется локальный кэш: %s", error)
            else:
                self._db_cities = loaded_cities
                self._db_venues = loaded_venues
                self._db_loaded_at_monotonic = time.monotonic()

        return (
            _merge_city_references(self._db_cities, self._yaml_cities),
            _merge_venue_references(self._db_venues, self._yaml_venues),
        )

    def _get_cached_coordinates(self, cache_key: _GeocodeCacheKey) -> Coordinates | None:
        """Читает координаты из LRU-кэша с обновлением позиции."""
        cached = self._geocode_cache.get(cache_key)
        if cached is not None:
            self._geocode_cache.move_to_end(cache_key)
        return cached

    def _store_cached_coordinates(self, cache_key: _GeocodeCacheKey, coordinates: Coordinates) -> None:
        """Сохраняет координаты в ограниченный LRU-кэш."""
        self._geocode_cache[cache_key] = coordinates
        self._geocode_cache.move_to_end(cache_key)
        while len(self._geocode_cache) > self._settings.CACHE_MAX_ENTRIES:
            self._geocode_cache.popitem(last=False)

    def _get_cached_travel(self, cache_key: _TravelCacheKey) -> TravelEstimate | None:
        """Читает оценку маршрута из LRU-кэша с обновлением позиции."""
        cached = self._travel_cache.get(cache_key)
        if cached is not None:
            self._travel_cache.move_to_end(cache_key)
        return cached

    def _store_cached_travel(self, cache_key: _TravelCacheKey, estimate: TravelEstimate) -> None:
        """Сохраняет оценку маршрута в ограниченный LRU-кэш."""
        self._travel_cache[cache_key] = estimate
        self._travel_cache.move_to_end(cache_key)
        while len(self._travel_cache) > self._settings.CACHE_MAX_ENTRIES:
            self._travel_cache.popitem(last=False)

    def _fallback_travel_estimate(self, *, origin: Coordinates, destination: Coordinates) -> TravelEstimate:
        """Вычисляет детерминированную локальную оценку перемещения по формуле гаверсинуса."""
        return estimate_urban_travel(
            origin=origin,
            destination=destination,
            average_speed_kilometers_per_hour=self._settings.AVERAGE_URBAN_SPEED_KM_PER_HOUR,
            transfer_overhead_minutes=self._settings.TRANSFER_OVERHEAD_MINUTES,
        )

    async def _geocode_external(self, *, query: str) -> Coordinates | None:
        """Выполняет ограниченный по таймауту запрос к Nominatim `/search` с изоляцией сбоев."""
        client = self._get_or_create_http_client()
        url = f"{self._settings.GEOCODER_BASE_URL}/search"
        params = {"q": query, "format": "jsonv2", "limit": "1", "accept-language": "ru"}
        headers = {"User-Agent": self._settings.USER_AGENT}

        try:
            response = await client.get(
                url, params=params, headers=headers, timeout=self._settings.REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError, RuntimeError, OSError, ApplicationException) as error:
            logger.warning("Ошибка внешнего геокодера Nominatim для запроса '%s': %s", query, error)
            return None

        if not isinstance(payload, list) or not payload:
            return None
        return _parse_coordinates(payload[0])

    async def resolve_city_center(self, *, city: str) -> Coordinates | None:
        """Возвращает координаты центра населённого пункта Республики Башкортостан.

        Arguments:
            city: Каноническое или разговорное название города.

        Returns:
            Координаты центра города или `None`, если город не найден.
        """
        cleaned_city = normalize_optional_text(city)
        if cleaned_city is None:
            return None

        cities, _ = await self._get_reference_catalogs()
        canonical_city = normalize_city(cleaned_city, cities=cities) or cleaned_city
        candidate_keys = {
            key
            for key in (normalize_text_for_matching(canonical_city), normalize_text_for_matching(cleaned_city))
            if key
        }

        for city_ref in cities:
            if city_ref.center_coordinates is None:
                continue
            variants = (city_ref.name, city_ref.normalized_name, *city_ref.aliases)
            if any(normalize_text_for_matching(variant) in candidate_keys for variant in variants):
                return city_ref.center_coordinates

        if not self._settings.EXTERNAL_ENABLED:
            return None

        cache_key: _GeocodeCacheKey = ("city", normalize_text_for_matching(canonical_city))
        cached = self._get_cached_coordinates(cache_key)
        if cached is not None:
            return cached

        resolved = await self._geocode_external(query=f"{cleaned_city}, Республика Башкортостан, Россия")
        if resolved is not None:
            self._store_cached_coordinates(cache_key, resolved)
        return resolved

    async def resolve_venue_coordinates(
        self, *, city: str, venue: str, address: str | None = None
    ) -> Coordinates | None:
        """Определяет координаты площадки по справочнику или внешнему геокодеру.

        При отказе внешнего геосервиса возвращает данные локального справочника
        или `None` без выброса необработанного исключения.

        Arguments:
            city: Город расположения площадки.
            venue: Название площадки.
            address: Необязательный адрес для уточнения координат.

        Returns:
            Координаты площадки или `None`, если координаты определить не удалось.
        """
        cleaned_venue = normalize_optional_text(venue)
        if cleaned_venue is None:
            return None

        cleaned_city = normalize_optional_text(city)
        cleaned_address = normalize_optional_text(address)

        cities, venues = await self._get_reference_catalogs()
        canonical_city = normalize_city(cleaned_city, cities=cities) if cleaned_city else None

        target_city_keys = {
            key
            for key in (
                normalize_text_for_matching(canonical_city) if canonical_city else "",
                normalize_text_for_matching(cleaned_city) if cleaned_city else "",
            )
            if key
        }
        scoped_venues = tuple(
            venue_ref
            for venue_ref in venues
            if venue_ref.coordinates is not None
            and (not target_city_keys or normalize_text_for_matching(venue_ref.city) in target_city_keys)
        )

        exact_match = normalize_venue(cleaned_venue, venues=scoped_venues, city=canonical_city or cleaned_city)
        if isinstance(exact_match, VenueReference) and exact_match.coordinates is not None:
            return exact_match.coordinates

        fuzzy_match = _find_fuzzy_venue_match(cleaned_venue, scoped_venues)
        if fuzzy_match is not None and fuzzy_match.coordinates is not None:
            return fuzzy_match.coordinates

        if not self._settings.EXTERNAL_ENABLED:
            return None

        effective_city = canonical_city or cleaned_city or ""
        cache_key: _GeocodeCacheKey = (
            "venue",
            normalize_text_for_matching(effective_city),
            normalize_text_for_matching(cleaned_venue),
            normalize_text_for_matching(cleaned_address or ""),
        )
        cached = self._get_cached_coordinates(cache_key)
        if cached is not None:
            return cached

        query_parts = [cleaned_venue]
        if cleaned_address is not None:
            query_parts.append(cleaned_address)
        if effective_city:
            query_parts.append(effective_city)
        query_parts.append("Республика Башкортостан")

        resolved = await self._geocode_external(query=", ".join(query_parts))
        if resolved is not None:
            self._store_cached_coordinates(cache_key, resolved)
        return resolved

    async def estimate_travel(self, *, origin: Coordinates, destination: Coordinates) -> TravelEstimate:
        """Оценивает расстояние и время в пути между двумя точками города.

        При отключённом внешнем геосервисе или любом сетевом/HTTP/JSON-сбое
        гарантированно возвращает локальную оценку по формуле гаверсинуса.

        Arguments:
            origin: Координаты точки отправления.
            destination: Координаты точки назначения.

        Returns:
            Оценка расстояния и времени перемещения в минутах.
        """
        if not self._settings.EXTERNAL_ENABLED:
            return self._fallback_travel_estimate(origin=origin, destination=destination)

        if origin.latitude == destination.latitude and origin.longitude == destination.longitude:
            return self._fallback_travel_estimate(origin=origin, destination=destination)

        cache_key: _TravelCacheKey = (
            round(origin.latitude, 6),
            round(origin.longitude, 6),
            round(destination.latitude, 6),
            round(destination.longitude, 6),
        )
        cached = self._get_cached_travel(cache_key)
        if cached is not None:
            return cached

        client = self._get_or_create_http_client()
        url = (
            f"{self._settings.ROUTER_BASE_URL}/route/v1/driving/"
            f"{origin.longitude},{origin.latitude};{destination.longitude},{destination.latitude}"
        )
        params = {"overview": "false"}

        try:
            response = await client.get(url, params=params, timeout=self._settings.REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping) or payload.get("code") != "Ok":
                raise ValueError("Некорректный статус ответа OSRM")
            routes = payload.get("routes")
            if not isinstance(routes, list) or not routes or not isinstance(routes[0], Mapping):
                raise ValueError("В ответе OSRM отсутствует маршрут")
            distance_meters = _parse_non_negative_finite_float(routes[0].get("distance"))
            duration_seconds = _parse_non_negative_finite_float(routes[0].get("duration"))
            if distance_meters is None or duration_seconds is None:
                raise ValueError("В ответе OSRM отсутствуют корректные числовые distance/duration")
        except (httpx.HTTPError, ValueError, TypeError, KeyError, RuntimeError, OSError, ApplicationException) as error:
            logger.warning("Ошибка внешнего сервиса маршрутизации OSRM, используется гаверсинус: %s", error)
            return self._fallback_travel_estimate(origin=origin, destination=destination)

        distance_kilometers = round(distance_meters / 1000.0, 2)
        travel_minutes = 0 if duration_seconds <= 0.0 else max(1, round(duration_seconds / 60.0))
        estimate = TravelEstimate(
            distance_kilometers=distance_kilometers, travel_minutes=travel_minutes, used_external_service=True
        )
        self._store_cached_travel(cache_key, estimate)
        return estimate
