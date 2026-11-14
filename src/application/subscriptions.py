"""Прикладной сервис управления подписками пользователей и обращениями организаторов."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from src.application._helpers import attach_default_timezone, resolve_normalized_city
from src.core.exceptions import ValidationException
from src.core.logger import get_logger
from src.core.types import DialogChannel, EventCategory, EventStatus
from src.domain.entities import (
    Event,
    FeedbackRequestRecord,
    FeedbackStatus,
    NotificationDispatchReport,
    SubscriptionNotificationRecord,
    SubscriptionRecord,
)
from src.domain.exceptions import PromptInjectionDetectedError
from src.domain.ports import (
    AdministrationRepositoryPort,
    EventRepositoryPort,
    NotificationDeliveryOutcome,
    SubscriptionNotificationSenderPort,
)
from src.domain.services import BASHKORTOSTAN_TIMEZONE, inspect_untrusted_text, normalize_optional_text

__all__ = ["SubscriptionService"]

logger = get_logger(__name__)

_MAXIMUM_FEEDBACK_LIST_LIMIT = 500
_MAXIMUM_NOTIFICATION_LIST_LIMIT = 500
_MAXIMUM_RETRY_DELAY_SECONDS = 3600
_BASE_RETRY_DELAY_SECONDS = 60


class SubscriptionService:
    """Прикладной сценарий оформления подписок, отписки, уведомлений и обращений по событиям."""

    def __init__(
        self,
        *,
        administration_repository: AdministrationRepositoryPort,
        event_repository: EventRepositoryPort | None = None,
        telegram_notification_sender: SubscriptionNotificationSenderPort | None = None,
        notification_batch_size: int = 50,
        notification_max_attempts: int = 3,
    ) -> None:
        """Инициализирует сервис подписок, доставки уведомлений и обратной связи.

        Arguments:
            administration_repository: Порт хранения подписок, уведомлений и обращений.
            event_repository: Необязательный порт каталога для нормализации городов и проверки карточек.
            telegram_notification_sender: Необязательный порт отправки уведомлений в Telegram.
            notification_batch_size: Размер пакета выборки Telegram-уведомлений за один цикл.
            notification_max_attempts: Максимальное число попыток доставки одного уведомления.

        Raises:
            ValidationException: Если `notification_batch_size` или `notification_max_attempts` меньше 1.
        """
        if notification_batch_size < 1:
            raise ValidationException("Размер пакета отправки уведомлений должен быть положительным числом.")
        if notification_max_attempts < 1:
            raise ValidationException("Максимальное число попыток доставки уведомлений должно быть положительным.")

        self._administration_repository = administration_repository
        self._event_repository = event_repository
        self._telegram_notification_sender = telegram_notification_sender
        self._notification_batch_size = notification_batch_size
        self._notification_max_attempts = notification_max_attempts

    async def subscribe(
        self,
        *,
        channel: DialogChannel,
        subscriber_identifier: str,
        city: str | None = None,
        category: EventCategory | None = None,
    ) -> SubscriptionRecord:
        """Оформляет или переиспользует идемпотентную подписку по техническому идентификатору.

        Arguments:
            channel: Канал доставки уведомлений (`web`, `telegram` и др.).
            subscriber_identifier: Технический идентификатор пользователя без персональных контактов.
            city: Необязательный город для уведомлений (нормализуется по справочнику).
            category: Необязательная категория мероприятий.

        Returns:
            Сохранённая запись подписки.

        Raises:
            ValidationException: Если технический идентификатор подписчика пуст.
        """
        cleaned_subscriber = _require_non_empty_text(
            subscriber_identifier, error_message="Технический идентификатор подписчика не может быть пустым."
        )
        resolved_city = await self._normalize_subscription_city(city)

        subscription = SubscriptionRecord(
            subscription_identifier=uuid4(),
            channel=channel,
            subscriber_identifier=cleaned_subscriber,
            city=resolved_city,
            category=category,
            created_at=datetime.now(tz=BASHKORTOSTAN_TIMEZONE),
        )
        return await self._administration_repository.create_subscription(subscription=subscription)

    async def list_subscriptions(
        self, *, channel: DialogChannel, subscriber_identifier: str
    ) -> tuple[SubscriptionRecord, ...]:
        """Возвращает активные подписки пользователя в выбранном канале.

        Arguments:
            channel: Канал подписки.
            subscriber_identifier: Технический идентификатор пользователя.

        Returns:
            Кортеж активных подписок пользователя.

        Raises:
            ValidationException: Если технический идентификатор подписчика пуст.
        """
        cleaned_subscriber = _require_non_empty_text(
            subscriber_identifier, error_message="Технический идентификатор подписчика не может быть пустым."
        )
        return await self._administration_repository.list_subscriptions_by_subscriber(
            channel=channel, subscriber_identifier=cleaned_subscriber
        )

    async def unsubscribe(self, *, subscription_identifier: UUID, subscriber_identifier: str | None = None) -> None:
        """Физически удаляет конкретную подписку пользователя из хранилища.

        Arguments:
            subscription_identifier: Уникальный идентификатор подписки.
            subscriber_identifier: Необязательный идентификатор владельца для защиты от чужой отписки.

        Raises:
            SubscriptionNotFoundError: Если подписка не найдена или принадлежит другому пользователю.
        """
        cleaned_subscriber = normalize_optional_text(subscriber_identifier)
        await self._administration_repository.delete_subscription(
            subscription_identifier=subscription_identifier, subscriber_identifier=cleaned_subscriber
        )

    async def unsubscribe_all(self, *, channel: DialogChannel, subscriber_identifier: str) -> int:
        """Физически удаляет все подписки пользователя в указанном канале.

        Arguments:
            channel: Канал подписки.
            subscriber_identifier: Технический идентификатор пользователя.

        Returns:
            Количество удалённых подписок.

        Raises:
            ValidationException: Если технический идентификатор подписчика пуст.
        """
        cleaned_subscriber = _require_non_empty_text(
            subscriber_identifier, error_message="Технический идентификатор подписчика не может быть пустым."
        )
        return await self._administration_repository.delete_all_subscriptions_for_subscriber(
            channel=channel, subscriber_identifier=cleaned_subscriber
        )

    async def submit_feedback(
        self,
        *,
        channel: DialogChannel,
        message_text: str,
        event_identifier: UUID | None = None,
        contact_hint: str | None = None,
    ) -> FeedbackRequestRecord:
        """Регистрирует обращение пользователя или заявку организатора на уточнение события.

        Arguments:
            channel: Канал отправки обращения.
            message_text: Текст уточнения или предложения.
            event_identifier: Необязательный идентификатор связанного мероприятия.
            contact_hint: Необязательный псевдоним или ссылка для обратной связи.

        Returns:
            Сохранённая запись обращения.

        Raises:
            ValidationException: Если текст обращения пуст.
            PromptInjectionDetectedError: Если в тексте обращения выявлены инъекционные конструкции.
            EventNotFoundError: Если указанное мероприятие отсутствует в каталоге.
        """
        message_inspection = inspect_untrusted_text(message_text, maximum_length=2000)
        if message_inspection.has_suspicious_instructions:
            raise PromptInjectionDetectedError(
                "Обращение отклонено: обнаружены подозрительные управляющие инструкции.",
                source_context="feedback_message",
                matched_patterns=message_inspection.matched_patterns,
            )

        cleaned_message = _require_non_empty_text(
            message_inspection.cleaned_text, error_message="Текст обращения не может быть пустым."
        )

        cleaned_contact: str | None = None
        normalized_contact = normalize_optional_text(contact_hint)
        if normalized_contact is not None:
            contact_inspection = inspect_untrusted_text(normalized_contact, maximum_length=255)
            if contact_inspection.has_suspicious_instructions:
                raise PromptInjectionDetectedError(
                    "Контактная подсказка отклонена: обнаружены подозрительные управляющие инструкции.",
                    source_context="feedback_contact",
                    matched_patterns=contact_inspection.matched_patterns,
                )
            cleaned_contact = normalize_optional_text(contact_inspection.cleaned_text)

        if event_identifier is not None and self._event_repository is not None:
            await self._event_repository.get_event_by_identifier(
                event_identifier=event_identifier, include_non_published=True
            )

        feedback_record = FeedbackRequestRecord(
            feedback_identifier=uuid4(),
            channel=channel,
            event_identifier=event_identifier,
            message_text=cleaned_message,
            contact_hint=cleaned_contact,
            created_at=datetime.now(tz=BASHKORTOSTAN_TIMEZONE),
            status="new",
        )
        return await self._administration_repository.save_feedback_request(feedback_request=feedback_record)

    async def list_feedback_requests(
        self, *, status: FeedbackStatus | None = None, limit: int = 50
    ) -> tuple[FeedbackRequestRecord, ...]:
        """Возвращает список обращений пользователей и организаторов.

        Arguments:
            status: Фильтр по статусу обработки (`new` или `reviewed`) либо `None` для всех.
            limit: Максимальное количество записей (`1..500`).

        Returns:
            Кортеж обращений.

        Raises:
            ValidationException: Если лимит выходит за допустимые границы.
        """
        if limit < 1 or limit > _MAXIMUM_FEEDBACK_LIST_LIMIT:
            raise ValidationException(f"Лимит выборки обращений должен быть от 1 до {_MAXIMUM_FEEDBACK_LIST_LIMIT}.")
        return await self._administration_repository.list_feedback_requests(status=status, limit=limit)

    async def mark_feedback_reviewed(self, *, feedback_identifier: UUID) -> FeedbackRequestRecord | None:
        """Переводит обращение пользователя в статус рассмотренного.

        Arguments:
            feedback_identifier: Идентификатор обращения.

        Returns:
            Обновлённая запись обращения или `None`, если обращение не найдено.
        """
        return await self._administration_repository.mark_feedback_reviewed(feedback_identifier=feedback_identifier)

    async def notify_subscribers_for_event(
        self, *, event: Event, dispatch_immediately: bool = True, current_time: datetime | None = None
    ) -> NotificationDispatchReport:
        """Ставит в очередь уведомления по подходящим подпискам и при необходимости запускает Telegram-рассылку.

        Arguments:
            event: Карточка мероприятия, перешедшая в статус `PUBLISHED`.
            dispatch_immediately: Немедленно запустить отправку ожидающих Telegram-уведомлений.
            current_time: Необязательная опорная метка времени для детерминированных прогонов.

        Returns:
            Сводный отчёт о постановке в очередь и выполненной отправке.
        """
        if event.status is not EventStatus.PUBLISHED:
            return NotificationDispatchReport()

        effective_time = _resolve_current_time(current_time)
        enqueued_count = await self._administration_repository.enqueue_subscription_notifications_for_event(
            event=event, created_at=effective_time
        )
        if not dispatch_immediately or self._telegram_notification_sender is None or enqueued_count <= 0:
            return NotificationDispatchReport(enqueued_count=enqueued_count)

        dispatch_report = await self.dispatch_pending_telegram_notifications(current_time=effective_time)
        return NotificationDispatchReport(
            enqueued_count=enqueued_count,
            attempted_count=dispatch_report.attempted_count,
            sent_count=dispatch_report.sent_count,
            failed_count=dispatch_report.failed_count,
            deferred_count=dispatch_report.deferred_count,
        )

    async def dispatch_pending_telegram_notifications(
        self, *, limit: int | None = None, current_time: datetime | None = None
    ) -> NotificationDispatchReport:
        """Резервирует и отправляет пакет ожидающих Telegram-уведомлений по подпискам.

        Arguments:
            limit: Максимальное число уведомлений за один вызов (по умолчанию `notification_batch_size`).
            current_time: Необязательная опорная метка времени для расчёта повторов и таймаутов.

        Returns:
            Отчёт об итогах отправки пакета (`NotificationDispatchReport`).

        Raises:
            ValidationException: Если явно передан неположительный `limit`.
        """
        if self._telegram_notification_sender is None:
            return NotificationDispatchReport()

        effective_limit = self._notification_batch_size if limit is None else limit
        if effective_limit < 1:
            raise ValidationException("Лимит пакета отправки уведомлений должен быть положительным числом.")

        effective_time = _resolve_current_time(current_time)
        claimed_notifications = await self._administration_repository.claim_pending_telegram_notifications(
            current_time=effective_time, limit=effective_limit, maximum_attempts=self._notification_max_attempts
        )
        if not claimed_notifications:
            return NotificationDispatchReport()

        sent_count = 0
        failed_count = 0
        deferred_count = 0

        for notification in claimed_notifications:
            outcome = await self._send_single_notification_safely(notification=notification)
            if outcome.delivered:
                await self._administration_repository.mark_subscription_notification_sent(
                    notification_identifier=notification.notification_identifier, sent_at=effective_time
                )
                sent_count += 1
                continue

            attempts = max(1, notification.attempts)
            is_exhausted = outcome.is_permanent_failure or attempts >= self._notification_max_attempts
            retry_at: datetime | None = None
            if not is_exhausted:
                delay_seconds = _resolve_retry_delay_seconds(
                    attempts=attempts, retry_after_seconds=outcome.retry_after_seconds
                )
                retry_at = effective_time + timedelta(seconds=delay_seconds)

            error_message = normalize_optional_text(outcome.error_message) or "Ошибка доставки уведомления в Telegram."
            await self._administration_repository.mark_subscription_notification_failed(
                notification_identifier=notification.notification_identifier,
                error_message=error_message,
                failed_at=effective_time,
                retry_at=retry_at,
                is_permanent=outcome.is_permanent_failure,
                maximum_attempts=self._notification_max_attempts,
            )
            if is_exhausted:
                failed_count += 1
            else:
                deferred_count += 1

        return NotificationDispatchReport(
            enqueued_count=0,
            attempted_count=len(claimed_notifications),
            sent_count=sent_count,
            failed_count=failed_count,
            deferred_count=deferred_count,
        )

    async def count_unread_web_notifications(self, *, visitor_identifier: str) -> int:
        """Возвращает количество непрочитанных веб-уведомлений пользователя.

        Arguments:
            visitor_identifier: Технический идентификатор посетителя веб-интерфейса.

        Returns:
            Число непрочитанных уведомлений.

        Raises:
            ValidationException: Если идентификатор посетителя пуст.
        """
        cleaned_visitor = _require_non_empty_text(
            visitor_identifier, error_message="Технический идентификатор посетителя не может быть пустым."
        )
        return await self._administration_repository.count_unread_web_subscription_notifications(
            subscriber_identifier=cleaned_visitor
        )

    async def list_web_notifications(
        self, *, visitor_identifier: str, include_read: bool = False, limit: int = 20
    ) -> tuple[SubscriptionNotificationRecord, ...]:
        """Возвращает список веб-уведомлений пользователя по активным подпискам.

        Arguments:
            visitor_identifier: Технический идентификатор посетителя веб-интерфейса.
            include_read: Включать ли уже прочитанные уведомления.
            limit: Максимальное число записей в выборке (`1..500`).

        Returns:
            Кортеж веб-уведомлений от новых к старым.

        Raises:
            ValidationException: Если идентификатор посетителя пуст или лимит вне допустимого диапазона.
        """
        cleaned_visitor = _require_non_empty_text(
            visitor_identifier, error_message="Технический идентификатор посетителя не может быть пустым."
        )
        if limit < 1 or limit > _MAXIMUM_NOTIFICATION_LIST_LIMIT:
            raise ValidationException(
                f"Лимит выборки веб-уведомлений должен быть от 1 до {_MAXIMUM_NOTIFICATION_LIST_LIMIT}."
            )
        return await self._administration_repository.list_web_subscription_notifications(
            subscriber_identifier=cleaned_visitor, include_read=include_read, limit=limit
        )

    async def mark_web_notification_read(
        self, *, notification_identifier: UUID, visitor_identifier: str, current_time: datetime | None = None
    ) -> bool:
        """Отмечает конкретное веб-уведомление пользователя как прочитанное.

        Arguments:
            notification_identifier: Идентификатор уведомления.
            visitor_identifier: Технический идентификатор владельца уведомления.
            current_time: Необязательная опорная метка времени прочтения.

        Returns:
            `True`, если уведомление найдено и отмечено прочитанным, иначе `False`.

        Raises:
            ValidationException: Если идентификатор посетителя пуст.
        """
        cleaned_visitor = _require_non_empty_text(
            visitor_identifier, error_message="Технический идентификатор посетителя не может быть пустым."
        )
        effective_time = _resolve_current_time(current_time)
        return await self._administration_repository.mark_web_subscription_notification_read(
            notification_identifier=notification_identifier,
            subscriber_identifier=cleaned_visitor,
            read_at=effective_time,
        )

    async def mark_all_web_notifications_read(
        self, *, visitor_identifier: str, current_time: datetime | None = None
    ) -> int:
        """Отмечает все непрочитанные веб-уведомления пользователя как прочитанные.

        Arguments:
            visitor_identifier: Технический идентификатор посетителя веб-интерфейса.
            current_time: Необязательная опорная метка времени прочтения.

        Returns:
            Количество уведомлений, отмеченных прочитанными.

        Raises:
            ValidationException: Если идентификатор посетителя пуст.
        """
        cleaned_visitor = _require_non_empty_text(
            visitor_identifier, error_message="Технический идентификатор посетителя не может быть пустым."
        )
        effective_time = _resolve_current_time(current_time)
        return await self._administration_repository.mark_all_web_subscription_notifications_read(
            subscriber_identifier=cleaned_visitor, read_at=effective_time
        )

    async def _send_single_notification_safely(
        self, *, notification: SubscriptionNotificationRecord
    ) -> NotificationDeliveryOutcome:
        assert self._telegram_notification_sender is not None
        try:
            return await self._telegram_notification_sender.send_event_notification(notification=notification)
        except Exception as error:
            logger.warning(
                "Исключение адаптера отправки Telegram-уведомления %s: %s", notification.notification_identifier, error
            )
            return NotificationDeliveryOutcome(
                delivered=False, is_permanent_failure=False, error_message=str(error) or error.__class__.__name__
            )

    async def _normalize_subscription_city(self, raw_city: str | None) -> str | None:
        cleaned_city = normalize_optional_text(raw_city)
        if cleaned_city is None or self._event_repository is None:
            return cleaned_city
        return await resolve_normalized_city(cleaned_city, event_repository=self._event_repository)


def _resolve_current_time(current_time: datetime | None) -> datetime:
    return attach_default_timezone(current_time or datetime.now(tz=BASHKORTOSTAN_TIMEZONE))


def _resolve_retry_delay_seconds(*, attempts: int, retry_after_seconds: int | None) -> int:
    if retry_after_seconds is not None and retry_after_seconds > 0:
        return min(retry_after_seconds, _MAXIMUM_RETRY_DELAY_SECONDS)
    exponential_delay = _BASE_RETRY_DELAY_SECONDS * (2 ** max(0, attempts - 1))
    return int(min(exponential_delay, _MAXIMUM_RETRY_DELAY_SECONDS))


def _require_non_empty_text(raw_value: str | None, *, error_message: str) -> str:
    cleaned = normalize_optional_text(raw_value)
    if cleaned is None:
        raise ValidationException(error_message)
    return cleaned
