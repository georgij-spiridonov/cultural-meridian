"""Модульные тесты доставки уведомлений по подпискам, хуков публикации, воркера и веб-маршрутов."""

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx
import pytest

from src.application import (
    AdministrationService,
    AgentDialogService,
    EventImportService,
    EventIngestionService,
    EventSearchService,
    FallbackSearchService,
    SubscriptionService,
    WeekendPlannerService,
)
from src.core.configuration import (
    ApplicationSettings,
    DatabaseSettings,
    GeolocationSettings,
    IngestionSettings,
    LanguageModelSettings,
    LoggingSettings,
    TelegramSettings,
    WebSettings,
)
from src.core.exceptions import ValidationException
from src.core.types import DialogChannel, EventCategory, EventStatus, RealCollection, SourceType
from src.domain.entities import (
    AuditLogEntry,
    CityReference,
    Coordinates,
    Event,
    EventSearchFilter,
    EventSearchPage,
    EventSourceReference,
    FeedbackRequestRecord,
    FeedbackStatus,
    FetchedSourceItem,
    LinkCheckResult,
    ModerationItem,
    ModerationStatus,
    NotificationDispatchReport,
    RawEventRecord,
    SourceDefinition,
    SubscriptionNotificationRecord,
    SubscriptionRecord,
    VenueReference,
)
from src.domain.exceptions import EventNotFoundError, ModerationItemNotFoundError, SubscriptionNotFoundError
from src.domain.ports import (
    AdministrationRepositoryPort,
    NotificationDeliveryOutcome,
    RawNormalizationStatus,
    SubscriptionNotificationSenderPort,
)
from src.domain.services import BASHKORTOSTAN_TIMEZONE, compute_deduplication_key, normalize_title
from src.entrypoints.background_worker.runner import BackgroundWorkerRunner
from src.entrypoints.bootstrap import ApplicationContainer
from src.entrypoints.web._formatting import VISITOR_COOKIE_NAME
from src.entrypoints.web.application import create_web_application
from src.infrastructure.notifications import TelegramSubscriptionNotificationSender

_REFERENCE_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)


def _make_event(
    *,
    title: str = "Вечерний джаз в Уфе",
    category: EventCategory = EventCategory.CONCERT,
    city: str = "Уфа",
    venue: str = "Башгосфилармония",
    address: str = "ул. Гоголя, 58",
    description: str = "Концертное выступление коллектива.",
    date_start: datetime | None = None,
    date_end: datetime | None = None,
    duration_minutes: int | None = 120,
    price_minimum: int | None = 500,
    price_maximum: int | None = 1200,
    is_free: bool = False,
    age_limit: int = 6,
    status: EventStatus = EventStatus.PUBLISHED,
    source_hash: str | None = None,
) -> Event:
    start_at = date_start or datetime(2026, 11, 20, 19, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)
    normalized = normalize_title(title)
    resolved_hash = source_hash or f"hash-{uuid4().hex}"
    return Event(
        identifier=uuid4(),
        title=title,
        normalized_title=normalized,
        category=category,
        tags=("джаз",),
        date_start=start_at,
        date_end=date_end,
        duration_minutes=duration_minutes,
        city=city,
        venue=venue,
        address=address,
        description=description,
        price_minimum=0 if is_free else price_minimum,
        price_maximum=0 if is_free else price_maximum,
        is_free=is_free,
        age_limit=age_limit,
        language="русский",
        source_url="https://example.org/events/jazz",
        source_type=SourceType.MANUAL,
        source_hash=resolved_hash,
        status=status,
        deduplication_key=compute_deduplication_key(
            normalized_title=normalized, date_start=start_at, city=city, venue=venue
        ),
        created_at=_REFERENCE_NOW,
        updated_at=_REFERENCE_NOW,
        sources=(
            EventSourceReference(
                source_name="Филармония",
                source_type=SourceType.MANUAL,
                source_url="https://example.org/events/jazz",
                source_hash=resolved_hash,
            ),
        ),
    )


def _make_notification(
    *,
    event: Event | None = None,
    channel: DialogChannel = DialogChannel.TELEGRAM,
    subscriber_identifier: str = "987654321",
    attempts: int = 0,
    status: Literal["pending", "sending", "sent", "failed", "read"] = "pending",
    created_at: datetime | None = None,
) -> SubscriptionNotificationRecord:
    moment = created_at or _REFERENCE_NOW
    return SubscriptionNotificationRecord(
        notification_identifier=uuid4(),
        subscription_identifier=uuid4(),
        channel=channel,
        subscriber_identifier=subscriber_identifier,
        event=event or _make_event(),
        status=status,
        attempts=attempts,
        last_error=None,
        next_retry_at=None,
        created_at=moment,
        updated_at=moment,
        sent_at=None,
        read_at=None,
    )


