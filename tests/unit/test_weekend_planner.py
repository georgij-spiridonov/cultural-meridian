"""Модульные тесты прикладного планировщика маршрута выходного дня `WeekendPlannerService`."""

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from src.application.weekend_planner import WeekendPlannerService
from src.core.configuration import PROJECT_ROOT, GeolocationSettings
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
from src.infrastructure.geolocation import HybridGeolocationResolver


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


def _build_real_geo_settings(*, external_enabled: bool = False) -> GeolocationSettings:
    return GeolocationSettings(
        _env_file=None,
        VENUES_CATALOG_PATH=PROJECT_ROOT / "configuration" / "venues_coordinates.yaml",
        EXTERNAL_ENABLED=external_enabled,
    )


async def test_weekend_planner_with_real_resolver_same_venue_and_zero_travel_limit() -> None:
    """Проверяет нулевое время переезда на одной площадке и работу при `maximum_travel_minutes=0`."""
    same_venue_morning = _make_day_event(
        title="Утренний показ",
        start_hour=10,
        duration_minutes=60,
        price_minimum=400,
        venue="Башкирский государственный театр оперы и балета",
    )
    same_venue_afternoon = _make_day_event(
        title="Дневной балет",
        start_hour=11,
        duration_minutes=90,
        price_minimum=500,
        venue="Башкирский государственный театр оперы и балета",
    )
    other_venue_event = _make_day_event(
        title="Удалённый концерт",
        start_hour=14,
        duration_minutes=60,
        price_minimum=100,
        venue="Городской дворец культуры",
    )

    repo = _PlannerEventRepositoryStub([same_venue_morning, same_venue_afternoon, other_venue_event])
    resolver = HybridGeolocationResolver(settings=_build_real_geo_settings())
    planner = WeekendPlannerService(event_repository=repo, geolocation_resolver=resolver)

    result = await planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 21),
            window_start_time=time(10, 0),
            window_end_time=time(18, 0),
            budget_rubles=2000,
            maximum_travel_minutes=0,
        )
    )

    assert result.is_feasible is True
    assert [step.title for step in result.steps] == ["Утренний показ", "Дневной балет"]
    assert result.total_travel_minutes == 0
    assert [step.travel_from_previous_minutes for step in result.steps] == [0, 0]


async def test_weekend_planner_with_real_resolver_two_unknown_venues_positive_travel() -> None:
    """Проверяет ненулевое время в пути (> 0 мин) между двумя разными неизвестными площадками."""
    event_a = _make_day_event(
        title="Встреча в клубе А", start_hour=10, duration_minutes=60, price_minimum=200, venue="Неизвестный клуб Альфа"
    )
    event_b = _make_day_event(
        title="Лекция в лофте Б", start_hour=12, duration_minutes=60, price_minimum=200, venue="Неизвестный лофт Бета"
    )

    repo = _PlannerEventRepositoryStub([event_a, event_b])
    resolver = HybridGeolocationResolver(settings=_build_real_geo_settings())
    planner = WeekendPlannerService(event_repository=repo, geolocation_resolver=resolver)

    result = await planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 21),
            window_start_time=time(10, 0),
            window_end_time=time(16, 0),
            budget_rubles=1000,
            maximum_travel_minutes=20,
        )
    )

    assert result.is_feasible is True
    assert len(result.steps) == 2
    assert result.steps[1].travel_from_previous_minutes > 0
    assert result.total_travel_minutes == result.steps[1].travel_from_previous_minutes


