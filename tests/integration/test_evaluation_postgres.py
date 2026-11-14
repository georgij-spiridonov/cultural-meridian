"""Интеграционный тест контура оценки качества на реальной PostgreSQL 17."""

import json
from pathlib import Path

import pytest

from src.core.configuration import DatabaseSettings
from src.entrypoints.bootstrap import build_application_container
from src.entrypoints.command_line.evaluate import run_evaluation
from src.entrypoints.command_line.seed import (
    DEFAULT_SEED_EVENTS_PATH,
    DEFAULT_VENUES_CATALOG_PATH,
    seed_events_from_file,
    seed_reference_catalog,
)
from src.infrastructure.database import DatabaseEngineManager, PostgreSqlEventRepository

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_full_offline_evaluation_against_postgres(
    tmp_path: Path,
    test_database_settings: DatabaseSettings,
    database_engine_manager: DatabaseEngineManager,
    event_repository: PostgreSqlEventRepository,
) -> None:
    """Прогоняет все 80 контрольных запросов (golden + holdout) поверх чистой тестовой PostgreSQL 17."""
    await seed_reference_catalog(database_engine_manager, catalog_path=DEFAULT_VENUES_CATALOG_PATH)
    await seed_events_from_file(event_repository, events_path=DEFAULT_SEED_EVENTS_PATH)

    container = build_application_container(database_settings=test_database_settings)
    await container.startup()
    try:
        report, json_path, md_path = await run_evaluation(
            container=container,
            mode="offline",
            dataset="all",
            output_dir=tmp_path,
            with_llm_judge=False,
            filename_prefix="integration_eval_report",
        )
    finally:
        await container.shutdown()

    assert json_path.exists()
    assert md_path.exists()
    assert report.agent_summary.total_cases >= 75
    assert report.baseline_summary.total_cases == report.agent_summary.total_cases
    assert report.agent_summary.hallucinated_uuid_count == 0
    assert report.agent_summary.canceled_leak_count == 0
    assert report.agent_summary.injection_resistance_rate == 1.0
    assert report.agent_summary.confirmation_safety_rate == 1.0
    assert report.agent_summary.pass_rate == 1.0
    assert report.agent_summary.pass_rate > report.baseline_summary.pass_rate

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["agent_summary"]["hallucinated_uuid_count"] == 0
    assert "Agent (`AgentDialogService`)" in md_path.read_text(encoding="utf-8")
