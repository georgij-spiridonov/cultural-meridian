"""Фабрика клиента OpenAI Agents SDK для OpenRouter и маппинг исключений."""

from typing import Final

import openai
from agents import ModelSettings, OpenAIChatCompletionsModel, RunConfig, set_tracing_disabled
from agents.exceptions import (
    AgentsException,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRefusalError,
    ModelTimeoutError,
    OutputGuardrailTripwireTriggered,
    UserError,
)
from openai import AsyncOpenAI
from openai.types.shared import Reasoning

from src.core.configuration import LanguageModelSettings
from src.core.exceptions import (
    AccessCredentialsInvalidError,
    AccessPermissionDeniedError,
    ApplicationException,
    ModelContentBlockedError,
    ModelContextLimitExceededError,
    ModelException,
    ModelNotFoundError,
    ModelOutputInvalidError,
    ModelOutputTruncatedError,
    ModelRequestInvalidError,
    NetworkConnectionFailureError,
    NetworkConnectionTimeoutError,
    ServiceRateLimitExceededError,
    ServiceUnavailableError,
)
from src.domain.exceptions import HallucinatedEventError, PromptInjectionDetectedError

__all__ = [
    "create_chat_model",
    "create_model_settings",
    "create_openai_client",
    "create_run_config",
    "map_language_model_exception",
    "supports_strict_json_schema",
]

set_tracing_disabled(True)

_CONTEXT_LIMIT_MARKERS: Final[tuple[str, ...]] = (
    "context length",
    "context_length_exceeded",
    "maximum context",
    "too many tokens",
)


def supports_strict_json_schema(model_name: str) -> bool:
    """Определяет, поддерживает ли модель строгий режим JSON-схемы без зацикливания.

    Arguments:
        model_name: Идентификатор модели у провайдера OpenRouter.

    Returns:
        `False` для моделей семейства `flash-lite`, иначе `True`.
    """
    return "flash-lite" not in model_name.lower()


def create_openai_client(settings: LanguageModelSettings) -> AsyncOpenAI:
    """Создаёт асинхронный клиент `AsyncOpenAI`, настроенный на OpenRouter.

    Arguments:
        settings: Конфигурация языковой модели.

    Returns:
        Инициализированный экземпляр `AsyncOpenAI`.

    Raises:
        AccessCredentialsInvalidError: Если не задан `LANGUAGE_MODEL_API_KEY`.
    """
    if not settings.API_KEY.strip():
        raise AccessCredentialsInvalidError("Не задан API-ключ языковой модели (LANGUAGE_MODEL_API_KEY).")

    return AsyncOpenAI(
        base_url=settings.BASE_URL,
        api_key=settings.API_KEY,
        timeout=settings.REQUEST_TIMEOUT_SECONDS,
        max_retries=settings.MAX_RETRIES,
        default_headers={"HTTP-Referer": settings.HTTP_REFERER, "X-Title": settings.APP_TITLE},
    )


def create_chat_model(model_name: str, *, openai_client: AsyncOpenAI) -> OpenAIChatCompletionsModel:
    """Создаёт адаптер чат-модели OpenAI Agents SDK поверх клиента OpenRouter.

    Arguments:
        model_name: Идентификатор модели у провайдера.
        openai_client: Настроенный асинхронный клиент `AsyncOpenAI`.

    Returns:
        Модель `OpenAIChatCompletionsModel` для передачи в `Agent` или `RunConfig`.
    """
    return OpenAIChatCompletionsModel(model=model_name, openai_client=openai_client)


def create_model_settings(
    settings: LanguageModelSettings, *, use_tools: bool = True, max_tokens: int | None = None
) -> ModelSettings:
    """Формирует параметры генерации `ModelSettings` для OpenRouter.

    Arguments:
        settings: Конфигурация языковой модели.
        use_tools: Признак использования инструментов агентом.
        max_tokens: Переопределение лимита выходных токенов.

    Returns:
        Настроенный объект `ModelSettings` с `preserve_raw_usage=True`.
    """
    return ModelSettings(
        temperature=0.0,
        preserve_raw_usage=True,
        tool_choice="auto" if use_tools else None,
        parallel_tool_calls=False if use_tools else None,
        max_tokens=max_tokens if max_tokens is not None else settings.MAX_OUTPUT_TOKENS,
        reasoning=Reasoning(effort="low"),
    )


def create_run_config(
    *, model: OpenAIChatCompletionsModel, model_settings: ModelSettings, workflow_name: str = "cultural-meridian"
) -> RunConfig:
    """Создаёт конфигурацию запуска `RunConfig` с отключённой отправкой трейсов.

    Arguments:
        model: Модель OpenAI Agents SDK.
        model_settings: Параметры генерации модели.
        workflow_name: Имя рабочего процесса для локальной идентификации.

    Returns:
        Конфигурация запуска с `tracing_disabled=True` и `trace_include_sensitive_data=False`.
    """
    set_tracing_disabled(True)
    return RunConfig(
        model=model,
        model_settings=model_settings,
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        workflow_name=workflow_name,
    )


def _map_input_guardrail_error(error: InputGuardrailTripwireTriggered) -> PromptInjectionDetectedError:
    """Преобразует срабатывание входного защитного фильтра в доменное исключение."""
    output_info = error.guardrail_result.output.output_info
    matched_patterns: tuple[str, ...] = ()
    source_context = "user_message"
    if isinstance(output_info, dict):
        raw_patterns = output_info.get("matched_patterns")
        if isinstance(raw_patterns, (list, tuple)):
            matched_patterns = tuple(str(item) for item in raw_patterns)
        raw_context = output_info.get("source_context")
        if isinstance(raw_context, str) and raw_context.strip():
            source_context = raw_context
    return PromptInjectionDetectedError(
        source_context=source_context,
        matched_patterns=matched_patterns,
        reason="Запрос заблокирован входным фильтром безопасности.",
    )


