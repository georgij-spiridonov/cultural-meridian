"""Неизменяемые доменные сущности модерации, аудита, подписок и обращений."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from src.core.types import DialogChannel, EventCategory
from src.domain.entities._helpers import (
    EMPTY_OBJECT_MAPPING,
    freeze_mapping,
    restore_dataclass_state,
    serialize_dataclass_state,
)

__all__ = [
    "AuditLogEntry",
    "FeedbackRequestRecord",
    "FeedbackStatus",
    "ModerationItem",
    "ModerationStatus",
    "ModeratorAccount",
    "SubscriptionRecord",
]

type ModerationStatus = Literal["pending", "approved", "rejected"]
type FeedbackStatus = Literal["new", "reviewed"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ModeratorAccount:
    """Учётная запись модератора для входа в административный интерфейс `/admin`.

    Attributes:
        moderator_identifier: Уникальный идентификатор модератора.
        username: Логин модератора для аутентификации и фиксации в `audit_logs.actor`.
        display_name: Отображаемое имя модератора.
        password_hash: Хэш пароля модератора.
        is_active: Признак активности учётной записи.
        created_at: Временная метка создания учётной записи.
        last_login_at: Временная метка последнего успешного входа.
    """

    moderator_identifier: UUID
    username: str
    display_name: str
    password_hash: str
    is_active: bool = True
    created_at: datetime
    last_login_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ModerationItem:
    """Элемент очереди модерации для спорных, непроверенных или подозрительных карточек.

    Attributes:
        moderation_identifier: Уникальный идентификатор задачи модерации.
        event_identifier: Идентификатор проверяемой карточки мероприятия.
        raw_event_identifier: Идентификатор исходной сырой записи, если она есть.
        reason: Причина попадания карточки в очередь модерации.
        status: Текущий статус проверки (`pending`, `approved`, `rejected`).
        has_suspicious_instructions: Признак обнаружения подозрительных инструкций в тексте.
        matched_injection_patterns: Список сработавших сигнатур защиты от инъекций.
        created_at: Временная метка постановки в очередь.
        resolved_at: Временная метка вынесения решения модератором.
        resolved_by: Идентификатор модератора, принявшего решение.
    """

    moderation_identifier: UUID
    event_identifier: UUID
    raw_event_identifier: UUID | None = None
    reason: str
    status: ModerationStatus = "pending"
    has_suspicious_instructions: bool = False
    matched_injection_patterns: tuple[str, ...] = ()
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None

    def __post_init__(self) -> None:
        """Фиксирует кортеж совпавших сигнатур инъекций."""
        object.__setattr__(self, "matched_injection_patterns", tuple(self.matched_injection_patterns))


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditLogEntry:
    """Запись журнала изменений каталога и модерации (кто, когда и что изменил).

    Attributes:
        entry_identifier: Уникальный идентификатор записи журнала.
        actor: Инициатор изменения (`administrator`, `importer`, `ingestion_worker` и т.п.).
        action: Тип выполненного действия (`create`, `update`, `cancel`, `publish`, `reject`).
        entity_type: Тип изменённой сущности (`event`, `moderation_item`).
        entity_identifier: Строковый идентификатор изменённой сущности.
        changes: Неизменяемый словарь зафиксированных изменений.
        created_at: Временная метка события аудита.
    """

    entry_identifier: UUID
    actor: str
    action: str
    entity_type: str
    entity_identifier: str
    changes: Mapping[str, object] = field(default=EMPTY_OBJECT_MAPPING, hash=False)
    created_at: datetime

    def __post_init__(self) -> None:
        """Обеспечивает глубокую неизменяемость словаря изменений."""
        object.__setattr__(self, "changes", freeze_mapping(self.changes))

    def __getstate__(self) -> dict[str, object]:
        """Подготавливает состояние объекта для сериализации."""
        return serialize_dataclass_state(self)

    def __setstate__(self, state: Mapping[str, object]) -> None:
        """Восстанавливает неизменяемое состояние объекта после десериализации."""
        restore_dataclass_state(self, state, self.__post_init__)


@dataclass(frozen=True, slots=True, kw_only=True)
class SubscriptionRecord:
    """Подписка пользователя на новые события по городу и категории с минимумом ПДн.

    Attributes:
        subscription_identifier: Уникальный идентификатор подписки.
        channel: Канал доставки или оформления подписки.
        subscriber_identifier: Технический идентификатор пользователя без телефонов и адресов.
        city: Выбранный город для уведомлений (или `None` для всех городов).
        category: Выбранная категория мероприятий (или `None` для всех категорий).
        created_at: Временная метка оформления подписки.
    """

    subscription_identifier: UUID
    channel: DialogChannel
    subscriber_identifier: str
    city: str | None = None
    category: EventCategory | None = None
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedbackRequestRecord:
    """Заявка организатора или обращение пользователя по уточнению события.

    Attributes:
        feedback_identifier: Уникальный идентификатор обращения.
        channel: Канал отправки обращения.
        event_identifier: Связанное мероприятие, если обращение касается конкретной карточки.
        message_text: Текст уточнения или заявки.
        contact_hint: Необязательная ссылка или псевдоним для связи с организатором.
        created_at: Временная метка регистрации обращения.
        status: Статус обработки обращения модератором.
    """

    feedback_identifier: UUID
    channel: DialogChannel
    event_identifier: UUID | None = None
    message_text: str
    contact_hint: str | None = None
    created_at: datetime
    status: FeedbackStatus = "new"
