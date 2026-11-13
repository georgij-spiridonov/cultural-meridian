"""Контрактный порт загрузки сырых карточек из внешнего или локального источника."""

from typing import Protocol, runtime_checkable

from src.domain.entities import FetchedSourceItem, SourceDefinition

__all__ = ["SourceFetcherPort"]


@runtime_checkable
class SourceFetcherPort(Protocol):
    """Протокол загрузчика событий из одного типа источника (RSS, API или веб-страница)."""

    async def fetch_items(self, *, source: SourceDefinition) -> tuple[FetchedSourceItem, ...]:
        """Загружает и первично разбирает список мероприятий из заданного источника.

        Arguments:
            source: Декларативное описание источника из реестра конфигурации.

        Returns:
            Кортеж сырых элементов мероприятий для сохранения и нормализации.

        Raises:
            NetworkException: При сетевой ошибке или превышении тайм-аута загрузки.
            ServiceAvailabilityException: При ответе внешнего ресурса с ошибкой 5xx или лимитом.
            UnsupportedSourceFormatError: Если формат ответа источника не поддерживается.
            ValidationException: При повреждённой разметке или схеме данных источника.
        """
        ...
