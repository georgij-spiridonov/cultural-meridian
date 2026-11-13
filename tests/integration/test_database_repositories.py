"""Интеграционные тесты четырёх PostgreSQL-репозиториев на реальной базе PostgreSQL 17."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

import pytest

from src.core.types import DialogChannel, EventCategory, EventStatus, SourceType
from src.domain.entities import (
    AgentToolInvocation,
    AgentTraceRecord,
    AuditLogEntry,
    DialogMessage,
    Event,
    EventSearchFilter,
    EventSortField,
    EventSourceReference,
    FeedbackRequestRecord,
    ModerationItem,
    PendingAction,
    RawEventRecord,
    SortDirection,
    SubscriptionRecord,
)
from src.domain.exceptions import (
    DuplicateEventConflictError,
    EventNotFoundError,
    PendingActionExpiredError,
    PendingActionNotFoundError,
    SubscriptionNotFoundError,
)
from src.domain.ports import (
    AdministrationRepositoryPort,
    DialogRepositoryPort,
    EventRepositoryPort,
    RawEventRepositoryPort,
)
from src.domain.services.deduplication import compute_deduplication_key
from src.domain.services.normalization import BASHKORTOSTAN_TIMEZONE, normalize_title
from src.infrastructure.database import (
    PostgreSqlAdministrationRepository,
    PostgreSqlDialogRepository,
    PostgreSqlEventRepository,
    PostgreSqlRawEventRepository,
)

pytestmark = pytest.mark.integration


def _build_test_event(
    *,
    title: str,
    city: str,
    venue: str,
    category: EventCategory,
    date_start: datetime,
    price_minimum: int | None = 500,
    price_maximum: int | None = 1200,
    is_free: bool = False,
    age_limit: int = 12,
    language: str | None = "русский",
    tags: tuple[str, ...] = ("культура",),
    status: EventStatus = EventStatus.PUBLISHED,
    source_name: str = "Культура.РФ",
    source_type: SourceType = SourceType.API,
    source_hash: str | None = None,
    latitude: float | None = 54.722222,
    longitude: float | None = 55.945000,
    description: str = "Описание культурного мероприятия Республики Башкортостан.",
) -> Event:
    """Создаёт доменную карточку мероприятия для интеграционной проверки репозитория."""
    normalized_title = normalize_title(title)
    resolved_hash = source_hash or uuid4().hex
    resolved_url = f"https://example.org/events/{resolved_hash}"
    now = datetime(2026, 11, 13, 18, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)
    return Event(
        identifier=uuid4(),
        title=title,
        normalized_title=normalized_title,
        category=category,
        tags=tags,
        date_start=date_start,
        date_end=date_start + timedelta(hours=2),
        duration_minutes=120,
        city=city,
        venue=venue,
        address="ул. Ленина, 5/1",
        latitude=latitude,
        longitude=longitude,
        description=description,
        price_minimum=price_minimum,
        price_maximum=price_maximum,
        is_free=is_free,
        age_limit=age_limit,
        language=language,
        organization_link=resolved_url,
        source_url=resolved_url,
        source_type=source_type,
        source_hash=resolved_hash,
        verified_at=now,
        confidence=0.98,
        status=status,
        deduplication_key=compute_deduplication_key(
            normalized_title=normalized_title, date_start=date_start, city=city, venue=venue
        ),
        created_at=now,
        updated_at=now,
        sources=(
            EventSourceReference(
                source_name=source_name,
                source_type=source_type,
                source_url=resolved_url,
                source_hash=resolved_hash,
                verified_at=now,
            ),
        ),
    )


async def test_repositories_satisfy_domain_protocols(
    event_repository: PostgreSqlEventRepository,
    raw_event_repository: PostgreSqlRawEventRepository,
    dialog_repository: PostgreSqlDialogRepository,
    administration_repository: PostgreSqlAdministrationRepository,
) -> None:
    """Проверяет соответствие всех четырёх адаптеров протоколам портов и доступность БД."""
    assert isinstance(event_repository, EventRepositoryPort)
    assert isinstance(raw_event_repository, RawEventRepositoryPort)
    assert isinstance(dialog_repository, DialogRepositoryPort)
    assert isinstance(administration_repository, AdministrationRepositoryPort)
    assert await event_repository.check_health() is True


async def test_raw_event_repository_idempotency_and_reprocessing(
    raw_event_repository: PostgreSqlRawEventRepository,
) -> None:
    """Проверяет идемпотентность сохранения сырых записей и смену статуса нормализации."""
    now = datetime.now(tz=UTC)
    first = RawEventRecord(
        identifier=uuid4(),
        source_name="afisha_feed",
        source_type=SourceType.RSS,
        external_identifier="ext-001",
        source_url="https://example.org/rss/001",
        source_hash="hash-001",
        raw_payload={"title": "Щелкунчик", "city": "Уфа"},
        fetched_at=now,
        normalization_status="pending",
    )
    saved_first, created_first = await raw_event_repository.save_raw_event(raw_event=first)
    assert created_first is True
    duplicate_with_new_identifier = RawEventRecord(
        identifier=uuid4(),
        source_name="afisha_feed",
        source_type=SourceType.RSS,
        external_identifier="ext-001-dup",
        source_url="https://example.org/rss/001",
        source_hash="hash-001",
        raw_payload={"title": "Щелкунчик", "city": "Уфа"},
        fetched_at=now + timedelta(minutes=1),
        normalization_status="pending",
    )
    saved_duplicate, created_duplicate = await raw_event_repository.save_raw_event(
        raw_event=duplicate_with_new_identifier
    )
    assert created_duplicate is False
    assert saved_duplicate.identifier == saved_first.identifier

    pending = await raw_event_repository.list_raw_events_for_reprocessing(
        source_name="afisha_feed", normalization_status="pending", limit=10
    )
    assert len(pending) == 1
    assert pending[0].identifier == saved_first.identifier

    await raw_event_repository.update_normalization_status(
        raw_event_identifier=saved_first.identifier, normalization_status="normalized"
    )
    pending_after = await raw_event_repository.list_raw_events_for_reprocessing(
        source_name="afisha_feed", normalization_status="pending", limit=10
    )
    assert pending_after == ()


async def test_event_repository_search_fts_trigram_timezone_and_conflicts(
    event_repository: PostgreSqlEventRepository,
) -> None:
    """Проверяет полнотекстовый и триграммный поиск, фильтрацию, отмены и конфликты источников."""
    nutcracker = _build_test_event(
        title="Щелкунчик (премьера, 6+)",
        city="Уфа",
        venue="Башкирский государственный театр оперы и балета",
        category=EventCategory.SPECTACLE,
        date_start=datetime(2026, 11, 14, 19, 0, tzinfo=BASHKORTOSTAN_TIMEZONE),
        price_minimum=700,
        price_maximum=2500,
        age_limit=6,
        tags=("балет", "классика", "дети"),
        source_hash="hash-nutcracker-1",
    )
    kids_bashkir = _build_test_event(
        title="Урал-батыр (на башкирском языке)",
        city="Стерлитамак",
        venue="Стерлитамакское государственное театрально-концертное объединение",
        category=EventCategory.SPECTACLE,
        date_start=datetime(2026, 11, 14, 12, 0, tzinfo=BASHKORTOSTAN_TIMEZONE),
        price_minimum=0,
        price_maximum=0,
        is_free=True,
        age_limit=0,
        language="башкирский",
        tags=("дети", "башкирский язык"),
        latitude=53.6339,
        longitude=55.9404,
        source_hash="hash-ural-batyr",
    )
    canceled_show = _build_test_event(
        title="Отменённый джазовый вечер",
        city="Уфа",
        venue="Башкирская государственная филармония",
        category=EventCategory.CONCERT,
        date_start=datetime(2026, 11, 14, 20, 0, tzinfo=BASHKORTOSTAN_TIMEZONE),
        status=EventStatus.CANCELED,
        source_hash="hash-canceled",
    )

    saved_nutcracker = await event_repository.save_event(event=nutcracker)
    await event_repository.save_event(event=kids_bashkir)
    saved_canceled = await event_repository.save_event(event=canceled_show)

    # Поиск с опечаткой по названию ("шилкунчик") и вечернему времени в +05:00
    typo_page = await event_repository.search_events(
        search_filter=EventSearchFilter(
            query="шилкунчик",
            time_from=time(18, 0),
            time_to=time(22, 0),
            sort_field=EventSortField.RELEVANCE,
            sort_direction=SortDirection.DESCENDING,
        )
    )
    assert typo_page.total_count == 1
    assert typo_page.items[0].identifier == saved_nutcracker.identifier

    # Опечатка города ("стерлитакмае") + фильтр бесплатного башкирского детского события
    sterlitamak_page = await event_repository.search_events(
        search_filter=EventSearchFilter(city="стерлитакмае", only_free=True, maximum_age_limit=6, language="башкирский")
    )
    assert sterlitamak_page.total_count == 1
    assert sterlitamak_page.items[0].title == "Урал-батыр (на башкирском языке)"

    # Отменённое событие не попадает в публичный поиск, но доступно по идентификатору
    all_published = await event_repository.search_events(search_filter=EventSearchFilter(page_size=20))
    assert all(item.identifier != saved_canceled.identifier for item in all_published.items)
    direct_canceled = await event_repository.get_event_by_identifier(event_identifier=saved_canceled.identifier)
    assert direct_canceled.status is EventStatus.CANCELED

    with pytest.raises(EventNotFoundError):
        await event_repository.get_event_by_identifier(event_identifier=uuid4())

    # Конфликт при попытке привязать уже занятый (source_type, source_hash) к другой карточке
    conflicting = _build_test_event(
        title="Другой концерт",
        city="Уфа",
        venue="ГКЗ «Башкортостан»",
        category=EventCategory.CONCERT,
        date_start=datetime(2026, 11, 15, 19, 0, tzinfo=BASHKORTOSTAN_TIMEZONE),
        source_hash="hash-nutcracker-1",
    )
    with pytest.raises(DuplicateEventConflictError):
        await event_repository.save_event(event=conflicting)

    # Перестановка основного источника без конфликта частичного индекса и изменение ETag
    fingerprint_before = await event_repository.get_catalog_fingerprint()
    secondary_source = EventSourceReference(
        source_name="Яндекс Афиша",
        source_type=SourceType.WEBPAGE,
        source_url="https://example.org/yandex/nutcracker",
        source_hash="hash-nutcracker-2",
        verified_at=saved_nutcracker.updated_at,
    )
    swapped_event = Event(
        identifier=saved_nutcracker.identifier,
        title=saved_nutcracker.title,
        normalized_title=saved_nutcracker.normalized_title,
        category=saved_nutcracker.category,
        tags=saved_nutcracker.tags,
        date_start=saved_nutcracker.date_start,
        date_end=saved_nutcracker.date_end,
        duration_minutes=saved_nutcracker.duration_minutes,
        city=saved_nutcracker.city,
        venue=saved_nutcracker.venue,
        address=saved_nutcracker.address,
        latitude=saved_nutcracker.latitude,
        longitude=saved_nutcracker.longitude,
        description=saved_nutcracker.description,
        price_minimum=saved_nutcracker.price_minimum,
        price_maximum=saved_nutcracker.price_maximum,
        is_free=saved_nutcracker.is_free,
        age_limit=saved_nutcracker.age_limit,
        language=saved_nutcracker.language,
        organization_link=saved_nutcracker.organization_link,
        source_url=secondary_source.source_url,
        source_type=secondary_source.source_type,
        source_hash=secondary_source.source_hash,
        verified_at=secondary_source.verified_at,
        confidence=saved_nutcracker.confidence,
        status=saved_nutcracker.status,
        deduplication_key=saved_nutcracker.deduplication_key,
        created_at=saved_nutcracker.created_at,
        updated_at=saved_nutcracker.updated_at + timedelta(minutes=5),
        sources=(secondary_source, saved_nutcracker.sources[0]),
    )
    updated_nutcracker = await event_repository.save_event(event=swapped_event)
    assert updated_nutcracker.source_type is SourceType.WEBPAGE
    assert len(updated_nutcracker.sources) == 2
    assert await event_repository.get_catalog_fingerprint() != fingerprint_before


async def test_dialog_repository_memory_pending_actions_traces_and_budget(
    dialog_repository: PostgreSqlDialogRepository, event_repository: PostgreSqlEventRepository
) -> None:
    """Проверяет скользящее окно диалога, одноразовые подтверждения, трассы и квоты."""
    base_time = datetime(2026, 11, 13, 19, 0, tzinfo=UTC)
    session_state = await dialog_repository.get_session_state(
        session_identifier="sess-test-1", channel=DialogChannel.WEB, subscriber_identifier="web-user-1", message_limit=5
    )
    assert session_state.recent_messages == ()

    messages = [
        DialogMessage(
            role="user" if index % 2 == 0 else "assistant",
            content=f"Реплика {index}",
            created_at=base_time + timedelta(seconds=index),
        )
        for index in range(8)
    ]
    await dialog_repository.append_messages(session_identifier="sess-test-1", messages=messages)
    reloaded = await dialog_repository.get_session_state(
        session_identifier="sess-test-1", channel=DialogChannel.WEB, subscriber_identifier="web-user-1", message_limit=5
    )
    assert len(reloaded.recent_messages) == 5
    assert [message.content for message in reloaded.recent_messages] == [
        "Реплика 3",
        "Реплика 4",
        "Реплика 5",
        "Реплика 6",
        "Реплика 7",
    ]

    # Создание и атомарное потребление отложенного действия
    action = PendingAction(
        action_identifier=uuid4(),
        session_identifier="sess-test-1",
        channel=DialogChannel.WEB,
        action_type="subscribe",
        payload={"city": "Уфа", "category": "concert"},
        confirmation_prompt="Подтвердить подписку на концерты в Уфе?",
        created_at=base_time,
        expires_at=base_time + timedelta(minutes=15),
    )
    await dialog_repository.create_pending_action(pending_action=action)
    consumed = await dialog_repository.consume_pending_action(
        action_identifier=action.action_identifier,
        session_identifier="sess-test-1",
        current_time=base_time + timedelta(minutes=2),
    )
    assert consumed.action_identifier == action.action_identifier
    with pytest.raises(PendingActionNotFoundError):
        await dialog_repository.consume_pending_action(
            action_identifier=action.action_identifier,
            session_identifier="sess-test-1",
            current_time=base_time + timedelta(minutes=3),
        )

    # Проверка истёкшего подтверждения
    expired_action = PendingAction(
        action_identifier=uuid4(),
        session_identifier="sess-test-1",
        channel=DialogChannel.WEB,
        action_type="save_feedback",
        payload={"message_text": "Уточните дату"},
        confirmation_prompt="Отправить обращение?",
        created_at=base_time,
        expires_at=base_time + timedelta(minutes=5),
    )
    await dialog_repository.create_pending_action(pending_action=expired_action)
    with pytest.raises(PendingActionExpiredError):
        await dialog_repository.consume_pending_action(
            action_identifier=expired_action.action_identifier,
            session_identifier="sess-test-1",
            current_time=base_time + timedelta(minutes=10),
        )

    # Сохранение трассы с подтверждённым идентификатором события
    grounded_event = await event_repository.save_event(
        event=_build_test_event(
            title="Симфонический вечер",
            city="Уфа",
            venue="Башкирская государственная филармония",
            category=EventCategory.CONCERT,
            date_start=datetime(2026, 11, 14, 19, 0, tzinfo=BASHKORTOSTAN_TIMEZONE),
        )
    )
    trace = AgentTraceRecord(
        trace_identifier=uuid4(),
        request_identifier="req-123",
        session_identifier="sess-test-1",
        channel=DialogChannel.WEB,
        user_query="Какие концерты в Уфе 14 ноября?",
        plan_summary="Вызов search_events по городу Уфа",
        tool_invocations=(
            AgentToolInvocation(
                tool_name="search_events",
                arguments={"city": "Уфа"},
                returned_event_identifiers=(grounded_event.identifier,),
                succeeded=True,
            ),
        ),
        grounded_event_identifiers=(grounded_event.identifier,),
        fallback_used=False,
        latency_milliseconds=420,
        tokens_input=350,
        tokens_output=120,
        cost_rubles=0.45,
        model_name="gemini-4-hydrogen",
        prompt_version="v1.0",
        created_at=base_time,
    )
    await dialog_repository.save_trace(trace=trace)
    fetched_trace = await dialog_repository.get_trace(trace_identifier=trace.trace_identifier)
    assert fetched_trace is not None
    assert fetched_trace.grounded_event_identifiers == (grounded_event.identifier,)
    assert len(await dialog_repository.list_recent_traces(limit=10)) == 1

    # Контроль суточных лимитов стоимости
    target_day = date(2026, 11, 13)
    await dialog_repository.record_model_usage(
        subscriber_identifier="web-user-1", target_date=target_day, cost_rubles=1.5, tokens_input=200, tokens_output=80
    )
    await dialog_repository.record_model_usage(
        subscriber_identifier="web-user-1", target_date=target_day, cost_rubles=2.0, tokens_input=300, tokens_output=110
    )
    budget = await dialog_repository.get_usage_budget_status(
        subscriber_identifier="web-user-1",
        target_date=target_day,
        user_daily_limit_rubles=3.0,
        global_daily_limit_rubles=50.0,
    )
    assert budget.user_daily_spent_rubles == pytest.approx(3.5)
    assert budget.is_budget_exceeded is True


async def test_administration_repository_subscriptions_feedback_moderation_and_audit(
    administration_repository: PostgreSqlAdministrationRepository, event_repository: PostgreSqlEventRepository
) -> None:
    """Проверяет подписки NULLS NOT DISTINCT, физическое удаление, модерацию и аудит."""
    now = datetime(2026, 11, 13, 20, 0, tzinfo=UTC)
    first_subscription = SubscriptionRecord(
        subscription_identifier=uuid4(),
        channel=DialogChannel.TELEGRAM,
        subscriber_identifier="tg-999",
        city="Уфа",
        category=None,
        created_at=now,
    )
    created_subscription = await administration_repository.create_subscription(subscription=first_subscription)
    duplicate_subscription = SubscriptionRecord(
        subscription_identifier=uuid4(),
        channel=DialogChannel.TELEGRAM,
        subscriber_identifier="tg-999",
        city="Уфа",
        category=None,
        created_at=now + timedelta(seconds=10),
    )
    deduplicated_subscription = await administration_repository.create_subscription(subscription=duplicate_subscription)
    assert deduplicated_subscription.subscription_identifier == created_subscription.subscription_identifier

    await administration_repository.delete_subscription(
        subscription_identifier=created_subscription.subscription_identifier, subscriber_identifier="tg-999"
    )
    assert (
        await administration_repository.list_subscriptions_by_subscriber(
            channel=DialogChannel.TELEGRAM, subscriber_identifier="tg-999"
        )
        == ()
    )
    with pytest.raises(SubscriptionNotFoundError):
        await administration_repository.delete_subscription(
            subscription_identifier=created_subscription.subscription_identifier
        )

    feedback = FeedbackRequestRecord(
        feedback_identifier=uuid4(),
        channel=DialogChannel.WEB,
        event_identifier=None,
        message_text="Прошу обновить время начала спектакля.",
        contact_hint="organizer@example.org",
        created_at=now,
    )
    saved_feedback = await administration_repository.save_feedback_request(feedback_request=feedback)
    reviewed = await administration_repository.mark_feedback_reviewed(
        feedback_identifier=saved_feedback.feedback_identifier
    )
    assert reviewed is not None
    assert reviewed.status == "reviewed"

    draft_event = await event_repository.save_event(
        event=_build_test_event(
            title="Черновик афиши",
            city="Уфа",
            venue="Кинотеатр «Родина»",
            category=EventCategory.CINEMA,
            date_start=datetime(2026, 11, 16, 19, 0, tzinfo=BASHKORTOSTAN_TIMEZONE),
            status=EventStatus.DRAFT,
        )
    )
    moderation = ModerationItem(
        moderation_identifier=uuid4(),
        event_identifier=draft_event.identifier,
        raw_event_identifier=None,
        reason="Проверка ссылки источника",
        status="pending",
        has_suspicious_instructions=False,
        created_at=now,
    )
    await administration_repository.enqueue_moderation_item(moderation_item=moderation)
    resolved = await administration_repository.resolve_moderation_item(
        moderation_identifier=moderation.moderation_identifier,
        status="approved",
        resolved_by="moderator-1",
        resolved_at=now + timedelta(minutes=1),
    )
    assert resolved.status == "approved"

    audit = AuditLogEntry(
        entry_identifier=uuid4(),
        actor="moderator-1",
        action="approve",
        entity_type="event",
        entity_identifier=str(draft_event.identifier),
        changes={"status": "published"},
        created_at=now + timedelta(minutes=1),
    )
    await administration_repository.append_audit_log(entry=audit)
    logs = await administration_repository.list_audit_logs(entity_identifier=str(draft_event.identifier))
    assert len(logs) == 1
    assert logs[0].changes["status"] == "published"
