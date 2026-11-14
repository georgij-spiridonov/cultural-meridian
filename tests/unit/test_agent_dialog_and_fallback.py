"""Модульные тесты оркестратора диалога `AgentDialogService` и резервного поиска `FallbackSearchService`."""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4

from src.application.agent_dialog import AgentDialogService
from src.application.fallback_search import FallbackSearchService
from src.application.search_events import EventSearchService
from src.application.subscriptions import SubscriptionService
from src.application.weekend_planner import WeekendPlannerService
from src.core.configuration import LanguageModelSettings
from src.core.exceptions import NetworkConnectionTimeoutError
from src.core.types import DialogChannel, EventCategory, EventStatus, RealCollection, SourceType
from src.domain.entities import (
    AgentToolInvocation,
    AgentTraceRecord,
    AgentTurnResult,
    CityReference,
    DialogMessage,
    DialogSessionState,
    Event,
    EventSearchFilter,
    EventSearchPage,
    FeedbackRequestRecord,
    PendingAction,
    SubscriptionRecord,
    UsageBudgetStatus,
    VenueReference,
)
from src.domain.exceptions import EventNotFoundError, PendingActionExpiredError, PendingActionNotFoundError
from src.domain.ports import AgentToolCallbacks
from src.domain.services import BASHKORTOSTAN_TIMEZONE


class _DialogEventRepositoryStub:
    """Минимальный стаб `EventRepositoryPort` для диалоговых сценариев."""

    def __init__(self, events: Sequence[Event]) -> None:
        self._events = {event.identifier: event for event in events}

    async def search_events(
        self, *, search_filter: EventSearchFilter, statuses: RealCollection[EventStatus] = (EventStatus.PUBLISHED,)
    ) -> EventSearchPage:
        matched = [
            event
            for event in self._events.values()
            if event.status in statuses
            and (search_filter.city is None or event.city.lower() == search_filter.city.lower())
            and (search_filter.category is None or event.category == search_filter.category)
            and (not search_filter.only_free or event.is_free)
        ]
        items = tuple(matched[: search_filter.page_size])
        return EventSearchPage(
            items=items,
            total_count=len(matched),
            page=search_filter.page,
            page_size=search_filter.page_size,
            has_more=len(matched) > len(items),
        )

    async def get_event_by_identifier(self, *, event_identifier: UUID, include_non_published: bool = True) -> Event:
        del include_non_published
        event = self._events.get(event_identifier)
        if event is None:
            raise EventNotFoundError("Событие не найдено", event_identifier=event_identifier)
        return event

    async def list_cities(self) -> tuple[CityReference, ...]:
        return (CityReference(name="Уфа", normalized_name="уфа"),)

    async def list_venues(self, *, city: str | None = None) -> tuple[VenueReference, ...]:
        del city
        return ()

    async def find_similar_events(self, **kwargs: object) -> tuple[Event, ...]:
        del kwargs
        return tuple(self._events.values())[:2]