class _EventRepositoryStub:
    """Минимальный стаб `EventRepositoryPort` для сценариев уведомлений, сбора и веб-маршрутов."""

    def __init__(self, *, events: Sequence[Event] = ()) -> None:
        self.events: dict[UUID, Event] = {event.identifier: event for event in events}
        self.cities: tuple[CityReference, ...] = (
            CityReference(
                name="Уфа",
                normalized_name="уфа",
                aliases=("уфе",),
                center_coordinates=Coordinates(latitude=54.7431, longitude=55.9678),
            ),
            CityReference(
                name="Стерлитамак",
                normalized_name="стерлитамак",
                aliases=("стерлитамаке",),
                center_coordinates=Coordinates(latitude=53.6246, longitude=55.9501),
            ),
        )
        self.venues: tuple[VenueReference, ...] = (
            VenueReference(
                name="Башкирская государственная филармония",
                normalized_name="башкирская государственная филармония",
                city="Уфа",
                address="ул. Гоголя, 58",
                aliases=("Башгосфилармония",),
                coordinates=Coordinates(latitude=54.7261, longitude=55.9465),
            ),
        )

    async def check_health(self) -> bool:
        return True

    async def search_events(
        self, *, search_filter: EventSearchFilter, statuses: RealCollection[EventStatus] = (EventStatus.PUBLISHED,)
    ) -> EventSearchPage:
        allowed = set(statuses)
        matched = [
            event
            for event in self.events.values()
            if event.status in allowed
            and (search_filter.city is None or event.city == search_filter.city)
            and (search_filter.category is None or event.category == search_filter.category)
        ]
        offset = (search_filter.page - 1) * search_filter.page_size
        page_items = tuple(matched[offset : offset + search_filter.page_size])
        return EventSearchPage(
            items=page_items,
            total_count=len(matched),
            page=search_filter.page,
            page_size=search_filter.page_size,
            has_more=(offset + len(page_items)) < len(matched),
        )

    async def count_events(self, *, search_filter: EventSearchFilter) -> int:
        page = await self.search_events(search_filter=search_filter)
        return page.total_count

    async def get_event_by_identifier(self, *, event_identifier: UUID, include_non_published: bool = True) -> Event:
        event = self.events.get(event_identifier)
        if event is None or (not include_non_published and event.status is not EventStatus.PUBLISHED):
            raise EventNotFoundError("Событие не найдено.", event_identifier=event_identifier)
        return event

    async def find_similar_events(
        self,
        *,
        event_identifier: UUID,
        city: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        near_center: bool = False,
        limit: int = 5,
    ) -> tuple[Event, ...]:
        del city, date_from, date_to, near_center
        return tuple(
            event
            for event in self.events.values()
            if event.identifier != event_identifier and event.status is EventStatus.PUBLISHED
        )[:limit]

    async def find_by_source_hash(self, *, source_type: SourceType, source_hash: str) -> Event | None:
        for event in self.events.values():
            if event.source_type == source_type and event.source_hash == source_hash:
                return event
            if any(src.source_type == source_type and src.source_hash == source_hash for src in event.sources):
                return event
        return None

    async def save_event(self, *, event: Event) -> Event:
        self.events[event.identifier] = event
        return event

    async def list_cities(self) -> tuple[CityReference, ...]:
        return self.cities

    async def list_languages(self) -> tuple[str, ...]:
        return ("русский",)

    async def list_venues(self, *, city: str | None = None) -> tuple[VenueReference, ...]:
        if city is None:
            return self.venues
        return tuple(venue for venue in self.venues if venue.city == city)

    async def get_catalog_fingerprint(self) -> str:
        return f"stub:{len(self.events)}"


