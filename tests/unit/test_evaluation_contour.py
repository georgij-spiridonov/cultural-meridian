"""Модульные тесты контура оценки качества (YAML-наборы, метрики, отчёты, LLM-judge)."""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from src.core.configuration import LanguageModelSettings
from src.core.types import EventCategory, EventStatus, SourceType
from src.domain.entities import AgentToolInvocation, AgentTraceRecord, Event
from src.domain.services import BASHKORTOSTAN_TIMEZONE
from src.entrypoints.command_line.evaluate import (
    DEFAULT_GOLDEN_DATASET_PATH,
    DEFAULT_HOLDOUT_DATASET_PATH,
    CaseEvaluationOutcome,
    EvaluationCaseSchema,
    EvaluationDatasetSchema,
    EvaluationReportSchema,
    JudgeEvaluationSchema,
    compute_metrics_summary,
    compute_worst_of_two_summary,
    evaluate_case_outcome,
    evaluate_with_llm_judge,
    load_evaluation_dataset,
    save_evaluation_report,
)


def _make_event(*, identifier: UUID | None = None, status: EventStatus = EventStatus.PUBLISHED) -> Event:
    """Создаёт тестовую карточку события."""
    now = datetime(2026, 11, 14, 10, 0, tzinfo=BASHKORTOSTAN_TIMEZONE)
    return Event(
        identifier=identifier or uuid4(),
        title="Тестовый концерт",
        normalized_title="тестовый концерт",
        category=EventCategory.CONCERT,
        date_start=datetime(2026, 11, 14, 19, 0, tzinfo=BASHKORTOSTAN_TIMEZONE),
        city="Уфа",
        venue="ГКЗ «Башкортостан»",
        address="ул. Ленина, 50",
        source_url="https://example.org/event",
        source_type=SourceType.MANUAL,
        source_hash="0" * 64,
        deduplication_key="test-dedup-key",
        status=status,
        created_at=now,
        updated_at=now,
    )


def test_golden_and_holdout_datasets_satisfy_schema_and_coverage() -> None:
    """Проверяет целостность, объём и покрытие категорий в YAML-наборах оценки."""
    golden = load_evaluation_dataset(DEFAULT_GOLDEN_DATASET_PATH)
    holdout = load_evaluation_dataset(DEFAULT_HOLDOUT_DATASET_PATH)

    assert len(golden.cases) >= 65
    assert len(holdout.cases) == 10

    golden_ids = {case.case_id for case in golden.cases}
    holdout_ids = {case.case_id for case in holdout.cases}
    assert len(golden_ids) == len(golden.cases)
    assert len(holdout_ids) == len(holdout.cases)
    assert golden_ids.isdisjoint(holdout_ids)

    injection_cases = [case for case in golden.cases if case.category == "injection"]
    assert len(injection_cases) >= 10
    assert all(case.expect_injection_blocked for case in injection_cases)

    session_counts = Counter(case.session_id for case in golden.cases if case.session_id is not None)
    assert any(count >= 5 for count in session_counts.values())

    golden_queries = {case.query.strip().lower() for case in golden.cases}
    holdout_queries = {case.query.strip().lower() for case in holdout.cases}
    assert golden_queries.isdisjoint(holdout_queries)


def test_dataset_schema_rejects_duplicate_case_ids() -> None:
    """Проверяет запрет дублирующихся `case_id` в схеме набора."""
    case = EvaluationCaseSchema(case_id="dup-1", category="simple_search", query="Концерты в Уфе")
    with pytest.raises(ValueError, match="дублирующийся case_id"):
        EvaluationDatasetSchema(
            dataset_name="broken",
            reference_datetime=datetime(2026, 11, 14, 10, 0, tzinfo=BASHKORTOSTAN_TIMEZONE),
            cases=(case, case),
        )


