"""Прикладной сервис администрирования каталога, очереди модерации и журнала изменений."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from src.application._helpers import (
    attach_default_timezone,
    resolve_duration_minutes,
    resolve_normalized_city,
    validate_http_url,
)
from src.application.subscriptions import SubscriptionService
from src.core.exceptions import ValidationException
from src.core.logger import get_logger
from src.core.types import EventCategory, EventStatus, RealCollection, SourceType
from src.domain.entities import (
    AuditLogEntry,
    Event,
    EventSearchFilter,
    EventSearchPage,
    EventSourceReference,
    ModerationItem,
    ModerationStatus,
    VenueReference,
)
from src.domain.exceptions import InvalidEventDataError, PromptInjectionDetectedError
from src.domain.ports import AdministrationRepositoryPort, EventRepositoryPort
from src.domain.services import (
    BASHKORTOSTAN_TIMEZONE,
    compute_deduplication_key,
    compute_payload_hash,
    extract_event_tags,
    inspect_untrusted_text,
    normalize_optional_text,
    normalize_title,
    normalize_venue,
)

__all__ = ["AdministrationService", "ManualEventInput"]

logger = get_logger(__name__)

_ALLOWED_AGE_LIMITS = frozenset({0, 6, 12, 16, 18})
_MAXIMUM_LIST_LIMIT = 500


@dataclass(frozen=True, slots=True, kw_only=True)
class ManualEventInput:
    """Входные данные для создания или редактирования карточки мероприятия администратором.

    Attributes:
        title: Название мероприятия.
        category: Категория мероприятия.
        date_start: Дата и время начала мероприятия.
        date_end: Необязательные дата и время окончания.
        duration_minutes: Необязательная длительность мероприятия в минутах.
        city: Город или населённый пункт проведения.
        venue: Название площадки проведения.
        address: Адрес площадки проведения.
        latitude: Географическая широта площадки.
        longitude: Географическая долгота площадки.
        description: Краткое описание мероприятия.
        price_minimum: Минимальная стоимость билета в рублях.
        price_maximum: Максимальная стоимость билета в рублях.
        is_free: Признак бесплатного входа.
        age_limit: Возрастной ценз (`0`, `6`, `12`, `16`, `18`).
        language: Язык проведения мероприятия.
        tags: Дополнительные тематические метки.
        organization_link: Ссылка на организатора или покупку билетов.
        source_url: Ссылка на первоисточник сведений.
        source_name: Название источника записи.
        status: Желаемый статус публикации.
    """

    title: str
    category: EventCategory
    date_start: datetime
    date_end: datetime | None = None
    duration_minutes: int | None = None
    city: str
    venue: str
    address: str
    latitude: float | None = None
    longitude: float | None = None
    description: str = ""
    price_minimum: int | None = None
    price_maximum: int | None = None
    is_free: bool = False
    age_limit: int = 0
    language: str | None = None
    tags: tuple[str, ...] = ()
    organization_link: str | None = None
    source_url: str
    source_name: str = "Администрирование"
    status: EventStatus = EventStatus.PUBLISHED


class AdministrationService:
    """Прикладной сценарий управления карточками событий, модерацией и аудитом."""

    def __init__(
        self,
        *,
        event_repository: EventRepositoryPort,
        administration_repository: AdministrationRepositoryPort,
        subscription_service: SubscriptionService | None = None,
    ) -> None:
        """Инициализирует сервис администрирования каталога.

        Arguments:
            event_repository: Реализация порта каталога мероприятий.
            administration_repository: Реализация порта модерации и аудита.
            subscription_service: Необязательный сервис уведомлений по подпискам при публикации.
        """
        self._event_repository = event_repository
        self._administration_repository = administration_repository
        self._subscription_service = subscription_service

    async def list_events(
        self,
        *,
        search_filter: EventSearchFilter | None = None,
        statuses: RealCollection[EventStatus] = (EventStatus.PUBLISHED, EventStatus.DRAFT, EventStatus.CANCELED),
    ) -> EventSearchPage:
        """Возвращает карточки мероприятий для панели администратора по заданным статусам.

        Arguments:
            search_filter: Фильтр и пагинация выборки.
            statuses: Набор статусов карточек, включаемых в выборку.

        Returns:
            Страница карточек мероприятий.

        Raises:
            ValidationException: Если передан пустой набор статусов.
        """
        if not statuses:
            raise ValidationException("Необходимо указать хотя бы один статус карточек для выборки.")
        effective_filter = search_filter or EventSearchFilter(page=1, page_size=20)
        return await self._event_repository.search_events(search_filter=effective_filter, statuses=statuses)

    async def get_event_details(self, *, event_identifier: UUID) -> Event:
        """Возвращает полную карточку мероприятия любого статуса для редактирования модератором.

        Arguments:
            event_identifier: Идентификатор карточки мероприятия.

        Returns:
            Доменная сущность мероприятия.

        Raises:
            EventNotFoundError: Если карточка не найдена.
        """
        return await self._event_repository.get_event_by_identifier(
            event_identifier=event_identifier, include_non_published=True
        )

    async def create_manual_event(self, *, event_input: ManualEventInput, actor: str) -> Event:
        """Создаёт новую карточку мероприятия вручную и фиксирует действие в журнале аудита.

        Arguments:
            event_input: Заполненные администратором поля мероприятия.
            actor: Логин инициатора изменения.

        Returns:
            Сохранённая карточка мероприятия.

        Raises:
            ValidationException: Если имя инициатора пустое.
            InvalidEventDataError: Если поля карточки не прошли проверку корректности.
            PromptInjectionDetectedError: Если текстовые поля содержат подозрительные инструкции.
        """
        cleaned_actor = _require_non_empty_actor(actor)
        current_time = datetime.now(tz=BASHKORTOSTAN_TIMEZONE)
        new_event = await self._build_event_from_manual_input(
            event_identifier=uuid4(),
            event_input=event_input,
            created_at=current_time,
            updated_at=current_time,
            existing_event=None,
        )
        saved_event = await self._event_repository.save_event(event=new_event)

        await self._administration_repository.append_audit_log(
            entry=AuditLogEntry(
                entry_identifier=uuid4(),
                actor=cleaned_actor,
                action="create",
                entity_type="event",
                entity_identifier=str(saved_event.identifier),
                changes={
                    "title": saved_event.title,
                    "city": saved_event.city,
                    "status": saved_event.status.value,
                    "date_start": saved_event.date_start.isoformat(),
                },
                created_at=current_time,
            )
        )
        if saved_event.status is EventStatus.PUBLISHED:
            await self._notify_subscribers_safely(event=saved_event, current_time=current_time)
        return saved_event

    async def update_manual_event(self, *, event_identifier: UUID, event_input: ManualEventInput, actor: str) -> Event:
        """Обновляет существующую карточку мероприятия и сохраняет журнал изменений.

        Arguments:
            event_identifier: Идентификатор редактируемой карточки.
            event_input: Обновлённый набор полей мероприятия.
            actor: Логин администратора, внёсшего правку.

        Returns:
            Обновлённая карточка мероприятия.

        Raises:
            EventNotFoundError: Если редактируемое мероприятие не найдено.
            ValidationException: Если имя инициатора пустое.
            InvalidEventDataError: Если поля карточки некорректны.
            PromptInjectionDetectedError: Если в тексте обнаружены подозрительные инструкции.
        """
        cleaned_actor = _require_non_empty_actor(actor)
        current_time = datetime.now(tz=BASHKORTOSTAN_TIMEZONE)
        existing_event = await self._event_repository.get_event_by_identifier(
            event_identifier=event_identifier, include_non_published=True
        )

        updated_event = await self._build_event_from_manual_input(
            event_identifier=existing_event.identifier,
            event_input=event_input,
            created_at=existing_event.created_at,
            updated_at=current_time,
            existing_event=existing_event,
        )
        saved_event = await self._event_repository.save_event(event=updated_event)
        changes = _compute_event_diff(previous_event=existing_event, current_event=saved_event)

        await self._administration_repository.append_audit_log(
            entry=AuditLogEntry(
                entry_identifier=uuid4(),
                actor=cleaned_actor,
                action="update",
                entity_type="event",
                entity_identifier=str(saved_event.identifier),
                changes=changes,
                created_at=current_time,
            )
        )
        if existing_event.status is not EventStatus.PUBLISHED and saved_event.status is EventStatus.PUBLISHED:
            await self._notify_subscribers_safely(event=saved_event, current_time=current_time)
        return saved_event

    async def cancel_event(self, *, event_identifier: UUID, actor: str, reason: str | None = None) -> Event:
        """Переводит мероприятие в статус `CANCELED` и фиксирует отмену в журнале изменений.

        Отменённое мероприятие исключается из стандартного поиска, но остаётся доступным
        по прямой ссылке с явным признаком отмены.

        Arguments:
            event_identifier: Идентификатор отменяемого мероприятия.
            actor: Логин администратора.
            reason: Необязательная причина отмены для журнала изменений.

        Returns:
            Обновлённая карточка мероприятия со статусом `CANCELED`.

        Raises:
            EventNotFoundError: Если мероприятие не найдено.
            ValidationException: Если имя инициатора пустое.
        """
        cleaned_actor = _require_non_empty_actor(actor)
        current_time = datetime.now(tz=BASHKORTOSTAN_TIMEZONE)
        existing_event = await self._event_repository.get_event_by_identifier(
            event_identifier=event_identifier, include_non_published=True
        )
        if existing_event.status is EventStatus.CANCELED:
            return existing_event

        canceled_event = Event(
            identifier=existing_event.identifier,
            title=existing_event.title,
            normalized_title=existing_event.normalized_title,
            category=existing_event.category,
            tags=existing_event.tags,
            date_start=existing_event.date_start,
            date_end=existing_event.date_end,
            duration_minutes=existing_event.duration_minutes,
            city=existing_event.city,
            venue=existing_event.venue,
            address=existing_event.address,
            latitude=existing_event.latitude,
            longitude=existing_event.longitude,
            description=existing_event.description,
            price_minimum=existing_event.price_minimum,
            price_maximum=existing_event.price_maximum,
            is_free=existing_event.is_free,
            age_limit=existing_event.age_limit,
            language=existing_event.language,
            organization_link=existing_event.organization_link,
            source_url=existing_event.source_url,
            source_type=existing_event.source_type,
            source_hash=existing_event.source_hash,
            verified_at=existing_event.verified_at,
            confidence=existing_event.confidence,
            status=EventStatus.CANCELED,
            deduplication_key=existing_event.deduplication_key,
            created_at=existing_event.created_at,
            updated_at=current_time,
            sources=existing_event.sources,
        )
        saved_event = await self._event_repository.save_event(event=canceled_event)

        cleaned_reason = normalize_optional_text(reason)
        audit_changes: dict[str, object] = {
            "previous_status": existing_event.status.value,
            "new_status": EventStatus.CANCELED.value,
        }
        if cleaned_reason is not None:
            audit_changes["reason"] = cleaned_reason

        await self._administration_repository.append_audit_log(
            entry=AuditLogEntry(
                entry_identifier=uuid4(),
                actor=cleaned_actor,
                action="cancel",
                entity_type="event",
                entity_identifier=str(saved_event.identifier),
                changes=audit_changes,
                created_at=current_time,
            )
        )
        return saved_event

    async def list_moderation_queue(
        self, *, status: ModerationStatus | None = "pending", limit: int = 50
    ) -> tuple[ModerationItem, ...]:
        """Возвращает элементы очереди модерации с фильтрацией по статусу.

        Arguments:
            status: Целевой статус (`pending`, `approved`, `rejected`) или `None` для всех записей.
            limit: Максимальное число возвращаемых задач (`1..500`).

        Returns:
            Кортеж задач модерации.

        Raises:
            ValidationException: Если лимит выходит за допустимый диапазон.
        """
        if limit < 1 or limit > _MAXIMUM_LIST_LIMIT:
            raise ValidationException(f"Лимит очереди модерации должен быть от 1 до {_MAXIMUM_LIST_LIMIT}.")
        return await self._administration_repository.list_moderation_items(status=status, limit=limit)

    async def resolve_moderation_decision(
        self,
        *,
        moderation_identifier: UUID,
        decision: Literal["approved", "rejected"],
        moderator_username: str,
        target_event_status: EventStatus | None = None,
    ) -> tuple[ModerationItem, Event]:
        """Фиксирует решение модератора, обновляет статус карточки и пишет запись аудита.

        При подтверждении (`approved`) связанная карточка публикуется в каталоге (`PUBLISHED`),
        а при отклонении (`rejected`) переводится в `CANCELED`, если явно не указан другой статус.

        Arguments:
            moderation_identifier: Идентификатор элемента очереди модерации.
            decision: Решение модератора (`approved` или `rejected`).
            moderator_username: Логин модератора.
            target_event_status: Необязательное явное переопределение итогового статуса карточки.

        Returns:
            Кортеж `(обновлённый элемент модерации, обновлённая карточка мероприятия)`.

        Raises:
            ValidationException: Если логин модератора пуст.
            ModerationItemNotFoundError: Если элемент очереди не найден.
            EventNotFoundError: Если связанная карточка мероприятия не найдена.
        """
        cleaned_moderator = _require_non_empty_actor(moderator_username)
        current_time = datetime.now(tz=BASHKORTOSTAN_TIMEZONE)

        resolved_item = await self._administration_repository.resolve_moderation_item(
            moderation_identifier=moderation_identifier,
            status=decision,
            resolved_by=cleaned_moderator,
            resolved_at=current_time,
        )
        linked_event = await self._event_repository.get_event_by_identifier(
            event_identifier=resolved_item.event_identifier, include_non_published=True
        )

        desired_status = (
            target_event_status
            if target_event_status is not None
            else (EventStatus.PUBLISHED if decision == "approved" else EventStatus.CANCELED)
        )

        updated_event = Event(
            identifier=linked_event.identifier,
            title=linked_event.title,
            normalized_title=linked_event.normalized_title,
            category=linked_event.category,
            tags=linked_event.tags,
            date_start=linked_event.date_start,
            date_end=linked_event.date_end,
            duration_minutes=linked_event.duration_minutes,
            city=linked_event.city,
            venue=linked_event.venue,
            address=linked_event.address,
            latitude=linked_event.latitude,
            longitude=linked_event.longitude,
            description=linked_event.description,
            price_minimum=linked_event.price_minimum,
            price_maximum=linked_event.price_maximum,
            is_free=linked_event.is_free,
            age_limit=linked_event.age_limit,
            language=linked_event.language,
            organization_link=linked_event.organization_link,
            source_url=linked_event.source_url,
            source_type=linked_event.source_type,
            source_hash=linked_event.source_hash,
            verified_at=current_time if decision == "approved" else linked_event.verified_at,
            confidence=linked_event.confidence,
            status=desired_status,
            deduplication_key=linked_event.deduplication_key,
            created_at=linked_event.created_at,
            updated_at=current_time,
            sources=linked_event.sources,
        )
        saved_event = await self._event_repository.save_event(event=updated_event)

        audit_action = "publish" if decision == "approved" else "reject"
        await self._administration_repository.append_audit_log(
            entry=AuditLogEntry(
                entry_identifier=uuid4(),
                actor=cleaned_moderator,
                action=audit_action,
                entity_type="event",
                entity_identifier=str(saved_event.identifier),
                changes={
                    "moderation_identifier": str(moderation_identifier),
                    "decision": decision,
                    "previous_status": linked_event.status.value,
                    "new_status": desired_status.value,
                },
                created_at=current_time,
            )
        )
        if (
            decision == "approved"
            and linked_event.status is not EventStatus.PUBLISHED
            and saved_event.status is EventStatus.PUBLISHED
        ):
            await self._notify_subscribers_safely(event=saved_event, current_time=current_time)
        return resolved_item, saved_event

    async def list_audit_logs(
        self, *, entity_identifier: str | None = None, limit: int = 100
    ) -> tuple[AuditLogEntry, ...]:
        """Возвращает записи журнала изменений административных действий.

        Arguments:
            entity_identifier: Необязательный фильтр по строковому идентификатору сущности.
            limit: Максимальное количество записей (`1..500`).

        Returns:
            Кортеж записей журнала аудита.

        Raises:
            ValidationException: Если лимит выходит за допустимый диапазон.
        """
        if limit < 1 or limit > _MAXIMUM_LIST_LIMIT:
            raise ValidationException(f"Лимит выборки журнала изменений должен быть от 1 до {_MAXIMUM_LIST_LIMIT}.")
        cleaned_entity = normalize_optional_text(entity_identifier)
        return await self._administration_repository.list_audit_logs(entity_identifier=cleaned_entity, limit=limit)

    async def _notify_subscribers_safely(self, *, event: Event, current_time: datetime) -> None:
        if self._subscription_service is None:
            return
        try:
            await self._subscription_service.notify_subscribers_for_event(
                event=event, dispatch_immediately=True, current_time=current_time
            )
        except Exception as error:
            logger.warning(
                "Не удалось выполнить уведомление подписчиков при публикации события %s: %s", event.identifier, error
            )

    async def _build_event_from_manual_input(
        self,
        *,
        event_identifier: UUID,
        event_input: ManualEventInput,
        created_at: datetime,
        updated_at: datetime,
        existing_event: Event | None,
    ) -> Event:
        cleaned_title = _inspect_and_require_text(
            event_input.title,
            field_name="title",
            error_message="Название мероприятия не может быть пустым.",
            maximum_length=500,
        )
        cleaned_city = _inspect_and_require_text(
            event_input.city,
            field_name="city",
            error_message="Город проведения не может быть пустым.",
            maximum_length=150,
        )
        cleaned_venue = _inspect_and_require_text(
            event_input.venue,
            field_name="venue",
            error_message="Площадка проведения не может быть пустой.",
            maximum_length=300,
        )
        cleaned_address = _inspect_and_require_text(
            event_input.address,
            field_name="address",
            error_message="Адрес площадки не может быть пустым.",
            maximum_length=500,
        )

        description_inspection = inspect_untrusted_text(event_input.description, maximum_length=4000)
        if description_inspection.has_suspicious_instructions:
            raise PromptInjectionDetectedError(
                "Описание мероприятия содержит запрещённые управляющие инструкции.",
                source_context="manual_event.description",
                matched_patterns=description_inspection.matched_patterns,
            )
        cleaned_description = description_inspection.cleaned_text

        date_start = attach_default_timezone(event_input.date_start)
        date_end = attach_default_timezone(event_input.date_end) if event_input.date_end is not None else None
        if date_end is not None and date_end < date_start:
            raise InvalidEventDataError(
                "Дата окончания мероприятия не может быть раньше даты начала.", field_name="date_end"
            )

        duration_minutes = resolve_duration_minutes(
            date_start=date_start, date_end=date_end, explicit_duration_minutes=event_input.duration_minutes
        )

        if event_input.age_limit not in _ALLOWED_AGE_LIMITS:
            raise InvalidEventDataError(
                "Возрастной ценз должен быть одним из значений: 0, 6, 12, 16, 18.", field_name="age_limit"
            )

        price_minimum = event_input.price_minimum
        price_maximum = event_input.price_maximum
        if price_minimum is not None and price_minimum < 0:
            raise InvalidEventDataError("Минимальная цена не может быть отрицательной.", field_name="price_minimum")
        if price_maximum is not None and price_maximum < 0:
            raise InvalidEventDataError("Максимальная цена не может быть отрицательной.", field_name="price_maximum")
        if price_minimum is not None and price_maximum is not None and price_minimum > price_maximum:
            raise InvalidEventDataError(
                "Минимальная цена билета не может превышать максимальную.", field_name="price_minimum"
            )

        is_free = event_input.is_free or (price_minimum == 0 and (price_maximum is None or price_maximum == 0))
        if event_input.is_free:
            if price_minimum is None:
                price_minimum = 0
            if price_maximum is None:
                price_maximum = 0

        if event_input.latitude is not None and not (-90.0 <= event_input.latitude <= 90.0):
            raise InvalidEventDataError("Широта должна находиться в диапазоне [-90; 90].", field_name="latitude")
        if event_input.longitude is not None and not (-180.0 <= event_input.longitude <= 180.0):
            raise InvalidEventDataError("Долгота должна находиться в диапазоне [-180; 180].", field_name="longitude")

        source_url = validate_http_url(event_input.source_url, field_name="source_url")
        organization_link: str | None = None
        if normalize_optional_text(event_input.organization_link) is not None:
            organization_link = validate_http_url(event_input.organization_link or "", field_name="organization_link")

        resolved_city = await resolve_normalized_city(cleaned_city, event_repository=self._event_repository)
        venues = await self._event_repository.list_venues(city=resolved_city)
        matched_venue = normalize_venue(cleaned_venue, venues=venues, city=resolved_city)
        resolved_venue = matched_venue.name if isinstance(matched_venue, VenueReference) else cleaned_venue

        latitude = event_input.latitude
        longitude = event_input.longitude
        if (
            latitude is None
            and longitude is None
            and isinstance(matched_venue, VenueReference)
            and matched_venue.coordinates is not None
        ):
            latitude = matched_venue.coordinates.latitude
            longitude = matched_venue.coordinates.longitude

        cleaned_language = normalize_optional_text(event_input.language)
        normalized_language = cleaned_language.lower() if cleaned_language is not None else None

        inferred_tags = extract_event_tags(
            raw_title=cleaned_title,
            raw_description=cleaned_description,
            category=event_input.category,
            is_free=is_free,
            language=normalized_language,
            age_limit=event_input.age_limit,
        )
        explicit_tags = [
            token.lower() for raw_tag in event_input.tags if (token := normalize_optional_text(raw_tag)) is not None
        ]
        normalized_tags = tuple(sorted(set(explicit_tags) | set(inferred_tags)))

        normalized_title = normalize_title(cleaned_title)
        deduplication_key = compute_deduplication_key(
            normalized_title=normalized_title, date_start=date_start, city=resolved_city, venue=resolved_venue
        )
        source_name = normalize_optional_text(event_input.source_name) or "Администрирование"
        source_hash = compute_payload_hash(
            {
                "event_identifier": str(event_identifier),
                "title": cleaned_title,
                "category": event_input.category.value,
                "date_start": date_start.isoformat(),
                "date_end": date_end.isoformat() if date_end else None,
                "duration_minutes": duration_minutes,
                "city": resolved_city,
                "venue": resolved_venue,
                "address": cleaned_address,
                "description": cleaned_description,
                "price_minimum": price_minimum,
                "price_maximum": price_maximum,
                "is_free": is_free,
                "age_limit": event_input.age_limit,
                "language": normalized_language,
                "source_url": source_url,
                "status": event_input.status.value,
            }
        )

        primary_source = EventSourceReference(
            source_name=source_name,
            source_type=SourceType.MANUAL,
            source_url=source_url,
            source_hash=source_hash,
            verified_at=updated_at,
        )
        sources: list[EventSourceReference] = [primary_source]
        if existing_event is not None:
            for existing_source in existing_event.sources:
                if existing_source.source_type is SourceType.MANUAL and existing_source.source_url == source_url:
                    continue
                if (existing_source.source_type, existing_source.source_hash) == (
                    primary_source.source_type,
                    primary_source.source_hash,
                ):
                    continue
                sources.append(existing_source)

        return Event(
            identifier=event_identifier,
            title=cleaned_title,
            normalized_title=normalized_title,
            category=event_input.category,
            tags=normalized_tags,
            date_start=date_start,
            date_end=date_end,
            duration_minutes=duration_minutes,
            city=resolved_city,
            venue=resolved_venue,
            address=cleaned_address,
            latitude=latitude,
            longitude=longitude,
            description=cleaned_description,
            price_minimum=price_minimum,
            price_maximum=price_maximum,
            is_free=is_free,
            age_limit=event_input.age_limit,
            language=normalized_language,
            organization_link=organization_link,
            source_url=source_url,
            source_type=SourceType.MANUAL,
            source_hash=source_hash,
            verified_at=updated_at,
            confidence=1.0,
            status=event_input.status,
            deduplication_key=deduplication_key,
            created_at=created_at,
            updated_at=updated_at,
            sources=tuple(sources),
        )


def _require_non_empty_actor(actor: str) -> str:
    cleaned = normalize_optional_text(actor)
    if cleaned is None:
        raise ValidationException("Логин инициатора административного действия не может быть пустым.")
    return cleaned


def _inspect_and_require_text(raw_value: str, *, field_name: str, error_message: str, maximum_length: int) -> str:
    inspection = inspect_untrusted_text(raw_value, maximum_length=maximum_length)
    if inspection.has_suspicious_instructions:
        raise PromptInjectionDetectedError(
            f"Поле «{field_name}» содержит запрещённые управляющие инструкции.",
            source_context=f"manual_event.{field_name}",
            matched_patterns=inspection.matched_patterns,
        )
    cleaned = normalize_optional_text(inspection.cleaned_text)
    if cleaned is None:
        raise InvalidEventDataError(error_message, field_name=field_name)
    return cleaned


def _compute_event_diff(*, previous_event: Event, current_event: Event) -> dict[str, object]:
    tracked_fields: tuple[tuple[str, object, object], ...] = (
        ("title", previous_event.title, current_event.title),
        ("category", previous_event.category.value, current_event.category.value),
        ("date_start", previous_event.date_start.isoformat(), current_event.date_start.isoformat()),
        (
            "date_end",
            previous_event.date_end.isoformat() if previous_event.date_end else None,
            current_event.date_end.isoformat() if current_event.date_end else None,
        ),
        ("city", previous_event.city, current_event.city),
        ("venue", previous_event.venue, current_event.venue),
        ("address", previous_event.address, current_event.address),
        ("price_minimum", previous_event.price_minimum, current_event.price_minimum),
        ("price_maximum", previous_event.price_maximum, current_event.price_maximum),
        ("is_free", previous_event.is_free, current_event.is_free),
        ("age_limit", previous_event.age_limit, current_event.age_limit),
        ("status", previous_event.status.value, current_event.status.value),
    )
    diff: dict[str, object] = {}
    for field_name, before_value, after_value in tracked_fields:
        if before_value != after_value:
            diff[field_name] = {"before": before_value, "after": after_value}
    if not diff:
        diff["updated"] = True
    return diff
