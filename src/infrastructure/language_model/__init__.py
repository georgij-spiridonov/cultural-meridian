"""Адаптер языковой модели поверх OpenAI Agents SDK и OpenRouter."""

from src.infrastructure.language_model.agent_orchestrator import OpenAiAgentOrchestrator
from src.infrastructure.language_model.client import (
    create_chat_model,
    create_model_settings,
    create_openai_client,
    create_run_config,
    map_language_model_exception,
    supports_strict_json_schema,
)
from src.infrastructure.language_model.cost_calculator import (
    ModelUsageCost,
    calculate_cost_rubles,
    calculate_run_usage_and_cost,
    extract_raw_usage_cost_usd,
    extract_token_usage,
)
from src.infrastructure.language_model.guardrails import (
    HasAllowedEventIds,
    extract_uuids_from_text,
    input_injection_guardrail,
    output_grounding_guardrail,
    sanitize_control_characters,
    wrap_untrusted_event_data,
)
from src.infrastructure.language_model.structured_extractor import OpenAiStructuredExtractor
from src.infrastructure.language_model.tools import (
    AgentRunContext,
    build_agent_tools,
    find_similar,
    get_event,
    get_venue_schedule,
    plan_weekend,
    prepare_feedback,
    prepare_subscribe,
    search_events,
)

__all__ = [
    "AgentRunContext",
    "HasAllowedEventIds",
    "ModelUsageCost",
    "OpenAiAgentOrchestrator",
    "OpenAiStructuredExtractor",
    "build_agent_tools",
    "calculate_cost_rubles",
    "calculate_run_usage_and_cost",
    "create_chat_model",
    "create_model_settings",
    "create_openai_client",
    "create_run_config",
    "extract_raw_usage_cost_usd",
    "extract_token_usage",
    "extract_uuids_from_text",
    "find_similar",
    "get_event",
    "get_venue_schedule",
    "input_injection_guardrail",
    "map_language_model_exception",
    "output_grounding_guardrail",
    "plan_weekend",
    "prepare_feedback",
    "prepare_subscribe",
    "sanitize_control_characters",
    "search_events",
    "supports_strict_json_schema",
    "wrap_untrusted_event_data",
]