def test_deterministic_evaluator_catches_hallucination_and_canceled_leak() -> None:
    """Проверяет поимку выдуманного UUID и утёкшего отменённого события."""
    valid_event = _make_event(status=EventStatus.PUBLISHED)
    canceled_event = _make_event(status=EventStatus.CANCELED)
    hallucinated_id = uuid4()
    fake_event = _make_event(identifier=hallucinated_id, status=EventStatus.PUBLISHED)
    catalog = {valid_event.identifier: valid_event, canceled_event.identifier: canceled_event}

    case = EvaluationCaseSchema(
        case_id="eval-check-1",
        category="simple_search",
        query="Концерты в Уфе",
        expected_tools=("search_events",),
        expected_argument_keys={"search_events": ("city",)},
    )
    invocation = AgentToolInvocation(
        tool_name="search_events",
        arguments={"city": "Уфа"},
        returned_event_identifiers=(valid_event.identifier, canceled_event.identifier),
    )
    trace = AgentTraceRecord(
        trace_identifier=uuid4(),
        request_identifier="req-1",
        session_identifier="sess-1",
        channel="evaluation",  # type: ignore[arg-type]
        user_query=case.query,
        plan_summary="search",
        tool_invocations=(invocation,),
        grounded_event_identifiers=(valid_event.identifier,),
        created_at=datetime(2026, 11, 14, 10, 0, tzinfo=BASHKORTOSTAN_TIMEZONE),
    )

    outcome = evaluate_case_outcome(
        case=case,
        contour="agent",
        recommended_events=(fake_event, canceled_event),
        answer_text="Вот подборка",
        clarifying_question=None,
        pending_action_type=None,
        tool_invocations=(invocation,),
        trace_record=trace,
        catalog_events_by_id=catalog,
        latency_ms=42,
        cost_rubles=0.05,
    )

    assert not outcome.passed
    assert outcome.hallucinated_event_ids == (hallucinated_id,)
    assert outcome.canceled_leaked_ids == (canceled_event.identifier,)
    assert outcome.tool_matched
    assert outcome.argument_matched


def test_deterministic_evaluator_checks_tool_arguments() -> None:
    """Проверяет обнаружение отсутствующих обязательных аргументов инструмента."""
    valid_event = _make_event()
    case = EvaluationCaseSchema(
        case_id="eval-args-1",
        category="combined_search",
        query="Концерты в Уфе",
        expected_tools=("search_events",),
        expected_argument_keys={"search_events": ("city", "category")},
    )
    invocation = AgentToolInvocation(
        tool_name="search_events", arguments={"city": "Уфа"}, returned_event_identifiers=(valid_event.identifier,)
    )

    outcome = evaluate_case_outcome(
        case=case,
        contour="agent",
        recommended_events=(valid_event,),
        answer_text="Нашёл",
        clarifying_question=None,
        pending_action_type=None,
        tool_invocations=(invocation,),
        trace_record=None,
        catalog_events_by_id={valid_event.identifier: valid_event},
        latency_ms=25,
    )

    assert not outcome.passed
    assert outcome.tool_matched
    assert not outcome.argument_matched


def test_metrics_summary_p50_p95_cost_and_report_files(tmp_path: Path) -> None:
    """Проверяет расчёт p50/p95, стоимости, сравнение agent vs baseline и сохранение отчётов."""
    latencies = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    agent_outcomes = [
        CaseEvaluationOutcome(
            case_id=f"c-{idx}", category="simple_search", contour="agent", passed=True, latency_ms=lat, cost_rubles=0.1
        )
        for idx, lat in enumerate(latencies)
    ]
    baseline_outcomes = [
        CaseEvaluationOutcome(
            case_id=f"c-{idx}",
            category="simple_search",
            contour="baseline",
            passed=(idx < 2),
            tool_matched=False,
            catalog_precision=0.2,
            recall_at_k=0.2,
            latency_ms=5,
        )
        for idx in range(10)
    ]

    agent_summary = compute_metrics_summary(agent_outcomes, contour="agent")
    baseline_summary = compute_metrics_summary(baseline_outcomes, contour="baseline")

    assert agent_summary.pass_rate == 1.0
    assert baseline_summary.pass_rate == 0.2
    assert agent_summary.latency_p50_ms == 50.0
    assert agent_summary.latency_p95_ms == 100.0
    assert agent_summary.total_cost_rubles == pytest.approx(1.0)
    assert agent_summary.average_cost_per_turn_rubles == pytest.approx(0.1)

    report = EvaluationReportSchema(
        run_timestamp_utc="2026-11-14T15:00:00+00:00",
        run_timestamp_yekaterinburg="2026-11-14T20:00:00+05:00",
        mode="offline",
        dataset="all",
        prompt_version="v1.0",
        primary_model="deterministic-offline-agent",
        fast_model="google/gemini-3.5-flash-lite",
        agent_summary=agent_summary,
        baseline_summary=baseline_summary,
        outcomes=tuple(agent_outcomes + baseline_outcomes),
    )

    json_path, md_path = save_evaluation_report(report, output_dir=tmp_path, filename_prefix="test_report")
    assert json_path.exists()
    assert md_path.exists()

    parsed_json = json.loads(json_path.read_text(encoding="utf-8"))
    assert parsed_json["agent_summary"]["pass_rate"] == 1.0
    assert parsed_json["baseline_summary"]["pass_rate"] == 0.2
    md_text = md_path.read_text(encoding="utf-8")
    assert "Agent (`AgentDialogService`)" in md_text
    assert "Baseline (`EventSearchService`)" in md_text


