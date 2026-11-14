"""Функции отображения между ORM-строками PostgreSQL 17 и неизменяемыми доменными сущностями."""

from typing import Literal

from src.core.exceptions import ValidationException
from src.core.types import DialogChannel, EventCategory, EventStatus, SourceType
from src.domain.entities import (
    AgentToolInvocation,
    AgentTraceRecord,
    AuditLogEntry,
    CityReference,
    Coordinates,
    DialogMessage,
    DialogRole,
    DialogSessionState,
    Event,
    EventSourceReference,
    FeedbackRequestRecord,
    FeedbackStatus,
    ModerationItem,
    ModerationStatus,
    ModeratorAccount,
    PendingAction,
    PendingActionType,
    RawEventRecord,
    SubscriptionNotificationRecord,
    SubscriptionNotificationStatus,
    SubscriptionRecord,
    UsageBudgetStatus,
    VenueReference,
)
from src.infrastructure.database.tables import (
    AgentTraceRow,
    AgentTraceToolInvocationRow,
    AuditLogRow,
    CityRow,
    DialogMessageRow,
    DialogSessionRow,
    EventRow,
    EventSourceRow,
    FeedbackRequestRow,
    ModerationQueueRow,
    ModeratorRow,
    PendingActionRow,
    RawEventRow,
    SubscriptionNotificationRow,
    SubscriptionRow,
    VenueRow,
)

__all__ = [
    "map_agent_trace_from_row",
    "map_audit_log_from_row",
    "map_city_from_row",
    "map_dialog_message_from_row",
    "map_dialog_session_from_row",
    "map_event_from_row",
    "map_event_source_from_row",
    "map_feedback_request_from_row",
    "map_moderation_item_from_row",
    "map_moderator_from_row",
    "map_pending_action_from_row",
    "map_raw_event_from_row",
    "map_subscription_from_row",
    "map_subscription_notification_from_row",
    "map_usage_budget_status",
    "map_venue_from_row",
]


def _build_coordinates(latitude: float | None, longitude: float | None) -> Coordinates | None:
    if latitude is None or longitude is None:
        return None
    return Coordinates(latitude=latitude, longitude=longitude)


def _cast_literal_role(role: str) -> DialogRole:
    if role == "user":
        return "user"
    if role == "assistant":
        return "assistant"
    raise ValidationException(f"Недопустимая роль сообщения диалога в базе данных: {role!r}.")


def _cast_pending_action_type(action_type: str) -> PendingActionType:
    if action_type == "subscribe":
        return "subscribe"
    if action_type == "save_feedback":
        return "save_feedback"
    raise ValidationException(f"Недопустимый тип отложенного действия в базе данных: {action_type!r}.")


def _cast_raw_normalization_status(status: str) -> Literal["pending", "normalized", "failed"]:
    if status == "pending":
        return "pending"
    if status == "normalized":
        return "normalized"
    if status == "failed":
        return "failed"
    raise ValidationException(f"Недопустимый статус нормализации сырой записи: {status!r}.")


def _cast_moderation_status(status: str) -> ModerationStatus:
    if status == "pending":
        return "pending"
    if status == "approved":
        return "approved"
    if status == "rejected":
        return "rejected"
    raise ValidationException(f"Недопустимый статус модерации в базе данных: {status!r}.")


def _cast_feedback_status(status: str) -> FeedbackStatus:
    if status == "new":
        return "new"
    if status == "reviewed":
        return "reviewed"
    raise ValidationException(f"Недопустимый статус обращения в базе данных: {status!r}.")


def _cast_subscription_notification_status(status: str) -> SubscriptionNotificationStatus:
    if status == "pending":
        return "pending"
    if status == "sending":
        return "sending"
    if status == "sent":
        return "sent"
    if status == "failed":
        return "failed"
    if status == "read":
        return "read"
    raise ValidationException(f"Недопустимый статус уведомления по подписке в базе данных: {status!r}.")


def map_city_from_row(row: CityRow) -> CityReference:
    """Преобразует ORM-строку города и его загруженные алиасы в `CityReference`.

    Arguments:
        row: ORM-строка таблицы `cities` с предзагруженным отношением `aliases`.

    Returns:
        Неизменяемая запись справочника городов.
    """
    return CityReference(
        name=row.name,
        normalized_name=row.normalized_name,
        aliases=tuple(alias.alias for alias in row.aliases),
        center_coordinates=_build_coordinates(row.center_latitude, row.center_longitude),
    )