class _AdministrationRepositoryStub:
    """Асинхронный стаб `AdministrationRepositoryPort` с очередью уведомлений в памяти."""

    def __init__(self) -> None:
        self.subscriptions: dict[UUID, SubscriptionRecord] = {}
        self.notifications: dict[UUID, SubscriptionNotificationRecord] = {}
        self.moderation_items: dict[UUID, ModerationItem] = {}
        self.audit_logs: list[AuditLogEntry] = []
        self.feedback_requests: dict[UUID, FeedbackRequestRecord] = {}
        self.enqueue_calls: list[UUID] = []
        self.failed_calls: list[tuple[UUID, str, datetime, datetime | None, bool, int]] = []

    async def create_subscription(self, *, subscription: SubscriptionRecord) -> SubscriptionRecord:
        for existing in self.subscriptions.values():
            if (
                existing.channel == subscription.channel
                and existing.subscriber_identifier == subscription.subscriber_identifier
                and existing.city == subscription.city
                and existing.category == subscription.category
            ):
                return existing
        self.subscriptions[subscription.subscription_identifier] = subscription
        return subscription

    async def list_subscriptions_by_subscriber(
        self, *, channel: DialogChannel, subscriber_identifier: str
    ) -> tuple[SubscriptionRecord, ...]:
        return tuple(
            item
            for item in self.subscriptions.values()
            if item.channel == channel and item.subscriber_identifier == subscriber_identifier
        )

    async def delete_subscription(
        self, *, subscription_identifier: UUID, subscriber_identifier: str | None = None
    ) -> None:
        existing = self.subscriptions.get(subscription_identifier)
        if existing is None or (
            subscriber_identifier is not None and existing.subscriber_identifier != subscriber_identifier
        ):
            raise SubscriptionNotFoundError("Подписка не найдена.", subscription_identifier=subscription_identifier)
        del self.subscriptions[subscription_identifier]

    async def delete_all_subscriptions_for_subscriber(
        self, *, channel: DialogChannel, subscriber_identifier: str
    ) -> int:
        matching = [
            identifier
            for identifier, item in self.subscriptions.items()
            if item.channel == channel and item.subscriber_identifier == subscriber_identifier
        ]
        for identifier in matching:
            del self.subscriptions[identifier]
        return len(matching)

    async def save_feedback_request(self, *, feedback_request: FeedbackRequestRecord) -> FeedbackRequestRecord:
        self.feedback_requests[feedback_request.feedback_identifier] = feedback_request
        return feedback_request

    async def list_feedback_requests(
        self, *, status: FeedbackStatus | None = None, limit: int = 50
    ) -> tuple[FeedbackRequestRecord, ...]:
        items = [item for item in self.feedback_requests.values() if status is None or item.status == status]
        return tuple(items[:limit])

    async def mark_feedback_reviewed(self, *, feedback_identifier: UUID) -> FeedbackRequestRecord | None:
        del feedback_identifier
        return None

    async def enqueue_moderation_item(self, *, moderation_item: ModerationItem) -> ModerationItem:
        self.moderation_items[moderation_item.moderation_identifier] = moderation_item
        return moderation_item

    async def list_moderation_items(
        self, *, status: ModerationStatus | None = "pending", limit: int = 50
    ) -> tuple[ModerationItem, ...]:
        items = [item for item in self.moderation_items.values() if status is None or item.status == status]
        return tuple(items[:limit])

    async def resolve_moderation_item(
        self,
        *,
        moderation_identifier: UUID,
        status: Literal["approved", "rejected"],
        resolved_by: str,
        resolved_at: datetime,
    ) -> ModerationItem:
        del status, resolved_by, resolved_at
        raise ModerationItemNotFoundError("Не найдено", moderation_identifier=moderation_identifier)

    async def append_audit_log(self, *, entry: AuditLogEntry) -> AuditLogEntry:
        self.audit_logs.append(entry)
        return entry

    async def list_audit_logs(
        self, *, entity_identifier: str | None = None, limit: int = 100
    ) -> tuple[AuditLogEntry, ...]:
        del entity_identifier, limit
        return tuple(self.audit_logs)

    async def enqueue_subscription_notifications_for_event(self, *, event: Event, created_at: datetime) -> int:
        self.enqueue_calls.append(event.identifier)
        if event.status is not EventStatus.PUBLISHED:
            return 0
        existing_keys = {
            (item.channel, item.subscriber_identifier, item.event.identifier) for item in self.notifications.values()
        }
        created = 0
        for sub in self.subscriptions.values():
            if sub.city is not None and sub.city != event.city:
                continue
            if sub.category is not None and sub.category != event.category:
                continue
            dedup_key = (sub.channel, sub.subscriber_identifier, event.identifier)
            if dedup_key in existing_keys:
                continue
            existing_keys.add(dedup_key)
            notification_id = uuid4()
            self.notifications[notification_id] = SubscriptionNotificationRecord(
                notification_identifier=notification_id,
                subscription_identifier=sub.subscription_identifier,
                channel=sub.channel,
                subscriber_identifier=sub.subscriber_identifier,
                event=event,
                status="pending",
                attempts=0,
                last_error=None,
                next_retry_at=None,
                created_at=created_at,
                updated_at=created_at,
                sent_at=None,
                read_at=None,
            )
            created += 1
        return created

    async def claim_pending_telegram_notifications(
        self, *, current_time: datetime, limit: int = 50, maximum_attempts: int = 3, stale_after_seconds: int = 300
    ) -> tuple[SubscriptionNotificationRecord, ...]:
        stale_cutoff = current_time - timedelta(seconds=stale_after_seconds)
        candidates = [
            item
            for item in self.notifications.values()
            if item.channel == DialogChannel.TELEGRAM
            and item.event.status is EventStatus.PUBLISHED
            and item.attempts < maximum_attempts
            and (
                (item.status == "pending" and (item.next_retry_at is None or item.next_retry_at <= current_time))
                or (item.status == "sending" and item.updated_at <= stale_cutoff)
            )
        ]
        candidates.sort(key=lambda item: (item.created_at, item.notification_identifier))
        claimed: list[SubscriptionNotificationRecord] = []
        for item in candidates[:limit]:
            updated = replace(item, status="sending", attempts=item.attempts + 1, updated_at=current_time)
            self.notifications[item.notification_identifier] = updated
            claimed.append(updated)
        return tuple(claimed)

    async def mark_subscription_notification_sent(self, *, notification_identifier: UUID, sent_at: datetime) -> None:
        existing = self.notifications.get(notification_identifier)
        if existing is None:
            return
        self.notifications[notification_identifier] = replace(
            existing, status="sent", sent_at=sent_at, updated_at=sent_at, last_error=None, next_retry_at=None
        )

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
        self.failed_calls.append(
            (notification_identifier, error_message, failed_at, retry_at, is_permanent, maximum_attempts)
        )
        existing = self.notifications.get(notification_identifier)
        if existing is None:
            return
        is_exhausted = is_permanent or existing.attempts >= maximum_attempts
        self.notifications[notification_identifier] = replace(
            existing,
            status="failed" if is_exhausted else "pending",
            last_error=error_message,
            next_retry_at=None if is_exhausted else retry_at,
            updated_at=failed_at,
        )

    async def count_unread_web_subscription_notifications(self, *, subscriber_identifier: str) -> int:
        cleaned = subscriber_identifier.strip()
        return sum(
            1
            for item in self.notifications.values()
            if item.channel == DialogChannel.WEB
            and item.subscriber_identifier == cleaned
            and item.status != "read"
            and item.event.status is EventStatus.PUBLISHED
        )

    async def list_web_subscription_notifications(
        self, *, subscriber_identifier: str, include_read: bool = False, limit: int = 20
    ) -> tuple[SubscriptionNotificationRecord, ...]:
        cleaned = subscriber_identifier.strip()
        items = [
            item
            for item in self.notifications.values()
            if item.channel == DialogChannel.WEB
            and item.subscriber_identifier == cleaned
            and item.event.status is EventStatus.PUBLISHED
            and (include_read or item.status != "read")
        ]
        items.sort(key=lambda item: (item.created_at, item.notification_identifier), reverse=True)
        return tuple(items[:limit])

    async def mark_web_subscription_notification_read(
        self, *, notification_identifier: UUID, subscriber_identifier: str, read_at: datetime
    ) -> bool:
        cleaned = subscriber_identifier.strip()
        existing = self.notifications.get(notification_identifier)
        if existing is None or existing.channel != DialogChannel.WEB or existing.subscriber_identifier != cleaned:
            return False
        self.notifications[notification_identifier] = replace(
            existing, status="read", read_at=existing.read_at or read_at, updated_at=read_at
        )
        return True

    async def mark_all_web_subscription_notifications_read(
        self, *, subscriber_identifier: str, read_at: datetime
    ) -> int:
        cleaned = subscriber_identifier.strip()
        marked = 0
        for identifier, item in list(self.notifications.items()):
            if item.channel == DialogChannel.WEB and item.subscriber_identifier == cleaned and item.status != "read":
                self.notifications[identifier] = replace(item, status="read", read_at=read_at, updated_at=read_at)
                marked += 1
        return marked


class _RawEventRepositoryStub:
    """Стаб `RawEventRepositoryPort` для проверки хуков `EventIngestionService`."""

    def __init__(self, initial_records: Sequence[RawEventRecord] = ()) -> None:
        self.records: dict[UUID, RawEventRecord] = {item.identifier: item for item in initial_records}

    async def save_raw_event(self, *, raw_event: RawEventRecord) -> tuple[RawEventRecord, bool]:
        for existing in self.records.values():
            if existing.source_name == raw_event.source_name and existing.source_hash == raw_event.source_hash:
                return existing, False
        self.records[raw_event.identifier] = raw_event
        return raw_event, True

    async def list_raw_events_for_reprocessing(
        self,
        *,
        source_name: str | None = None,
        normalization_status: RawNormalizationStatus | None = None,
        limit: int = 100,
    ) -> tuple[RawEventRecord, ...]:
        matched = [
            record
            for record in self.records.values()
            if (source_name is None or record.source_name == source_name)
            and (normalization_status is None or record.normalization_status == normalization_status)
        ]
        return tuple(matched[:limit])

    async def update_normalization_status(
        self,
        *,
        raw_event_identifier: UUID,
        normalization_status: RawNormalizationStatus,
        error_message: str | None = None,
    ) -> None:
        existing = self.records[raw_event_identifier]
        self.records[raw_event_identifier] = replace(
            existing, normalization_status=normalization_status, error_message=error_message
        )


