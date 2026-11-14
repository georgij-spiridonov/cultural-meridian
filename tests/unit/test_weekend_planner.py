"""Модульные тесты прикладного планировщика маршрута выходного дня `WeekendPlannerService`."""

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest

from src.application.weekend_planner import WeekendPlannerService
from src.core.exceptions import NetworkConnectionTimeoutError
from src.core.types import EventCategory, EventStatus, RealCollection, SourceType
from src.domain.entities import (
    CityReference,
    Coordinates,
    Event,
    EventSearchFilter,
    EventSearchPage,
    TravelEstimate,
    VenueReference,
    WeekendPlanRequest,
)
from src.domain.exceptions import InvalidWeekendPlanRequestError
from src.domain.services import BASHKORTOSTAN_TIMEZONE


class _PlannerEventRepositoryStub:
    """Асинхронный стаб `EventRepositoryPort` для модульных тестов планировщика."""

    def __init__(self, events: Sequence[Event], *, cities: Sequence[CityReference] = ()) -> None:
        """Инициализирует стаб списком мероприятий и справочником городов."""
        self._events = list(events)
        self._cities = tuple(cities)

    async def search_events(
        self, *, search_filter: EventSearchFilter, statuses: RealCollection[EventStatus] = (EventStatus.PUBLISHED,)
    ) -> EventSearchPage:
        """Возвращает опубликованные мероприятия, подходящие под город и интервал дат."""
        filtered = [
            event
            for event in self._events
            if event.status in statuses
            and (search_filter.city is None or event.city.lower() == search_filter.city.lower())
            and (search_filter.date_from is None or event.date_start >= search_filter.date_from)
            and (search_filter.date_to is None or event.date_start <= search_filter.date_to)
        ]
        filtered.sort(key=lambda item: item.date_start)
        return EventSearchPage(
            items=tuple(filtered), total_count=len(filtered), page=1, page_size=len(filtered) or 1, has_more=False
        )

    async def list_cities(self) -> tuple[CityReference, ...]:
        """Возвращает справочник городов."""
        return self._cities

    async def list_venues(self, *, city: str | None = None) -> tuple[VenueReference, ...]:
        """Возвращает пустой справочник площадок."""
        del city
        return ()

    async def get_event_by_identifier(self, *, event_identifier: UUID, include_non_published: bool = True) -> Event:
        """Не используется в тестах планировщика."""
        del event_identifier, include_non_published
        raise NotImplementedError


class _StubGeolocationResolver:
    """Стаб `GeolocationResolverPort` с настраиваемым временем в пути или имитацией сбоя."""

    def __init__(self, *, travel_minutes: int = 15, fail_coordinates: bool = False, fail_travel: bool = False) -> None:
        """Сохраняет параметры поведения стаба."""
        self._travel_minutes = travel_minutes
        self._fail_coordinates = fail_coordinates
        self._fail_travel = fail_travel

    async def resolve_venue_coordinates(self, *, city: str, venue: str, address: str) -> Coordinates | None:
        """Возвращает координаты или выбрасывает сетевое исключение."""
        del city, venue, address
        if self._fail_coordinates:
            raise NetworkConnectionTimeoutError("Геокодер не ответил вовремя.")
        return Coordinates(latitude=54.7388, longitude=55.9721)

    async def estimate_travel(self, *, origin: Coordinates, destination: Coordinates) -> TravelEstimate:
        """Возвращает оценку перемещения или выбрасывает ошибку."""
        del origin, destination
        if self._fail_travel:
            raise RuntimeError("Сбой расчёта маршрута")
        return TravelEstimate(distance_kilometers=3.0, travel_minutes=self._travel_minutes)