def _map_output_guardrail_error(error: OutputGuardrailTripwireTriggered) -> ApplicationException:
    """Преобразует срабатывание выходного защитного фильтра в доменное или модельно-инфраструктурное исключение."""
    output_info = error.guardrail_result.output.output_info
    if isinstance(output_info, dict) and output_info.get("violation_type") == "hallucinated_event":
        raw_ids = output_info.get("ungrounded_event_identifiers")
        return HallucinatedEventError(
            reason=(f"Обнаружены идентификаторы событий вне результатов инструментов: {raw_ids}.")
            if raw_ids
            else "Ответ содержит идентификатор события вне результатов инструментов."
        )
    return ModelContentBlockedError("Ответ модели заблокирован выходным фильтром защиты системных инструкций.")


def _map_api_status_error(error: openai.APIStatusError) -> ApplicationException:
    """Преобразует HTTP-ошибку OpenAI API в иерархию исключений проекта."""
    message_lower = str(error).lower()
    if any(marker in message_lower for marker in _CONTEXT_LIMIT_MARKERS):
        return ModelContextLimitExceededError(f"Превышен лимит контекста языковой модели: {error}")
    if error.status_code == 401:
        return AccessCredentialsInvalidError(f"Отклонён ключ доступа к провайдеру модели: {error}")
    if error.status_code == 403:
        return AccessPermissionDeniedError(f"Доступ к ресурсу провайдера модели запрещён: {error}")
    if error.status_code == 404:
        return ModelNotFoundError(f"Запрошенная модель не найдена у провайдера: {error}")
    if error.status_code == 429:
        return ServiceRateLimitExceededError(f"Превышен лимит запросов к провайдеру модели: {error}")
    if error.status_code >= 500:
        return ServiceUnavailableError(f"Провайдер языковой модели временно недоступен: {error}")
    return ModelRequestInvalidError(f"Некорректный запрос к провайдеру языковой модели: {error}")


def map_language_model_exception(error: Exception) -> ApplicationException:
    """Маппит исключения `openai` и `agents.exceptions` в исключения `core` и `domain`.

    Arguments:
        error: Исходное исключение, возникшее при вызове модели или агента.

    Returns:
        Соответствующее исключение из `src.core.exceptions` или `src.domain.exceptions`.
    """
    if isinstance(error, ApplicationException):
        return error

    if isinstance(error, InputGuardrailTripwireTriggered):
        return _map_input_guardrail_error(error)
    if isinstance(error, OutputGuardrailTripwireTriggered):
        return _map_output_guardrail_error(error)
    if isinstance(error, ModelTimeoutError):
        return NetworkConnectionTimeoutError(f"Превышен тайм-аут ожидания ответа модели: {error}")
    if isinstance(error, ModelRefusalError):
        return ModelContentBlockedError(f"Модель отказалась выполнять запрос: {error}")
    if isinstance(error, ModelBehaviorError):
        return ModelOutputInvalidError(f"Нарушение контракта поведения или схемы вывода модели: {error}")
    if isinstance(error, MaxTurnsExceeded):
        return ModelOutputTruncatedError(f"Агент превысил допустимое число шагов генерации: {error}")
    if isinstance(error, UserError):
        return ModelRequestInvalidError(f"Некорректная конфигурация вызова агента: {error}")
    if isinstance(error, AgentsException):
        return ModelException(f"Внутренняя ошибка OpenAI Agents SDK: {error}")

    if isinstance(error, openai.AuthenticationError):
        return AccessCredentialsInvalidError(f"Недействительные учётные данные OpenRouter: {error}")
    if isinstance(error, openai.PermissionDeniedError):
        return AccessPermissionDeniedError(f"Недостаточно прав для вызова модели OpenRouter: {error}")
    if isinstance(error, openai.NotFoundError):
        return ModelNotFoundError(f"Модель не найдена в OpenRouter: {error}")
    if isinstance(error, openai.RateLimitError):
        return ServiceRateLimitExceededError(f"Превышен лимит частоты запросов OpenRouter: {error}")
    if isinstance(error, openai.APITimeoutError):
        return NetworkConnectionTimeoutError(f"Превышен тайм-аут HTTP-запроса к OpenRouter: {error}")
    if isinstance(error, openai.APIConnectionError):
        return NetworkConnectionFailureError(f"Ошибка сетевого подключения к OpenRouter: {error}")
    if isinstance(error, openai.LengthFinishReasonError):
        return ModelOutputTruncatedError(f"Ответ модели оборван из-за ограничения длины: {error}")
    if isinstance(error, openai.ContentFilterFinishReasonError):
        return ModelContentBlockedError(f"Ответ модели заблокирован фильтром контента: {error}")
    if isinstance(error, openai.APIResponseValidationError):
        return ModelOutputInvalidError(f"Некорректный формат ответа от OpenRouter: {error}")
    if isinstance(error, openai.APIStatusError):
        return _map_api_status_error(error)
    if isinstance(error, openai.OpenAIError):
        return ModelException(f"Ошибка клиента OpenAI при обращении к модели: {error}")

    return ModelException(f"Непредвиденная ошибка вызова языковой модели: {error}")