class _StubSourceFetcher:
    def __init__(self, items_by_source: dict[str, tuple[FetchedSourceItem, ...]]) -> None:
        self._items_by_source = items_by_source

    async def fetch_items(self, *, source: SourceDefinition) -> tuple[FetchedSourceItem, ...]:
        return self._items_by_source.get(source.name, ())


class _StubLinkChecker:
    async def check_link(self, *, target_url: str, timeout_seconds: float = 4.0) -> LinkCheckResult:
        del timeout_seconds
        return LinkCheckResult(
            target_url=target_url,
            is_reachable=True,
            http_status_code=200,
            final_url=target_url,
            error_reason=None,
            checked_at=_REFERENCE_NOW,
        )


class _RecordingTelegramSender:
    """Управляемый стаб `SubscriptionNotificationSenderPort`."""

    def __init__(
        self,
        *,
        results: Sequence[NotificationDeliveryOutcome | Exception] = (),
        results_by_subscriber: dict[str, NotificationDeliveryOutcome | Exception] | None = None,
    ) -> None:
        self._results = list(results)
        self._results_by_subscriber = dict(results_by_subscriber or {})
        self.calls: list[SubscriptionNotificationRecord] = []

    async def send_event_notification(
        self, *, notification: SubscriptionNotificationRecord
    ) -> NotificationDeliveryOutcome:
        self.calls.append(notification)
        if notification.subscriber_identifier in self._results_by_subscriber:
            mapped = self._results_by_subscriber[notification.subscriber_identifier]
            if isinstance(mapped, Exception):
                raise mapped
            return mapped
        if not self._results:
            return NotificationDeliveryOutcome(delivered=True)
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def test_telegram_sender_formats_html_timezone_and_buttons_and_closes_client() -> None:
    """Проверяет HTML-экранирование, перевод времени в +05:00, inline-кнопки и `close()`."""
    captured_requests: list[tuple[str, dict[str, Any]]] = []
    closed_flags: list[bool] = []

    class _RecordingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            captured_requests.append((str(request.url), payload))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

        async def aclose(self) -> None:
            closed_flags.append(True)

    utc_start = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
    utc_end = datetime(2026, 11, 20, 16, 30, tzinfo=UTC)
    event = _make_event(
        title='Джаз <Live> & "Квартет"',
        city="Уфа & Башкирия",
        venue='Зал "Башкортостан" <1>',
        address="ул. Ленина, 50 & корпус A",
        description="Описание с <script>alert(1)</script> и <b>тегами</b>",
        date_start=utc_start,
        date_end=utc_end,
        is_free=True,
        age_limit=12,
    )
    notification = _make_notification(event=event, subscriber_identifier="555000111")

    sender_with_web = TelegramSubscriptionNotificationSender(
        bot_token="123456:token-abc", public_web_base_url="https://afisha.example.ru/", transport=_RecordingTransport()
    )
    assert isinstance(sender_with_web, SubscriptionNotificationSenderPort)
    outcome = await sender_with_web.send_event_notification(notification=notification)
    assert outcome == NotificationDeliveryOutcome(delivered=True)

    url, payload = captured_requests[0]
    assert url == "https://api.telegram.org/bot123456:token-abc/sendMessage"
    assert payload["chat_id"] == "555000111"
    assert payload["parse_mode"] == "HTML"
    assert payload["disable_web_page_preview"] is True

    text = str(payload["text"])
    assert "&lt;Live&gt;" in text
    assert "&amp;" in text
    assert "&quot;Квартет&quot;" in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<script>" not in text
    assert "19:00–21:30 (+05:00)" in text
    assert "<code>Asia/Yekaterinburg</code>" in text
    assert "Бесплатно" in text
    assert "12+" in text

    keyboard = payload["reply_markup"]["inline_keyboard"]
    assert keyboard == [
        [
            {"text": "🎟 Подробнее", "callback_data": f"event:{event.identifier}"},
            {"text": "🌐 Открыть на сайте", "url": f"https://afisha.example.ru/events/{event.identifier}"},
        ],
        [{"text": "🔔 Мои подписки", "callback_data": "m:subscriptions"}],
    ]

    await sender_with_web.close()
    assert closed_flags == [True]
    assert sender_with_web._client.is_closed is True

    # При пустом public_web_base_url кнопка перехода на сайт отсутствует
    sender_without_web = TelegramSubscriptionNotificationSender(
        bot_token="123456:token-abc", public_web_base_url="", transport=_RecordingTransport()
    )
    await sender_without_web.send_event_notification(notification=notification)
    _, payload_without_web = captured_requests[1]
    assert payload_without_web["reply_markup"]["inline_keyboard"] == [
        [{"text": "🎟 Подробнее", "callback_data": f"event:{event.identifier}"}],
        [{"text": "🔔 Мои подписки", "callback_data": "m:subscriptions"}],
    ]
    await sender_without_web.close()


@pytest.mark.parametrize(
    ("status_code", "json_body", "headers", "expected_permanent", "expected_retry_after"),
    [
        (
            403,
            {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"},
            {},
            True,
            None,
        ),
        (400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}, {}, True, None),
        (404, {"ok": False, "error_code": 404, "description": "Not Found"}, {}, True, None),
        (
            429,
            {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 27}},
            {},
            False,
            27,
        ),
        (429, {"ok": False, "description": "Rate limited"}, {"Retry-After": "15"}, False, 15),
        (500, {"ok": False, "description": "Internal Server Error"}, {}, False, None),
    ],
)
async def test_telegram_sender_handles_http_status_codes(
    status_code: int,
    json_body: dict[str, Any],
    headers: dict[str, str],
    expected_permanent: bool,
    expected_retry_after: int | None,
) -> None:
    """Проверяет классификацию ответов 400/403/404, 429 retry_after и 500 от Telegram Bot API."""
    transport = httpx.MockTransport(lambda _request: httpx.Response(status_code, json=json_body, headers=headers))
    sender = TelegramSubscriptionNotificationSender(bot_token="123456:token", transport=transport)
    notification = _make_notification()

    outcome = await sender.send_event_notification(notification=notification)
    await sender.close()

    assert outcome.delivered is False
    assert outcome.is_permanent_failure is expected_permanent
    assert outcome.retry_after_seconds == expected_retry_after
    assert outcome.error_message is not None