def _make_day_event(
    *,
    title: str,
    start_hour: int,
    start_minute: int = 0,
    duration_minutes: int = 60,
    price_minimum: int = 300,
    price_maximum: int | None = None,
    is_free: bool = False,
    age_limit: int = 0,
    venue: str = "Площадка А",
    latitude: float | None = None,
    longitude: float | None = None,
) -> Event:
    """Создаёт опубликованное событие на фиксированную субботу 2026-11-21."""
    start = datetime(2026, 11, 21, start_hour, start_minute, tzinfo=BASHKORTOSTAN_TIMEZONE)
    return Event(
        identifier=uuid4(),
        title=title,
        normalized_title=title.lower(),
        description=f"Описание: {title}",
        category=EventCategory.EXHIBITION,
        tags=("выходные",),
        date_start=start,
        date_end=start + timedelta(minutes=duration_minutes),
        duration_minutes=duration_minutes,
        city="Уфа",
        venue=venue,
        address=f"ул. Тестовая, {start_hour}",
        latitude=latitude,
        longitude=longitude,
        price_minimum=0 if is_free else price_minimum,
        price_maximum=0 if is_free else (price_maximum if price_maximum is not None else price_minimum),
        is_free=is_free,
        age_limit=age_limit,
        language="русский",
        source_type=SourceType.MANUAL,
        source_url="https://example.org/event",
        source_hash=f"hash-{uuid4().hex}",
        deduplication_key=f"dedup-{uuid4().hex}",
        status=EventStatus.PUBLISHED,
        created_at=start - timedelta(days=1),
        updated_at=start - timedelta(days=1),
    )


async def test_weekend_planner_builds_non_overlapping_route_with_travel_and_companions() -> None:
    """Проверяет сборку маршрута 2–4 событий без пересечений с учётом переезда и числа участников."""
    event_morning = _make_day_event(
        title="Утренняя выставка", start_hour=10, duration_minutes=60, price_minimum=200, venue="Музей"
    )
    # Пересекается с дорогой (11:00 + 20 мин > 11:10)
    event_too_soon = _make_day_event(
        title="Слишком ранний спектакль",
        start_hour=11,
        start_minute=10,
        duration_minutes=60,
        price_minimum=100,
        venue="Театр",
    )
    event_afternoon = _make_day_event(
        title="Дневной концерт", start_hour=12, duration_minutes=90, price_minimum=300, venue="Филармония"
    )
    event_evening = _make_day_event(
        title="Вечерний кинопоказ", start_hour=15, duration_minutes=90, price_minimum=250, venue="Кинотеатр"
    )

    repo = _PlannerEventRepositoryStub(
        [event_morning, event_too_soon, event_afternoon, event_evening],
        cities=(CityReference(name="Уфа", normalized_name="уфа"),),
    )
    geo = _StubGeolocationResolver(travel_minutes=20)
    planner = WeekendPlannerService(event_repository=repo, geolocation_resolver=geo)

    request = WeekendPlanRequest(
        city="Уфа",
        target_date=date(2026, 11, 21),
        window_start_time=time(9, 30),
        window_end_time=time(18, 0),
        budget_rubles=3000,
        companions_count=3,
        maximum_travel_minutes=25,
    )
    result = await planner.plan_weekend(request)

    assert result.is_feasible is True
    assert 2 <= len(result.steps) <= 4
    titles = [step.title for step in result.steps]
    assert "Слишком ранний спектакль" not in titles
    assert titles == ["Утренняя выставка", "Дневной концерт", "Вечерний кинопоказ"]

    # Проверяем перемножение стоимости на companions_count=3
    assert [step.total_cost_rubles for step in result.steps] == [600, 900, 750]
    assert result.total_minimum_cost_rubles == 2250
    assert result.total_travel_minutes == 40
    assert result.steps[0].travel_from_previous_minutes == 0
    assert result.steps[1].travel_from_previous_minutes == 20
    assert result.steps[2].travel_from_previous_minutes == 20


async def test_weekend_planner_filters_by_child_age() -> None:
    """Проверяет фильтрацию мероприятий по допустимому возрасту ребёнка (`child_age`)."""
    kids_event_1 = _make_day_event(title="Сказка 0+", start_hour=10, age_limit=0, venue="Кукольный театр")
    adult_event = _make_day_event(title="Ночной стендап 18+", start_hour=12, age_limit=18, venue="Клуб")
    kids_event_2 = _make_day_event(title="Планетарий 6+", start_hour=14, age_limit=6, venue="Планетарий")

    repo = _PlannerEventRepositoryStub([kids_event_1, adult_event, kids_event_2])
    planner = WeekendPlannerService(event_repository=repo)

    result = await planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 21),
            window_start_time=time(9, 0),
            window_end_time=time(18, 0),
            budget_rubles=5000,
            child_age=7,
        )
    )

    assert result.is_feasible is True
    assert [step.title for step in result.steps] == ["Сказка 0+", "Планетарий 6+"]


