"""Контрактный порт отправки уведомлений по подпискам пользователям."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from src.domain.entities import SubscriptionNotificationRecord

__all__ = ["NotificationDeliveryOutcome", "SubscriptionNotificationSenderPort"]


@dataclass(frozen=True, slots=True, kw_only=True)
class NotificationDeliveryOutcome:
    """Результат одной попытки доставки уведомления по подписке во внешний канал.

    Attributes:
        delivered: Признак успешной доставки уведомления получателю.
        is_permanent_failure: Признак необратимого сбоя (например, бот заблокирован пользователем).
        retry_after_seconds: Рекомендуемая задержка до следующей попытки в секундах (например, при `429`).
        error_message: Диагностическое описание причины сбоя доставки, если он возник.
    """

    delivered: bool
    is_permanent_failure: bool = False
    retry_after_seconds: int | None = None
    error_message: str | None = None


@runtime_checkable
class SubscriptionNotificationSenderPort(Protocol):
    """Протокол транспортного адаптера доставки уведомлений по подпискам."""

    async def send_event_notification(
        self, *, notification: SubscriptionNotificationRecord
    ) -> NotificationDeliveryOutcome:
        """Отправляет уведомление о новом опубликованном мероприятии подписчику.

        Arguments:
            notification: Запись уведомления по подписке с карточкой мероприятия.

        Returns:
            Результат попытки доставки уведомления.
        """
        ...
