"""PostgreSQL-репозиторий модерации, аудита, подписок и обращений организаторов."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.exceptions import ValidationException
from src.core.logger import get_logger
from src.core.types import DialogChannel
from src.domain.entities import (
    AuditLogEntry,
    FeedbackRequestRecord,
    FeedbackStatus,
    ModerationItem,
    ModerationStatus,
    SubscriptionRecord,
)
from src.domain.exceptions import ModerationItemNotFoundError, SubscriptionNotFoundError
from src.domain.ports import AdministrationRepositoryPort
from src.domain.services.normalization import normalize_optional_text
from src.infrastructure.database._helpers import resolve_or_create_city, validate_positive_limit
from src.infrastructure.database.engine import DatabaseEngineManager
from src.infrastructure.database.errors import handle_database_errors
from src.infrastructure.database.mappers import (
    map_audit_log_from_row,
    map_feedback_request_from_row,
    map_moderation_item_from_row,
    map_subscription_from_row,
)
from src.infrastructure.database.tables import (
    AuditLogRow,
    FeedbackRequestRow,
    ModerationQueueRow,
    ModeratorRow,
    SubscriptionRow,
)

__all__ = ["PostgreSqlAdministrationRepository"]

logger = get_logger(__name__)


async def _find_moderator_identifier(session: AsyncSession, *, username: str | None) -> UUID | None:
    cleaned = normalize_optional_text(username)
    if cleaned is None:
        return None
    return (
        await session.execute(select(ModeratorRow.identifier).where(ModeratorRow.username == cleaned.lower()))
    ).scalar_one_or_none()


class PostgreSqlAdministrationRepository(AdministrationRepositoryPort):
    """Реализация `AdministrationRepositoryPort` на базе PostgreSQL 17."""

    def __init__(self, engine_manager: DatabaseEngineManager) -> None:
        """Инициализирует репозиторий администрирования, модерации, аудита и подписок.

        Arguments:
            engine_manager: Менеджер асинхронного движка и пула сессий PostgreSQL.
        """
        self._engine_manager = engine_manager

    async def create_subscription(self, *, subscription: SubscriptionRecord) -> SubscriptionRecord:
        """Создаёт или возвращает существующую подписку пользователя на фильтры.

        Arguments:
            subscription: Параметры оформляемой подписки без лишних персональных данных.

        Returns:
            Актуальная запись подписки.
        """
        cleaned_subscriber = subscription.subscriber_identifier.strip()
        if not cleaned_subscriber:
            raise ValidationException("Идентификатор подписчика не может быть пустым.")
        category_code = subscription.category.value if subscription.category is not None else None

        async with (
            handle_database_errors(operation="оформление подписки на мероприятия"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            city_identifier: UUID | None = None
            if subscription.city is not None and subscription.city.strip():
                city_row = await resolve_or_create_city(session, city_name=subscription.city)
                city_identifier = city_row.identifier

            insert_statement = (
                postgresql_insert(SubscriptionRow)
                .values(
                    identifier=subscription.subscription_identifier,
                    channel=subscription.channel.value,
                    subscriber_identifier=cleaned_subscriber,
                    city_identifier=city_identifier,
                    category_code=category_code,
                    created_at=subscription.created_at,
                )
                .on_conflict_do_nothing(constraint="unique_filter")
                .returning(SubscriptionRow.identifier)
            )
            inserted_id = (await session.execute(insert_statement)).scalar_one_or_none()

            if inserted_id is not None:
                row = (
                    await session.execute(
                        select(SubscriptionRow)
                        .where(SubscriptionRow.identifier == inserted_id)
                        .options(selectinload(SubscriptionRow.city))
                    )
                ).scalar_one()
                return map_subscription_from_row(row)

            existing_row = (
                await session.execute(
                    select(SubscriptionRow)
                    .where(
                        (SubscriptionRow.channel == subscription.channel.value)
                        & (SubscriptionRow.subscriber_identifier == cleaned_subscriber)
                        & SubscriptionRow.city_identifier.is_not_distinct_from(city_identifier)
                        & SubscriptionRow.category_code.is_not_distinct_from(category_code)
                    )
                    .options(selectinload(SubscriptionRow.city))
                )
            ).scalar_one()
            return map_subscription_from_row(existing_row)

    async def list_subscriptions_by_subscriber(
        self, *, channel: DialogChannel, subscriber_identifier: str
    ) -> tuple[SubscriptionRecord, ...]:
        """Возвращает активные подписки указанного пользователя в канале.

        Arguments:
            channel: Канал оформления подписки.
            subscriber_identifier: Технический идентификатор подписчика.

        Returns:
            Кортеж активных подписок пользователя.
        """
        cleaned_subscriber = subscriber_identifier.strip()
        if not cleaned_subscriber:
            return ()

        statement = (
            select(SubscriptionRow)
            .where(
                (SubscriptionRow.channel == channel.value)
                & (SubscriptionRow.subscriber_identifier == cleaned_subscriber)
            )
            .options(selectinload(SubscriptionRow.city))
            .order_by(SubscriptionRow.created_at.desc(), SubscriptionRow.identifier.asc())
        )
        async with (
            handle_database_errors(operation="чтение подписок пользователя"),
            self._engine_manager.session() as session,
        ):
            rows = (await session.execute(statement)).scalars().all()
            return tuple(map_subscription_from_row(row) for row in rows)

    async def delete_subscription(
        self, *, subscription_identifier: UUID, subscriber_identifier: str | None = None
    ) -> None:
        """Физически удаляет подписку по её идентификатору (требование минимизации ПДн).

        Arguments:
            subscription_identifier: Идентификатор удаляемой подписки.
            subscriber_identifier: Необязательная проверка владельца подписки.

        Raises:
            SubscriptionNotFoundError: Если указанная подписка не найдена.
        """
        base_statement = delete(SubscriptionRow).where(SubscriptionRow.identifier == subscription_identifier)
        cleaned_subscriber = normalize_optional_text(subscriber_identifier)
        if cleaned_subscriber is not None:
            base_statement = base_statement.where(SubscriptionRow.subscriber_identifier == cleaned_subscriber)
        statement = base_statement.returning(SubscriptionRow.identifier)

        async with (
            handle_database_errors(operation="удаление подписки пользователя"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            deleted_id = (await session.execute(statement)).scalar_one_or_none()
            if deleted_id is None:
                raise SubscriptionNotFoundError(
                    f"Подписка с идентификатором {subscription_identifier} не найдена.",
                    subscription_identifier=subscription_identifier,
                    subscriber_identifier=cleaned_subscriber,
                )

    async def delete_all_subscriptions_for_subscriber(
        self, *, channel: DialogChannel, subscriber_identifier: str
    ) -> int:
        """Физически удаляет все подписки пользователя одной командой.

        Arguments:
            channel: Канал пользователя.
            subscriber_identifier: Технический идентификатор подписчика.

        Returns:
            Количество удалённых подписок.
        """
        cleaned_subscriber = subscriber_identifier.strip()
        if not cleaned_subscriber:
            return 0

        statement = (
            delete(SubscriptionRow)
            .where(
                (SubscriptionRow.channel == channel.value)
                & (SubscriptionRow.subscriber_identifier == cleaned_subscriber)
            )
            .returning(SubscriptionRow.identifier)
        )
        async with (
            handle_database_errors(operation="удаление всех подписок пользователя"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            deleted_ids = (await session.execute(statement)).scalars().all()
            return len(deleted_ids)

    async def save_feedback_request(self, *, feedback_request: FeedbackRequestRecord) -> FeedbackRequestRecord:
        """Сохраняет обращение пользователя или заявку организатора мероприятия.

        Arguments:
            feedback_request: Регистрируемое обращение.

        Returns:
            Сохранённая запись обращения.
        """
        cleaned_message = feedback_request.message_text.strip()
        if not cleaned_message:
            raise ValidationException("Текст обращения не может быть пустым.")
        cleaned_contact = normalize_optional_text(feedback_request.contact_hint)

        row = FeedbackRequestRow(
            identifier=feedback_request.feedback_identifier,
            channel=feedback_request.channel.value,
            event_identifier=feedback_request.event_identifier,
            message_text=cleaned_message,
            contact_hint=cleaned_contact,
            status=feedback_request.status,
            created_at=feedback_request.created_at,
        )
        async with (
            handle_database_errors(operation="сохранение обращения или заявки организатора"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            session.add(row)
            await session.flush()
            return map_feedback_request_from_row(row)

    async def list_feedback_requests(
        self, *, status: FeedbackStatus | None = None, limit: int = 50
    ) -> tuple[FeedbackRequestRecord, ...]:
        """Возвращает список обращений организаторов и пользователей для модератора.

        Arguments:
            status: Необязательный фильтр по статусу обработки.
            limit: Максимальное число записей.

        Returns:
            Кортеж записей обращений.
        """
        validated_limit = validate_positive_limit(limit, parameter_name="limit", maximum=500)
        statement = select(FeedbackRequestRow)
        if status is not None:
            statement = statement.where(FeedbackRequestRow.status == status)
        statement = statement.order_by(FeedbackRequestRow.created_at.desc(), FeedbackRequestRow.identifier.asc()).limit(
            validated_limit
        )

        async with (
            handle_database_errors(operation="чтение списка обращений и заявок"),
            self._engine_manager.session() as session,
        ):
            rows = (await session.execute(statement)).scalars().all()
            return tuple(map_feedback_request_from_row(row) for row in rows)

    async def mark_feedback_reviewed(self, *, feedback_identifier: UUID) -> FeedbackRequestRecord | None:
        """Помечает обращение организатора как рассмотренное.

        Arguments:
            feedback_identifier: Идентификатор обращения.

        Returns:
            Обновлённая запись обращения или `None`, если обращение не найдено.
        """
        statement = (
            update(FeedbackRequestRow)
            .where(FeedbackRequestRow.identifier == feedback_identifier)
            .values(status="reviewed")
            .returning(FeedbackRequestRow)
        )
        async with (
            handle_database_errors(operation="обновление статуса обращения"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            updated_row = (await session.execute(statement)).scalar_one_or_none()
            return map_feedback_request_from_row(updated_row) if updated_row is not None else None

    async def enqueue_moderation_item(self, *, moderation_item: ModerationItem) -> ModerationItem:
        """Добавляет спорную или подозрительную карточку в очередь модерации.

        Arguments:
            moderation_item: Элемент очереди модерации.

        Returns:
            Сохранённый элемент очереди модерации.
        """
        cleaned_reason = moderation_item.reason.strip()
        if not cleaned_reason:
            raise ValidationException("Причина постановки карточки в очередь модерации не может быть пустой.")

        async with (
            handle_database_errors(operation="добавление карточки в очередь модерации"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            moderator_id = await _find_moderator_identifier(session, username=moderation_item.resolved_by)
            row = ModerationQueueRow(
                identifier=moderation_item.moderation_identifier,
                event_identifier=moderation_item.event_identifier,
                raw_event_identifier=moderation_item.raw_event_identifier,
                reason=cleaned_reason,
                status=moderation_item.status,
                has_suspicious_instructions=moderation_item.has_suspicious_instructions,
                matched_injection_patterns=list(moderation_item.matched_injection_patterns),
                created_at=moderation_item.created_at,
                resolved_at=moderation_item.resolved_at,
                resolved_by=normalize_optional_text(moderation_item.resolved_by),
                resolved_by_moderator_identifier=moderator_id,
            )
            session.add(row)
            await session.flush()
            return map_moderation_item_from_row(row)

    async def list_moderation_items(
        self, *, status: ModerationStatus | None = "pending", limit: int = 50
    ) -> tuple[ModerationItem, ...]:
        """Возвращает элементы очереди модерации с фильтрацией по статусу.

        Arguments:
            status: Статус задач модерации (`pending`, `approved`, `rejected`) или `None` для всех.
            limit: Максимальное количество элементов.

        Returns:
            Кортеж элементов очереди модерации.
        """
        validated_limit = validate_positive_limit(limit, parameter_name="limit", maximum=500)
        statement = select(ModerationQueueRow)
        if status is not None:
            statement = statement.where(ModerationQueueRow.status == status)
        statement = statement.order_by(ModerationQueueRow.created_at.asc(), ModerationQueueRow.identifier.asc()).limit(
            validated_limit
        )

        async with (
            handle_database_errors(operation="чтение очереди модерации"),
            self._engine_manager.session() as session,
        ):
            rows = (await session.execute(statement)).scalars().all()
            return tuple(map_moderation_item_from_row(row) for row in rows)

    async def resolve_moderation_item(
        self,
        *,
        moderation_identifier: UUID,
        status: Literal["approved", "rejected"],
        resolved_by: str,
        resolved_at: datetime,
    ) -> ModerationItem:
        """Фиксирует решение модератора по спорной карточке.

        Arguments:
            moderation_identifier: Идентификатор элемента модерации.
            status: Итоговое решение (`approved` или `rejected`).
            resolved_by: Идентификатор модератора.
            resolved_at: Временная метка принятия решения.

        Returns:
            Обновлённая запись очереди модерации.

        Raises:
            ModerationItemNotFoundError: Если элемент модерации не найден.
        """
        cleaned_moderator = resolved_by.strip()
        if not cleaned_moderator:
            raise ValidationException("Имя модератора при вынесении решения не может быть пустым.")

        async with (
            handle_database_errors(operation="фиксация решения модератора"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            moderator_id = await _find_moderator_identifier(session, username=cleaned_moderator)
            statement = (
                update(ModerationQueueRow)
                .where(ModerationQueueRow.identifier == moderation_identifier)
                .values(
                    status=status,
                    resolved_by=cleaned_moderator,
                    resolved_at=resolved_at,
                    resolved_by_moderator_identifier=moderator_id,
                )
                .returning(ModerationQueueRow)
            )
            updated_row = (await session.execute(statement)).scalar_one_or_none()
            if updated_row is None:
                raise ModerationItemNotFoundError(
                    f"Элемент очереди модерации {moderation_identifier} не найден.",
                    moderation_identifier=moderation_identifier,
                )
            return map_moderation_item_from_row(updated_row)

    async def append_audit_log(self, *, entry: AuditLogEntry) -> AuditLogEntry:
        """Добавляет неизменяемую запись в журнал изменений каталога и модерации.

        Arguments:
            entry: Запись аудита (кто, когда и что изменил).

        Returns:
            Сохранённая запись журнала изменений.
        """
        cleaned_actor = entry.actor.strip()
        cleaned_action = entry.action.strip()
        cleaned_entity_type = entry.entity_type.strip()
        cleaned_entity_id = entry.entity_identifier.strip()
        if not (cleaned_actor and cleaned_action and cleaned_entity_type and cleaned_entity_id):
            raise ValidationException("Все обязательные поля записи аудита должны быть заполнены.")

        async with (
            handle_database_errors(operation="запись события в журнал изменений"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            moderator_id = await _find_moderator_identifier(session, username=cleaned_actor)
            row = AuditLogRow(
                identifier=entry.entry_identifier,
                moderator_identifier=moderator_id,
                actor=cleaned_actor,
                action=cleaned_action,
                entity_type=cleaned_entity_type,
                entity_identifier=cleaned_entity_id,
                changes=dict(entry.changes),
                created_at=entry.created_at,
            )
            session.add(row)
            await session.flush()
            return map_audit_log_from_row(row)

    async def list_audit_logs(
        self, *, entity_identifier: str | None = None, limit: int = 100
    ) -> tuple[AuditLogEntry, ...]:
        """Возвращает последние записи журнала изменений каталога.

        Arguments:
            entity_identifier: Необязательный фильтр по идентификатору изменённой сущности.
            limit: Максимальное число записей.

        Returns:
            Кортеж записей аудита от новых к старым.
        """
        validated_limit = validate_positive_limit(limit, parameter_name="limit", maximum=500)
        statement = select(AuditLogRow)
        cleaned_entity_id = normalize_optional_text(entity_identifier)
        if cleaned_entity_id is not None:
            statement = statement.where(AuditLogRow.entity_identifier == cleaned_entity_id)
        statement = statement.order_by(AuditLogRow.created_at.desc(), AuditLogRow.identifier.asc()).limit(
            validated_limit
        )

        async with (
            handle_database_errors(operation="чтение журнала изменений каталога"),
            self._engine_manager.session() as session,
        ):
            rows = (await session.execute(statement)).scalars().all()
            return tuple(map_audit_log_from_row(row) for row in rows)