async def test_weekend_planner_relaxation_suggestions_on_tight_constraints() -> None:
    """Проверяет генерацию `ConstraintRelaxationSuggestion` при жёстком бюджете, окне и переезде."""
    event_a = _make_day_event(
        title="Выставка", start_hour=10, duration_minutes=60, price_minimum=1000, venue="Площадка 1"
    )
    event_b = _make_day_event(
        title="Концерт", start_hour=13, duration_minutes=60, price_minimum=1000, venue="Площадка 2"
    )
    repo = _PlannerEventRepositoryStub([event_a, event_b])

    # 1. Жёсткий бюджет
    planner = WeekendPlannerService(event_repository=repo)
    budget_res = await planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 21),
            window_start_time=time(9, 0),
            window_end_time=time(18, 0),
            budget_rubles=500,
        )
    )
    assert budget_res.is_feasible is False
    assert any(s.constraint_name == "budget" for s in budget_res.relaxation_suggestions)

    # 2. Жёсткое временное окно
    window_res = await planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 21),
            window_start_time=time(10, 0),
            window_end_time=time(12, 0),
            budget_rubles=5000,
        )
    )
    assert window_res.is_feasible is False
    assert any(s.constraint_name == "time_window" for s in window_res.relaxation_suggestions)

    # 3. Жёсткий лимит времени перемещения
    slow_geo = _StubGeolocationResolver(travel_minutes=45)
    travel_planner = WeekendPlannerService(event_repository=repo, geolocation_resolver=slow_geo)
    travel_res = await travel_planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 21),
            window_start_time=time(9, 0),
            window_end_time=time(18, 0),
            budget_rubles=5000,
            maximum_travel_minutes=10,
        )
    )
    assert travel_res.is_feasible is False
    assert any(s.constraint_name == "travel" for s in travel_res.relaxation_suggestions)


async def test_weekend_planner_validates_invalid_requests() -> None:
    """Проверяет выброс `InvalidWeekendPlanRequestError` при некорректных параметрах запроса."""
    planner = WeekendPlannerService(event_repository=_PlannerEventRepositoryStub([]))
    base_kwargs = {
        "city": "Уфа",
        "target_date": date(2026, 11, 21),
        "window_start_time": time(10, 0),
        "window_end_time": time(18, 0),
        "budget_rubles": 1000,
    }

    invalid_cases = [
        {**base_kwargs, "city": "   "},
        {**base_kwargs, "window_end_time": time(10, 0)},
        {**base_kwargs, "budget_rubles": -100},
        {**base_kwargs, "companions_count": 0},
        {**base_kwargs, "maximum_travel_minutes": -5},
        {**base_kwargs, "child_age": -1},
    ]

    for case in invalid_cases:
        with pytest.raises(InvalidWeekendPlanRequestError):
            await planner.plan_weekend(WeekendPlanRequest(**case))  # type: ignore[arg-type]


async def test_weekend_planner_resilient_to_geolocation_failure() -> None:
    """Проверяет устойчивость планировщика при сбое геокодера и расчёта времени в пути."""
    event_1 = _make_day_event(title="Событие 1", start_hour=10, duration_minutes=60, venue="Площадка 1")
    event_2 = _make_day_event(title="Событие 2", start_hour=12, duration_minutes=60, venue="Площадка 2")
    repo = _PlannerEventRepositoryStub([event_1, event_2])

    failing_geo = _StubGeolocationResolver(fail_coordinates=True, fail_travel=True)
    planner = WeekendPlannerService(event_repository=repo, geolocation_resolver=failing_geo)

    result = await planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 21),
            window_start_time=time(9, 0),
            window_end_time=time(18, 0),
            budget_rubles=5000,
            maximum_travel_minutes=20,
        )
    )

    assert result.is_feasible is True
    assert len(result.steps) == 2
    # Фоллбэк для одного города = 15 минут
    assert result.total_travel_minutes == 15
