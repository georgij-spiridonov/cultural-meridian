"""Контрактный порт геокодирования площадок и оценки времени перемещения."""

from typing import Protocol, runtime_checkable

from src.domain.entities import Coordinates, TravelEstimate

__all__ = ["GeolocationResolverPort"]


@runtime_checkable
class GeolocationResolverPort(Protocol):
    """Протокол определения координат и времени перемещения с защитой от сбоев геосервиса."""

    async def resolve_city_center(self, *, city: str) -> Coordinates | None:
        """Возвращает координаты центра населённого пункта Республики Башкортостан.

        Arguments:
            city: Каноническое или разговорное название города.

        Returns:
            Координаты центра города или `None`, если город не найден.
        """
        ...

    async def resolve_venue_coordinates(
        self, *, city: str, venue: str, address: str | None = None
    ) -> Coordinates | None:
        """Определяет координаты площадки по справочнику или внешнему геокодеру.

        При отказе внешнего геосервиса адаптер обязан вернуть данные локального
        справочника или `None` без выброса необработанного исключения.

        Arguments:
            city: Город расположения площадки.
            venue: Название площадки.
            address: Необязательный адрес для уточнения координат.

        Returns:
            Координаты площадки или `None`, если координаты определить не удалось.
        """
        ...

    async def estimate_travel(self, *, origin: Coordinates, destination: Coordinates) -> TravelEstimate:
        """Оценивает расстояние и время в пути между двумя точками города.

        При сбое внешнего сервиса маршрутизации автоматически переключается на
        локальную детерминированную оценку по формуле гаверсинуса.

        Arguments:
            origin: Координаты точки отправления.
            destination: Координаты точки назначения.

        Returns:
            Оценка расстояния и времени перемещения в минутах.
        """
        ...
