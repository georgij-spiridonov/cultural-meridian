"""Прикладной сервис управления подписками пользователей и обращениями организаторов."""

from datetime import datetime
from uuid import UUID, uuid4

from src.application._helpers import resolve_normalized_city
from src.core.exceptions import ValidationException
from src.core.types import DialogChannel, EventCategory
from src.domain.entities import FeedbackRequestRecord, FeedbackStatus, SubscriptionRecord
from src.domain.exceptions import PromptInjectionDetectedError
from src.domain.ports import AdministrationRepositoryPort, EventRepositoryPort
from src.domain.services import BASHKORTOSTAN_TIMEZONE, inspect_untrusted_text, normalize_optional_text

__all__ = ["SubscriptionService"]

_MAXIMUM_FEEDBACK_LIST_LIMIT = 500


class SubscriptionService:
    """Прикладной сценарий оформления подписок, отписки и обращений по событиям."""

    def __init__(
        self,
        *,
        administration_repository: AdministrationRepositoryPort,
        event_repository: EventRepositoryPort | None = None,
    ) -> None:
        """Инициализирует сервис подписок и обратной связи.

        Arguments:
            administration_repository: Порт хранения подписок и обращений.
            event_repository: Необязательный порт каталога для нормализации городов и проверки карточек.
        """
        self._administration_repository = administration_repository
        self._event_repository = event_repository

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

    async def _normalize_subscription_city(self, raw_city: str | None) -> str | None:
        cleaned_city = normalize_optional_text(raw_city)
        if cleaned_city is None or self._event_repository is None:
            return cleaned_city
        return await resolve_normalized_city(cleaned_city, event_repository=self._event_repository)


def _require_non_empty_text(raw_value: str | None, *, error_message: str) -> str:
    cleaned = normalize_optional_text(raw_value)
    if cleaned is None:
        raise ValidationException(error_message)
    return cleaned