async def test_telegram_sender_handles_network_exceptions_and_validation() -> None:
    """Проверяет обработку сетевых исключений `httpx` и валидацию таймаута."""
    with pytest.raises(ValidationException):
        TelegramSubscriptionNotificationSender(bot_token="123:abc", request_timeout_seconds=0.0)

    def _raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    sender = TelegramSubscriptionNotificationSender(
        bot_token="123456:token", transport=httpx.MockTransport(_raise_connect_error)
    )
    outcome = await sender.send_event_notification(notification=_make_notification())
    await sender.close()

    assert outcome.delivered is False
    assert outcome.is_permanent_failure is False
    assert outcome.error_message is not None
    assert "Сетевая ошибка" in outcome.error_message


async def test_subscription_service_notify_and_dispatch_retries_and_isolation() -> None:
    """Проверяет постановку в очередь, exponential backoff, retry_after, постоянный отказ и изоляцию исключений."""
    admin_repo = _AdministrationRepositoryStub()
    assert isinstance(admin_repo, AdministrationRepositoryPort)

    # 1. Не-PUBLISHED событие игнорируется
    service = SubscriptionService(administration_repository=admin_repo, telegram_notification_sender=None)
    draft_event = _make_event(status=EventStatus.DRAFT)
    draft_report = await service.notify_subscribers_for_event(event=draft_event, current_time=_REFERENCE_NOW)
    assert draft_report == NotificationDispatchReport()
    assert admin_repo.enqueue_calls == []

    # 2. Публикация ставит в очередь и немедленно отправляет в Telegram
    await service.subscribe(
        channel=DialogChannel.TELEGRAM, subscriber_identifier="tg-ok", city="Уфа", category=EventCategory.CONCERT
    )
    await service.subscribe(
        channel=DialogChannel.WEB, subscriber_identifier="web-ok", city="Уфа", category=EventCategory.CONCERT
    )
    sender = _RecordingTelegramSender()
    service_with_sender = SubscriptionService(
        administration_repository=admin_repo,
        telegram_notification_sender=sender,
        notification_batch_size=50,
        notification_max_attempts=3,
    )
    published_event = _make_event(status=EventStatus.PUBLISHED)
    immediate_report = await service_with_sender.notify_subscribers_for_event(
        event=published_event, dispatch_immediately=True, current_time=_REFERENCE_NOW
    )
    assert immediate_report == NotificationDispatchReport(
        enqueued_count=2, attempted_count=1, sent_count=1, failed_count=0, deferred_count=0
    )
    assert len(sender.calls) == 1

    # 3. Exponential backoff (60s -> 120s -> exhausted failed)
    backoff_repo = _AdministrationRepositoryStub()
    await SubscriptionService(administration_repository=backoff_repo).subscribe(
        channel=DialogChannel.TELEGRAM, subscriber_identifier="tg-retry", city="Уфа"
    )
    await backoff_repo.enqueue_subscription_notifications_for_event(event=published_event, created_at=_REFERENCE_NOW)
    notif_id = next(iter(backoff_repo.notifications))

    failing_sender = _RecordingTelegramSender(
        results=[
            NotificationDeliveryOutcome(delivered=False, is_permanent_failure=False, error_message="HTTP 500"),
            NotificationDeliveryOutcome(delivered=False, is_permanent_failure=False, error_message="HTTP 502"),
            NotificationDeliveryOutcome(delivered=False, is_permanent_failure=False, error_message="HTTP 503"),
        ]
    )
    retry_service = SubscriptionService(
        administration_repository=backoff_repo, telegram_notification_sender=failing_sender, notification_max_attempts=3
    )

    t0 = _REFERENCE_NOW
    r1 = await retry_service.dispatch_pending_telegram_notifications(current_time=t0)
    assert r1 == NotificationDispatchReport(attempted_count=1, deferred_count=1)
    assert backoff_repo.notifications[notif_id].status == "pending"
    assert backoff_repo.notifications[notif_id].next_retry_at == t0 + timedelta(seconds=60)

    # До наступления next_retry_at уведомление не берётся в работу
    assert (await retry_service.dispatch_pending_telegram_notifications(current_time=t0 + timedelta(seconds=30))) == (
        NotificationDispatchReport()
    )

    t1 = t0 + timedelta(seconds=60)
    r2 = await retry_service.dispatch_pending_telegram_notifications(current_time=t1)
    assert r2 == NotificationDispatchReport(attempted_count=1, deferred_count=1)
    assert backoff_repo.notifications[notif_id].next_retry_at == t1 + timedelta(seconds=120)

    t2 = t1 + timedelta(seconds=120)
    r3 = await retry_service.dispatch_pending_telegram_notifications(current_time=t2)
    assert r3 == NotificationDispatchReport(attempted_count=1, failed_count=1)
    assert backoff_repo.notifications[notif_id].status == "failed"
    assert backoff_repo.notifications[notif_id].next_retry_at is None

    # 4. retry_after_seconds, постоянный отказ и изоляция исключения сендера в одной пачке
    batch_repo = _AdministrationRepositoryStub()
    batch_sub_service = SubscriptionService(administration_repository=batch_repo)
    await batch_sub_service.subscribe(channel=DialogChannel.TELEGRAM, subscriber_identifier="tg-1", city="Уфа")
    await batch_sub_service.subscribe(channel=DialogChannel.TELEGRAM, subscriber_identifier="tg-2", city="Уфа")
    await batch_sub_service.subscribe(channel=DialogChannel.TELEGRAM, subscriber_identifier="tg-3", city="Уфа")
    await batch_sub_service.subscribe(channel=DialogChannel.TELEGRAM, subscriber_identifier="tg-4", city="Уфа")
    await batch_repo.enqueue_subscription_notifications_for_event(event=published_event, created_at=_REFERENCE_NOW)

    batch_sender = _RecordingTelegramSender(
        results_by_subscriber={
            "tg-1": NotificationDeliveryOutcome(
                delivered=False, is_permanent_failure=False, retry_after_seconds=45, error_message="429"
            ),
            "tg-2": NotificationDeliveryOutcome(
                delivered=False, is_permanent_failure=True, error_message="403 Blocked"
            ),
            "tg-3": RuntimeError("Неожиданное исключение транспорта"),
            "tg-4": NotificationDeliveryOutcome(delivered=True),
        }
    )
    batch_dispatch_service = SubscriptionService(
        administration_repository=batch_repo, telegram_notification_sender=batch_sender, notification_max_attempts=3
    )
    batch_report = await batch_dispatch_service.dispatch_pending_telegram_notifications(current_time=_REFERENCE_NOW)
    assert batch_report == NotificationDispatchReport(
        enqueued_count=0, attempted_count=4, sent_count=1, failed_count=1, deferred_count=2
    )
    statuses_by_subscriber = {item.subscriber_identifier: item for item in batch_repo.notifications.values()}
    assert statuses_by_subscriber["tg-1"].next_retry_at == _REFERENCE_NOW + timedelta(seconds=45)
    assert statuses_by_subscriber["tg-2"].status == "failed"
    assert statuses_by_subscriber["tg-2"].next_retry_at is None
    assert statuses_by_subscriber["tg-3"].status == "pending"
    assert statuses_by_subscriber["tg-3"].next_retry_at == _REFERENCE_NOW + timedelta(seconds=60)
    assert statuses_by_subscriber["tg-4"].status == "sent"


