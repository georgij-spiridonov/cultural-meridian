"""Контрактный порт хранилища сырых записей внешних источников."""

from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from src.domain.entities import RawEventRecord

__all__ = ["RawEventRepositoryPort", "RawNormalizationStatus"]

type RawNormalizationStatus = Literal["pending", "normalized", "failed"]


@runtime_checkable
class RawEventRepositoryPort(Protocol):
    """Протокол управления сырыми данными внешних источников.

    Хранит исходные полезные нагрузки отдельно от нормализованных карточек, чтобы
    нормализацию можно было переигрывать без повторного опроса внешних сайтов.
    """

    async def save_raw_event(self, *, raw_event: RawEventRecord) -> tuple[RawEventRecord, bool]:
        """Идемпотентно сохраняет сырую запись источника по паре `(source_name, source_hash)`.

        Arguments:
            raw_event: Сырая запись, полученная загрузчиком источника.

        Returns:
            Кортеж `(сохранённая_запись, признак_создания_новой_записи)`.

        Raises:
            InfrastructureException: При ошибке сохранения сырой записи в хранилище.
        """
        ...

    async def list_raw_events_for_reprocessing(
        self,
        *,
        source_name: str | None = None,
        normalization_status: RawNormalizationStatus | None = None,
        limit: int = 100,
    ) -> tuple[RawEventRecord, ...]:
        """Возвращает выборку сырых записей для первичной или повторной нормализации.

        Arguments:
            source_name: Необязательный фильтр по имени источника.
            normalization_status: Необязательный фильтр по статусу обработки.
            limit: Максимальное количество записей за один проход.

        Returns:
            Кортеж сырых записей в порядке поступления.

        Raises:
            InfrastructureException: При ошибке чтения из хранилища.
        """
        ...

    async def update_normalization_status(
        self,
        *,
        raw_event_identifier: UUID,
        normalization_status: RawNormalizationStatus,
        error_message: str | None = None,
    ) -> None:
        """Обновляет статус нормализации сырой записи после обработки конвейером.

        Arguments:
            raw_event_identifier: Идентификатор сырой записи.
            normalization_status: Итоговый статус обработки (`pending`, `normalized`, `failed`).
            error_message: Описание ошибки при неуспешной нормализации.

        Raises:
            InfrastructureException: При ошибке обновления статуса в хранилище.
        """
        ...