def map_venue_from_row(row: VenueRow) -> VenueReference:
    """Преобразует ORM-строку площадки с загруженными городом и алиасами в `VenueReference`.

    Arguments:
        row: ORM-строка таблицы `venues` с предзагруженными `city` и `aliases`.

    Returns:
        Неизменяемая запись справочника площадок.
    """
    return VenueReference(
        name=row.name,
        normalized_name=row.normalized_name,
        city=row.city.name,
        address=row.address,
        aliases=tuple(alias.alias for alias in row.aliases),
        coordinates=_build_coordinates(row.latitude, row.longitude),
    )


def map_event_source_from_row(row: EventSourceRow) -> EventSourceReference:
    """Преобразует ORM-строку подтверждённого источника в `EventSourceReference`.

    Arguments:
        row: ORM-строка таблицы `event_sources`.

    Returns:
        Неизменяемые сведения о подтверждённом источнике.
    """
    return EventSourceReference(
        source_name=row.source_name,
        source_type=SourceType(row.source_type),
        source_url=row.source_url,
        source_hash=row.source_hash,
        verified_at=row.verified_at,
    )


def map_event_from_row(row: EventRow) -> Event:
    """Собирает доменную карточку `Event` из нормализованных таблиц каталога.

    Восстанавливает город и площадку из `venue -> city`, теги из `tags` и
    основной источник с полным перечнем подтверждений из `sources`.

    Arguments:
        row: ORM-строка таблицы `events` с предзагруженными `venue.city`, `sources` и `tags`.

    Returns:
        Неизменяемая доменная сущность мероприятия.

    Raises:
        ValidationException: Если у мероприятия отсутствуют записи источников в `event_sources`.
    """
    if not row.sources:
        raise ValidationException(f"У карточки мероприятия {row.identifier} отсутствуют источники в event_sources.")

    ordered_sources = sorted(row.sources, key=lambda source: (not source.is_primary, source.created_at))
    primary_source = next((source for source in ordered_sources if source.is_primary), ordered_sources[0])

    return Event(
        identifier=row.identifier,
        title=row.title,
        normalized_title=row.normalized_title,
        category=EventCategory(row.category_code),
        tags=tuple(tag.name for tag in row.tags),
        date_start=row.date_start,
        date_end=row.date_end,
        duration_minutes=row.duration_minutes,
        city=row.venue.city.name,
        venue=row.venue.name,
        address=row.venue.address,
        latitude=row.venue.latitude,
        longitude=row.venue.longitude,
        description=row.description,
        price_minimum=row.price_minimum,
        price_maximum=row.price_maximum,
        is_free=row.is_free,
        age_limit=row.age_limit,
        language=row.language,
        organization_link=row.organization_link,
        source_url=primary_source.source_url,
        source_type=SourceType(primary_source.source_type),
        source_hash=primary_source.source_hash,
        verified_at=primary_source.verified_at,
        confidence=row.confidence,
        status=EventStatus(row.status),
        deduplication_key=row.deduplication_key,
        created_at=row.created_at,
        updated_at=row.updated_at,
        sources=tuple(map_event_source_from_row(source) for source in ordered_sources),
    )


def map_raw_event_from_row(row: RawEventRow) -> RawEventRecord:
    """Преобразует ORM-строку сырой записи источника в `RawEventRecord`.

    Arguments:
        row: ORM-строка таблицы `raw_events`.

    Returns:
        Неизменяемая доменная запись сырых данных.
    """
    return RawEventRecord(
        identifier=row.identifier,
        source_name=row.source_name,
        source_type=SourceType(row.source_type),
        external_identifier=row.external_identifier,
        source_url=row.source_url,
        source_hash=row.source_hash,
        raw_payload=dict(row.raw_payload),
        fetched_at=row.fetched_at,
        normalization_status=_cast_raw_normalization_status(row.normalization_status),
        error_message=row.error_message,
    )


def map_moderator_from_row(row: ModeratorRow) -> ModeratorAccount:
    """Преобразует ORM-строку модератора в `ModeratorAccount`.

    Arguments:
        row: ORM-строка таблицы `moderators`.

    Returns:
        Неизменяемая учётная запись модератора.
    """
    return ModeratorAccount(
        moderator_identifier=row.identifier,
        username=row.username,
        display_name=row.display_name,
        password_hash=row.password_hash,
        is_active=row.is_active,
        created_at=row.created_at,
        last_login_at=row.last_login_at,
    )