async def test_subscription_service_web_notification_methods_and_validation() -> None:
    """Проверяет веб-методы подсчёта, чтения и отметки уведомлений прочитанными."""
    admin_repo = _AdministrationRepositoryStub()
    service = SubscriptionService(administration_repository=admin_repo)

    await service.subscribe(channel=DialogChannel.WEB, subscriber_identifier="visitor-1", city="Уфа")
    event_first = _make_event(title="Событие 1")
    event_second = _make_event(title="Событие 2")
    await service.notify_subscribers_for_event(
        event=event_first, dispatch_immediately=False, current_time=_REFERENCE_NOW
    )
    await service.notify_subscribers_for_event(
        event=event_second, dispatch_immediately=False, current_time=_REFERENCE_NOW + timedelta(minutes=1)
    )

    assert await service.count_unread_web_notifications(visitor_identifier="visitor-1") == 2
    unread_list = await service.list_web_notifications(visitor_identifier="visitor-1")
    assert [item.event.title for item in unread_list] == ["Событие 2", "Событие 1"]

    # Чужой пользователь не может отметить уведомление
    assert (
        await service.mark_web_notification_read(
            notification_identifier=unread_list[0].notification_identifier,
            visitor_identifier="other-visitor",
            current_time=_REFERENCE_NOW,
        )
        is False
    )
    assert await service.count_unread_web_notifications(visitor_identifier="visitor-1") == 2

    # Владелец отмечает одно уведомление
    assert (
        await service.mark_web_notification_read(
            notification_identifier=unread_list[0].notification_identifier,
            visitor_identifier="visitor-1",
            current_time=_REFERENCE_NOW + timedelta(minutes=2),
        )
        is True
    )
    assert await service.count_unread_web_notifications(visitor_identifier="visitor-1") == 1
    assert len(await service.list_web_notifications(visitor_identifier="visitor-1", include_read=False)) == 1
    assert len(await service.list_web_notifications(visitor_identifier="visitor-1", include_read=True)) == 2

    # Массовая отметка оставшихся прочитанными
    assert (
        await service.mark_all_web_notifications_read(
            visitor_identifier="visitor-1", current_time=_REFERENCE_NOW + timedelta(minutes=3)
        )
        == 1
    )
    assert await service.count_unread_web_notifications(visitor_identifier="visitor-1") == 0

    with pytest.raises(ValidationException):
        await service.count_unread_web_notifications(visitor_identifier="  ")
    with pytest.raises(ValidationException):
        await service.list_web_notifications(visitor_identifier="visitor-1", limit=0)


async def test_event_ingestion_service_enqueues_and_dispatches_on_ingest_and_reprocess() -> None:
    """Проверяет постановку в очередь и рассылку по завершении `ingest_sources` и `reprocess_raw_events`."""
    event_repo = _EventRepositoryStub()
    admin_repo = _AdministrationRepositoryStub()
    raw_repo = _RawEventRepositoryStub()
    sender = _RecordingTelegramSender()
    subscription_service = SubscriptionService(
        administration_repository=admin_repo, event_repository=event_repo, telegram_notification_sender=sender
    )
    await subscription_service.subscribe(
        channel=DialogChannel.TELEGRAM, subscriber_identifier="tg-ingest", city="Уфа", category=EventCategory.CONCERT
    )

    call_log: list[tuple[str, bool]] = []
    original_notify = subscription_service.notify_subscribers_for_event
    original_dispatch = subscription_service.dispatch_pending_telegram_notifications

    async def _spied_notify(
        *, event: Event, dispatch_immediately: bool = True, current_time: datetime | None = None
    ) -> NotificationDispatchReport:
        call_log.append(("notify", dispatch_immediately))
        return await original_notify(event=event, dispatch_immediately=dispatch_immediately, current_time=current_time)

    async def _spied_dispatch(
        *, limit: int | None = None, current_time: datetime | None = None
    ) -> NotificationDispatchReport:
        call_log.append(("dispatch", True))
        return await original_dispatch(limit=limit, current_time=current_time)

    subscription_service.notify_subscribers_for_event = _spied_notify  # type: ignore[method-assign]
    subscription_service.dispatch_pending_telegram_notifications = _spied_dispatch  # type: ignore[method-assign]

    source = SourceDefinition(
        name="ufa_rss",
        source_type=SourceType.RSS,
        source_url="https://example.org/rss.xml",
        default_city="Уфа",
        is_enabled=True,
    )
    fetched_item = FetchedSourceItem(
        source_name="ufa_rss",
        source_type=SourceType.RSS,
        external_identifier="ext-1",
        source_url="https://example.org/events/symphony-1",
        raw_title="Симфонический концерт в Уфе",
        raw_description="Концерт классической музыки в ГКЗ Башкортостан.",
        raw_date_text="2026-11-25 19:00",
        raw_price_text="500 ₽",
        raw_city="Уфа",
        raw_venue="ГКЗ Башкортостан",
        raw_category="concert",
        raw_age_text="6+",
    )
    service = EventIngestionService(
        source_fetcher=_StubSourceFetcher({"ufa_rss": (fetched_item,)}),
        link_checker=_StubLinkChecker(),
        raw_event_repository=raw_repo,
        event_repository=event_repo,
        administration_repository=admin_repo,
        subscription_service=subscription_service,
    )

    ingest_report = await service.ingest_sources(sources=[source], current_time=_REFERENCE_NOW)
    assert ingest_report.items_published == 1
    assert call_log == [("notify", False), ("dispatch", True)]
    assert len(sender.calls) == 1

    # Повторный прогон по сырой записи для reprocess_raw_events (переход новой сырой записи -> PUBLISHED)
    call_log.clear()
    reprocess_raw_id = uuid4()
    raw_repo.records[reprocess_raw_id] = RawEventRecord(
        identifier=reprocess_raw_id,
        source_name="ufa_rss",
        source_type=SourceType.RSS,
        external_identifier="ext-reprocess-2",
        source_url="https://example.org/events/jazz-reprocess",
        source_hash="hash-reprocess-2",
        raw_payload={
            "source_name": "ufa_rss",
            "source_type": "rss",
            "external_identifier": "ext-reprocess-2",
            "source_url": "https://example.org/events/jazz-reprocess",
            "raw_title": "Джаз-вечер (reprocess)",
            "raw_description": "Живой джаз в центре Уфы.",
            "raw_date_text": "2026-11-26 20:00",
            "raw_price_text": "Бесплатно",
            "raw_city": "Уфа",
            "raw_venue": "Филармония",
            "raw_category": "concert",
            "raw_age_text": "6+",
        },
        fetched_at=_REFERENCE_NOW,
        normalization_status="pending",
    )
    reprocess_report = await service.reprocess_raw_events(
        normalization_status="pending", limit=50, current_time=_REFERENCE_NOW, sources_by_name={"ufa_rss": source}
    )
    assert reprocess_report.items_published == 1
    assert call_log == [("notify", False), ("dispatch", True)]
    assert len(sender.calls) == 2


