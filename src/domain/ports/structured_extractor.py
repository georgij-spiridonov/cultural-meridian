"""Контрактный порт гибридного извлечения полей мероприятий с помощью языковой модели."""

from datetime import datetime
from typing import Protocol, runtime_checkable

from src.domain.entities import ExtractedEventFields

__all__ = ["StructuredExtractorPort"]


@runtime_checkable
class StructuredExtractorPort(Protocol):
    """Протокол извлечения структурированных полей из неформализованного текста афиши.

    Используется конвейером сбора как второй слой гибридной нормализации для сложных
    описаний, когда детерминированные правила не обеспечивают достаточной уверенности.
    """

    async def extract_event_fields(
        self,
        *,
        raw_title: str,
        raw_description: str | None = None,
        raw_date_text: str | None = None,
        raw_price_text: str | None = None,
        raw_city: str | None = None,
        raw_venue: str | None = None,
        raw_category: str | None = None,
        raw_age_text: str | None = None,
        reference_datetime: datetime,
    ) -> ExtractedEventFields:
        """Извлекает структурированные поля события из сырых фрагментов источника.

        Все входные строки передаются в модель как недоверенные данные внутри
        изолирующих разделителей и дополнительно проверяются доменными правилами.

        Arguments:
            raw_title: Исходный заголовок события.
            raw_description: Исходное описание мероприятия.
            raw_date_text: Исходная строка даты и времени.
            raw_price_text: Исходная строка стоимости.
            raw_city: Исходное название города.
            raw_venue: Исходное название площадки.
            raw_category: Исходная категория источника.
            raw_age_text: Исходное возрастное ограничение.
            reference_datetime: Опорная дата сбора для разрешения относительных дат.

        Returns:
            Структурированные поля мероприятия с оценкой уверенности извлечения.

        Raises:
            AccessException: При ошибке авторизации или адреса шлюза модели.
            ModelException: При сбое структурированного вывода модели.
            NetworkException: При сетевой ошибке или тайм-ауте запроса.
            ServiceAvailabilityException: При ограничении частоты или недоступности провайдера.
        """
        ...