@pytest.mark.asyncio
async def test_llm_judge_mocked_and_resilient_to_failure() -> None:
    """Проверяет работу мокнутого LLM-judge и устойчивость при ошибке или отсутствии ключа."""
    case = EvaluationCaseSchema(case_id="judge-1", category="simple_search", query="Концерты в Уфе")

    no_key_settings = LanguageModelSettings(API_KEY="")
    assert (
        await evaluate_with_llm_judge(
            case=case, answer_text="Ответ", clarifying_question=None, recommended_events=(), settings=no_key_settings
        )
        is None
    )

    active_settings = LanguageModelSettings(API_KEY="sk-test-fake-key", FAST="google/gemini-3.5-flash-lite")
    expected_judge = JudgeEvaluationSchema(
        explanation_quality_score=5,
        clarification_quality_score=4,
        grounding_and_honesty_score=5,
        overall_verdict="pass",
        rationale="Ответ точный и лаконичный.",
    )

    fake_client = AsyncMock()
    with (
        patch("src.entrypoints.command_line.evaluate.create_openai_client", return_value=fake_client),
        patch(
            "src.entrypoints.command_line.evaluate.Runner.run",
            new=AsyncMock(return_value=SimpleNamespace(final_output=expected_judge)),
        ),
    ):
        result = await evaluate_with_llm_judge(
            case=case,
            answer_text="Нашёл концерты.",
            clarifying_question=None,
            recommended_events=(_make_event(),),
            settings=active_settings,
        )
    assert result == expected_judge
    fake_client.close.assert_awaited_once()

    failing_client = AsyncMock()
    with (
        patch("src.entrypoints.command_line.evaluate.create_openai_client", return_value=failing_client),
        patch("src.entrypoints.command_line.evaluate.Runner.run", new=AsyncMock(side_effect=RuntimeError("API down"))),
    ):
        failed_result = await evaluate_with_llm_judge(
            case=case,
            answer_text="Нашёл концерты.",
            clarifying_question=None,
            recommended_events=(),
            settings=active_settings,
        )
    assert failed_result is None
    failing_client.close.assert_awaited_once()


def test_worst_of_two_summary_and_markdown_table(tmp_path: Path) -> None:
    """Проверяет расчёт сводки «худшее из двух» и её отображение в Markdown-отчёте."""
    golden_outcomes = [
        CaseEvaluationOutcome(
            case_id="g-1",
            category="simple_search",
            contour="agent",
            passed=True,
            catalog_precision=0.88,
            recall_at_k=0.95,
            latency_ms=40,
        )
    ]
    holdout_outcomes = [
        CaseEvaluationOutcome(
            case_id="h-1",
            category="simple_search",
            contour="agent",
            passed=True,
            catalog_precision=0.92,
            recall_at_k=1.0,
            latency_ms=60,
        )
    ]
    golden_summary = compute_metrics_summary(golden_outcomes, contour="agent")
    holdout_summary = compute_metrics_summary(holdout_outcomes, contour="agent")
    worst = compute_worst_of_two_summary(golden_summary, holdout_summary)

    assert worst.catalog_accuracy == pytest.approx(0.88)
    assert worst.recall_at_k == pytest.approx(0.95)
    assert worst.latency_p95_ms == pytest.approx(60.0)

    report = EvaluationReportSchema(
        run_timestamp_utc="2026-11-14T15:00:00+00:00",
        run_timestamp_yekaterinburg="2026-11-14T20:00:00+05:00",
        mode="offline",
        dataset="all",
        prompt_version="v1.0",
        primary_model="deterministic-offline-agent",
        fast_model="google/gemini-3.5-flash-lite",
        agent_summary=compute_metrics_summary(golden_outcomes + holdout_outcomes, contour="agent"),
        baseline_summary=compute_metrics_summary([], contour="baseline"),
        golden_agent_summary=golden_summary,
        holdout_agent_summary=holdout_summary,
        worst_of_two_agent_summary=worst,
        outcomes=tuple(golden_outcomes + holdout_outcomes),
    )
    _, md_path = save_evaluation_report(report, output_dir=tmp_path, filename_prefix="worst_test")
    md_text = md_path.read_text(encoding="utf-8")
    assert "Раздельные метрики и «худшее из двух»" in md_text
    assert "| Golden | External Holdout | Worst-of-two | Baseline (all) |" in md_text