class _InMemoryDialogRepositoryStub:
    """Асинхронный стаб `DialogRepositoryPort` с поддержкой истории, pending_actions, трасс и бюджета."""

    def __init__(self, *, user_spent_rubles: float = 0.0, global_spent_rubles: float = 0.0) -> None:
        self.messages_by_session: dict[str, list[DialogMessage]] = {}
        self.pending_by_session: dict[str, PendingAction] = {}
        self.traces: list[AgentTraceRecord] = []
        self.usage_records: list[tuple[str, date, float, int, int]] = []
        self.user_spent_rubles = user_spent_rubles
        self.global_spent_rubles = global_spent_rubles

    async def get_session_state(
        self, *, session_identifier: str, channel: DialogChannel, subscriber_identifier: str, message_limit: int = 10
    ) -> DialogSessionState:
        history = self.messages_by_session.get(session_identifier, [])
        return DialogSessionState(
            session_identifier=session_identifier,
            channel=channel,
            subscriber_identifier=subscriber_identifier,
            recent_messages=tuple(history[-message_limit:]),
            active_pending_action=self.pending_by_session.get(session_identifier),
        )

    async def append_messages(self, *, session_identifier: str, messages: Sequence[DialogMessage]) -> None:
        self.messages_by_session.setdefault(session_identifier, []).extend(messages)

    async def create_pending_action(self, *, pending_action: PendingAction) -> PendingAction:
        self.pending_by_session[pending_action.session_identifier] = pending_action
        return pending_action

    async def consume_pending_action(
        self, *, action_identifier: UUID, session_identifier: str, current_time: datetime
    ) -> PendingAction:
        pending = self.pending_by_session.get(session_identifier)
        if pending is None or pending.action_identifier != action_identifier:
            raise PendingActionNotFoundError("Не найдено", action_identifier=action_identifier)
        if current_time > pending.expires_at:
            self.pending_by_session.pop(session_identifier, None)
            raise PendingActionExpiredError("Истёк TTL", action_identifier=action_identifier)
        self.pending_by_session.pop(session_identifier, None)
        return pending

    async def cancel_pending_action(self, *, action_identifier: UUID, session_identifier: str) -> None:
        pending = self.pending_by_session.get(session_identifier)
        if pending is None or pending.action_identifier != action_identifier:
            raise PendingActionNotFoundError("Не найдено", action_identifier=action_identifier)
        self.pending_by_session.pop(session_identifier, None)

    async def save_trace(self, *, trace: AgentTraceRecord) -> None:
        self.traces.append(trace)

    async def get_trace(self, *, trace_identifier: UUID) -> AgentTraceRecord | None:
        return next((item for item in self.traces if item.trace_identifier == trace_identifier), None)

    async def list_recent_traces(self, *, limit: int = 50) -> tuple[AgentTraceRecord, ...]:
        return tuple(self.traces[-limit:])

    async def record_model_usage(
        self,
        *,
        subscriber_identifier: str,
        target_date: date,
        cost_rubles: float,
        tokens_input: int,
        tokens_output: int,
    ) -> None:
        self.usage_records.append((subscriber_identifier, target_date, cost_rubles, tokens_input, tokens_output))
        self.user_spent_rubles += cost_rubles
        self.global_spent_rubles += cost_rubles

    async def get_usage_budget_status(
        self,
        *,
        subscriber_identifier: str,
        target_date: date,
        user_daily_limit_rubles: float,
        global_daily_limit_rubles: float,
    ) -> UsageBudgetStatus:
        del subscriber_identifier, target_date
        exceeded = (
            self.user_spent_rubles >= user_daily_limit_rubles or self.global_spent_rubles >= global_daily_limit_rubles
        )
        return UsageBudgetStatus(
            user_daily_spent_rubles=self.user_spent_rubles,
            user_daily_limit_rubles=user_daily_limit_rubles,
            global_daily_spent_rubles=self.global_spent_rubles,
            global_daily_limit_rubles=global_daily_limit_rubles,
            is_budget_exceeded=exceeded,
        )


class _InMemoryAdministrationRepositoryStub:
    """Минимальный стаб `AdministrationRepositoryPort` для проверки подписок и обратной связи."""

    def __init__(self) -> None:
        self.subscriptions: list[SubscriptionRecord] = []
        self.feedback_requests: list[FeedbackRequestRecord] = []

    async def create_subscription(self, *, subscription: SubscriptionRecord) -> SubscriptionRecord:
        self.subscriptions.append(subscription)
        return subscription

    async def save_feedback_request(self, *, feedback: FeedbackRequestRecord) -> FeedbackRequestRecord:
        self.feedback_requests.append(feedback)
        return feedback

    async def list_subscriptions_by_subscriber(
        self, *, channel: DialogChannel, subscriber_identifier: str
    ) -> tuple[SubscriptionRecord, ...]:
        return tuple(
            s for s in self.subscriptions if s.channel == channel and s.subscriber_identifier == subscriber_identifier
        )