async def test_background_worker_run_once_calls_dispatch_pending_telegram_notifications(tmp_path: Path) -> None:
    """Проверяет, что `BackgroundWorkerRunner.run_once` вызывает `dispatch_pending_telegram_notifications`."""
    sources_yaml = tmp_path / "sources.yaml"
    sources_yaml.write_text("sources: []\n", encoding="utf-8")

    event_repo = _EventRepositoryStub()
    admin_repo = _AdministrationRepositoryStub()
    raw_repo = _RawEventRepositoryStub()
    ingestion_service = EventIngestionService(
        source_fetcher=_StubSourceFetcher({}),
        link_checker=_StubLinkChecker(),
        raw_event_repository=raw_repo,
        event_repository=event_repo,
        administration_repository=admin_repo,
    )

    dispatch_calls: list[datetime | None] = []

    class _SubscriptionServiceSpy:
        async def dispatch_pending_telegram_notifications(
            self, *, limit: int | None = None, current_time: datetime | None = None
        ) -> NotificationDispatchReport:
            del limit
            dispatch_calls.append(current_time)
            return NotificationDispatchReport(attempted_count=1, sent_count=1)

    fake_container = MagicMock(spec=ApplicationContainer)
    fake_container.ingestion_settings = IngestionSettings(
        _env_file=None, SOURCES_CONFIG_PATH=sources_yaml, POLL_INTERVAL_SECONDS=10
    )
    fake_container.event_ingestion_service = ingestion_service
    fake_container.subscription_service = _SubscriptionServiceSpy()

    runner = BackgroundWorkerRunner(cast(ApplicationContainer, fake_container), sources_config_path=sources_yaml)
    await runner.run_once(current_time=_REFERENCE_NOW)

    assert dispatch_calls == [_REFERENCE_NOW]


def _build_web_test_container(
    *, event_repository: _EventRepositoryStub, admin_repository: _AdministrationRepositoryStub
) -> ApplicationContainer:
    search_service = EventSearchService(event_repository=event_repository)
    subscription_service = SubscriptionService(
        administration_repository=admin_repository, event_repository=event_repository
    )
    weekend_planner = WeekendPlannerService(event_repository=event_repository)
    fallback_search = FallbackSearchService(event_repository=event_repository, weekend_planner=weekend_planner)
    administration_service = AdministrationService(
        event_repository=event_repository,
        administration_repository=admin_repository,
        subscription_service=subscription_service,
    )
    import_service = EventImportService(
        event_repository=event_repository,
        administration_repository=admin_repository,
        subscription_service=subscription_service,
    )
    dialog_repository = cast(Any, MagicMock())
    agent_dialog_service = AgentDialogService(
        event_repository=event_repository,
        dialog_repository=dialog_repository,
        administration_repository=admin_repository,
        search_service=search_service,
        subscription_service=subscription_service,
        weekend_planner=weekend_planner,
        fallback_search=fallback_search,
        agent_orchestrator=None,
        language_model_settings=LanguageModelSettings(_env_file=None, API_KEY=""),
    )
    return ApplicationContainer(
        application_settings=ApplicationSettings(_env_file=None),
        database_settings=DatabaseSettings(_env_file=None),
        logging_settings=LoggingSettings(_env_file=None, TO_FILE=False),
        web_settings=WebSettings(_env_file=None, RATE_LIMIT_REQUESTS_PER_MINUTE=500),
        telegram_settings=TelegramSettings(_env_file=None, BOT_TOKEN=""),
        language_model_settings=LanguageModelSettings(_env_file=None, API_KEY=""),
        database_engine_manager=cast(Any, MagicMock()),
        event_repository=cast(Any, event_repository),
        raw_event_repository=cast(Any, MagicMock()),
        dialog_repository=dialog_repository,
        administration_repository=cast(Any, admin_repository),
        structured_extractor=cast(Any, MagicMock()),
        agent_orchestrator=None,
        search_service=search_service,
        import_service=import_service,
        subscription_service=subscription_service,
        administration_service=administration_service,
        weekend_planner=weekend_planner,
        fallback_search=fallback_search,
        agent_dialog_service=agent_dialog_service,
        geolocation_settings=GeolocationSettings(_env_file=None, EXTERNAL_ENABLED=False),
    )


