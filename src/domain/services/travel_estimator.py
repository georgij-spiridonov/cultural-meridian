"""Чистые доменные расчёты расстояний и времени перемещения между площадками."""

import math
from typing import Final

from src.domain.entities import Coordinates, TravelEstimate

__all__ = [
    "EARTH_MEAN_RADIUS_KILOMETERS",
    "calculate_haversine_kilometers",
    "estimate_urban_travel",
    "is_within_center_radius",
]

EARTH_MEAN_RADIUS_KILOMETERS: Final[float] = 6371.0088
_SAME_VENUE_THRESHOLD_KILOMETERS: Final[float] = 0.05
_WALKING_THRESHOLD_KILOMETERS: Final[float] = 0.6
_WALKING_SPEED_KILOMETERS_PER_HOUR: Final[float] = 4.5
_URBAN_ROAD_DETOUR_FACTOR: Final[float] = 1.25


def calculate_haversine_kilometers(*, origin: Coordinates, destination: Coordinates) -> float:
    """Вычисляет расстояние по поверхности Земли между двумя точками (формула гаверсинуса).

    Arguments:
        origin: Координаты точки отправления.
        destination: Координаты точки назначения.

    Returns:
        Расстояние в километрах, округлённое до двух знаков после запятой.
    """
    latitude_origin_radians = math.radians(origin.latitude)
    latitude_destination_radians = math.radians(destination.latitude)
    delta_latitude = math.radians(destination.latitude - origin.latitude)
    delta_longitude = math.radians(destination.longitude - origin.longitude)

    half_chord_squared = (
        math.sin(delta_latitude / 2.0) ** 2
        + math.cos(latitude_origin_radians)
        * math.cos(latitude_destination_radians)
        * math.sin(delta_longitude / 2.0) ** 2
    )
    clamped = min(1.0, max(0.0, half_chord_squared))
    central_angle = 2.0 * math.atan2(math.sqrt(clamped), math.sqrt(1.0 - clamped))
    return round(EARTH_MEAN_RADIUS_KILOMETERS * central_angle, 2)


def estimate_urban_travel(
    *,
    origin: Coordinates,
    destination: Coordinates,
    average_speed_kilometers_per_hour: float = 25.0,
    transfer_overhead_minutes: int = 7,
) -> TravelEstimate:
    """Оценивает время перемещения по городу на основе расстояния между координатами.

    Для одной и той же площадки (< 50 м) возвращает 0 минут, для шаговой доступности
    (< 600 м) — пешую оценку без ожидания транспорта, далее — оценку городского
    маршрута с коэффициентом извилистости улиц и временем на посадку.

    Arguments:
        origin: Координаты исходной площадки.
        destination: Координаты целевой площадки.
        average_speed_kilometers_per_hour: Средняя скорость городского транспорта в км/ч.
        transfer_overhead_minutes: Дополнительное время на посадку и подход к остановке.

    Returns:
        Оценка расстояния и времени пути (`used_external_service=False`).
    """
    distance_kilometers = calculate_haversine_kilometers(origin=origin, destination=destination)
    if distance_kilometers <= _SAME_VENUE_THRESHOLD_KILOMETERS:
        return TravelEstimate(distance_kilometers=distance_kilometers, travel_minutes=0, used_external_service=False)

    if distance_kilometers <= _WALKING_THRESHOLD_KILOMETERS:
        walking_minutes = max(
            2, round((distance_kilometers * _URBAN_ROAD_DETOUR_FACTOR / _WALKING_SPEED_KILOMETERS_PER_HOUR) * 60)
        )
        return TravelEstimate(
            distance_kilometers=distance_kilometers, travel_minutes=walking_minutes, used_external_service=False
        )

    effective_speed = max(1.0, average_speed_kilometers_per_hour)
    driving_minutes = round((distance_kilometers * _URBAN_ROAD_DETOUR_FACTOR / effective_speed) * 60)
    total_minutes = max(transfer_overhead_minutes + 1, driving_minutes + transfer_overhead_minutes)
    return TravelEstimate(
        distance_kilometers=distance_kilometers, travel_minutes=total_minutes, used_external_service=False
    )


def is_within_center_radius(*, point: Coordinates, city_center: Coordinates, maximum_kilometers: float = 4.0) -> bool:
    """Проверяет, находится ли площадка в пределах радиуса центральной части города.

    Arguments:
        point: Координаты проверяемой площадки.
        city_center: Координаты центра города.
        maximum_kilometers: Допустимый радиус близости к центру в километрах.

    Returns:
        `True`, если расстояние до центра не превышает заданный радиус.
    """
    return calculate_haversine_kilometers(origin=point, destination=city_center) <= maximum_kilometers