def map_moderation_item_from_row(row: ModerationQueueRow) -> ModerationItem:
    """Преобразует ORM-строку очереди модерации в `ModerationItem`.

    Arguments:
        row: ORM-строка таблицы `moderation_queue`.

    Returns:
        Неизменяемый элемент очереди модерации.
    """
    return ModerationItem(
        moderation_identifier=row.identifier,
        event_identifier=row.event_identifier,
        raw_event_identifier=row.raw_event_identifier,
        reason=row.reason,
        status=_cast_moderation_status(row.status),
        has_suspicious_instructions=row.has_suspicious_instructions,
        matched_injection_patterns=tuple(str(pattern) for pattern in row.matched_injection_patterns),
        created_at=row.created_at,
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
    )


def map_audit_log_from_row(row: AuditLogRow) -> AuditLogEntry:
    """Преобразует ORM-строку журнала изменений в `AuditLogEntry`.

    Arguments:
        row: ORM-строка таблицы `audit_logs`.

    Returns:
        Неизменяемая запись журнала изменений.
    """
    return AuditLogEntry(
        entry_identifier=row.identifier,
        actor=row.actor,
        action=row.action,
        entity_type=row.entity_type,
        entity_identifier=row.entity_identifier,
        changes=dict(row.changes),
        created_at=row.created_at,
    )


def map_subscription_from_row(row: SubscriptionRow) -> SubscriptionRecord:
    """Преобразует ORM-строку подписки с загруженным городом в `SubscriptionRecord`.

    Arguments:
        row: ORM-строка таблицы `subscriptions` с предзагруженным `city`.

    Returns:
        Неизменяемая запись подписки пользователя.
    """
    return SubscriptionRecord(
        subscription_identifier=row.identifier,
        channel=DialogChannel(row.channel),
        subscriber_identifier=row.subscriber_identifier,
        city=row.city.name if row.city is not None else None,
        category=EventCategory(row.category_code) if row.category_code is not None else None,
        created_at=row.created_at,
    )


def map_subscription_notification_from_row(row: SubscriptionNotificationRow) -> SubscriptionNotificationRecord:
    """Преобразует ORM-строку уведомления по подписке с загруженным событием в `SubscriptionNotificationRecord`.

    Arguments:
        row: ORM-строка таблицы `subscription_notifications` с предзагруженным `event`.

    Returns:
        Неизменяемая запись уведомления по подписке.
    """
    return SubscriptionNotificationRecord(
        notification_identifier=row.identifier,
        subscription_identifier=row.subscription_identifier,
        channel=DialogChannel(row.channel),
        subscriber_identifier=row.subscriber_identifier,
        event=map_event_from_row(row.event),
        status=_cast_subscription_notification_status(row.status),
        attempts=row.attempts,
        last_error=row.last_error,
        next_retry_at=row.next_retry_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        sent_at=row.sent_at,
        read_at=row.read_at,
    )


def map_feedback_request_from_row(row: FeedbackRequestRow) -> FeedbackRequestRecord:
    """Преобразует ORM-строку обращения организатора или пользователя в `FeedbackRequestRecord`.

    Arguments:
        row: ORM-строка таблицы `feedback_requests`.

    Returns:
        Неизменяемая запись обращения.
    """
    return FeedbackRequestRecord(
        feedback_identifier=row.identifier,
        channel=DialogChannel(row.channel),
        event_identifier=row.event_identifier,
        message_text=row.message_text,
        contact_hint=row.contact_hint,
        created_at=row.created_at,
        status=_cast_feedback_status(row.status),
    )


def map_dialog_message_from_row(row: DialogMessageRow) -> DialogMessage:
    """Преобразует ORM-строку сообщения диалога в `DialogMessage`.

    Arguments:
        row: ORM-строка таблицы `dialog_messages`.

    Returns:
        Неизменяемая реплика диалога.
    """
    return DialogMessage(role=_cast_literal_role(row.role), content=row.content, created_at=row.created_at)


