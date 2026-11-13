"""Неизменяемые доменные сущности геолокации и планировщика «Выходной день»."""

from dataclasses import dataclass
from datetime import date, datetime, time
from uuid import UUID

from src.core.types import EventCategory

__all__ = [
    "ConstraintRelaxationSuggestion",
    "Coordinates",
    "TravelEstimate",
    "WeekendPlanRequest",
    "WeekendPlanResult",
    "WeekendPlanStep",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class Coordinates:
    """Географические координаты площадки или центра населённого пункта.

    Attributes:
        latitude: Географическая широта в десятичных градусах.
        longitude: Географическая долгота в десятичных градусах.
    """

    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True, kw_only=True)
class TravelEstimate:
    """Оценка расстояния и времени перемещения между двумя точками.

    Attributes:
        distance_kilometers: Расстояние по прямой в километрах.
        travel_minutes: Оценочное время в пути в минутах с учётом посадки и пересадки.
        used_external_service: Признак того, что расчёт получен от внешнего геосервиса.
    """

    distance_kilometers: float
    travel_minutes: int
    used_external_service: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class WeekendPlanRequest:
    """Запрос на построение маршрута выходного дня без пересечений по времени.

    Attributes:
        city: Город или населённый пункт Республики Башкортостан.
        target_date: Целевая дата посещения мероприятий.
        window_start_time: Время начала свободного окна.
        window_end_time: Время окончания свободного окна.
        budget_rubles: Максимальный общий бюджет на весь состав участников в рублях.
        companions_count: Количество участников.
        child_age: Возраст ребёнка в годах, если маршрут планируется с ребёнком.
        maximum_travel_minutes: Допустимое время перемещения между соседними площадками.
    """

    city: str
    target_date: date
    window_start_time: time
    window_end_time: time
    budget_rubles: int
    companions_count: int = 1
    child_age: int | None = None
    maximum_travel_minutes: int = 20


@dataclass(frozen=True, slots=True, kw_only=True)
class WeekendPlanStep:
    """Отдельное мероприятие в составе маршрута «Выходной день».

    Attributes:
        event_identifier: Уникальный идентификатор мероприятия в каталоге.
        title: Название мероприятия.
        category: Категория мероприятия.
        venue: Название площадки проведения.
        address: Адрес площадки.
        starts_at: Время начала мероприятия.
        ends_at: Расчётное или точное время окончания мероприятия.
        price_minimum_rubles: Минимальная стоимость одного билета в рублях.
        price_maximum_rubles: Максимальная стоимость одного билета в рублях.
        total_cost_rubles: Оценочная минимальная стоимость на всех участников в рублях.
        travel_from_previous_minutes: Время в пути от предыдущей точки маршрута в минутах.
    """

    event_identifier: UUID
    title: str
    category: EventCategory
    venue: str
    address: str
    starts_at: datetime
    ends_at: datetime
    price_minimum_rubles: int
    price_maximum_rubles: int
    total_cost_rubles: int
    travel_from_previous_minutes: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class ConstraintRelaxationSuggestion:
    """Предложение минимального ослабления ограничения при невозможности составить маршрут.

    Attributes:
        constraint_name: Название конфликтующего ограничения (`budget`, `time_window`, `travel`, `age`, `count`).
        explanation: Понятное жителю объяснение причины невозможности подбора.
        suggested_change: Минимальное изменение параметра, при котором маршрут собирается.
    """

    constraint_name: str
    explanation: str
    suggested_change: str


@dataclass(frozen=True, slots=True, kw_only=True)
class WeekendPlanResult:
    """Результат работы планировщика «Выходной день».

    Attributes:
        is_feasible: Признак успешного построения маршрута из 2–4 событий.
        steps: Упорядоченная по времени последовательность шагов маршрута.
        total_minimum_cost_rubles: Суммарная минимальная стоимость маршрута на всех участников.
        total_maximum_cost_rubles: Суммарная максимальная стоимость маршрута на всех участников.
        total_travel_minutes: Суммарное время на перемещения между площадками в минутах.
        failure_reason: Причина отсутствия маршрута, если `is_feasible` равно `False`.
        relaxation_suggestions: Список предложений минимального смягчения ограничений.
    """

    is_feasible: bool
    steps: tuple[WeekendPlanStep, ...] = ()
    total_minimum_cost_rubles: int = 0
    total_maximum_cost_rubles: int = 0
    total_travel_minutes: int = 0
    failure_reason: str | None = None
    relaxation_suggestions: tuple[ConstraintRelaxationSuggestion, ...] = ()

    def __post_init__(self) -> None:
        """Фиксирует коллекции шагов и предложений в виде неизменяемых кортежей."""
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "relaxation_suggestions", tuple(self.relaxation_suggestions))
