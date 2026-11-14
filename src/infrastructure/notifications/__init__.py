"""Инфраструктурный пакет отправки уведомлений пользователям по подпискам."""

from src.infrastructure.notifications.telegram_sender import TelegramSubscriptionNotificationSender

__all__ = ["TelegramSubscriptionNotificationSender"]
