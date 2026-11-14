"""Входные и выходные защитные фильтры (guardrails) для OpenAI Agents SDK."""

import re
from collections.abc import Mapping
from typing import Final, Protocol
from uuid import UUID

from agents import (
    Agent,
    GuardrailFunctionOutput,
    RunContextWrapper,
    TResponseInputItem,
    input_guardrail,
    output_guardrail,
)
from pydantic import BaseModel

from src.domain.services.sanitization import (
    contains_system_prompt_leak,
    inspect_untrusted_text,
    sanitize_control_characters,
    wrap_untrusted_event_data,
)
from src.infrastructure.language_model.tools import AgentRunContext

__all__ = [
    "HasAllowedEventIds",
    "extract_uuids_from_text",
    "input_injection_guardrail",
    "output_grounding_guardrail",
    "sanitize_control_characters",
    "wrap_untrusted_event_data",
]

_UUID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


class HasAllowedEventIds(Protocol):
    """Протокол контекста, предоставляющего множество допустимых UUID событий."""

    @property
    def allowed_event_ids(self) -> frozenset[UUID]:
        """Возвращает множество идентификаторов событий, подтверждённых инструментами."""
        ...


def extract_uuids_from_text(text: str) -> tuple[UUID, ...]:
    """Извлекает все уникальные валидные UUID из произвольного текста.

    Arguments:
        text: Проверяемый текст ответа или сериализованной структуры.

    Returns:
        Кортеж уникальных UUID в порядке первого появления.
    """
    found: list[UUID] = []
    seen: set[UUID] = set()
    for match in _UUID_PATTERN.findall(text):
        try:
            parsed = UUID(match)
        except ValueError:
            continue
        if parsed not in seen:
            seen.add(parsed)
            found.append(parsed)
    return tuple(found)


def _extract_user_texts_from_input(input_data: str | list[TResponseInputItem]) -> tuple[str, ...]:
    """Извлекает пользовательские текстовые фрагменты из входных данных `Runner.run`."""
    if isinstance(input_data, str):
        return (input_data,)

    texts: list[str] = []
    for item in input_data:
        if not isinstance(item, Mapping):
            continue
        role = item.get("role")
        if role not in (None, "user"):
            continue
        content = item.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, Mapping):
                    part_text = part.get("text")
                    if isinstance(part_text, str):
                        texts.append(part_text)
    return tuple(texts)


def _serialize_agent_output_for_check(agent_output: object) -> str:
    """Преобразует выход агента (строку или Pydantic-модель) в строку для проверки."""
    if isinstance(agent_output, BaseModel):
        return agent_output.model_dump_json()
    return str(agent_output)


@input_guardrail(run_in_parallel=False)
def input_injection_guardrail(
    ctx: RunContextWrapper[AgentRunContext], agent: Agent[AgentRunContext], input_data: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    """Блокирует попытки prompt injection до запуска языковой модели и инструментов.

    Arguments:
        ctx: Контекст выполнения агента.
        agent: Целевой экземпляр агента.
        input_data: Входная строка или список сообщений диалога.

    Returns:
        Результат проверки с `tripwire_triggered=True` при обнаружении инъекции.
    """
    del ctx, agent
    for user_text in _extract_user_texts_from_input(input_data):
        inspection = inspect_untrusted_text(user_text)
        if inspection.has_suspicious_instructions:
            return GuardrailFunctionOutput(
                output_info={
                    "violation_type": "prompt_injection",
                    "source_context": "user_message",
                    "matched_patterns": inspection.matched_patterns,
                },
                tripwire_triggered=True,
            )

    return GuardrailFunctionOutput(output_info={"matched_patterns": ()}, tripwire_triggered=False)


@output_guardrail
def output_grounding_guardrail(
    ctx: RunContextWrapper[AgentRunContext], agent: Agent[AgentRunContext], agent_output: object
) -> GuardrailFunctionOutput:
    """Проверяет ответ модели на утечку системного промпта и выдуманные UUID событий.

    Arguments:
        ctx: Контекст выполнения с допустимыми идентификаторами `allowed_event_ids`.
        agent: Экземпляр агента.
        agent_output: Финальный вывод модели (строка или структурированная схема).

    Returns:
        Результат проверки с `tripwire_triggered=True` при нарушении инвариантов.
    """
    del agent
    serialized_output = _serialize_agent_output_for_check(agent_output)
    if contains_system_prompt_leak(serialized_output):
        return GuardrailFunctionOutput(output_info={"violation_type": "system_prompt_leak"}, tripwire_triggered=True)

    allowed_ids = ctx.context.allowed_event_ids
    found_uuids = extract_uuids_from_text(serialized_output)
    ungrounded = tuple(uid for uid in found_uuids if uid not in allowed_ids)
    if ungrounded:
        return GuardrailFunctionOutput(
            output_info={
                "violation_type": "hallucinated_event",
                "ungrounded_event_identifiers": tuple(str(uid) for uid in ungrounded),
            },
            tripwire_triggered=True,
        )

    return GuardrailFunctionOutput(output_info={"checked_uuids_count": len(found_uuids)}, tripwire_triggered=False)
