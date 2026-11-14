"""Расчёт токенов и стоимости вызовов языковой модели в рублях."""

from collections.abc import Mapping
from dataclasses import dataclass

from agents.result import RunResult

from src.core.configuration import LanguageModelSettings

__all__ = [
    "ModelUsageCost",
    "calculate_cost_rubles",
    "calculate_run_usage_and_cost",
    "extract_raw_usage_cost_usd",
    "extract_token_usage",
]

_TOKENS_PER_MILLION = 1_000_000.0


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelUsageCost:
    """Сводка по израсходованным токенам и расчётной стоимости в рублях.

    Attributes:
        tokens_input: Количество входных токенов.
        tokens_output: Количество выходных токенов.
        cost_rubles: Расчётная стоимость запроса в рублях.
    """

    tokens_input: int
    tokens_output: int
    cost_rubles: float


def extract_token_usage(run_result: RunResult) -> tuple[int, int]:
    """Извлекает число входных и выходных токенов из `RunResult.context_wrapper.usage`.

    Arguments:
        run_result: Итоговый результат выполнения `Runner.run`.

    Returns:
        Кортеж `(tokens_input, tokens_output)`.
    """
    usage = run_result.context_wrapper.usage
    tokens_input = max(0, int(usage.input_tokens or 0))
    tokens_output = max(0, int(usage.output_tokens or 0))
    return tokens_input, tokens_output


def extract_raw_usage_cost_usd(run_result: RunResult) -> float | None:
    """Извлекает суммарную стоимость в USD из `raw_usage` ответов модели, если она передана.

    Arguments:
        run_result: Итоговый результат выполнения `Runner.run`.

    Returns:
        Суммарная стоимость в долларах США или `None`, если провайдер её не вернул.
    """
    total_cost_usd = 0.0
    found_cost = False
    for response in run_result.raw_responses:
        raw_usage = response.raw_usage
        if not isinstance(raw_usage, Mapping):
            continue
        raw_cost = raw_usage.get("cost")
        if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
            total_cost_usd += max(0.0, float(raw_cost))
            found_cost = True
    return total_cost_usd if found_cost else None


def calculate_cost_rubles(
    *,
    model_name: str,
    tokens_input: int,
    tokens_output: int,
    settings: LanguageModelSettings,
    raw_usage: Mapping[str, object] | None = None,
    raw_cost_usd: float | None = None,
) -> float:
    """Рассчитывает стоимость запроса в рублях по `PRICING_JSON` или fallback на `raw_usage['cost']`.

    Arguments:
        model_name: Идентификатор модели у провайдера.
        tokens_input: Количество входных токенов.
        tokens_output: Количество выходных токенов.
        settings: Настройки языковой модели с тарифами и курсом USD/RUB.
        raw_usage: Необязательный словарь `raw_usage` от провайдера.
        raw_cost_usd: Предварительно извлечённая стоимость в USD из всех `raw_responses`.

    Returns:
        Стоимость вызова в рублях, округлённая до 6 знаков после запятой.
    """
    pricing_map = settings.get_pricing_map()
    if model_name in pricing_map:
        input_rate_rub, output_rate_rub = pricing_map[model_name]
        cost_rubles = (max(0, tokens_input) / _TOKENS_PER_MILLION) * float(input_rate_rub) + (
            max(0, tokens_output) / _TOKENS_PER_MILLION
        ) * float(output_rate_rub)
        return float(round(max(0.0, cost_rubles), 6))

    effective_usd_cost = raw_cost_usd
    if effective_usd_cost is None and raw_usage is not None:
        raw_cost = raw_usage.get("cost")
        if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
            effective_usd_cost = float(raw_cost)

    if effective_usd_cost is not None:
        return float(round(max(0.0, effective_usd_cost) * float(settings.USD_TO_RUB_RATE), 6))

    return 0.0


def calculate_run_usage_and_cost(
    run_result: RunResult, *, model_name: str, settings: LanguageModelSettings
) -> ModelUsageCost:
    """Извлекает токены из `RunResult` и рассчитывает стоимость вызова в рублях.

    Arguments:
        run_result: Итоговый результат выполнения `Runner.run`.
        model_name: Идентификатор модели у провайдера.
        settings: Настройки языковой модели.

    Returns:
        Объект `ModelUsageCost` с числом токенов и стоимостью в рублях.
    """
    tokens_input, tokens_output = extract_token_usage(run_result)
    raw_cost_usd = extract_raw_usage_cost_usd(run_result)
    cost_rubles = calculate_cost_rubles(
        model_name=model_name,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        settings=settings,
        raw_cost_usd=raw_cost_usd,
    )
    return ModelUsageCost(tokens_input=tokens_input, tokens_output=tokens_output, cost_rubles=cost_rubles)
