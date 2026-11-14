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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import combinations

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
        prepared_all = [
            self._prepare_candidate(event, companions_count=request.companions_count) for event in day_events
        ]
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
            child_age=request.child_age,
            maximum_travel_minutes=request.maximum_travel_minutes,
            get_travel_minutes=get_travel_minutes,
        )

        if best_chain is not None:
            return self._build_feasible_result(candidates=prepared_all, chain=best_chain)

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

    async def _load_city_day_events(self, *, city: str, target_date: date) -> list[Event]:
        """Загружает опубликованные события указанного города на целевую дату."""
        day_start = datetime.combine(target_date, time(0, 0), tzinfo=BASHKORTOSTAN_TIMEZONE)
        day_end = datetime.combine(target_date, time(23, 59, 59, 999999), tzinfo=BASHKORTOSTAN_TIMEZONE)

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
                event_start = attach_default_timezone(event.date_start).astimezone(BASHKORTOSTAN_TIMEZONE)
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
    def _prepare_candidate(event: Event, *, companions_count: int) -> _PreparedCandidate:
        """Вычисляет часовое окно и стоимость билета на одного участника и всю компанию."""
        starts_at = attach_default_timezone(event.date_start).astimezone(BASHKORTOSTAN_TIMEZONE)
        date_end = (
            attach_default_timezone(event.date_end).astimezone(BASHKORTOSTAN_TIMEZONE)
            if event.date_end is not None
            else None
        )
        explicit_duration = (
            event.duration_minutes if event.duration_minutes is not None and event.duration_minutes > 0 else None
        )
        duration_minutes = resolve_duration_minutes(
            date_start=starts_at, date_end=date_end, explicit_duration_minutes=explicit_duration
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
            total_minimum_cost_rubles=price_min * companions_count,
            total_maximum_cost_rubles=price_max * companions_count,
        )

    async def _resolve_event_coordinates(
        self, event: Event, *, coordinates_cache: dict[tuple[str, str, str], Coordinates | None]
    ) -> Coordinates | None:
        """Определяет координаты площадки события из карточки или через георезолвер."""
        if event.latitude is not None and event.longitude is not None:
            return Coordinates(latitude=event.latitude, longitude=event.longitude)

        city_key = normalize_text_for_matching(event.city)
        venue_key = normalize_text_for_matching(event.venue)
        address_key = normalize_text_for_matching(event.address)
        if not venue_key and not address_key:
            return None

        cache_key = (city_key, venue_key, address_key)
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
        first_venue = normalize_text_for_matching(first.venue)
        second_venue = normalize_text_for_matching(second.venue)
        first_address = normalize_text_for_matching(first.address)
        second_address = normalize_text_for_matching(second.address)
        same_venue = same_city and (
            (bool(first_venue) and first_venue == second_venue)
            or (not first_venue and not second_venue and bool(first_address) and first_address == second_address)
        )
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
            return max(0, estimate_urban_travel(origin=origin, destination=destination).travel_minutes)

        return _DEFAULT_SAME_CITY_TRAVEL_MINUTES if same_city else _DEFAULT_CROSS_CITY_TRAVEL_MINUTES

    async def _collect_dp_chains(
        self,
        *,
        candidates: list[_PreparedCandidate],
        window_start: datetime,
        window_end: datetime,
        budget_rubles: int,
        child_age: int | None,
        maximum_travel_minutes: int,
        get_travel_minutes: Callable[[int, int], Awaitable[int]],
    ) -> list[_ChainState]:
        """Собирает Парето-оптимальные цепочки длиной 2–4 события для заданных ограничений."""
        eligible_indices: list[int] = []
        for index, item in enumerate(candidates):
            if item.starts_at < window_start or item.ends_at > window_end:
                continue
            if child_age is not None and item.event.age_limit > child_age:
                continue
            if item.total_minimum_cost_rubles > budget_rubles:
                continue
            eligible_indices.append(index)

        if len(eligible_indices) < _MINIMUM_PLAN_STEPS:
            return []

        dp: dict[int, dict[int, list[_ChainState]]] = {length: {} for length in range(1, _MAXIMUM_PLAN_STEPS + 1)}
        for index in eligible_indices:
            dp[1][index] = [
                _ChainState(
                    total_minimum_cost_rubles=candidates[index].total_minimum_cost_rubles,
                    total_travel_minutes=0,
                    indices=(index,),
                    leg_travels=(0,),
                )
            ]

        for length in range(2, _MAXIMUM_PLAN_STEPS + 1):
            previous_layer = dp[length - 1]
            current_layer = dp[length]
            for next_index in eligible_indices:
                next_candidate = candidates[next_index]
                states_for_node: list[_ChainState] = []

                for prev_index, prev_states in previous_layer.items():
                    if prev_index >= next_index:
                        continue
                    prev_candidate = candidates[prev_index]
                    if next_candidate.starts_at < prev_candidate.ends_at:
                        continue
                    travel_minutes = await get_travel_minutes(prev_index, next_index)
                    if travel_minutes > maximum_travel_minutes:
                        continue
                    if next_candidate.starts_at < prev_candidate.ends_at + timedelta(minutes=travel_minutes):
                        continue

                    for prev_state in prev_states:
                        combined_cost = prev_state.total_minimum_cost_rubles + next_candidate.total_minimum_cost_rubles
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

        all_chains: list[_ChainState] = []
        for target_length in range(_MAXIMUM_PLAN_STEPS, _MINIMUM_PLAN_STEPS - 1, -1):
            for node_states in dp[target_length].values():
                all_chains.extend(node_states)
        return all_chains

    async def _find_optimal_chain(
        self,
        *,
        candidates: list[_PreparedCandidate],
        window_start: datetime,
        window_end: datetime,
        budget_rubles: int,
        child_age: int | None,
        maximum_travel_minutes: int,
        get_travel_minutes: Callable[[int, int], Awaitable[int]],
    ) -> _ChainState | None:
        """Находит оптимальную цепочку из 2–4 непересекающихся событий."""
        chains = await self._collect_dp_chains(
            candidates=candidates,
            window_start=window_start,
            window_end=window_end,
            budget_rubles=budget_rubles,
            child_age=child_age,
            maximum_travel_minutes=maximum_travel_minutes,
            get_travel_minutes=get_travel_minutes,
        )
        if not chains:
            return None
        return min(
            chains,
            key=lambda state: (
                -len(state.indices),
                state.total_travel_minutes,
                state.total_minimum_cost_rubles,
                state.indices,
            ),
        )

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
    def _build_feasible_result(*, candidates: list[_PreparedCandidate], chain: _ChainState) -> WeekendPlanResult:
        """Формирует успешный `WeekendPlanResult` из найденной цепочки."""
        steps: list[WeekendPlanStep] = []
        total_min_cost = 0
        total_max_cost = 0

        for candidate_index, leg_travel in zip(chain.indices, chain.leg_travels, strict=True):
            item = candidates[candidate_index]
            step_min_cost = item.total_minimum_cost_rubles
            step_max_cost = item.total_maximum_cost_rubles
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

    async def _select_relaxed_chain(
        self,
        *,
        relaxed_constraints: tuple[str, ...],
        request: WeekendPlanRequest,
        candidates: list[_PreparedCandidate],
        window_start: datetime,
        window_end: datetime,
        full_day_start: datetime,
        full_day_end: datetime,
        relaxed_budget: int,
        get_travel_minutes: Callable[[int, int], Awaitable[int]],
    ) -> _ChainState | None:
        """Ищет цепочку при ослабленном наборе ограничений, минимизируя величину смягчения."""
        relaxed_set = frozenset(relaxed_constraints)
        chains = await self._collect_dp_chains(
            candidates=candidates,
            window_start=full_day_start if "time_window" in relaxed_set else window_start,
            window_end=full_day_end if "time_window" in relaxed_set else window_end,
            budget_rubles=relaxed_budget if "budget" in relaxed_set else request.budget_rubles,
            child_age=None if "age" in relaxed_set else request.child_age,
            maximum_travel_minutes=(
                _RELAXED_TRAVEL_LIMIT_MINUTES if "travel" in relaxed_set else request.maximum_travel_minutes
            ),
            get_travel_minutes=get_travel_minutes,
        )
        if not chains:
            return None

        def sort_key(state: _ChainState) -> tuple[int, int, int, int, int, int, tuple[int, ...]]:
            budget_excess = (
                max(0, state.total_minimum_cost_rubles - request.budget_rubles) if "budget" in relaxed_set else 0
            )
            if "time_window" in relaxed_set:
                early_seconds = max(0, int((window_start - candidates[state.indices[0]].starts_at).total_seconds()))
                late_seconds = max(0, int((candidates[state.indices[-1]].ends_at - window_end).total_seconds()))
                window_excess = early_seconds + late_seconds
            else:
                window_excess = 0
            travel_excess = (
                max(0, max(state.leg_travels) - request.maximum_travel_minutes) if "travel" in relaxed_set else 0
            )
            if "age" in relaxed_set:
                max_age = max(candidates[idx].event.age_limit for idx in state.indices)
                age_excess = max(0, max_age - (request.child_age or 0))
            else:
                age_excess = 0
            return (
                budget_excess,
                window_excess,
                travel_excess,
                age_excess,
                state.total_travel_minutes,
                -len(state.indices),
                state.indices,
            )

        return min(chains, key=sort_key)

    @staticmethod
    def _build_relaxation_suggestion(
        *,
        constraint_name: str,
        chain: _ChainState,
        request: WeekendPlanRequest,
        candidates: list[_PreparedCandidate],
        window_start: datetime,
        window_end: datetime,
        joint_with: tuple[str, ...] = (),
    ) -> ConstraintRelaxationSuggestion:
        """Формирует `ConstraintRelaxationSuggestion` с точным числовым ориентиром."""
        partner_labels = {
            "budget": "увеличением бюджета",
            "time_window": "расширением временного окна",
            "travel": "увеличением времени переезда",
            "age": "смягчением возрастного фильтра",
        }
        joint_suffix = ""
        if joint_with:
            joined = " и ".join(partner_labels[name] for name in joint_with)
            joint_suffix = f" (совместно с {joined})"

        if constraint_name == "age":
            needed_age = max(candidates[idx].event.age_limit for idx in chain.indices)
            explanation = (
                f"Возрастное ограничение {request.child_age}+ отсеяло часть подходящих мероприятий{joint_suffix}."
            )
            suggested_change = f"Ослабить возрастное ограничение минимум до {needed_age}+ или убрать детский фильтр."
        elif constraint_name == "time_window":
            needed_start = min(window_start, candidates[chain.indices[0]].starts_at).strftime("%H:%M")
            needed_end = max(window_end, candidates[chain.indices[-1]].ends_at).strftime("%H:%M")
            explanation = (
                f"Заданное окно {request.window_start_time:%H:%M}–{request.window_end_time:%H:%M} "
                f"не вмещает 2–4 непересекающихся события с учётом дороги{joint_suffix}."
            )
            suggested_change = f"Расширить свободное окно до {needed_start}–{needed_end}."
        elif constraint_name == "budget":
            explanation = (
                f"Общего бюджета {request.budget_rubles} ₽ на {request.companions_count} чел. "
                f"недостаточно для набора из 2–4 подходящих мероприятий{joint_suffix}."
            )
            suggested_change = f"Увеличить общий бюджет минимум до {chain.total_minimum_cost_rubles} ₽."
        else:
            max_leg = max(chain.leg_travels)
            explanation = (
                f"Ограничение на переезд ({request.maximum_travel_minutes} мин.) не позволяет "
                f"успеть между площадками выбранных мероприятий{joint_suffix}."
            )
            suggested_change = f"Увеличить допустимое время перемещения между площадками до {max_leg} мин."

        return ConstraintRelaxationSuggestion(
            constraint_name=constraint_name, explanation=explanation, suggested_change=suggested_change
        )

    async def _build_infeasible_result(
        self,
        *,
        request: WeekendPlanRequest,
        resolved_city: str,
        candidates: list[_PreparedCandidate],
        window_start: datetime,
        window_end: datetime,
        get_travel_minutes: Callable[[int, int], Awaitable[int]],
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

        full_day_start = datetime.combine(request.target_date, time(0, 0), tzinfo=BASHKORTOSTAN_TIMEZONE)
        full_day_end = datetime.combine(request.target_date, time(23, 59, 59), tzinfo=BASHKORTOSTAN_TIMEZONE)
        relaxed_budget = max(_RELAXED_BUDGET_RUBLES, sum(item.total_minimum_cost_rubles for item in candidates))

        eligibility: dict[str, bool] = {
            "age": request.child_age is not None,
            "time_window": window_start > full_day_start or window_end < full_day_end,
            "budget": request.budget_rubles < relaxed_budget,
            "travel": request.maximum_travel_minutes < _RELAXED_TRAVEL_LIMIT_MINUTES,
        }

        suggestions: list[ConstraintRelaxationSuggestion] = []
        for constraint_name in ("age", "time_window", "budget", "travel"):
            if not eligibility[constraint_name]:
                continue
            chain = await self._select_relaxed_chain(
                relaxed_constraints=(constraint_name,),
                request=request,
                candidates=candidates,
                window_start=window_start,
                window_end=window_end,
                full_day_start=full_day_start,
                full_day_end=full_day_end,
                relaxed_budget=relaxed_budget,
                get_travel_minutes=get_travel_minutes,
            )
            if chain is not None:
                suggestions.append(
                    self._build_relaxation_suggestion(
                        constraint_name=constraint_name,
                        chain=chain,
                        request=request,
                        candidates=candidates,
                        window_start=window_start,
                        window_end=window_end,
                    )
                )

        if not suggestions:
            eligible_for_combo = [name for name in ("budget", "time_window", "travel", "age") if eligibility[name]]
            for combo_size in range(2, len(eligible_for_combo) + 1):
                for combo in combinations(eligible_for_combo, combo_size):
                    chain = await self._select_relaxed_chain(
                        relaxed_constraints=combo,
                        request=request,
                        candidates=candidates,
                        window_start=window_start,
                        window_end=window_end,
                        full_day_start=full_day_start,
                        full_day_end=full_day_end,
                        relaxed_budget=relaxed_budget,
                        get_travel_minutes=get_travel_minutes,
                    )
                    if chain is not None:
                        for name in combo:
                            partners = tuple(other for other in combo if other != name)
                            suggestions.append(
                                self._build_relaxation_suggestion(
                                    constraint_name=name,
                                    chain=chain,
                                    request=request,
                                    candidates=candidates,
                                    window_start=window_start,
                                    window_end=window_end,
                                    joint_with=partners,
                                )
                            )
                        break
                if suggestions:
                    break

        if not suggestions:
            formatted_date = request.target_date.strftime("%d.%m.%Y")
            suggestions.append(
                ConstraintRelaxationSuggestion(
                    constraint_name="count",
                    explanation=(
                        f"Доступные мероприятия ({len(candidates)} шт.) в городе {resolved_city} на {formatted_date} "
                        "пересекаются по времени или разделены слишком длительным переездом."
                    ),
                    suggested_change="Выберите другую дату (например, ближайшие выходные) или соседний город.",
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