async def test_weekend_planner_with_real_resolver_close_ufa_center_vs_distant_venues() -> None:
    """Проверяет выбор близких площадок в центре Уфы при отсечении удалённых площадок."""
    # Центр Уфы: Оперный театр (54.722222, 55.945000) -> Национальный музей (54.722883, 55.942731) ~ 8 мин
    # Удалённая площадка: ГДК (54.799722, 56.033333) ~ 33 мин от центра
    center_opera = _make_day_event(
        title="Опера в центре", start_hour=10, duration_minutes=60, price_minimum=400, venue="Башопера"
    )
    center_museum = _make_day_event(
        title="Экспозиция в музее",
        start_hour=11,
        start_minute=30,
        duration_minutes=60,
        price_minimum=250,
        venue="Национальный музей РБ",
    )
    distant_gdk = _make_day_event(
        title="Концерт на окраине",
        start_hour=13,
        start_minute=15,
        duration_minutes=60,
        price_minimum=100,
        venue="ГДК Уфа",
    )

    repo = _PlannerEventRepositoryStub([center_opera, center_museum, distant_gdk])
    resolver = HybridGeolocationResolver(settings=_build_real_geo_settings())
    planner = WeekendPlannerService(event_repository=repo, geolocation_resolver=resolver)

    tight_travel_result = await planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 21),
            window_start_time=time(10, 0),
            window_end_time=time(18, 0),
            budget_rubles=3000,
            maximum_travel_minutes=15,
        )
    )

    assert tight_travel_result.is_feasible is True
    assert [step.title for step in tight_travel_result.steps] == ["Опера в центре", "Экспозиция в музее"]
    assert 0 < tight_travel_result.total_travel_minutes <= 15

    # При расширении лимита переезда удалённая площадка включается третьим шагом
    relaxed_travel_result = await planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 21),
            window_start_time=time(10, 0),
            window_end_time=time(18, 0),
            budget_rubles=3000,
            maximum_travel_minutes=45,
        )
    )
    assert relaxed_travel_result.is_feasible is True
    assert len(relaxed_travel_result.steps) == 3
    assert relaxed_travel_result.steps[2].travel_from_previous_minutes > 15


async def test_weekend_planner_with_real_resolver_external_service_failure_degradation() -> None:
    """Проверяет штатную сборку маршрута при отказе внешнего Nominatim/OSRM в реальном резолвере."""
    event_1 = _make_day_event(
        title="Утренний музей", start_hour=10, duration_minutes=60, price_minimum=200, venue="Музей Нестерова"
    )
    event_2 = _make_day_event(
        title="Дневной театр", start_hour=12, duration_minutes=60, price_minimum=300, venue="Театр «Нур»"
    )
    repo = _PlannerEventRepositoryStub([event_1, event_2])

    def failing_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Внешний геосервис не отвечает", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(failing_transport)) as client:
        resolver = HybridGeolocationResolver(
            settings=_build_real_geo_settings(external_enabled=True), http_client=client
        )
        planner = WeekendPlannerService(event_repository=repo, geolocation_resolver=resolver)

        result = await planner.plan_weekend(
            WeekendPlanRequest(
                city="Уфа",
                target_date=date(2026, 11, 21),
                window_start_time=time(9, 0),
                window_end_time=time(18, 0),
                budget_rubles=1500,
                maximum_travel_minutes=30,
            )
        )

    assert result.is_feasible is True
    assert len(result.steps) == 2
    assert result.total_travel_minutes > 0