class _OrchestratorStub:
    """Стаб `AgentOrchestratorPort`, фиксирующий историю и имитирующий ответы/ошибки."""

    def __init__(
        self,
        *,
        turn_result: AgentTurnResult | None = None,
        error: Exception | None = None,
        prepare_subscribe_city: str | None = None,
        prepare_feedback_text: str | None = None,
    ) -> None:
        self.calls: list[DialogSessionState] = []
        self._turn_result = turn_result
        self._error = error
        self._prepare_subscribe_city = prepare_subscribe_city
        self._prepare_feedback_text = prepare_feedback_text

    async def run_turn(
        self,
        *,
        session_state: DialogSessionState,
        user_message: str,
        current_datetime: datetime,
        tool_callbacks: AgentToolCallbacks,
        maximum_steps: int = 5,
    ) -> AgentTurnResult:
        del user_message, current_datetime, maximum_steps
        self.calls.append(session_state)
        if self._error is not None:
            raise self._error
        if self._prepare_subscribe_city is not None:
            pending = await tool_callbacks.prepare_subscribe(self._prepare_subscribe_city, EventCategory.CONCERT)
            return AgentTurnResult(
                answer_text=pending.confirmation_prompt,
                pending_action=pending,
                tokens_input=100,
                tokens_output=50,
                cost_rubles=0.05,
                model_name="google/gemini-4-hydrogen",
            )
        if self._prepare_feedback_text is not None:
            pending = await tool_callbacks.prepare_feedback(None, self._prepare_feedback_text, "@user")
            return AgentTurnResult(
                answer_text=pending.confirmation_prompt,
                pending_action=pending,
                tokens_input=100,
                tokens_output=50,
                cost_rubles=0.05,
                model_name="google/gemini-4-hydrogen",
            )
        assert self._turn_result is not None
        return self._turn_result


def _make_event(*, title: str = "Бесплатный концерт в Уфе", is_free: bool = True) -> Event:
    start = datetime(2026, 11, 21, 19, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)
    return Event(
        identifier=uuid4(),
        title=title,
        normalized_title=title.lower(),
        description="Культурная программа",
        category=EventCategory.CONCERT,
        tags=("музыка",),
        date_start=start,
        date_end=start + timedelta(hours=2),
        duration_minutes=120,
        city="Уфа",
        venue="Концертный зал",
        address="ул. Ленина, 10",
        price_minimum=0 if is_free else 500,
        price_maximum=0 if is_free else 1000,
        is_free=is_free,
        age_limit=6,
        language="русский",
        source_type=SourceType.MANUAL,
        source_url="https://example.org/event",
        source_hash=f"hash-{uuid4().hex}",
        deduplication_key=f"dedup-{uuid4().hex}",
        status=EventStatus.PUBLISHED,
        created_at=start - timedelta(days=1),
        updated_at=start - timedelta(days=1),
    )


def _build_dialog_service(
    *,
    events: Sequence[Event],
    dialog_repo: _InMemoryDialogRepositoryStub,
    admin_repo: _InMemoryAdministrationRepositoryStub,
    orchestrator: _OrchestratorStub | None,
    settings: LanguageModelSettings | None,
    dialog_history_limit: int = 10,
    pending_action_ttl_seconds: int = 600,
) -> AgentDialogService:
    event_repo = _DialogEventRepositoryStub(events)
    search_service = EventSearchService(event_repository=event_repo)  # type: ignore[arg-type]
    subscription_service = SubscriptionService(
        event_repository=event_repo,  # type: ignore[arg-type]
        administration_repository=admin_repo,  # type: ignore[arg-type]
    )
    weekend_planner = WeekendPlannerService(event_repository=event_repo)  # type: ignore[arg-type]
    fallback_search = FallbackSearchService(event_repository=event_repo, weekend_planner=weekend_planner)  # type: ignore[arg-type]
    return AgentDialogService(
        event_repository=event_repo,  # type: ignore[arg-type]
        dialog_repository=dialog_repo,  # type: ignore[arg-type]
        administration_repository=admin_repo,  # type: ignore[arg-type]
        search_service=search_service,
        subscription_service=subscription_service,
        weekend_planner=weekend_planner,
        fallback_search=fallback_search,
        agent_orchestrator=orchestrator,
        language_model_settings=settings,
        dialog_history_limit=dialog_history_limit,
        pending_action_ttl_seconds=pending_action_ttl_seconds,
    )


