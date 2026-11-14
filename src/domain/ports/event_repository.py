"""Контрактный порт репозитория каталога мероприятий и справочников."""

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from src.core.types import EventStatus, RealCollection, SourceType
from src.domain.entities import CityReference, Event, EventSearchFilter, EventSearchPage, VenueReference

__all__ = ["EventRepositoryPort"]


@runtime_checkable
class EventRepositoryPort(Protocol):
    """Протокол доступа к нормализованному каталогу культурных мероприятий и справочникам."""

    async def check_health(self) -> bool:
        """Проверяет доступность хранилища каталога мероприятий.

        Returns:
            `True`, если соединение с базой данных активно и готово к работе.

        Raises:
            InfrastructureException: При критической недоступности хранилища.
        """
        ...

    async def search_events(
        self, *, search_filter: EventSearchFilter, statuses: RealCollection[EventStatus] = (EventStatus.PUBLISHED,)
    ) -> EventSearchPage:
        """Выполняет поиск мероприятий по фильтрам, пагинации и допустимым статусам.

        Используется для публичной выдачи (`PUBLISHED`), выборки кандидатов планировщика и
        дедупликации, а также для таблиц администрирования.

        Arguments:
            search_filter: Неизменяемый набор критериев поиска, сортировки и пагинации.
            statuses: Допустимые статусы публикаций в выборке (по умолчанию только `PUBLISHED`).

        Returns:
            Страница результатов поиска с общим количеством совпадений.

        Raises:
            InfrastructureException: При ошибке выполнения запроса в хранилище.
            InvalidSearchFilterError: При некорректных параметрах пагинации или диапазонов.
        """
        ...

    async def count_events(self, *, search_filter: EventSearchFilter) -> int:
        """Подсчитывает количество опубликованных мероприятий без загрузки карточек.

        Используется для детерминированной диагностики пустой выдачи.

        Arguments:
            search_filter: Набор критериев фильтрации.

        Returns:
            Количество подходящих опубликованных мероприятий.

        Raises:
            InfrastructureException: При ошибке выполнения запроса в хранилище.
        """
        ...

    async def get_event_by_identifier(self, *, event_identifier: UUID, include_non_published: bool = True) -> Event:
        """Возвращает полную карточку мероприятия по уникальному идентификатору.

        Поддерживает чтение отменённых карточек по прямой ссылке при `include_non_published=True`.

        Arguments:
            event_identifier: Уникальный идентификатор мероприятия.
            include_non_published: Разрешать ли возврат черновиков и отменённых карточек.

        Returns:
            Полная доменная сущность мероприятия с перечнем подтверждённых источников.

        Raises:
            EventNotFoundError: Если мероприятие с указанным идентификатором не найдено.
            InfrastructureException: При ошибке чтения из хранилища.
        """
        ...

    async def find_similar_events(
        self,
        *,
        event_identifier: UUID,
        city: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        near_center: bool = False,
        limit: int = 5,
    ) -> tuple[Event, ...]:
        """Подбирает похожие опубликованные мероприятия по категории, тегам и близости.

        Arguments:
            event_identifier: Идентификатор исходного мероприятия.
            city: Необязательное ограничение по городу.
            date_from: Нижняя граница времени начала похожих мероприятий.
            date_to: Верхняя граница времени начала похожих мероприятий.
            near_center: Приоритет мероприятий ближе к центру города.
            limit: Максимальное число возвращаемых карточек.

        Returns:
            Кортеж похожих опубликованных мероприятий.

        Raises:
            EventNotFoundError: Если исходное мероприятие не найдено.
            InfrastructureException: При ошибке чтения из хранилища.
        """
        ...

    async def find_by_source_hash(self, *, source_type: SourceType, source_hash: str) -> Event | None:
        """Ищет существующую карточку по типу источника и контрольной сумме записи.

        Arguments:
            source_type: Тип источника поступления.
            source_hash: Хэш исходной записи для идемпотентности.

        Returns:
            Найденная карточка мероприятия или `None`.

        Raises:
            InfrastructureException: При ошибке чтения из хранилища.
        """
        ...

    async def save_event(self, *, event: Event) -> Event:
        """Идемпотентно создаёт или обновляет карточку мероприятия, её статус и список источников.

        Arguments:
            event: Сохраняемая доменная карточка мероприятия.

        Returns:
            Актуальная сохранённая карточка мероприятия.

        Raises:
            DuplicateEventConflictError: При нарушении уникальности конфликтующих данных.
            InfrastructureException: При ошибке записи в хранилище.
        """
        ...

    async def list_cities(self) -> tuple[CityReference, ...]:
        """Возвращает нормализованный справочник городов Республики Башкортостан.

        Returns:
            Кортеж записей справочника городов.

        Raises:
            InfrastructureException: При ошибке чтения справочника.
        """
        ...

    async def list_languages(self) -> tuple[str, ...]:
        """Возвращает отсортированный список языков опубликованных мероприятий.

        Returns:
            Кортеж нормализованных названий языков.

        Raises:
            InfrastructureException: При ошибке чтения языков мероприятий.
        """
        ...

    async def list_venues(self, *, city: str | None = None) -> tuple[VenueReference, ...]:
        """Возвращает справочник культурных площадок с фильтрацией по городу.

        Arguments:
            city: Необязательное название города для отбора площадок.

        Returns:
            Кортеж записей справочника площадок.

        Raises:
            InfrastructureException: При ошибке чтения справочника.
        """
        ...

    async def get_catalog_fingerprint(self) -> str:
        """Возвращает стабильный отпечаток актуального состояния каталога и справочников.

        Используется веб-слоем для формирования заголовка `ETag` и кэширования.

        Returns:
            Строковый токен версии каталога и справочников.

        Raises:
            InfrastructureException: При ошибке получения отпечатка из хранилища.
        """
        ...
