"""PostgreSQL-репозиторий модерации, аудита, подписок и обращений организаторов."""

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import DateTime, and_, case, cast, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.base import ExecutableOption

from src.core.exceptions import ValidationException
from src.core.logger import get_logger
from src.core.types import DialogChannel, EventStatus
from src.domain.entities import (
    AuditLogEntry,
    Event,
    FeedbackRequestRecord,
    FeedbackStatus,
    ModerationItem,
    ModerationStatus,
    SubscriptionNotificationRecord,
    SubscriptionRecord,
)
from src.domain.exceptions import ModerationItemNotFoundError, SubscriptionNotFoundError
from src.domain.ports import AdministrationRepositoryPort
from src.domain.services.normalization import normalize_city, normalize_optional_text, normalize_text_for_matching
from src.infrastructure.database._helpers import resolve_or_create_city, validate_positive_limit
from src.infrastructure.database.engine import DatabaseEngineManager
from src.infrastructure.database.errors import handle_database_errors
from src.infrastructure.database.mappers import (
    map_audit_log_from_row,
    map_feedback_request_from_row,
    map_moderation_item_from_row,
    map_subscription_from_row,
    map_subscription_notification_from_row,
)
from src.infrastructure.database.tables import (
    AuditLogRow,
    CityAliasRow,
    CityRow,
    EventRow,
    FeedbackRequestRow,
    ModerationQueueRow,
    ModeratorRow,
    SubscriptionNotificationRow,
    SubscriptionRow,
    VenueRow,
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


def _notification_load_options() -> tuple[ExecutableOption, ...]:
    return (
        selectinload(SubscriptionNotificationRow.event).selectinload(EventRow.venue).selectinload(VenueRow.city),
        selectinload(SubscriptionNotificationRow.event).selectinload(EventRow.sources),
        selectinload(SubscriptionNotificationRow.event).selectinload(EventRow.tags),
    )


async def _find_city_identifier_by_name(session: AsyncSession, *, city_name: str) -> UUID | None:
    cleaned_city = normalize_optional_text(city_name)
    if cleaned_city is None:
        return None
    canonical_city = normalize_city(cleaned_city) or cleaned_city
    candidates = tuple(
        {
            value
            for value in (normalize_text_for_matching(canonical_city), normalize_text_for_matching(cleaned_city))
            if value
        }
    )
    if not candidates:
        return None

    city_identifier = (
        await session.execute(select(CityRow.identifier).where(CityRow.normalized_name.in_(candidates)).limit(1))
    ).scalar_one_or_none()
    if city_identifier is not None:
        return city_identifier

    return (
        await session.execute(
            select(CityAliasRow.city_identifier).where(CityAliasRow.normalized_alias.in_(candidates)).limit(1)
        )
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

    async def enqueue_subscription_notifications_for_event(self, *, event: Event, created_at: datetime) -> int:
        """Создаёт уведомления для всех подписок, подходящих под опубликованное мероприятие.

        Arguments:
            event: Опубликованная карточка мероприятия.
            created_at: Временная метка постановки уведомлений в очередь.

        Returns:
            Количество фактически созданных новых записей уведомлений.
        """
        if event.status is not EventStatus.PUBLISHED:
            return 0

        async with (
            handle_database_errors(operation="постановка уведомлений по подпискам в очередь"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            event_city_id = await _find_city_identifier_by_name(session, city_name=event.city)
            city_condition = (
                SubscriptionRow.city_identifier.is_(None) | (SubscriptionRow.city_identifier == event_city_id)
                if event_city_id is not None
                else SubscriptionRow.city_identifier.is_(None)
            )
            category_condition = SubscriptionRow.category_code.is_(None) | (
                SubscriptionRow.category_code == event.category.value
            )

            matching_subscriptions = (
                (
                    await session.execute(
                        select(SubscriptionRow)
                        .where(city_condition & category_condition)
                        .order_by(SubscriptionRow.created_at.asc(), SubscriptionRow.identifier.asc())
                    )
                )
                .scalars()
                .all()
            )
            if not matching_subscriptions:
                return 0

            rows_payload = [
                {
                    "subscription_identifier": subscription.identifier,
                    "event_identifier": event.identifier,
                    "channel": subscription.channel,
                    "subscriber_identifier": subscription.subscriber_identifier,
                    "status": "pending",
                    "attempts": 0,
                    "last_error": None,
                    "next_retry_at": None,
                    "created_at": created_at,
                    "updated_at": created_at,
                    "sent_at": None,
                    "read_at": None,
                }
                for subscription in matching_subscriptions
            ]
            insert_statement = (
                postgresql_insert(SubscriptionNotificationRow)
                .values(rows_payload)
                .on_conflict_do_nothing(index_elements=["channel", "subscriber_identifier", "event_identifier"])
                .returning(SubscriptionNotificationRow.identifier)
            )
            inserted_identifiers = (await session.execute(insert_statement)).scalars().all()
            return len(inserted_identifiers)

    async def claim_pending_telegram_notifications(
        self, *, current_time: datetime, limit: int = 50, maximum_attempts: int = 3, stale_after_seconds: int = 300
    ) -> tuple[SubscriptionNotificationRecord, ...]:
        """Атомарно резервирует пакет готовых к отправке Telegram-уведомлений.

        Arguments:
            current_time: Текущее время расчёта готовности и таймаутов зависших отправок.
            limit: Максимальное количество уведомлений в пакете.
            maximum_attempts: Максимально допустимое число попыток отправки.
            stale_after_seconds: Порог времени, после которого зависшая отправка в статусе `sending`
                возвращается в обработку.

        Returns:
            Кортеж зарезервированных уведомлений с предзагруженными карточками мероприятий.
        """
        validated_limit = validate_positive_limit(limit, parameter_name="limit", maximum=500)
        if maximum_attempts < 1:
            raise ValidationException("Параметр «maximum_attempts» должен быть не меньше 1.")
        if stale_after_seconds < 0:
            raise ValidationException("Параметр «stale_after_seconds» не может быть отрицательным.")

        stale_cutoff = current_time - timedelta(seconds=stale_after_seconds)
        pending_ready = and_(
            SubscriptionNotificationRow.status == "pending",
            or_(
                SubscriptionNotificationRow.next_retry_at.is_(None),
                SubscriptionNotificationRow.next_retry_at <= current_time,
            ),
        )
        stale_sending = and_(
            SubscriptionNotificationRow.status == "sending", SubscriptionNotificationRow.updated_at <= stale_cutoff
        )
        candidate_statement = (
            select(SubscriptionNotificationRow.identifier)
            .join(EventRow, EventRow.identifier == SubscriptionNotificationRow.event_identifier)
            .where(
                (SubscriptionNotificationRow.channel == DialogChannel.TELEGRAM.value)
                & (EventRow.status == EventStatus.PUBLISHED.value)
                & (SubscriptionNotificationRow.attempts < maximum_attempts)
                & or_(pending_ready, stale_sending)
            )
            .order_by(SubscriptionNotificationRow.created_at.asc(), SubscriptionNotificationRow.identifier.asc())
            .limit(validated_limit)
            .with_for_update(of=SubscriptionNotificationRow, skip_locked=True)
        )

        async with (
            handle_database_errors(operation="резервирование очереди Telegram-уведомлений"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            claimed_identifiers = list((await session.execute(candidate_statement)).scalars().all())
            if not claimed_identifiers:
                return ()

            update_statement = (
                update(SubscriptionNotificationRow)
                .where(SubscriptionNotificationRow.identifier.in_(claimed_identifiers))
                .values(status="sending", attempts=SubscriptionNotificationRow.attempts + 1, updated_at=current_time)
            )
            await session.execute(update_statement)

            rows_statement = (
                select(SubscriptionNotificationRow)
                .where(SubscriptionNotificationRow.identifier.in_(claimed_identifiers))
                .options(*_notification_load_options())
            )
            rows = (await session.execute(rows_statement)).scalars().all()
            rows_by_identifier = {row.identifier: row for row in rows}
            return tuple(
                map_subscription_notification_from_row(rows_by_identifier[identifier])
                for identifier in claimed_identifiers
                if identifier in rows_by_identifier
            )

    async def mark_subscription_notification_sent(self, *, notification_identifier: UUID, sent_at: datetime) -> None:
        """Помечает уведомление по подписке как успешно доставленное.

        Arguments:
            notification_identifier: Идентификатор уведомления.
            sent_at: Временная метка успешной доставки.
        """
        statement = (
            update(SubscriptionNotificationRow)
            .where(SubscriptionNotificationRow.identifier == notification_identifier)
            .values(status="sent", sent_at=sent_at, updated_at=sent_at, last_error=None, next_retry_at=None)
        )
        async with (
            handle_database_errors(operation="фиксация успешной отправки уведомления по подписке"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            await session.execute(statement)

    async def mark_subscription_notification_failed(
        self,
        *,
        notification_identifier: UUID,
        error_message: str,
        failed_at: datetime,
        retry_at: datetime | None = None,
        is_permanent: bool = False,
        maximum_attempts: int = 3,
    ) -> None:
        """Фиксирует ошибку попытки доставки уведомления и переводит его в повтор или `failed`.

        Arguments:
            notification_identifier: Идентификатор уведомления.
            error_message: Диагностическое описание ошибки доставки.
            failed_at: Временная метка неудачной попытки.
            retry_at: Временная метка следующей попытки (если повтор разрешён).
            is_permanent: Признак постоянной ошибки без дальнейших повторов.
            maximum_attempts: Максимальное число попыток до перевода в окончательный статус `failed`.
        """
        if maximum_attempts < 1:
            raise ValidationException("Параметр «maximum_attempts» должен быть не меньше 1.")

        cleaned_error = error_message.strip()
        truncated_error = cleaned_error[:1000] if cleaned_error else "Неизвестная ошибка доставки уведомления."

        if is_permanent:
            statement = (
                update(SubscriptionNotificationRow)
                .where(SubscriptionNotificationRow.identifier == notification_identifier)
                .values(status="failed", last_error=truncated_error, next_retry_at=None, updated_at=failed_at)
            )
        else:
            is_exhausted = SubscriptionNotificationRow.attempts >= maximum_attempts
            statement = (
                update(SubscriptionNotificationRow)
                .where(SubscriptionNotificationRow.identifier == notification_identifier)
                .values(
                    status=case((is_exhausted, "failed"), else_="pending"),
                    last_error=truncated_error,
                    next_retry_at=case(
                        (is_exhausted, cast(None, DateTime(timezone=True))),
                        else_=cast(retry_at, DateTime(timezone=True)),
                    ),
                    updated_at=failed_at,
                )
            )

        async with (
            handle_database_errors(operation="фиксация ошибки отправки уведомления по подписке"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            await session.execute(statement)

    async def count_unread_web_subscription_notifications(self, *, subscriber_identifier: str) -> int:
        """Подсчитывает количество непрочитанных веб-уведомлений пользователя.

        Arguments:
            subscriber_identifier: Технический идентификатор подписчика в веб-канале.

        Returns:
            Количество непрочитанных уведомлений по опубликованным мероприятиям.
        """
        cleaned_subscriber = subscriber_identifier.strip()
        if not cleaned_subscriber:
            return 0

        statement = (
            select(func.count(SubscriptionNotificationRow.identifier))
            .join(EventRow, EventRow.identifier == SubscriptionNotificationRow.event_identifier)
            .where(
                (SubscriptionNotificationRow.channel == DialogChannel.WEB.value)
                & (SubscriptionNotificationRow.subscriber_identifier == cleaned_subscriber)
                & (SubscriptionNotificationRow.status != "read")
                & (EventRow.status == EventStatus.PUBLISHED.value)
            )
        )
        async with (
            handle_database_errors(operation="подсчёт непрочитанных веб-уведомлений по подпискам"),
            self._engine_manager.session() as session,
        ):
            return int((await session.execute(statement)).scalar_one())

    async def list_web_subscription_notifications(
        self, *, subscriber_identifier: str, include_read: bool = False, limit: int = 20
    ) -> tuple[SubscriptionNotificationRecord, ...]:
        """Возвращает список веб-уведомлений пользователя по подпискам.

        Arguments:
            subscriber_identifier: Технический идентификатор подписчика в веб-канале.
            include_read: Включать ли уже прочитанные уведомления.
            limit: Максимальное количество возвращаемых записей.

        Returns:
            Кортеж веб-уведомлений от новых к старым.
        """
        validated_limit = validate_positive_limit(limit, parameter_name="limit", maximum=500)
        cleaned_subscriber = subscriber_identifier.strip()
        if not cleaned_subscriber:
            return ()

        conditions = [
            SubscriptionNotificationRow.channel == DialogChannel.WEB.value,
            SubscriptionNotificationRow.subscriber_identifier == cleaned_subscriber,
            EventRow.status == EventStatus.PUBLISHED.value,
        ]
        if not include_read:
            conditions.append(SubscriptionNotificationRow.status != "read")

        statement = (
            select(SubscriptionNotificationRow)
            .join(EventRow, EventRow.identifier == SubscriptionNotificationRow.event_identifier)
            .where(*conditions)
            .options(*_notification_load_options())
            .order_by(SubscriptionNotificationRow.created_at.desc(), SubscriptionNotificationRow.identifier.desc())
            .limit(validated_limit)
        )
        async with (
            handle_database_errors(operation="чтение списка веб-уведомлений по подпискам"),
            self._engine_manager.session() as session,
        ):
            rows = (await session.execute(statement)).scalars().all()
            return tuple(map_subscription_notification_from_row(row) for row in rows)

    async def mark_web_subscription_notification_read(
        self, *, notification_identifier: UUID, subscriber_identifier: str, read_at: datetime
    ) -> bool:
        """Помечает конкретное веб-уведомление пользователя как прочитанное.

        Arguments:
            notification_identifier: Идентификатор уведомления.
            subscriber_identifier: Технический идентификатор владельца уведомления.
            read_at: Временная метка прочтения.

        Returns:
            `True`, если уведомление найдено у данного подписчика и отмечено прочитанным, иначе `False`.
        """
        cleaned_subscriber = subscriber_identifier.strip()
        if not cleaned_subscriber:
            return False

        statement = (
            update(SubscriptionNotificationRow)
            .where(
                (SubscriptionNotificationRow.identifier == notification_identifier)
                & (SubscriptionNotificationRow.channel == DialogChannel.WEB.value)
                & (SubscriptionNotificationRow.subscriber_identifier == cleaned_subscriber)
            )
            .values(
                status="read", read_at=func.coalesce(SubscriptionNotificationRow.read_at, read_at), updated_at=read_at
            )
            .returning(SubscriptionNotificationRow.identifier)
        )
        async with (
            handle_database_errors(operation="отметка веб-уведомления прочитанным"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            updated_identifier = (await session.execute(statement)).scalar_one_or_none()
            return updated_identifier is not None

    async def mark_all_web_subscription_notifications_read(
        self, *, subscriber_identifier: str, read_at: datetime
    ) -> int:
        """Помечает все непрочитанные веб-уведомления пользователя как прочитанные.

        Arguments:
            subscriber_identifier: Технический идентификатор подписчика в веб-канале.
            read_at: Временная метка прочтения.

        Returns:
            Количество уведомлений, переведённых в статус `read`.
        """
        cleaned_subscriber = subscriber_identifier.strip()
        if not cleaned_subscriber:
            return 0

        statement = (
            update(SubscriptionNotificationRow)
            .where(
                (SubscriptionNotificationRow.channel == DialogChannel.WEB.value)
                & (SubscriptionNotificationRow.subscriber_identifier == cleaned_subscriber)
                & (SubscriptionNotificationRow.status != "read")
            )
            .values(status="read", read_at=read_at, updated_at=read_at)
            .returning(SubscriptionNotificationRow.identifier)
        )
        async with (
            handle_database_errors(operation="массовая отметка веб-уведомлений прочитанными"),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            updated_identifiers = (await session.execute(statement)).scalars().all()
            return len(updated_identifiers)