async def test_dialog_service_preserves_at_least_five_turns_and_post_validates_uuids() -> None:
    """Проверяет сохранение памяти >= 5 реплик, пост-валидацию UUID и запись `AgentTraceRecord`."""
    grounded_event = _make_event(title="Джаз в Уфе")
    ungrounded_event = _make_event(title="Галлюцинированный концерт")
    dialog_repo = _InMemoryDialogRepositoryStub()
    admin_repo = _InMemoryAdministrationRepositoryStub()

    # Предварительно кладём 6 сообщений в сессию, а limit ставим 2 -> должен подняться до >= 5
    now = datetime(2026, 11, 20, 12, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)
    dialog_repo.messages_by_session["sess-mem"] = [
        DialogMessage(role="user" if idx % 2 == 0 else "assistant", content=f"Реплика {idx}", created_at=now)
        for idx in range(6)
    ]

    orchestrator = _OrchestratorStub(
        turn_result=AgentTurnResult(
            answer_text="Вот проверенный концерт.",
            recommended_events=(grounded_event, ungrounded_event),
            explanations_by_event={
                grounded_event.identifier: "Подтверждено инструментом.",
                ungrounded_event.identifier: "Не должно пройти пост-валидацию.",
            },
            tool_invocations=(
                AgentToolInvocation(
                    tool_name="search_events",
                    arguments={"city": "Уфа"},
                    returned_event_identifiers=(grounded_event.identifier,),
                    succeeded=True,
                ),
            ),
            tokens_input=300,
            tokens_output=120,
            cost_rubles=0.12,
            model_name="google/gemini-4-hydrogen",
        )
    )
    settings = LanguageModelSettings(_env_file=None, API_KEY="test-key", PROMPT_VERSION="v1.0")
    service = _build_dialog_service(
        events=[grounded_event],
        dialog_repo=dialog_repo,
        admin_repo=admin_repo,
        orchestrator=orchestrator,
        settings=settings,
        dialog_history_limit=2,
    )

    result = await service.handle_message(
        session_identifier="sess-mem",
        channel=DialogChannel.WEB,
        subscriber_identifier="user-mem",
        user_message="Подбери джаз",
        current_datetime=now,
    )

    assert len(orchestrator.calls[0].recent_messages) >= 5
    assert [e.identifier for e in result.recommended_events] == [grounded_event.identifier]
    assert result.explanations_by_event == {grounded_event.identifier: "Подтверждено инструментом."}
    assert result.trace_identifier is not None
    assert len(dialog_repo.traces) == 1
    assert dialog_repo.traces[0].trace_identifier == result.trace_identifier
    assert dialog_repo.traces[0].fallback_used is False


async def test_dialog_service_fallbacks_on_budget_missing_key_and_timeout() -> None:
    """Проверяет переход на `FallbackSearchService` при превышении бюджета, без ключа и при таймауте."""
    sample_event = _make_event(title="Бесплатный концерт в Уфе", is_free=True)
    now = datetime(2026, 11, 20, 12, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)
    admin_repo = _InMemoryAdministrationRepositoryStub()

    # 1. Превышен суточный бюджет llm_usage_daily
    budget_repo = _InMemoryDialogRepositoryStub(user_spent_rubles=20.0, global_spent_rubles=20.0)
    settings_with_key = LanguageModelSettings(_env_file=None, API_KEY="test-key", USER_DAILY_BUDGET_RUBLES=15.0)
    orchestrator_unused = _OrchestratorStub(error=RuntimeError("Не должен вызываться"))
    budget_service = _build_dialog_service(
        events=[sample_event],
        dialog_repo=budget_repo,
        admin_repo=admin_repo,
        orchestrator=orchestrator_unused,
        settings=settings_with_key,
    )
    budget_result = await budget_service.handle_message(
        session_identifier="sess-budget",
        channel=DialogChannel.TELEGRAM,
        subscriber_identifier="user-budget",
        user_message="Бесплатные концерты в Уфе",
        current_datetime=now,
    )
    assert budget_result.fallback_used is True
    assert budget_result.fallback_reason == "budget_exceeded"
    assert budget_result.model_name == "deterministic-fallback"
    assert budget_result.trace_identifier is not None
    assert len(orchestrator_unused.calls) == 0

    # 2. Отсутствует API-ключ
    no_key_repo = _InMemoryDialogRepositoryStub()
    no_key_service = _build_dialog_service(
        events=[sample_event],
        dialog_repo=no_key_repo,
        admin_repo=admin_repo,
        orchestrator=None,
        settings=LanguageModelSettings(_env_file=None, API_KEY=""),
    )
    no_key_result = await no_key_service.handle_message(
        session_identifier="sess-nokey",
        channel=DialogChannel.WEB,
        subscriber_identifier="user-nokey",
        user_message="Концерты в Уфе",
        current_datetime=now,
    )
    assert no_key_result.fallback_used is True
    assert no_key_result.fallback_reason == "model_not_configured"
    assert no_key_result.trace_identifier is not None

    # 3. Таймаут оркестратора
    timeout_repo = _InMemoryDialogRepositoryStub()
    timeout_orchestrator = _OrchestratorStub(error=NetworkConnectionTimeoutError("Таймаут модели"))
    timeout_service = _build_dialog_service(
        events=[sample_event],
        dialog_repo=timeout_repo,
        admin_repo=admin_repo,
        orchestrator=timeout_orchestrator,
        settings=settings_with_key,
    )
    timeout_result = await timeout_service.handle_message(
        session_identifier="sess-timeout",
        channel=DialogChannel.WEB,
        subscriber_identifier="user-timeout",
        user_message="Концерты в Уфе",
        current_datetime=now,
    )
    assert timeout_result.fallback_used is True
    assert timeout_result.fallback_reason == "orchestrator_error:NetworkConnectionTimeoutError"
    assert len(timeout_result.recommended_events) == 1
    assert timeout_result.trace_identifier is not None