async def test_web_routes_subscription_notifications_and_htmx_headers() -> None:
    """Проверяет SSR/HTMX-маршруты каталога, подписок, уведомлений и cookie `visitor_identifier` без БД."""
    published_event_1 = _make_event(title="Осенний джаз в филармонии")
    published_event_2 = _make_event(title="Ночной орган")
    event_repository = _EventRepositoryStub(events=(published_event_1, published_event_2))
    admin_repository = _AdministrationRepositoryStub()
    container = _build_web_test_container(event_repository=event_repository, admin_repository=admin_repository)
    app = create_web_application(container)

    visitor_id = "visitor-web-notifications-001"
    await container.subscription_service.subscribe(
        channel=DialogChannel.WEB, subscriber_identifier=visitor_id, city="Уфа", category=EventCategory.CONCERT
    )
    await container.subscription_service.notify_subscribers_for_event(
        event=published_event_1, dispatch_immediately=False, current_time=_REFERENCE_NOW
    )
    await container.subscription_service.notify_subscribers_for_event(
        event=published_event_2, dispatch_immediately=False, current_time=_REFERENCE_NOW + timedelta(minutes=1)
    )
    notifications = await container.subscription_service.list_web_notifications(visitor_identifier=visitor_id)
    assert len(notifications) == 2

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as anon_client:
        # 1. GET / без cookie НЕ выставляет visitor_identifier cookie и НЕ показывает блок уведомлений
        anon_home = await anon_client.get("/")
        assert anon_home.status_code == 200
        assert VISITOR_COOKIE_NAME not in anon_home.cookies
        assert "Новое по вашим подпискам" not in anon_home.text

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={VISITOR_COOKIE_NAME: "visitor-without-alerts"},
    ) as empty_visitor_client:
        # 4. GET /subscriptions без уведомлений отображает пустое состояние
        empty_subs_page = await empty_visitor_client.get("/subscriptions")
        assert empty_subs_page.status_code == 200
        assert "Новое по вашим подпискам" in empty_subs_page.text
        assert "Новых уведомлений пока нет" in empty_subs_page.text

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", cookies={VISITOR_COOKIE_NAME: visitor_id}
    ) as client:
        # 2. GET / с cookie и непрочитанными показывает блок «Новое по вашим подпискам» и бейдж #nav-subscriptions-badge
        visitor_home = await client.get("/")
        assert visitor_home.status_code == 200
        assert "Новое по вашим подпискам" in visitor_home.text
        assert 'id="nav-subscriptions-badge"' in visitor_home.text
        assert "непрочитанных уведомлений: 2" in visitor_home.text

        # 3. GET /subscriptions отображает блок уведомлений
        subs_page = await client.get("/subscriptions")
        assert subs_page.status_code == 200
        assert "Новое по вашим подпискам" in subs_page.text
        assert "Осенний джаз в филармонии" in subs_page.text

        # 5. POST /subscriptions/notifications/{id}/read в HTMX-режиме возвращает фрагмент + OOB-бейдж
        target_id = notifications[0].notification_identifier
        htmx_read_one = await client.post(
            f"/subscriptions/notifications/{target_id}/read",
            headers={"HX-Request": "true"},
            data={"surface": "subscriptions", "limit": "20"},
        )
        assert htmx_read_one.status_code == 200
        assert 'id="subscription-notifications-section"' in htmx_read_one.text
        assert 'id="nav-subscriptions-badge"' in htmx_read_one.text
        assert 'hx-swap-oob="outerHTML"' in htmx_read_one.text
        assert "непрочитанных уведомлений: 1" in htmx_read_one.text

        # 6. POST /subscriptions/notifications/{id}/read без HTMX возвращает 303 See Other
        second_id = notifications[1].notification_identifier
        plain_read_one = await client.post(
            f"/subscriptions/notifications/{second_id}/read",
            data={"surface": "catalog", "limit": "3"},
            follow_redirects=False,
        )
        assert plain_read_one.status_code == 303
        assert plain_read_one.headers["location"] == "/"

        # Подготовим ещё уведомления для проверки read-all
        published_event_3 = _make_event(title="Утренний камерный концерт")
        await event_repository.save_event(event=published_event_3)
        await container.subscription_service.notify_subscribers_for_event(
            event=published_event_3, dispatch_immediately=False, current_time=_REFERENCE_NOW + timedelta(minutes=2)
        )

        # 7. POST /subscriptions/notifications/read-all в HTMX-режиме возвращает фрагмент + скрытый OOB-бейдж
        htmx_read_all = await client.post(
            "/subscriptions/notifications/read-all",
            headers={"HX-Request": "true"},
            data={"surface": "subscriptions", "limit": "20"},
        )
        assert htmx_read_all.status_code == 200
        assert 'id="subscription-notifications-section"' in htmx_read_all.text
        assert 'id="nav-subscriptions-badge"' in htmx_read_all.text
        assert 'hx-swap-oob="outerHTML"' in htmx_read_all.text
        assert "Новых уведомлений пока нет" in htmx_read_all.text

        # 8. POST /subscriptions/notifications/read-all без HTMX возвращает 303 See Other
        plain_read_all = await client.post(
            "/subscriptions/notifications/read-all",
            data={"surface": "subscriptions", "limit": "20"},
            follow_redirects=False,
        )
        assert plain_read_all.status_code == 303
        assert plain_read_all.headers["location"] == "/subscriptions"

        # 9. POST /subscriptions и DELETE выставляют заголовок HX-Trigger: subscriptionsChanged
        create_sub_resp = await client.post(
            "/subscriptions", headers={"HX-Request": "true"}, data={"city": "Стерлитамак", "category": "exhibition"}
        )
        assert create_sub_resp.status_code == 200
        assert create_sub_resp.headers.get("HX-Trigger") == "subscriptionsChanged"

        current_subs = await container.subscription_service.list_subscriptions(
            channel=DialogChannel.WEB, subscriber_identifier=visitor_id
        )
        created_sub = next(item for item in current_subs if item.city == "Стерлитамак")
        delete_sub_resp = await client.post(
            f"/subscriptions/{created_sub.subscription_identifier}/delete", headers={"HX-Request": "true"}
        )
        assert delete_sub_resp.status_code == 200
        assert delete_sub_resp.headers.get("HX-Trigger") == "subscriptionsChanged"
