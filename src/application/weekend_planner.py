"""Прикладной планировщик маршрута «Выходной день» без пересечений по времени.

Архитектурное решение:
    Алгоритм планировщика реализован как чистый прикладной сценарий поверх портов
    `EventRepositoryPort` и опционального `GeolocationResolverPort` без прямого
    ввода-вывода. Для подбора последовательности из 2–4 непересекающихся мероприятий
    используется динамическое программирование по длине цепочки с Парето-отсечением
    по суммарному времени переездов и минимальной стоимости на всех участников.
    При невозможности собрать маршрут выполняется пошаговая диагностика конфликтующих
    ограничений (`budget`, `time_window`, `travel`, `age`, `count`) с расчётом
    минимального смягчения параметров.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from src.application._helpers import attach_default_timezone, resolve_duration_minutes, resolve_normalized_city
from src.core.exceptions import ApplicationException
from src.core.types import EventStatus
from src.domain.entities import (
    ConstraintRelaxationSuggestion,
    Coordinates,
    Event,
    EventSearchFilter,
    EventSortField,
    SortDirection,
    WeekendPlanRequest,
    WeekendPlanResult,
    WeekendPlanStep,
)
from src.domain.exceptions import InvalidWeekendPlanRequestError
from src.domain.ports import EventRepositoryPort, GeolocationResolverPort
from src.domain.services import (
    BASHKORTOSTAN_TIMEZONE,
    estimate_urban_travel,
    normalize_optional_text,
    normalize_text_for_matching,
)

__all__ = ["WeekendPlannerService"]

_DEFAULT_EVENT_DURATION_MINUTES = 90
_DEFAULT_SAME_CITY_TRAVEL_MINUTES = 15
_DEFAULT_CROSS_CITY_TRAVEL_MINUTES = 45
_MINIMUM_PLAN_STEPS = 2
_MAXIMUM_PLAN_STEPS = 4
_SEARCH_PAGE_SIZE = 100
_MAXIMUM_SEARCH_PAGES = 5
_MAXIMUM_PARETO_STATES_PER_NODE = 16
_RELAXED_TRAVEL_LIMIT_MINUTES = 180
_RELAXED_BUDGET_RUBLES = 10_000_000


@dataclass(frozen=True, slots=True, kw_only=True)
class _PreparedCandidate:
    """Подготовленный кандидат мероприятия с вычисленным интервалом и стоимостью."""

    event: Event
    starts_at: datetime
    ends_at: datetime
    price_minimum_rubles: int
    price_maximum_rubles: int
    total_minimum_cost_rubles: int
    total_maximum_cost_rubles: int


@dataclass(frozen=True, slots=True, kw_only=True)
class _ChainState:
    """Промежуточное состояние цепочки планировщика для Парето-оптимизации."""

    total_minimum_cost_rubles: int
    total_travel_minutes: int
    indices: tuple[int, ...]
    leg_travels: tuple[int, ...]


class WeekendPlannerService:
    """Прикладной сервис построения маршрута выходного дня из 2–4 мероприятий."""

    def __init__(
        self, event_repository: EventRepositoryPort, geolocation_resolver: GeolocationResolverPort | None = None
    ) -> None:
        """Инициализирует планировщик маршрута выходного дня.

        Arguments:
            event_repository: Порт доступа к каталогу опубликованных мероприятий.
            geolocation_resolver: Необязательный порт геокодирования и расчёта времени в пути.
        """
        self._event_repository = event_repository
        self._geolocation_resolver = geolocation_resolver

    async def plan_weekend(self, request: WeekendPlanRequest) -> WeekendPlanResult:
        """Строит оптимальный непересекающийся маршрут из 2–4 событий по заданным ограничениям.

        Arguments:
            request: Параметры запроса планировщика (город, дата, окно, бюджет, компания, возраст, переезды).

        Returns:
            Результат планирования с шагами маршрута либо причиной отказа и предложениями релаксации.

        Raises:
            InvalidWeekendPlanRequestError: При некорректных параметрах входного запроса.
        """
        cleaned_city = self._validate_request(request)
        resolved_city = await resolve_normalized_city(cleaned_city, event_repository=self._event_repository)

        day_events = await self._load_city_day_events(city=resolved_city, target_date=request.target_date)
        prepared_all = [self._prepare_candidate(event) for event in day_events]
        prepared_all.sort(
            key=lambda item: (item.starts_at, item.ends_at, item.total_minimum_cost_rubles, item.event.identifier)
        )

        # Предвычисляем матрицу переездов с кэшированием геозапросов для одного вызова.
        coordinates_cache: dict[tuple[str, str, str], Coordinates | None] = {}
        travel_cache: dict[tuple[int, int], int] = {}

        async def get_travel_minutes(first_index: int, second_index: int) -> int:
            pair = (first_index, second_index)
            cached = travel_cache.get(pair)
            if cached is not None:
                return cached
            minutes = await self._estimate_pair_travel_minutes(
                first=prepared_all[first_index].event,
                second=prepared_all[second_index].event,
                coordinates_cache=coordinates_cache,
            )
            travel_cache[pair] = minutes
            return minutes

        window_start = datetime.combine(request.target_date, request.window_start_time, tzinfo=BASHKORTOSTAN_TIMEZONE)
        window_end = datetime.combine(request.target_date, request.window_end_time, tzinfo=BASHKORTOSTAN_TIMEZONE)

        best_chain = await self._find_optimal_chain(
            candidates=prepared_all,
            window_start=window_start,
            window_end=window_end,
            budget_rubles=request.budget_rubles,
            companions_count=request.companions_count,
            child_age=request.child_age,
            maximum_travel_minutes=request.maximum_travel_minutes,
            get_travel_minutes=get_travel_minutes,
        )

        if best_chain is not None:
            return self._build_feasible_result(
                candidates=prepared_all, chain=best_chain, companions_count=request.companions_count
            )

        return await self._build_infeasible_result(
            request=request,
            resolved_city=resolved_city,
            candidates=prepared_all,
            window_start=window_start,
            window_end=window_end,
            get_travel_minutes=get_travel_minutes,
        )

    @staticmethod
    def _validate_request(request: WeekendPlanRequest) -> str:
        """Проверяет доменные инварианты параметров запроса планировщика."""
        cleaned_city = normalize_optional_text(request.city)
        if cleaned_city is None:
            raise InvalidWeekendPlanRequestError(
                "Название города для планировщика не может быть пустым.",
                parameter_name="city",
                reason="Отсутствует город проведения маршрута",
            )
        if request.window_end_time <= request.window_start_time:
            raise InvalidWeekendPlanRequestError(
                "Время окончания свободного окна должно быть позже времени начала.",
                parameter_name="window_end_time",
                reason="Некорректный временной интервал окна",
            )
        if request.budget_rubles < 0:
            raise InvalidWeekendPlanRequestError(
                "Бюджет маршрута не может быть отрицательным.",
                parameter_name="budget_rubles",
                reason="Указан отрицательный бюджет",
            )
        if request.companions_count < 1:
            raise InvalidWeekendPlanRequestError(
                "Количество участников маршрута должно быть не меньше 1.",
                parameter_name="companions_count",
                reason="Неположительное число участников",
            )
        if request.maximum_travel_minutes < 0:
            raise InvalidWeekendPlanRequestError(
                "Допустимое время перемещения между площадками не может быть отрицательным.",
                parameter_name="maximum_travel_minutes",
                reason="Указано отрицательное время в пути",
            )
        if request.child_age is not None and request.child_age < 0:
            raise InvalidWeekendPlanRequestError(
                "Возраст ребёнка не может быть отрицательным.",
                parameter_name="child_age",
                reason="Указан отрицательный возраст ребёнка",
            )
        return cleaned_city

    async def _load_city_day_events(self, *, city: str, target_date: object) -> list[Event]:
        """Загружает опубликованные события указанного города на целевую дату."""
        from datetime import date as date_type

        assert isinstance(target_date, date_type)
        day_start = datetime.combine(target_date, time(0, 0), tzinfo=BASHKORTOSTAN_TIMEZONE)
        day_end = datetime.combine(target_date, time(23, 59, 59), tzinfo=BASHKORTOSTAN_TIMEZONE)

        collected: list[Event] = []
        seen_identifiers: set[object] = set()
        normalized_target_city = normalize_text_for_matching(city)

        for page_number in range(1, _MAXIMUM_SEARCH_PAGES + 1):
            page = await self._event_repository.search_events(
                search_filter=EventSearchFilter(
                    city=city,
                    date_from=day_start,
                    date_to=day_end,
                    sort_field=EventSortField.DATE_START,
                    sort_direction=SortDirection.ASCENDING,
                    page=page_number,
                    page_size=_SEARCH_PAGE_SIZE,
                ),
                statuses=(EventStatus.PUBLISHED,),
            )
            for event in page.items:
                if event.identifier in seen_identifiers or event.status is not EventStatus.PUBLISHED:
                    continue
                event_start = attach_default_timezone(event.date_start)
                if event_start.date() != target_date:
                    continue
                if normalize_text_for_matching(event.city) != normalized_target_city:
                    continue
                seen_identifiers.add(event.identifier)
                collected.append(event)
            if not page.has_more:
                break

        return collected

    @staticmethod
    def _prepare_candidate(event: Event) -> _PreparedCandidate:
        """Вычисляет часовое окно и базовую стоимость билета для события."""
        starts_at = attach_default_timezone(event.date_start)
        date_end = attach_default_timezone(event.date_end) if event.date_end is not None else None
        duration_minutes = resolve_duration_minutes(
            date_start=starts_at, date_end=date_end, explicit_duration_minutes=event.duration_minutes
        )
        effective_duration = duration_minutes if duration_minutes is not None else _DEFAULT_EVENT_DURATION_MINUTES
        ends_at = starts_at + timedelta(minutes=effective_duration)

        if event.is_free:
            price_min = 0
            price_max = 0
        else:
            price_min = max(0, event.price_minimum or 0)
            price_max = max(price_min, event.price_maximum if event.price_maximum is not None else price_min)

        return _PreparedCandidate(
            event=event,
            starts_at=starts_at,
            ends_at=ends_at,
            price_minimum_rubles=price_min,
            price_maximum_rubles=price_max,
            total_minimum_cost_rubles=price_min,
            total_maximum_cost_rubles=price_max,
        )

    async def _resolve_event_coordinates(
        self, event: Event, *, coordinates_cache: dict[tuple[str, str, str], Coordinates | None]
    ) -> Coordinates | None:
        """Определяет координаты площадки события из карточки или через георезолвер."""
        if event.latitude is not None and event.longitude is not None:
            return Coordinates(latitude=event.latitude, longitude=event.longitude)

        cache_key = (
            normalize_text_for_matching(event.city),
            normalize_text_for_matching(event.venue),
            normalize_text_for_matching(event.address),
        )
        if cache_key in coordinates_cache:
            return coordinates_cache[cache_key]

        if self._geolocation_resolver is None:
            coordinates_cache[cache_key] = None
            return None

        try:
            resolved = await self._geolocation_resolver.resolve_venue_coordinates(
                city=event.city, venue=event.venue, address=event.address
            )
        except ApplicationException, RuntimeError, ValueError, OSError:
            resolved = None

        coordinates_cache[cache_key] = resolved
        return resolved

    async def _estimate_pair_travel_minutes(
        self, *, first: Event, second: Event, coordinates_cache: dict[tuple[str, str, str], Coordinates | None]
    ) -> int:
        """Оценивает время перемещения в минутах между двумя площадками с безопасным фоллбэком."""
        same_city = normalize_text_for_matching(first.city) == normalize_text_for_matching(second.city)
        same_venue = same_city and normalize_text_for_matching(first.venue) == normalize_text_for_matching(second.venue)
        if same_venue:
            return 0

        origin = await self._resolve_event_coordinates(first, coordinates_cache=coordinates_cache)
        destination = await self._resolve_event_coordinates(second, coordinates_cache=coordinates_cache)

        if origin is not None and destination is not None:
            if self._geolocation_resolver is not None:
                try:
                    estimate = await self._geolocation_resolver.estimate_travel(origin=origin, destination=destination)
                    return max(0, estimate.travel_minutes)
                except ApplicationException, RuntimeError, ValueError, OSError:
                    pass
            return estimate_urban_travel(origin=origin, destination=destination).travel_minutes

        return _DEFAULT_SAME_CITY_TRAVEL_MINUTES if same_city else _DEFAULT_CROSS_CITY_TRAVEL_MINUTES

    async def _find_optimal_chain(
        self,
        *,
        candidates: list[_PreparedCandidate],
        window_start: datetime,
        window_end: datetime,
        budget_rubles: int,
        companions_count: int,
        child_age: int | None,
        maximum_travel_minutes: int,
        get_travel_minutes: object,
    ) -> _ChainState | None:
        """Находит оптимальную цепочку из 2–4 непересекающихся событий."""
        from collections.abc import Awaitable, Callable

        travel_lookup: Callable[[int, int], Awaitable[int]] = get_travel_minutes  # type: ignore[assignment]

        eligible_indices: list[int] = []
        for index, item in enumerate(candidates):
            if item.starts_at < window_start or item.ends_at > window_end:
                continue
            if child_age is not None and item.event.age_limit > child_age:
                continue
            step_cost = item.price_minimum_rubles * companions_count
            if step_cost > budget_rubles:
                continue
            eligible_indices.append(index)

        if len(eligible_indices) < _MINIMUM_PLAN_STEPS:
            return None

        dp: dict[int, dict[int, list[_ChainState]]] = {length: {} for length in range(1, _MAXIMUM_PLAN_STEPS + 1)}
        for index in eligible_indices:
            step_cost = candidates[index].price_minimum_rubles * companions_count
            dp[1][index] = [
                _ChainState(
                    total_minimum_cost_rubles=step_cost, total_travel_minutes=0, indices=(index,), leg_travels=(0,)
                )
            ]

        for length in range(2, _MAXIMUM_PLAN_STEPS + 1):
            previous_layer = dp[length - 1]
            current_layer = dp[length]
            for next_index in eligible_indices:
                next_candidate = candidates[next_index]
                next_cost = next_candidate.price_minimum_rubles * companions_count
                states_for_node: list[_ChainState] = []

                for prev_index, prev_states in previous_layer.items():
                    if prev_index >= next_index:
                        continue
                    prev_candidate = candidates[prev_index]
                    if next_candidate.starts_at < prev_candidate.ends_at:
                        continue
                    travel_minutes = await travel_lookup(prev_index, next_index)
                    if travel_minutes > maximum_travel_minutes:
                        continue
                    if next_candidate.starts_at < prev_candidate.ends_at + timedelta(minutes=travel_minutes):
                        continue

                    for prev_state in prev_states:
                        combined_cost = prev_state.total_minimum_cost_rubles + next_cost
                        if combined_cost > budget_rubles:
                            continue
                        states_for_node.append(
                            _ChainState(
                                total_minimum_cost_rubles=combined_cost,
                                total_travel_minutes=prev_state.total_travel_minutes + travel_minutes,
                                indices=(*prev_state.indices, next_index),
                                leg_travels=(*prev_state.leg_travels, travel_minutes),
                            )
                        )

                if states_for_node:
                    current_layer[next_index] = self._prune_pareto_states(states_for_node)

        for target_length in range(_MAXIMUM_PLAN_STEPS, _MINIMUM_PLAN_STEPS - 1, -1):
            layer_states = [state for node_states in dp[target_length].values() for state in node_states]
            if layer_states:
                return min(
                    layer_states,
                    key=lambda state: (state.total_travel_minutes, state.total_minimum_cost_rubles, state.indices),
                )

        return None

    @staticmethod
    def _prune_pareto_states(states: list[_ChainState]) -> list[_ChainState]:
        """Оставляет только Парето-оптимальные состояния по переездам и стоимости."""
        sorted_states = sorted(
            states, key=lambda state: (state.total_travel_minutes, state.total_minimum_cost_rubles, state.indices)
        )
        pareto: list[_ChainState] = []
        best_cost_seen: int | None = None
        for state in sorted_states:
            if best_cost_seen is None or state.total_minimum_cost_rubles < best_cost_seen:
                pareto.append(state)
                best_cost_seen = state.total_minimum_cost_rubles
                if len(pareto) >= _MAXIMUM_PARETO_STATES_PER_NODE:
                    break
        return pareto

    @staticmethod
    def _build_feasible_result(
        *, candidates: list[_PreparedCandidate], chain: _ChainState, companions_count: int
    ) -> WeekendPlanResult:
        """Формирует успешный `WeekendPlanResult` из найденной цепочки."""
        steps: list[WeekendPlanStep] = []
        total_min_cost = 0
        total_max_cost = 0

        for candidate_index, leg_travel in zip(chain.indices, chain.leg_travels, strict=True):
            item = candidates[candidate_index]
            step_min_cost = item.price_minimum_rubles * companions_count
            step_max_cost = item.price_maximum_rubles * companions_count
            total_min_cost += step_min_cost
            total_max_cost += step_max_cost
            steps.append(
                WeekendPlanStep(
                    event_identifier=item.event.identifier,
                    title=item.event.title,
                    category=item.event.category,
                    venue=item.event.venue,
                    address=item.event.address,
                    starts_at=item.starts_at,
                    ends_at=item.ends_at,
                    price_minimum_rubles=item.price_minimum_rubles,
                    price_maximum_rubles=item.price_maximum_rubles,
                    total_cost_rubles=step_min_cost,
                    travel_from_previous_minutes=leg_travel,
                )
            )

        return WeekendPlanResult(
            is_feasible=True,
            steps=tuple(steps),
            total_minimum_cost_rubles=total_min_cost,
            total_maximum_cost_rubles=total_max_cost,
            total_travel_minutes=chain.total_travel_minutes,
        )

    async def _build_infeasible_result(
        self,
        *,
        request: WeekendPlanRequest,
        resolved_city: str,
        candidates: list[_PreparedCandidate],
        window_start: datetime,
        window_end: datetime,
        get_travel_minutes: object,
    ) -> WeekendPlanResult:
        """Диагностирует причину невозможности маршрута и подбирает конкретные ослабления."""
        if len(candidates) < _MINIMUM_PLAN_STEPS:
            formatted_date = request.target_date.strftime("%d.%m.%Y")
            suggestion = ConstraintRelaxationSuggestion(
                constraint_name="count",
                explanation=(
                    f"В городе {resolved_city} на {formatted_date} найдено недостаточно "
                    f"опубликованных мероприятий ({len(candidates)} из минимум {_MINIMUM_PLAN_STEPS})."
                ),
                suggested_change="Выберите другую дату (например, ближайшие выходные) или соседний город.",
            )
            return WeekendPlanResult(
                is_feasible=False,
                failure_reason=(
                    f"Не удалось собрать маршрут из 2–4 событий: на {formatted_date} в городе "
                    f"{resolved_city} доступно только {len(candidates)} мероприятий."
                ),
                relaxation_suggestions=(suggestion,),
            )

        suggestions: list[ConstraintRelaxationSuggestion] = []

        if request.child_age is not None:
            relaxed_age_chain = await self._find_optimal_chain(
                candidates=candidates,
                window_start=window_start,
                window_end=window_end,
                budget_rubles=request.budget_rubles,
                companions_count=request.companions_count,
                child_age=None,
                maximum_travel_minutes=request.maximum_travel_minutes,
                get_travel_minutes=get_travel_minutes,
            )
            if relaxed_age_chain is not None:
                suggestions.append(
                    ConstraintRelaxationSuggestion(
                        constraint_name="age",
                        explanation=(
                            f"Возрастное ограничение {request.child_age}+ отсеяло часть подходящих по времени "
                            "и бюджету мероприятий."
                        ),
                        suggested_change=(
                            "Ослабить возрастное ограничение или рассмотреть маршрут без детского фильтра."
                        ),
                    )
                )

        full_day_start = datetime.combine(request.target_date, time(0, 0), tzinfo=BASHKORTOSTAN_TIMEZONE)
        full_day_end = datetime.combine(request.target_date, time(23, 59, 59), tzinfo=BASHKORTOSTAN_TIMEZONE)
        relaxed_window_chain = await self._find_optimal_chain(
            candidates=candidates,
            window_start=full_day_start,
            window_end=full_day_end,
            budget_rubles=request.budget_rubles,
            companions_count=request.companions_count,
            child_age=request.child_age,
            maximum_travel_minutes=request.maximum_travel_minutes,
            get_travel_minutes=get_travel_minutes,
        )
        if relaxed_window_chain is not None:
            needed_start = candidates[relaxed_window_chain.indices[0]].starts_at.strftime("%H:%M")
            needed_end = candidates[relaxed_window_chain.indices[-1]].ends_at.strftime("%H:%M")
            suggestions.append(
                ConstraintRelaxationSuggestion(
                    constraint_name="time_window",
                    explanation=(
                        f"Заданное окно {request.window_start_time:%H:%M}–{request.window_end_time:%H:%M} "
                        "не вмещает 2–4 непересекающихся события с учётом дороги."
                    ),
                    suggested_change=f"Расширить свободное окно до {needed_start}–{needed_end}.",
                )
            )

        relaxed_budget_chain = await self._find_optimal_chain(
            candidates=candidates,
            window_start=window_start,
            window_end=window_end,
            budget_rubles=_RELAXED_BUDGET_RUBLES,
            companions_count=request.companions_count,
            child_age=request.child_age,
            maximum_travel_minutes=request.maximum_travel_minutes,
            get_travel_minutes=get_travel_minutes,
        )
        if relaxed_budget_chain is not None:
            suggestions.append(
                ConstraintRelaxationSuggestion(
                    constraint_name="budget",
                    explanation=(
                        f"Общего бюджета {request.budget_rubles} ₽ на {request.companions_count} чел. "
                        "недостаточно для набора из 2–4 подходящих мероприятий."
                    ),
                    suggested_change=(
                        f"Увеличить общий бюджет минимум до {relaxed_budget_chain.total_minimum_cost_rubles} ₽."
                    ),
                )
            )

        relaxed_travel_chain = await self._find_optimal_chain(
            candidates=candidates,
            window_start=window_start,
            window_end=window_end,
            budget_rubles=request.budget_rubles,
            companions_count=request.companions_count,
            child_age=request.child_age,
            maximum_travel_minutes=_RELAXED_TRAVEL_LIMIT_MINUTES,
            get_travel_minutes=get_travel_minutes,
        )
        if relaxed_travel_chain is not None:
            max_leg = max(relaxed_travel_chain.leg_travels)
            suggestions.append(
                ConstraintRelaxationSuggestion(
                    constraint_name="travel",
                    explanation=(
                        f"Ограничение на переезд ({request.maximum_travel_minutes} мин.) не позволяет "
                        "успеть между площадками выбранных мероприятий."
                    ),
                    suggested_change=f"Увеличить допустимое время перемещения между площадками до {max_leg} мин.",
                )
            )

        if not suggestions:
            suggestions.append(
                ConstraintRelaxationSuggestion(
                    constraint_name="time_window",
                    explanation=(
                        "Одновременно действуют несколько ограничений (время, бюджет, переезды или "
                        "пересечение расписания мероприятий)."
                    ),
                    suggested_change="Расширьте временное окно и увеличьте допустимый бюджет или время переезда.",
                )
            )

        return WeekendPlanResult(
            is_feasible=False,
            failure_reason=(
                "Не удалось подобрать от 2 до 4 непересекающихся мероприятий при заданных ограничениях "
                f"в городе {resolved_city}."
            ),
            relaxation_suggestions=tuple(suggestions),
        )