def map_pending_action_from_row(row: PendingActionRow, *, channel: DialogChannel | None = None) -> PendingAction:
    """Преобразует ORM-строку отложенного действия в `PendingAction`.

    Arguments:
        row: ORM-строка таблицы `pending_actions` (при отсутствии `channel` требуется загруженный `row.session`).
        channel: Необязательный канал диалога, если он уже известен из контекста сессии.

    Returns:
        Неизменяемое отложенное действие, ожидающее подтверждения.
    """
    resolved_channel = channel if channel is not None else DialogChannel(row.session.channel)
    return PendingAction(
        action_identifier=row.action_identifier,
        session_identifier=row.session_identifier,
        channel=resolved_channel,
        action_type=_cast_pending_action_type(row.action_type),
        payload=dict(row.payload),
        confirmation_prompt=row.confirmation_prompt,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def map_dialog_session_from_row(
    session_row: DialogSessionRow,
    *,
    recent_messages: list[DialogMessageRow],
    active_pending_action: PendingActionRow | None,
) -> DialogSessionState:
    """Собирает `DialogSessionState` из строки сессии, последних сообщений и активного подтверждения.

    Arguments:
        session_row: ORM-строка таблицы `dialog_sessions`.
        recent_messages: Последние сообщения сессии в хронологическом порядке.
        active_pending_action: Активное неподтверждённое действие либо `None`.

    Returns:
        Неизменяемое состояние диалоговой сессии.
    """
    channel = DialogChannel(session_row.channel)
    mapped_action = (
        map_pending_action_from_row(active_pending_action, channel=channel)
        if active_pending_action is not None
        else None
    )
    return DialogSessionState(
        session_identifier=session_row.session_identifier,
        channel=channel,
        subscriber_identifier=session_row.subscriber_identifier,
        recent_messages=tuple(map_dialog_message_from_row(message) for message in recent_messages),
        active_pending_action=mapped_action,
    )


def _map_tool_invocation_from_row(row: AgentTraceToolInvocationRow) -> AgentToolInvocation:
    return AgentToolInvocation(
        tool_name=row.tool_name,
        arguments=dict(row.arguments),
        returned_event_identifiers=tuple(row.returned_event_identifiers),
        succeeded=row.succeeded,
        error_message=row.error_message,
    )


def map_agent_trace_from_row(row: AgentTraceRow) -> AgentTraceRecord:
    """Собирает `AgentTraceRecord` из таблицы трасс и нормализованных дочерних таблиц.

    Arguments:
        row: ORM-строка `agent_traces` с предзагруженными `tool_invocations` и `grounded_events`.

    Returns:
        Неизменяемая полная трасса ответа ИИ-агента.
    """
    ordered_invocations = sorted(row.tool_invocations, key=lambda invocation: invocation.sequence_number)
    return AgentTraceRecord(
        trace_identifier=row.trace_identifier,
        request_identifier=row.request_identifier,
        session_identifier=row.session_identifier,
        channel=DialogChannel(row.channel),
        user_query=row.user_query,
        plan_summary=row.plan_summary,
        tool_invocations=tuple(_map_tool_invocation_from_row(invocation) for invocation in ordered_invocations),
        grounded_event_identifiers=tuple(item.event_identifier for item in row.grounded_events),
        fallback_used=row.fallback_used,
        latency_milliseconds=row.latency_milliseconds,
        tokens_input=row.tokens_input,
        tokens_output=row.tokens_output,
        cost_rubles=float(row.cost_rubles),
        model_name=row.model_name,
        prompt_version=row.prompt_version,
        created_at=row.created_at,
    )


def map_usage_budget_status(
    *,
    user_daily_spent_rubles: float,
    user_daily_limit_rubles: float,
    global_daily_spent_rubles: float,
    global_daily_limit_rubles: float,
) -> UsageBudgetStatus:
    """Формирует снимок суточного бюджета ИИ-модели с вычислением флага превышения.

    Arguments:
        user_daily_spent_rubles: Суточные расходы конкретного пользователя в рублях.
        user_daily_limit_rubles: Суточный лимит на одного пользователя в рублях.
        global_daily_spent_rubles: Суммарные суточные расходы сервиса в рублях.
        global_daily_limit_rubles: Общий суточный лимит сервиса в рублях.

    Returns:
        Неизменяемый снимок статуса лимитов использования модели.
    """
    is_budget_exceeded = (
        user_daily_spent_rubles >= user_daily_limit_rubles or global_daily_spent_rubles >= global_daily_limit_rubles
    )
    return UsageBudgetStatus(
        user_daily_spent_rubles=user_daily_spent_rubles,
        user_daily_limit_rubles=user_daily_limit_rubles,
        global_daily_spent_rubles=global_daily_spent_rubles,
        global_daily_limit_rubles=global_daily_limit_rubles,
        is_budget_exceeded=is_budget_exceeded,
    )
