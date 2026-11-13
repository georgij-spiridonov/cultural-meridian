"""Контрактный порт администрирования, модерации, аудита, подписок и обращений."""

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from src.core.types import DialogChannel
from src.domain.entities import (
    AuditLogEntry,
    FeedbackRequestRecord,
    FeedbackStatus,
    ModerationItem,
    ModerationStatus,
    SubscriptionRecord,
)

__all__ = ["AdministrationRepositoryPort"]


@runtime_checkable
class AdministrationRepositoryPort(Protocol):
    """Протокол управления модерацией, журналом изменений, подписками и заявками."""

    async def create_subscription(self, *, subscription: SubscriptionRecord) -> SubscriptionRecord:
        """Создаёт или возвращает существующую подписку пользователя на фильтры.

        Arguments:
            subscription: Параметры оформляемой подписки без лишних персональных данных.

        Returns:
            Актуальная запись подписки.

        Raises:
            InfrastructureException: При ошибке сохранения подписки в хранилище.
        """
        ...

    async def list_subscriptions_by_subscriber(
        self, *, channel: DialogChannel, subscriber_identifier: str
    ) -> tuple[SubscriptionRecord, ...]:
        """Возвращает активные подписки указанного пользователя в канале.

        Arguments:
            channel: Канал оформления подписки.
            subscriber_identifier: Технический идентификатор подписчика.

        Returns:
            Кортеж активных подписок пользователя.

        Raises:
            InfrastructureException: При ошибке чтения подписок из хранилища.
        """
        ...

    async def delete_subscription(
        self, *, subscription_identifier: UUID, subscriber_identifier: str | None = None
    ) -> None:
        """Физически удаляет подписку по её идентификатору (требование минимизации ПДн).

        Arguments:
            subscription_identifier: Идентификатор удаляемой подписки.
            subscriber_identifier: Необязательная проверка владельца подписки.

        Raises:
            InfrastructureException: При ошибке удаления записи из хранилища.
            SubscriptionNotFoundError: Если указанная подписка не найдена.
        """
        ...

    async def delete_all_subscriptions_for_subscriber(
        self, *, channel: DialogChannel, subscriber_identifier: str
    ) -> int:
        """Физически удаляет все подписки пользователя одной командой.

        Arguments:
            channel: Канал пользователя.
            subscriber_identifier: Технический идентификатор подписчика.

        Returns:
            Количество удалённых подписок.

        Raises:
            InfrastructureException: При ошибке удаления записей из хранилища.
        """
        ...

    async def save_feedback_request(self, *, feedback_request: FeedbackRequestRecord) -> FeedbackRequestRecord:
        """Сохраняет обращение пользователя или заявку организатора мероприятия.

        Arguments:
            feedback_request: Регистрируемое обращение.

        Returns:
            Сохранённая запись обращения.

        Raises:
            InfrastructureException: При ошибке сохранения в хранилище.
        """
        ...

    async def list_feedback_requests(
        self, *, status: FeedbackStatus | None = None, limit: int = 50
    ) -> tuple[FeedbackRequestRecord, ...]:
        """Возвращает список обращений организаторов и пользователей для модератора.

        Arguments:
            status: Необязательный фильтр по статусу обработки.
            limit: Максимальное число записей.

        Returns:
            Кортеж записей обращений.

        Raises:
            InfrastructureException: При ошибке чтения обращений из хранилища.
        """
        ...

    async def mark_feedback_reviewed(self, *, feedback_identifier: UUID) -> FeedbackRequestRecord | None:
        """Помечает обращение организатора как рассмотренное.

        Arguments:
            feedback_identifier: Идентификатор обращения.

        Returns:
            Обновлённая запись обращения или `None`, если обращение не найдено.

        Raises:
            InfrastructureException: При ошибке обновления статуса в хранилище.
        """
        ...

    async def enqueue_moderation_item(self, *, moderation_item: ModerationItem) -> ModerationItem:
        """Добавляет спорную или подозрительную карточку в очередь модерации.

        Arguments:
            moderation_item: Элемент очереди модерации.

        Returns:
            Сохранённый элемент очереди модерации.

        Raises:
            InfrastructureException: При ошибке записи в хранилище.
        """
        ...

    async def list_moderation_items(
        self, *, status: ModerationStatus | None = "pending", limit: int = 50
    ) -> tuple[ModerationItem, ...]:
        """Возвращает элементы очереди модерации с фильтрацией по статусу.

        Arguments:
            status: Статус задач модерации (`pending`, `approved`, `rejected`) или `None` для всех.
            limit: Максимальное количество элементов.

        Returns:
            Кортеж элементов очереди модерации.

        Raises:
            InfrastructureException: При ошибке чтения из хранилища.
        """
        ...

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
            InfrastructureException: При ошибке обновления записи в хранилище.
            ModerationItemNotFoundError: Если элемент модерации не найден.
        """
        ...

    async def append_audit_log(self, *, entry: AuditLogEntry) -> AuditLogEntry:
        """Добавляет неизменяемую запись в журнал изменений каталога и модерации.

        Arguments:
            entry: Запись аудита (кто, когда и что изменил).

        Returns:
            Сохранённая запись журнала изменений.

        Raises:
            InfrastructureException: При ошибке записи в журнал.
        """
        ...

    async def list_audit_logs(
        self, *, entity_identifier: str | None = None, limit: int = 100
    ) -> tuple[AuditLogEntry, ...]:
        """Возвращает последние записи журнала изменений каталога.

        Arguments:
            entity_identifier: Необязательный фильтр по идентификатору изменённой сущности.
            limit: Максимальное число записей.

        Returns:
            Кортеж записей аудита от новых к старым.

        Raises:
            InfrastructureException: При ошибке чтения журнала.
        """
        ...
