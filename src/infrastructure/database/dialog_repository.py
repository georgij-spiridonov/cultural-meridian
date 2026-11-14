"""PostgreSQL-репозиторий диалоговых сессий, отложенных подтверждений, трасс и бюджета ИИ."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import selectinload

from src.core.exceptions import ValidationException
from src.core.logger import get_logger
from src.core.types import DialogChannel, RealCollection
from src.domain.entities import AgentTraceRecord, DialogMessage, DialogSessionState, PendingAction, UsageBudgetStatus
from src.domain.exceptions import PendingActionExpiredError, PendingActionNotFoundError
from src.domain.ports import DialogRepositoryPort
from src.domain.services.normalization import normalize_optional_text
from src.infrastructure.database._helpers import validate_positive_limit
from src.infrastructure.database.engine import DatabaseEngineManager
from src.infrastructure.database.errors import handle_database_errors
from src.infrastructure.database.mappers import (
    map_agent_trace_from_row,
    map_dialog_session_from_row,
    map_pending_action_from_row,
    map_usage_budget_status,
)
from src.infrastructure.database.tables import (
    AgentTraceGroundedEventRow,
    AgentTraceRow,
    AgentTraceToolInvocationRow,
    DialogMessageRow,
    DialogSessionRow,
    LanguageModelUsageDailyRow,
    PendingActionRow,
)

__all__ = ["PostgreSqlDialogRepository"]

logger = get_logger(__name__)


class PostgreSqlDialogRepository(DialogRepositoryPort):
    """Реализация `DialogRepositoryPort` на базе PostgreSQL 17."""

    def __init__(self, engine_manager: DatabaseEngineManager) -> None:
        """Инициализирует репозиторий памяти диалогов, подтверждений, трассировки и бюджета.

        Arguments:
            engine_manager: Менеджер асинхронного движка и пула сессий PostgreSQL.
        """
        self._engine_manager = engine_manager

    async def get_session_state(
        self, *, session_identifier: str, channel: DialogChannel, subscriber_identifier: str, message_limit: int = 10
    ) -> DialogSessionState:
        """Загружает состояние сессии диалога с последними репликами и ожидающим действием.

        Arguments:
            session_identifier: Стабильный идентификатор диалоговой сессии.
            channel: Канал взаимодействия пользователя.
            subscriber_identifier: Технический идентификатор пользователя без персональных данных.
            message_limit: Максимальное количество последних реплик (не менее 5 по контракту).

        Returns:
            Текущее неизменяемое состояние диалоговой сессии.
        """
        cleaned_session_id = session_identifier.strip()
        cleaned_subscriber_id = subscriber_identifier.strip()
        if not cleaned_session_id or not cleaned_subscriber_id:
            raise ValidationException("Идентификаторы сессии и пользователя не могут быть пустыми.")
        validated_limit = validate_positive_limit(message_limit, parameter_name="message_limit", maximum=500)

        async with (
            handle_database_errors(operation="загрузка состояния диалоговой сессии"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            upsert_statement = (
                postgresql_insert(DialogSessionRow)
                .values(
                    session_identifier=cleaned_session_id,
                    channel=channel.value,
                    subscriber_identifier=cleaned_subscriber_id,
                )
                .on_conflict_do_update(
                    index_elements=[DialogSessionRow.session_identifier],
                    set_={
                        "channel": channel.value,
                        "subscriber_identifier": cleaned_subscriber_id,
                        "updated_at": func.now(),
                    },
                )
                .returning(DialogSessionRow)
            )
            session_row = (await session.execute(upsert_statement)).scalar_one()

            recent_desc_rows = (
                (
                    await session.execute(
                        select(DialogMessageRow)
                        .where(DialogMessageRow.session_identifier == cleaned_session_id)
                        .order_by(DialogMessageRow.created_at.desc(), DialogMessageRow.identifier.desc())
                        .limit(validated_limit)
                    )
                )
                .scalars()
                .all()
            )
            chronological_messages = list(reversed(recent_desc_rows))

            active_pending_action = (
                await session.execute(
                    select(PendingActionRow).where(
                        (PendingActionRow.session_identifier == cleaned_session_id)
                        & (PendingActionRow.status == "pending")
                    )
                )
            ).scalar_one_or_none()

            if active_pending_action is not None and active_pending_action.expires_at <= datetime.now(UTC):
                active_pending_action.status = "expired"
                active_pending_action = None

            return map_dialog_session_from_row(
                session_row, recent_messages=chronological_messages, active_pending_action=active_pending_action
            )

    async def append_messages(self, *, session_identifier: str, messages: RealCollection[DialogMessage]) -> None:
        """Добавляет новые реплики пользователя и ассистента в историю сессии.

        Arguments:
            session_identifier: Идентификатор диалоговой сессии.
            messages: Конечная коллекция добавляемых сообщений.
        """
        cleaned_session_id = session_identifier.strip()
        if not cleaned_session_id:
            raise ValidationException("Идентификатор сессии не может быть пустым.")
        if not messages:
            return

        async with (
            handle_database_errors(operation="добавление реплик диалога"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            session_exists = (
                await session.execute(
                    select(DialogSessionRow.session_identifier).where(
                        DialogSessionRow.session_identifier == cleaned_session_id
                    )
                )
            ).scalar_one_or_none()
            if session_exists is None:
                raise ValidationException(f"Диалоговая сессия {cleaned_session_id!r} не найдена.")

            for message in messages:
                session.add(
                    DialogMessageRow(
                        session_identifier=cleaned_session_id,
                        role=message.role,
                        content=message.content.strip(),
                        created_at=message.created_at,
                    )
                )
            await session.execute(
                update(DialogSessionRow)
                .where(DialogSessionRow.session_identifier == cleaned_session_id)
                .values(updated_at=func.now())
            )

    async def create_pending_action(self, *, pending_action: PendingAction) -> PendingAction:
        """Регистрирует отложенное изменяющее действие, ожидающее подтверждения.

        Деактивирует предыдущее неподтверждённое действие той же сессии, если оно было.

        Arguments:
            pending_action: Создаваемое отложенное действие.

        Returns:
            Сохранённое отложенное действие.
        """
        cleaned_session_id = pending_action.session_identifier.strip()
        cleaned_prompt = pending_action.confirmation_prompt.strip()
        if not cleaned_session_id or not cleaned_prompt:
            raise ValidationException("Параметры отложенного действия не могут быть пустыми.")

        async with (
            handle_database_errors(operation="создание отложенного действия"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            await session.execute(
                postgresql_insert(DialogSessionRow)
                .values(
                    session_identifier=cleaned_session_id,
                    channel=pending_action.channel.value,
                    subscriber_identifier=cleaned_session_id,
                )
                .on_conflict_do_nothing(index_elements=[DialogSessionRow.session_identifier])
            )
            await session.execute(
                update(PendingActionRow)
                .where(
                    (PendingActionRow.session_identifier == cleaned_session_id) & (PendingActionRow.status == "pending")
                )
                .values(status="canceled")
            )
            await session.flush()

            action_row = PendingActionRow(
                action_identifier=pending_action.action_identifier,
                session_identifier=cleaned_session_id,
                action_type=pending_action.action_type,
                payload=dict(pending_action.payload),
                confirmation_prompt=cleaned_prompt,
                status="pending",
                created_at=pending_action.created_at,
                expires_at=pending_action.expires_at,
            )
            session.add(action_row)
            await session.flush()
            return map_pending_action_from_row(action_row, channel=pending_action.channel)

    async def consume_pending_action(
        self, *, action_identifier: UUID, session_identifier: str, current_time: datetime
    ) -> PendingAction:
        """Атомарно проверяет и закрывает отложенное действие после подтверждения пользователем.

        Arguments:
            action_identifier: Идентификатор подтверждаемого действия.
            session_identifier: Идентификатор сессии, приславшей подтверждение.
            current_time: Текущее время проверки срока действия.

        Returns:
            Подтверждённое отложенное действие с параметрами исполнения.

        Raises:
            PendingActionNotFoundError: Если действие отсутствует или принадлежит другой сессии.
            PendingActionExpiredError: Если срок действия подтверждения истёк.
        """
        cleaned_session_id = session_identifier.strip()
        expired_timestamp: datetime | None = None
        consumed_action: PendingAction | None = None

        async with (
            handle_database_errors(operation="подтверждение отложенного действия"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            row = (
                await session.execute(
                    select(PendingActionRow)
                    .where(PendingActionRow.action_identifier == action_identifier)
                    .options(selectinload(PendingActionRow.session))
                    .with_for_update(of=PendingActionRow)
                )
            ).scalar_one_or_none()

            if row is not None and row.session_identifier == cleaned_session_id and row.status == "pending":
                if row.expires_at <= current_time:
                    row.status = "expired"
                    expired_timestamp = row.expires_at
                else:
                    row.status = "consumed"
                    consumed_action = map_pending_action_from_row(row)

        if expired_timestamp is not None:
            raise PendingActionExpiredError(
                f"Срок действия подтверждения {action_identifier} истёк.",
                action_identifier=action_identifier,
                expired_at=expired_timestamp,
            )
        if consumed_action is None:
            raise PendingActionNotFoundError(
                f"Активное действие подтверждения {action_identifier} не найдено.",
                action_identifier=action_identifier,
                session_identifier=cleaned_session_id,
            )
        return consumed_action

    async def cancel_pending_action(self, *, action_identifier: UUID, session_identifier: str) -> None:
        """Отменяет отложенное действие по инициативе пользователя.

        Arguments:
            action_identifier: Идентификатор отменяемого действия.
            session_identifier: Идентификатор сессии владельца.

        Raises:
            PendingActionNotFoundError: Если активное действие не найдено.
        """
        cleaned_session_id = session_identifier.strip()
        statement = (
            update(PendingActionRow)
            .where(
                (PendingActionRow.action_identifier == action_identifier)
                & (PendingActionRow.session_identifier == cleaned_session_id)
                & (PendingActionRow.status == "pending")
            )
            .values(status="canceled")
            .returning(PendingActionRow.action_identifier)
        )

        async with (
            handle_database_errors(operation="отмена отложенного действия"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            canceled_id = (await session.execute(statement)).scalar_one_or_none()
            if canceled_id is None:
                raise PendingActionNotFoundError(
                    f"Активное действие подтверждения {action_identifier} не найдено для отмены.",
                    action_identifier=action_identifier,
                    session_identifier=cleaned_session_id,
                )

    async def reset_session_history(self, *, session_identifier: str) -> bool:
        """Очищает историю сообщений сессии и отменяет активное ожидающее действие.

        Не затрагивает сохранённые трассы ответов (`agent_traces`), лимиты расходов и подписки.

        Arguments:
            session_identifier: Стабильный идентификатор диалоговой сессии.

        Returns:
            `True`, если были удалены реплики или отменено активное подтверждение.
        """
        cleaned_session_id = session_identifier.strip()
        if not cleaned_session_id:
            raise ValidationException("Идентификатор сессии не может быть пустым.")

        async with (
            handle_database_errors(operation="сброс истории диалоговой сессии"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            deleted_message_ids = (
                (
                    await session.execute(
                        delete(DialogMessageRow)
                        .where(DialogMessageRow.session_identifier == cleaned_session_id)
                        .returning(DialogMessageRow.identifier)
                    )
                )
                .scalars()
                .all()
            )
            canceled_action_ids = (
                (
                    await session.execute(
                        update(PendingActionRow)
                        .where(
                            (PendingActionRow.session_identifier == cleaned_session_id)
                            & (PendingActionRow.status == "pending")
                        )
                        .values(status="canceled")
                        .returning(PendingActionRow.action_identifier)
                    )
                )
                .scalars()
                .all()
            )
            await session.execute(
                update(DialogSessionRow)
                .where(DialogSessionRow.session_identifier == cleaned_session_id)
                .values(updated_at=func.now())
            )
            return bool(deleted_message_ids or canceled_action_ids)

    async def save_trace(self, *, trace: AgentTraceRecord) -> AgentTraceRecord:
        """Сохраняет полную трассу ответа ИИ-агента или резервного поиска.

        Arguments:
            trace: Неизменяемая запись трассировки ответа.

        Returns:
            Сохранённая запись трассы.
        """
        unique_grounded_ids = list(dict.fromkeys(trace.grounded_event_identifiers))
        trace_row = AgentTraceRow(
            trace_identifier=trace.trace_identifier,
            request_identifier=trace.request_identifier.strip(),
            session_identifier=trace.session_identifier.strip(),
            channel=trace.channel.value,
            user_query=trace.user_query,
            plan_summary=trace.plan_summary,
            fallback_used=trace.fallback_used,
            latency_milliseconds=trace.latency_milliseconds,
            tokens_input=trace.tokens_input,
            tokens_output=trace.tokens_output,
            cost_rubles=Decimal(str(trace.cost_rubles)),
            model_name=trace.model_name,
            prompt_version=trace.prompt_version,
            created_at=trace.created_at,
        )
        trace_row.tool_invocations = [
            AgentTraceToolInvocationRow(
                trace_identifier=trace.trace_identifier,
                sequence_number=index,
                tool_name=invocation.tool_name,
                arguments=dict(invocation.arguments),
                returned_event_identifiers=list(invocation.returned_event_identifiers),
                succeeded=invocation.succeeded,
                error_message=normalize_optional_text(invocation.error_message),
            )
            for index, invocation in enumerate(trace.tool_invocations)
        ]
        trace_row.grounded_events = [
            AgentTraceGroundedEventRow(trace_identifier=trace.trace_identifier, event_identifier=event_id)
            for event_id in unique_grounded_ids
        ]

        async with (
            handle_database_errors(operation="сохранение трассы ответа агента"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            session.add(trace_row)
            await session.flush()
            loaded_row = (
                await session.execute(
                    select(AgentTraceRow)
                    .where(AgentTraceRow.trace_identifier == trace.trace_identifier)
                    .options(selectinload(AgentTraceRow.tool_invocations), selectinload(AgentTraceRow.grounded_events))
                )
            ).scalar_one()
            return map_agent_trace_from_row(loaded_row)

    async def get_trace(self, *, trace_identifier: UUID) -> AgentTraceRecord | None:
        """Возвращает сохранённую трассу ответа по её идентификатору.

        Arguments:
            trace_identifier: Идентификатор трассы.

        Returns:
            Запись трассы или `None`, если она не найдена.
        """
        statement = (
            select(AgentTraceRow)
            .where(AgentTraceRow.trace_identifier == trace_identifier)
            .options(selectinload(AgentTraceRow.tool_invocations), selectinload(AgentTraceRow.grounded_events))
        )
        async with (
            handle_database_errors(operation="чтение трассы ответа агента"),
            self._engine_manager.session() as session,
        ):
            row = (await session.execute(statement)).scalar_one_or_none()
            return map_agent_trace_from_row(row) if row is not None else None

    async def list_recent_traces(self, *, limit: int = 50) -> tuple[AgentTraceRecord, ...]:
        """Возвращает последние трассы ответов для просмотра в интерфейсе администрирования.

        Arguments:
            limit: Максимальное количество последних трасс.

        Returns:
            Кортеж записей трассировки, упорядоченных от новых к старым.
        """
        validated_limit = validate_positive_limit(limit, parameter_name="limit", maximum=500)
        statement = (
            select(AgentTraceRow)
            .options(selectinload(AgentTraceRow.tool_invocations), selectinload(AgentTraceRow.grounded_events))
            .order_by(AgentTraceRow.created_at.desc(), AgentTraceRow.trace_identifier.asc())
            .limit(validated_limit)
        )
        async with (
            handle_database_errors(operation="чтение списка последних трасс"),
            self._engine_manager.session() as session,
        ):
            rows = (await session.execute(statement)).scalars().unique().all()
            return tuple(map_agent_trace_from_row(row) for row in rows)

    async def get_usage_budget_status(
        self,
        *,
        subscriber_identifier: str,
        target_date: date,
        user_daily_limit_rubles: float,
        global_daily_limit_rubles: float,
    ) -> UsageBudgetStatus:
        """Проверяет текущее состояние суточных лимитов стоимости модели.

        Arguments:
            subscriber_identifier: Технический идентификатор пользователя.
            target_date: Дата расчётных суток.
            user_daily_limit_rubles: Порог суточных расходов на одного пользователя в рублях.
            global_daily_limit_rubles: Общий суточный порог расходов сервиса в рублях.

        Returns:
            Снимок расходов и признак превышения лимитов.
        """
        cleaned_subscriber = subscriber_identifier.strip()
        if not cleaned_subscriber:
            raise ValidationException("Идентификатор пользователя не может быть пустым.")

        user_query = select(func.coalesce(LanguageModelUsageDailyRow.spent_rubles, Decimal(0))).where(
            (LanguageModelUsageDailyRow.usage_date == target_date)
            & (LanguageModelUsageDailyRow.subscriber_identifier == cleaned_subscriber)
        )
        global_query = select(func.coalesce(func.sum(LanguageModelUsageDailyRow.spent_rubles), Decimal(0))).where(
            LanguageModelUsageDailyRow.usage_date == target_date
        )

        async with (
            handle_database_errors(operation="проверка суточного бюджета модели"),
            self._engine_manager.session() as session,
        ):
            user_spent_raw = (await session.execute(user_query)).scalar_one_or_none() or Decimal(0)
            global_spent_raw = (await session.execute(global_query)).scalar_one_or_none() or Decimal(0)
            return map_usage_budget_status(
                user_daily_spent_rubles=float(user_spent_raw),
                user_daily_limit_rubles=user_daily_limit_rubles,
                global_daily_spent_rubles=float(global_spent_raw),
                global_daily_limit_rubles=global_daily_limit_rubles,
            )

    async def record_model_usage(
        self,
        *,
        subscriber_identifier: str,
        target_date: date,
        cost_rubles: float,
        tokens_input: int,
        tokens_output: int,
    ) -> None:
        """Фиксирует израсходованные токены и стоимость вызова модели за текущие сутки.

        Arguments:
            subscriber_identifier: Технический идентификатор пользователя.
            target_date: Дата расчётных суток.
            cost_rubles: Стоимость выполненного вызова в рублях.
            tokens_input: Число входных токенов.
            tokens_output: Число выходных токенов.
        """
        cleaned_subscriber = subscriber_identifier.strip()
        if not cleaned_subscriber:
            raise ValidationException("Идентификатор пользователя не может быть пустым.")
        if cost_rubles < 0 or tokens_input < 0 or tokens_output < 0:
            raise ValidationException("Расходы и количество токенов не могут быть отрицательными.")

        cost_decimal = Decimal(str(round(cost_rubles, 6)))
        statement = (
            postgresql_insert(LanguageModelUsageDailyRow)
            .values(
                usage_date=target_date,
                subscriber_identifier=cleaned_subscriber,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                spent_rubles=cost_decimal,
            )
            .on_conflict_do_update(
                index_elements=[
                    LanguageModelUsageDailyRow.usage_date,
                    LanguageModelUsageDailyRow.subscriber_identifier,
                ],
                set_={
                    "tokens_input": LanguageModelUsageDailyRow.tokens_input + tokens_input,
                    "tokens_output": LanguageModelUsageDailyRow.tokens_output + tokens_output,
                    "spent_rubles": LanguageModelUsageDailyRow.spent_rubles + cost_decimal,
                    "updated_at": func.now(),
                },
            )
        )

        async with (
            handle_database_errors(operation="учёт суточных расходов языковой модели"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            await session.execute(statement)
