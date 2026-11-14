"""Интеграционные тесты `AgentDialogService` поверх реального PostgreSQL 17."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from src.core.configuration import DatabaseSettings, LanguageModelSettings
from src.core.types import DialogChannel, EventCategory, EventStatus, SourceType
from src.domain.entities import AgentToolInvocation, AgentTurnResult, DialogSessionState, Event
from src.domain.ports import AgentToolCallbacks
from src.domain.services import BASHKORTOSTAN_TIMEZONE
from src.entrypoints.bootstrap import build_application_container
from src.infrastructure.database import DatabaseEngineManager

pytestmark = pytest.mark.integration


class _MockAgentOrchestrator:
    """Детерминированный мок `AgentOrchestratorPort` для интеграционных тестов с PostgreSQL."""

    def __init__(self, *, event: Event, trigger_subscribe: bool = False) -> None:
        self._event = event
        self._trigger_subscribe = trigger_subscribe
        self.calls_count = 0

    async def run_turn(
        self,
        *,
        session_state: DialogSessionState,
        user_message: str,
        current_datetime: datetime,
        tool_callbacks: AgentToolCallbacks,
        maximum_steps: int = 5,
    ) -> AgentTurnResult:
        del session_state, user_message, current_datetime, maximum_steps
        self.calls_count += 1
        if self._trigger_subscribe:
            pending = await tool_callbacks.prepare_subscribe("Уфа", EventCategory.CONCERT)
            return AgentTurnResult(
                answer_text=pending.confirmation_prompt,
                pending_action=pending,
                tokens_input=400,
                tokens_output=150,
                cost_rubles=1.25,
                model_name="google/gemini-4-hydrogen",
                prompt_version="v1.0",
            )

        found_event = await tool_callbacks.get_event(self._event.identifier)
        return AgentTurnResult(
            answer_text=f"Рекомендую посетить «{found_event.title}» в городе {found_event.city}.",
            recommended_events=(found_event,),
            explanations_by_event={found_event.identifier: "Подтверждено в каталоге PostgreSQL."},
            tool_invocations=(
                AgentToolInvocation(
                    tool_name="get_event",
                    arguments={"event_id": str(found_event.identifier)},
                    returned_event_identifiers=(found_event.identifier,),
                    succeeded=True,
                ),
            ),
            tokens_input=1200,
            tokens_output=300,
            cost_rubles=2.5,
            model_name="google/gemini-4-hydrogen",
            prompt_version="v1.0",
        )


async def test_agent_dialog_postgres_persists_sessions_messages_pending_traces_and_usage(
    test_database_settings: DatabaseSettings, database_engine_manager: DatabaseEngineManager
) -> None:
    """Проверяет запись в таблицы сессий, сообщений, pending_actions, agent_traces и llm_usage_daily."""
    del database_engine_manager  # Гарантирует чистую БД перед и после теста
    lm_settings = LanguageModelSettings(
        _env_file=None,
        API_KEY="mock-key-no-network",
        USER_DAILY_BUDGET_RUBLES=3.0,
        GLOBAL_DAILY_BUDGET_RUBLES=100.0,
        PROMPT_VERSION="v1.0",
    )
    container = build_application_container(
        database_settings=test_database_settings, language_model_settings=lm_settings
    )
    await container.startup()
    try:
        start_time = datetime(2026, 11, 22, 19, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)
        sample_event = Event(
            identifier=uuid4(),
            title="Симфонический вечер в Уфе",
            normalized_title="симфонический вечер в уфе",
            description="Классическая музыка",
            category=EventCategory.CONCERT,
            tags=("классика",),
            date_start=start_time,
            date_end=start_time + timedelta(hours=2),
            duration_minutes=120,
            city="Уфа",
            venue="Башгосфилармония",
            address="ул. Гоголя, 58",
            price_minimum=600,
            price_maximum=1200,
            is_free=False,
            age_limit=6,
            language="русский",
            source_type=SourceType.MANUAL,
            source_url="https://example.org/event",
            source_hash=f"pg-hash-{uuid4().hex}",
            deduplication_key=f"pg-dedup-{uuid4().hex}",
            status=EventStatus.PUBLISHED,
            created_at=start_time - timedelta(days=1),
            updated_at=start_time - timedelta(days=1),
        )
        await container.event_repository.save_event(event=sample_event)

        now = datetime(2026, 11, 20, 12, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)
        session_id = "pg-dialog-session-1"
        subscriber_id = "pg-subscriber-1"

        # 1. Ход с ИИ-оркестратором (мок) -> запись в dialog_sessions, dialog_messages, agent_traces, llm_usage_daily
        mock_search = _MockAgentOrchestrator(event=sample_event, trigger_subscribe=False)
        container.agent_dialog_service._agent_orchestrator = mock_search

        turn1 = await container.agent_dialog_service.handle_message(
            session_identifier=session_id,
            channel=DialogChannel.WEB,
            subscriber_identifier=subscriber_id,
            user_message="Покажи концерт в Уфе",
            current_datetime=now,
        )
        assert turn1.fallback_used is False
        assert turn1.trace_identifier is not None
        assert [e.identifier for e in turn1.recommended_events] == [sample_event.identifier]

        # 2. Ход с prepare_subscribe -> запись в pending_actions -> подтверждение «Да»
        mock_subscribe = _MockAgentOrchestrator(event=sample_event, trigger_subscribe=True)
        container.agent_dialog_service._agent_orchestrator = mock_subscribe

        turn2 = await container.agent_dialog_service.handle_message(
            session_identifier=session_id,
            channel=DialogChannel.WEB,
            subscriber_identifier=subscriber_id,
            user_message="Подпиши меня на концерты в Уфе",
            current_datetime=now + timedelta(seconds=10),
        )
        assert turn2.pending_action is not None

        async with container.database_engine_manager.session() as db_session:
            pending_count = (
                await db_session.execute(
                    text("SELECT COUNT(*) FROM pending_actions WHERE session_identifier = :sid AND status = 'pending'"),
                    {"sid": session_id},
                )
            ).scalar_one()
            assert pending_count == 1

        turn3 = await container.agent_dialog_service.handle_message(
            session_identifier=session_id,
            channel=DialogChannel.WEB,
            subscriber_identifier=subscriber_id,
            user_message="Да",
            current_datetime=now + timedelta(seconds=20),
        )
        assert "Подписка оформлена" in turn3.answer_text

        # Суммарный расход 2.5 + 1.25 = 3.75 > 3.0 -> следующий ход уйдёт в детерминированный fallback
        turn4 = await container.agent_dialog_service.handle_message(
            session_identifier=session_id,
            channel=DialogChannel.WEB,
            subscriber_identifier=subscriber_id,
            user_message="Найди концерт в Уфе",
            current_datetime=now + timedelta(seconds=30),
        )
        assert turn4.fallback_used is True
        assert turn4.fallback_reason == "budget_exceeded"
        assert mock_subscribe.calls_count == 1

        # Проверяем фактическое состояние всех 5 таблиц PostgreSQL
        async with container.database_engine_manager.session() as db_session:
            sessions_count = (
                await db_session.execute(
                    text("SELECT COUNT(*) FROM dialog_sessions WHERE session_identifier = :sid"), {"sid": session_id}
                )
            ).scalar_one()
            messages_count = (
                await db_session.execute(
                    text("SELECT COUNT(*) FROM dialog_messages WHERE session_identifier = :sid"), {"sid": session_id}
                )
            ).scalar_one()
            consumed_pending_count = (
                await db_session.execute(
                    text(
                        "SELECT COUNT(*) FROM pending_actions WHERE session_identifier = :sid AND status = 'consumed'"
                    ),
                    {"sid": session_id},
                )
            ).scalar_one()
            traces_count = (
                await db_session.execute(
                    text("SELECT COUNT(*) FROM agent_traces WHERE session_identifier = :sid"), {"sid": session_id}
                )
            ).scalar_one()
            usage_row = (
                await db_session.execute(
                    text(
                        "SELECT spent_rubles, tokens_input, tokens_output "
                        "FROM llm_usage_daily WHERE subscriber_identifier = :sub AND usage_date = :dt"
                    ),
                    {"sub": subscriber_id, "dt": now.date()},
                )
            ).one()

        assert sessions_count == 1
        assert messages_count == 8
        assert consumed_pending_count == 1
        assert traces_count == 4
        assert float(usage_row.spent_rubles) == pytest.approx(3.75)
        assert usage_row.tokens_input == 1600
        assert usage_row.tokens_output == 450
    finally:
        await container.shutdown()
