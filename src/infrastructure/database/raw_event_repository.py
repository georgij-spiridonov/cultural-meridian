"""PostgreSQL-репозиторий сырых записей внешних источников."""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from src.core.exceptions import ValidationException
from src.core.logger import get_logger
from src.domain.entities import RawEventRecord
from src.domain.ports import RawEventRepositoryPort, RawNormalizationStatus
from src.domain.services.normalization import normalize_optional_text
from src.infrastructure.database._helpers import validate_positive_limit
from src.infrastructure.database.engine import DatabaseEngineManager
from src.infrastructure.database.errors import handle_database_errors
from src.infrastructure.database.mappers import map_raw_event_from_row
from src.infrastructure.database.tables import RawEventRow

__all__ = ["PostgreSqlRawEventRepository"]

logger = get_logger(__name__)


class PostgreSqlRawEventRepository(RawEventRepositoryPort):
    """Реализация `RawEventRepositoryPort` на базе PostgreSQL 17."""

    def __init__(self, engine_manager: DatabaseEngineManager) -> None:
        """Инициализирует репозиторий сырых записей источников.

        Arguments:
            engine_manager: Менеджер асинхронного движка и пула сессий PostgreSQL.
        """
        self._engine_manager = engine_manager

    async def save_raw_event(self, *, raw_event: RawEventRecord) -> tuple[RawEventRecord, bool]:
        """Идемпотентно сохраняет сырую запись источника по паре `(source_name, source_hash)`.

        Arguments:
            raw_event: Сырая запись, полученная загрузчиком источника.

        Returns:
            Кортеж `(сохранённая_запись, признак_создания_новой_записи)`.
        """
        cleaned_source_name = raw_event.source_name.strip()
        cleaned_external_id = raw_event.external_identifier.strip()
        cleaned_source_url = raw_event.source_url.strip()
        cleaned_source_hash = raw_event.source_hash.strip()
        cleaned_error = normalize_optional_text(raw_event.error_message)

        async with (
            handle_database_errors(operation="сохранение сырой записи источника"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            insert_statement = (
                postgresql_insert(RawEventRow)
                .values(
                    identifier=raw_event.identifier,
                    source_name=cleaned_source_name,
                    source_type=raw_event.source_type.value,
                    external_identifier=cleaned_external_id,
                    source_url=cleaned_source_url,
                    source_hash=cleaned_source_hash,
                    raw_payload=dict(raw_event.raw_payload),
                    fetched_at=raw_event.fetched_at,
                    normalization_status=raw_event.normalization_status,
                    error_message=cleaned_error,
                )
                .on_conflict_do_nothing(constraint="source_name_hash")
                .returning(RawEventRow)
            )
            inserted_row = (await session.execute(insert_statement)).scalar_one_or_none()
            if inserted_row is not None:
                logger.debug(
                    "Создана сырая запись источника %s (source_hash=%s).", cleaned_source_name, cleaned_source_hash
                )
                return map_raw_event_from_row(inserted_row), True

            existing_row = (
                await session.execute(
                    select(RawEventRow).where(
                        (RawEventRow.source_name == cleaned_source_name)
                        & (RawEventRow.source_hash == cleaned_source_hash)
                    )
                )
            ).scalar_one()
            return map_raw_event_from_row(existing_row), False

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
        """
        validated_limit = validate_positive_limit(limit, parameter_name="limit")
        statement = select(RawEventRow)
        cleaned_source_name = normalize_optional_text(source_name)
        if cleaned_source_name is not None:
            statement = statement.where(RawEventRow.source_name == cleaned_source_name)
        if normalization_status is not None:
            statement = statement.where(RawEventRow.normalization_status == normalization_status)

        statement = statement.order_by(RawEventRow.fetched_at.asc(), RawEventRow.identifier.asc()).limit(
            validated_limit
        )

        async with (
            handle_database_errors(operation="чтение сырых записей для нормализации"),
            self._engine_manager.session() as session,
        ):
            rows = (await session.execute(statement)).scalars().all()
            return tuple(map_raw_event_from_row(row) for row in rows)

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
        """
        cleaned_error = normalize_optional_text(error_message)
        statement = (
            update(RawEventRow)
            .where(RawEventRow.identifier == raw_event_identifier)
            .values(normalization_status=normalization_status, error_message=cleaned_error)
            .returning(RawEventRow.identifier)
        )

        async with (
            handle_database_errors(operation="обновление статуса нормализации сырой записи"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            updated_identifier = (await session.execute(statement)).scalar_one_or_none()
            if updated_identifier is None:
                raise ValidationException(
                    f"Сырая запись источника с идентификатором {raw_event_identifier} не найдена."
                )