async def test_dialog_service_two_step_confirmation_confirm_cancel_and_expired_ttl() -> None:
    """Проверяет двухшаговое подтверждение `subscribe` и `save_feedback` (да/отмена/истёкший TTL)."""
    now = datetime(2026, 11, 20, 12, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)
    settings = LanguageModelSettings(_env_file=None, API_KEY="test-key")
    dialog_repo = _InMemoryDialogRepositoryStub()
    admin_repo = _InMemoryAdministrationRepositoryStub()

    # 1. Подписка: prepare_subscribe -> "Да"
    sub_service = _build_dialog_service(
        events=[],
        dialog_repo=dialog_repo,
        admin_repo=admin_repo,
        orchestrator=_OrchestratorStub(prepare_subscribe_city="Уфа"),
        settings=settings,
    )
    step1 = await sub_service.handle_message(
        session_identifier="sess-sub",
        channel=DialogChannel.TELEGRAM,
        subscriber_identifier="tg-100",
        user_message="Подпиши меня на концерты в Уфе",
        current_datetime=now,
    )
    assert step1.pending_action is not None
    assert len(admin_repo.subscriptions) == 0

    step2 = await sub_service.handle_message(
        session_identifier="sess-sub",
        channel=DialogChannel.TELEGRAM,
        subscriber_identifier="tg-100",
        user_message="Да",
        current_datetime=now + timedelta(seconds=10),
    )
    assert "Подписка оформлена" in step2.answer_text
    assert len(admin_repo.subscriptions) == 1
    assert admin_repo.subscriptions[0].city == "Уфа"

    # 2. Обратная связь: prepare_feedback -> "Отмена"
    fb_service = _build_dialog_service(
        events=[],
        dialog_repo=dialog_repo,
        admin_repo=admin_repo,
        orchestrator=_OrchestratorStub(prepare_feedback_text="Добавить больше выставок"),
        settings=settings,
    )
    fb1 = await fb_service.handle_message(
        session_identifier="sess-fb",
        channel=DialogChannel.WEB,
        subscriber_identifier="web-200",
        user_message="Передай организаторам пожелание",
        current_datetime=now,
    )
    assert fb1.pending_action is not None

    fb2 = await fb_service.handle_message(
        session_identifier="sess-fb",
        channel=DialogChannel.WEB,
        subscriber_identifier="web-200",
        user_message="Отмена",
        current_datetime=now + timedelta(seconds=10),
    )
    assert "отменено" in fb2.answer_text.lower()
    assert len(admin_repo.feedback_requests) == 0

    # 3. Истёкший TTL
    exp_service = _build_dialog_service(
        events=[],
        dialog_repo=dialog_repo,
        admin_repo=admin_repo,
        orchestrator=_OrchestratorStub(prepare_subscribe_city="Уфа"),
        settings=settings,
        pending_action_ttl_seconds=60,
    )
    await exp_service.handle_message(
        session_identifier="sess-ttl",
        channel=DialogChannel.WEB,
        subscriber_identifier="web-300",
        user_message="Подпиши меня",
        current_datetime=now,
    )
    expired_reply = await exp_service.handle_message(
        session_identifier="sess-ttl",
        channel=DialogChannel.WEB,
        subscriber_identifier="web-300",
        user_message="Да",
        current_datetime=now + timedelta(seconds=120),
    )
    assert "истёк" in expired_reply.answer_text.lower()
    assert len(admin_repo.subscriptions) == 1