async def test_weekend_planner_four_steps_and_exact_boundary_times() -> None:
    """Проверяет цепочку из 4 событий и строгие граничные времена окна и стыковки переезда."""
    # Между Башопера и Национальным музеем РБ расстояние ~0.2-0.3 км -> 8 мин по гаверсинусу (25 км/ч + 7 мин overhead).
    # Проверим точную стыковку на границе окна (10:00..16:00) и стыковку переезда.
    resolver = HybridGeolocationResolver(settings=_build_real_geo_settings())
    coords_a = await resolver.resolve_venue_coordinates(city="Уфа", venue="Башопера")
    coords_b = await resolver.resolve_venue_coordinates(city="Уфа", venue="Национальный музей РБ")
    assert coords_a is not None and coords_b is not None
    exact_leg_minutes = (await resolver.estimate_travel(origin=coords_a, destination=coords_b)).travel_minutes

    # Шаг 1: 10:00-11:00 (граница начала окна) на Башопера
    step_1 = _make_day_event(title="Шаг 1", start_hour=10, start_minute=0, duration_minutes=60, venue="Башопера")
    # Шаг 2: стартует ровно в 11:00 + exact_leg_minutes на Национальный музей РБ
    step_2_start = datetime(2026, 11, 21, 11, 0, tzinfo=BASHKORTOSTAN_TIMEZONE) + timedelta(minutes=exact_leg_minutes)
    step_2 = _make_day_event(
        title="Шаг 2",
        start_hour=step_2_start.hour,
        start_minute=step_2_start.minute,
        duration_minutes=50,
        venue="Национальный музей РБ",
    )
    # Шаг 3: на той же площадке (Национальный музей РБ), стартует ровно в 13:00 (переезд 0)
    step_3 = _make_day_event(
        title="Шаг 3", start_hour=13, start_minute=0, duration_minutes=60, venue="Национальный музей РБ"
    )
    # Шаг 4: заканчивается ровно в 16:00 (граница окончания окна), на той же площадке
    step_4 = _make_day_event(
        title="Шаг 4", start_hour=15, start_minute=0, duration_minutes=60, venue="Национальный музей РБ"
    )
    # Лишнее 5-е событие (дороже и на удалённой площадке) не должно раздуть длину маршрута сверх 4 шагов
    step_5 = _make_day_event(
        title="Лишний шаг 5", start_hour=16, start_minute=30, duration_minutes=30, price_minimum=2000, venue="ГДК Уфа"
    )

    repo = _PlannerEventRepositoryStub([step_1, step_2, step_3, step_4, step_5])
    planner = WeekendPlannerService(event_repository=repo, geolocation_resolver=resolver)

    result = await planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 21),
            window_start_time=time(10, 0),
            window_end_time=time(17, 30),
            budget_rubles=5000,
            maximum_travel_minutes=exact_leg_minutes,
        )
    )

    assert result.is_feasible is True
    assert len(result.steps) == 4
    assert [step.title for step in result.steps] == ["Шаг 1", "Шаг 2", "Шаг 3", "Шаг 4"]
    assert result.steps[1].travel_from_previous_minutes == exact_leg_minutes

    # Проверяем точную границу окончания окна: шаг 4 (до 16:00) не проходит при window_end_time=15:59
    tight_end_result = await planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 21),
            window_start_time=time(10, 0),
            window_end_time=time(15, 59),
            budget_rubles=5000,
            maximum_travel_minutes=exact_leg_minutes,
        )
    )
    assert tight_end_result.is_feasible is True
    assert [step.title for step in tight_end_result.steps] == ["Шаг 1", "Шаг 2", "Шаг 3"]

    # Если сдвинуть старт шага 2 на 1 минуту раньше, пара step_1 -> step_2 не стыкуется по дороге
    too_early_start = step_2_start - timedelta(minutes=1)
    too_early_step_2 = _make_day_event(
        title="Слишком ранний шаг 2",
        start_hour=too_early_start.hour,
        start_minute=too_early_start.minute,
        duration_minutes=50,
        venue="Национальный музей РБ",
    )
    boundary_repo = _PlannerEventRepositoryStub([step_1, too_early_step_2])
    boundary_planner = WeekendPlannerService(event_repository=boundary_repo, geolocation_resolver=resolver)
    boundary_result = await boundary_planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 21),
            window_start_time=time(10, 0),
            window_end_time=time(16, 0),
            budget_rubles=5000,
            maximum_travel_minutes=exact_leg_minutes,
        )
    )
    assert boundary_result.is_feasible is False


async def test_weekend_planner_combined_relaxation_and_insufficient_events_diagnostics() -> None:
    """Проверяет комбинированную диагностику релаксации и отказ при нехватке событий (`count`)."""
    early_expensive = _make_day_event(
        title="Ранний дорогой концерт",
        start_hour=9,
        duration_minutes=60,
        price_minimum=1200,
        age_limit=12,
        venue="Башопера",
    )
    late_expensive = _make_day_event(
        title="Вечерний дорогой спектакль",
        start_hour=19,
        duration_minutes=60,
        price_minimum=1300,
        age_limit=12,
        venue="Башопера",
    )

    resolver = HybridGeolocationResolver(settings=_build_real_geo_settings())

    # 1. Комбинированный конфликт: одновременно малы бюджет (500), окно (11..18) и возраст (6+)
    combo_repo = _PlannerEventRepositoryStub([early_expensive, late_expensive])
    combo_planner = WeekendPlannerService(event_repository=combo_repo, geolocation_resolver=resolver)
    combo_res = await combo_planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 21),
            window_start_time=time(11, 0),
            window_end_time=time(18, 0),
            budget_rubles=500,
            child_age=6,
            maximum_travel_minutes=20,
        )
    )
    assert combo_res.is_feasible is False
    suggested_names = {item.constraint_name for item in combo_res.relaxation_suggestions}
    assert {"budget", "time_window", "age"}.issubset(suggested_names)
    assert any("совместно с" in item.explanation for item in combo_res.relaxation_suggestions)

    # 2. Недостаточно событий на день (< 2) -> диагностика count
    single_repo = _PlannerEventRepositoryStub([early_expensive])
    single_planner = WeekendPlannerService(event_repository=single_repo, geolocation_resolver=resolver)
    count_res = await single_planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 21),
            window_start_time=time(8, 0),
            window_end_time=time(22, 0),
            budget_rubles=5000,
        )
    )
    assert count_res.is_feasible is False
    assert len(count_res.relaxation_suggestions) == 1
    assert count_res.relaxation_suggestions[0].constraint_name == "count"
