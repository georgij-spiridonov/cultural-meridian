"""Контекст выполнения и 7 инструментов OpenAI Agents SDK для работы с каталогом событий."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from uuid import UUID

from agents import RunContextWrapper, Tool, function_tool

from src.core.exceptions import DomainException
from src.core.types import EventCategory
from src.domain.entities import (
    AgentToolInvocation,
    Event,
    EventSearchFilter,
    PendingAction,
    WeekendPlanRequest,
    WeekendPlanResult,
)
from src.domain.ports import AgentToolCallbacks
from src.domain.services.sanitization import sanitize_control_characters, wrap_untrusted_event_data

__all__ = [
    "AgentRunContext",
    "build_agent_tools",
    "find_similar",
    "get_event",
    "get_venue_schedule",
    "plan_weekend",
    "prepare_feedback",
    "prepare_subscribe",
    "search_events",
]

_MAX_ALLOWED_STEPS = 5


@dataclass(slots=True, kw_only=True)
class AgentRunContext:
    """Мутабельный контекст одного диалогового хода агента.

    Attributes:
        callbacks: Набор прикладных обработчиков инструментов.
        maximum_steps: Максимальное число вызовов инструментов за ход (от 1 до 5).
        tool_calls_count: Счётчик выполненных вызовов инструментов.
        seen_events: Словарь подтверждённых карточек событий, полученных из инструментов.
        invocations: Журнал вызовов инструментов за текущий ход.
        pending_action: Созданное отложенное действие, ожидающее подтверждения пользователя.
    """

    callbacks: AgentToolCallbacks
    maximum_steps: int = _MAX_ALLOWED_STEPS
    tool_calls_count: int = 0
    seen_events: dict[UUID, Event] = field(default_factory=dict)
    invocations: list[AgentToolInvocation] = field(default_factory=list)
    pending_action: PendingAction | None = None

    def __post_init__(self) -> None:
        """Ограничивает допустимый лимит шагов инструментов диапазоном 1..5."""
        self.maximum_steps = max(1, min(self.maximum_steps, _MAX_ALLOWED_STEPS))

    @property
    def allowed_event_ids(self) -> frozenset[UUID]:
        """Возвращает неизменяемое множество подтверждённых идентификаторов событий."""
        return frozenset(self.seen_events.keys())

    def try_consume_step(self, *, tool_name: str, arguments: Mapping[str, object]) -> str | None:
        """Проверяет и резервирует один шаг вызова инструмента.

        Arguments:
            tool_name: Имя вызываемого инструмента.
            arguments: Аргументы вызова для фиксации в трассе при отказе.

        Returns:
            JSON-строку с ошибкой, если лимит шагов исчерпан, иначе `None`.
        """
        if self.tool_calls_count >= self.maximum_steps:
            error_message = f"Превышен лимит вызовов инструментов ({self.maximum_steps})."
            self.invocations.append(
                AgentToolInvocation(
                    tool_name=tool_name,
                    arguments=arguments,
                    returned_event_identifiers=(),
                    succeeded=False,
                    error_message=error_message,
                )
            )
            return _dumps_json(
                {"error": error_message, "hint": "Сформируйте ответ пользователю на основе уже полученных данных."}
            )
        self.tool_calls_count += 1
        return None

    def record_events(self, events: Iterable[Event]) -> tuple[UUID, ...]:
        """Сохраняет карточки событий в `seen_events` и возвращает их идентификаторы."""
        identifiers: list[UUID] = []
        for event in events:
            self.seen_events[event.identifier] = event
            identifiers.append(event.identifier)
        return tuple(identifiers)

    def record_success(
        self, *, tool_name: str, arguments: Mapping[str, object], event_identifiers: tuple[UUID, ...] = ()
    ) -> None:
        """Фиксирует успешный вызов инструмента в журнале трассировки."""
        self.invocations.append(
            AgentToolInvocation(
                tool_name=tool_name, arguments=arguments, returned_event_identifiers=event_identifiers, succeeded=True
            )
        )

    def record_failure(self, *, tool_name: str, arguments: Mapping[str, object], error_message: str) -> None:
        """Фиксирует неуспешный вызов инструмента в журнале трассировки."""
        self.invocations.append(
            AgentToolInvocation(
                tool_name=tool_name,
                arguments=arguments,
                returned_event_identifiers=(),
                succeeded=False,
                error_message=error_message,
            )
        )


def _dumps_json(payload: object) -> str:
    """Сериализует ответ инструмента в JSON с поддержкой кириллицы."""
    return json.dumps(payload, ensure_ascii=False)


def _serialize_event_for_tool(event: Event) -> dict[str, object]:
    """Преобразует карточку события в безопасный JSON-словарь с изоляцией недоверенных текстов."""
    return {
        "identifier": str(event.identifier),
        "title": wrap_untrusted_event_data(event.title, maximum_length=300),
        "description": wrap_untrusted_event_data(event.description, maximum_length=1000) if event.description else "",
        "category": event.category.value,
        "tags": list(event.tags),
        "date_start": event.date_start.isoformat(),
        "date_end": event.date_end.isoformat() if event.date_end is not None else None,
        "duration_minutes": event.duration_minutes,
        "city": sanitize_control_characters(event.city, maximum_length=120),
        "venue": wrap_untrusted_event_data(event.venue, maximum_length=200),
        "address": sanitize_control_characters(event.address, maximum_length=300),
        "price_minimum": event.price_minimum,
        "price_maximum": event.price_maximum,
        "is_free": event.is_free,
        "age_limit": event.age_limit,
        "language": event.language,
        "status": event.status.value,
    }


def _parse_optional_uuid(raw_value: str | None, *, field_name: str) -> UUID | None:
    """Разбирает необязательный UUID из строкового аргумента инструмента."""
    if raw_value is None or not raw_value.strip():
        return None
    try:
        return UUID(raw_value.strip())
    except ValueError as error:
        raise ValueError(f"Некорректный UUID в параметре '{field_name}': {raw_value!r}.") from error


def _parse_required_uuid(raw_value: str, *, field_name: str) -> UUID:
    """Разбирает обязательный UUID из строкового аргумента инструмента."""
    parsed = _parse_optional_uuid(raw_value, field_name=field_name)
    if parsed is None:
        raise ValueError(f"Параметр '{field_name}' обязателен и должен содержать UUID.")
    return parsed


def _parse_optional_category(raw_value: str | None) -> EventCategory | None:
    """Разбирает необязательную категорию события."""
    if raw_value is None or not raw_value.strip():
        return None
    normalized = raw_value.strip().lower()
    try:
        return EventCategory(normalized)
    except ValueError as error:
        allowed = ", ".join(item.value for item in EventCategory)
        raise ValueError(f"Неизвестная категория {raw_value!r}. Допустимые значения: {allowed}.") from error


def _parse_optional_datetime(raw_value: str | None, *, field_name: str) -> datetime | None:
    """Разбирает необязательные дату и время в формате ISO 8601."""
    if raw_value is None or not raw_value.strip():
        return None
    try:
        return datetime.fromisoformat(raw_value.strip())
    except ValueError as error:
        raise ValueError(f"Некорректный формат даты/времени ISO в '{field_name}': {raw_value!r}.") from error


def _parse_optional_time(raw_value: str | None, *, field_name: str) -> time | None:
    """Разбирает необязательное время в формате HH:MM или HH:MM:SS."""
    if raw_value is None or not raw_value.strip():
        return None
    try:
        return time.fromisoformat(raw_value.strip())
    except ValueError as error:
        raise ValueError(f"Некорректный формат времени в '{field_name}': {raw_value!r}.") from error


def _parse_required_date(raw_value: str, *, field_name: str) -> date:
    """Разбирает обязательную дату в формате YYYY-MM-DD."""
    try:
        return date.fromisoformat(raw_value.strip())
    except ValueError as error:
        raise ValueError(f"Некорректный формат даты YYYY-MM-DD в '{field_name}': {raw_value!r}.") from error


def _parse_required_time(raw_value: str, *, field_name: str) -> time:
    """Разбирает обязательное время в формате HH:MM."""
    parsed = _parse_optional_time(raw_value, field_name=field_name)
    if parsed is None:
        raise ValueError(f"Параметр '{field_name}' обязателен и должен содержать время HH:MM.")
    return parsed


@function_tool
async def search_events(
    ctx: RunContextWrapper[AgentRunContext],
    query: str | None = None,
    city: str | None = None,
    category: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
    maximum_price: int | None = None,
    only_free: bool = False,
    maximum_age_limit: int | None = None,
    maximum_duration_minutes: int | None = None,
    language: str | None = None,
    near_center: bool = False,
    venue: str | None = None,
    page: int = 1,
    page_size: int = 5,
) -> str:
    """Выполняет поиск культурных мероприятий по фильтрам каталога.

    Arguments:
        ctx: Контекст выполнения агента.
        query: Свободный текстовый запрос (название, жанр, теги).
        city: Город или населённый пункт Республики Башкортостан.
        category: Категория (`concert`, `spectacle`, `exhibition`, `festival`, `cinema`, `excursion`, `other`).
        date_from: Нижняя граница даты и времени начала в формате ISO 8601.
        date_to: Верхняя граница даты и времени начала в формате ISO 8601.
        time_from: Минимальное время начала в течение дня (`HH:MM`).
        time_to: Максимальное время начала в течение дня (`HH:MM`).
        maximum_price: Максимальная цена билета в рублях.
        only_free: Показывать только бесплатные мероприятия.
        maximum_age_limit: Максимально допустимый возрастной ценз (0, 6, 12, 16, 18).
        maximum_duration_minutes: Максимальная длительность мероприятия в минутах.
        language: Язык проведения мероприятия.
        near_center: Приоритет мероприятий рядом с центром города.
        venue: Название площадки проведения.
        page: Номер страницы выдачи (начиная с 1).
        page_size: Количество карточек на странице (до 5).

    Returns:
        JSON-строку со списком найденных мероприятий и пояснением при пустой выдаче.
    """
    arguments: dict[str, object] = {
        "query": query,
        "city": city,
        "category": category,
        "date_from": date_from,
        "date_to": date_to,
        "time_from": time_from,
        "time_to": time_to,
        "maximum_price": maximum_price,
        "only_free": only_free,
        "maximum_age_limit": maximum_age_limit,
        "maximum_duration_minutes": maximum_duration_minutes,
        "language": language,
        "near_center": near_center,
        "venue": venue,
        "page": page,
        "page_size": page_size,
    }
    if (limit_error := ctx.context.try_consume_step(tool_name="search_events", arguments=arguments)) is not None:
        return limit_error

    try:
        search_filter = EventSearchFilter(
            query=query,
            city=city,
            category=_parse_optional_category(category),
            date_from=_parse_optional_datetime(date_from, field_name="date_from"),
            date_to=_parse_optional_datetime(date_to, field_name="date_to"),
            time_from=_parse_optional_time(time_from, field_name="time_from"),
            time_to=_parse_optional_time(time_to, field_name="time_to"),
            maximum_price=maximum_price,
            only_free=only_free,
            maximum_age_limit=maximum_age_limit,
            maximum_duration_minutes=maximum_duration_minutes,
            language=language,
            near_center=near_center,
            venue=venue,
            page=max(1, page),
            page_size=max(1, min(page_size, 5)),
        )
        result_page = await ctx.context.callbacks.search_events(search_filter)
    except (ValueError, DomainException) as error:
        ctx.context.record_failure(tool_name="search_events", arguments=arguments, error_message=str(error))
        return _dumps_json({"error": str(error)})

    event_ids = ctx.context.record_events(result_page.items)
    ctx.context.record_success(tool_name="search_events", arguments=arguments, event_identifiers=event_ids)
    empty_info: dict[str, object] | None = None
    if result_page.empty_explanation is not None:
        empty_info = {
            "summary": result_page.empty_explanation.summary,
            "blocking_constraints": list(result_page.empty_explanation.blocking_constraints),
            "suggested_relaxations": list(result_page.empty_explanation.suggested_relaxations),
        }
    return _dumps_json(
        {
            "total_count": result_page.total_count,
            "page": result_page.page,
            "has_more": result_page.has_more,
            "items": [_serialize_event_for_tool(item) for item in result_page.items],
            "empty_explanation": empty_info,
        }
    )


@function_tool
async def get_event(ctx: RunContextWrapper[AgentRunContext], event_id: str) -> str:
    """Возвращает полную карточку мероприятия по его UUID.

    Arguments:
        ctx: Контекст выполнения агента.
        event_id: Идентификатор мероприятия в формате UUID.

    Returns:
        JSON-строку с полной карточкой мероприятия или сообщением об ошибке.
    """
    arguments: dict[str, object] = {"event_id": event_id}
    if (limit_error := ctx.context.try_consume_step(tool_name="get_event", arguments=arguments)) is not None:
        return limit_error

    try:
        parsed_uuid = _parse_required_uuid(event_id, field_name="event_id")
        event = await ctx.context.callbacks.get_event(parsed_uuid)
    except (ValueError, DomainException) as error:
        ctx.context.record_failure(tool_name="get_event", arguments=arguments, error_message=str(error))
        return _dumps_json({"error": str(error)})

    event_ids = ctx.context.record_events((event,))
    ctx.context.record_success(tool_name="get_event", arguments=arguments, event_identifiers=event_ids)
    return _dumps_json({"event": _serialize_event_for_tool(event)})


@function_tool
async def find_similar(
    ctx: RunContextWrapper[AgentRunContext],
    event_id: str,
    city: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only_free: bool = False,
    limit: int = 5,
) -> str:
    """Подбирает похожие культурные мероприятия относительно указанного события.

    Arguments:
        ctx: Контекст выполнения агента.
        event_id: Идентификатор опорного мероприятия в формате UUID.
        city: Необязательный фильтр по городу.
        date_from: Нижняя граница даты начала в формате ISO 8601.
        date_to: Верхняя граница даты начала в формате ISO 8601.
        only_free: Искать только бесплатные аналоги.
        limit: Максимальное число похожих карточек (до 5).

    Returns:
        JSON-строку со списком похожих мероприятий.
    """
    arguments: dict[str, object] = {
        "event_id": event_id,
        "city": city,
        "date_from": date_from,
        "date_to": date_to,
        "only_free": only_free,
        "limit": limit,
    }
    if (limit_error := ctx.context.try_consume_step(tool_name="find_similar", arguments=arguments)) is not None:
        return limit_error

    try:
        parsed_uuid = _parse_required_uuid(event_id, field_name="event_id")
        events = await ctx.context.callbacks.find_similar(
            parsed_uuid,
            city,
            _parse_optional_datetime(date_from, field_name="date_from"),
            _parse_optional_datetime(date_to, field_name="date_to"),
            only_free,
            max(1, min(limit, 5)),
        )
    except (ValueError, DomainException) as error:
        ctx.context.record_failure(tool_name="find_similar", arguments=arguments, error_message=str(error))
        return _dumps_json({"error": str(error)})

    event_ids = ctx.context.record_events(events)
    ctx.context.record_success(tool_name="find_similar", arguments=arguments, event_identifiers=event_ids)
    return _dumps_json({"items": [_serialize_event_for_tool(item) for item in events]})


@function_tool
async def get_venue_schedule(
    ctx: RunContextWrapper[AgentRunContext],
    venue: str,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 5,
) -> str:
    """Возвращает расписание мероприятий для указанной площадки.

    Arguments:
        ctx: Контекст выполнения агента.
        venue: Название площадки проведения.
        date_from: Нижняя граница даты начала в формате ISO 8601.
        date_to: Верхняя граница даты начала в формате ISO 8601.
        limit: Максимальное число мероприятий (до 10).

    Returns:
        JSON-строку с расписанием площадки.
    """
    arguments: dict[str, object] = {"venue": venue, "date_from": date_from, "date_to": date_to, "limit": limit}
    if (limit_error := ctx.context.try_consume_step(tool_name="get_venue_schedule", arguments=arguments)) is not None:
        return limit_error

    try:
        events = await ctx.context.callbacks.get_venue_schedule(
            venue,
            _parse_optional_datetime(date_from, field_name="date_from"),
            _parse_optional_datetime(date_to, field_name="date_to"),
            max(1, min(limit, 10)),
        )
    except (ValueError, DomainException) as error:
        ctx.context.record_failure(tool_name="get_venue_schedule", arguments=arguments, error_message=str(error))
        return _dumps_json({"error": str(error)})

    event_ids = ctx.context.record_events(events)
    ctx.context.record_success(tool_name="get_venue_schedule", arguments=arguments, event_identifiers=event_ids)
    return _dumps_json({"items": [_serialize_event_for_tool(item) for item in events]})


def _serialize_weekend_plan(plan_result: WeekendPlanResult) -> dict[str, object]:
    """Сериализует результат планировщика «Выходной день» с защитой текстовых полей."""
    return {
        "is_feasible": plan_result.is_feasible,
        "total_minimum_cost_rubles": plan_result.total_minimum_cost_rubles,
        "total_maximum_cost_rubles": plan_result.total_maximum_cost_rubles,
        "total_travel_minutes": plan_result.total_travel_minutes,
        "failure_reason": plan_result.failure_reason,
        "steps": [
            {
                "event_identifier": str(step.event_identifier),
                "title": wrap_untrusted_event_data(step.title, maximum_length=300),
                "category": step.category.value,
                "venue": wrap_untrusted_event_data(step.venue, maximum_length=200),
                "address": sanitize_control_characters(step.address, maximum_length=300),
                "starts_at": step.starts_at.isoformat(),
                "ends_at": step.ends_at.isoformat(),
                "price_minimum_rubles": step.price_minimum_rubles,
                "price_maximum_rubles": step.price_maximum_rubles,
                "total_cost_rubles": step.total_cost_rubles,
                "travel_from_previous_minutes": step.travel_from_previous_minutes,
            }
            for step in plan_result.steps
        ],
        "relaxation_suggestions": [
            {
                "constraint_name": suggestion.constraint_name,
                "explanation": suggestion.explanation,
                "suggested_change": suggestion.suggested_change,
            }
            for suggestion in plan_result.relaxation_suggestions
        ],
    }


@function_tool
async def plan_weekend(
    ctx: RunContextWrapper[AgentRunContext],
    city: str,
    target_date: str,
    window_start_time: str,
    window_end_time: str,
    budget_rubles: int,
    companions_count: int = 1,
    child_age: int | None = None,
    maximum_travel_minutes: int = 20,
) -> str:
    """Строит согласованный по времени и бюджету маршрут «Выходной день» из 2–4 мероприятий.

    Arguments:
        ctx: Контекст выполнения агента.
        city: Город или населённый пункт.
        target_date: Целевая дата в формате `YYYY-MM-DD`.
        window_start_time: Время начала свободного окна (`HH:MM`).
        window_end_time: Время окончания свободного окна (`HH:MM`).
        budget_rubles: Максимальный общий бюджет на всех участников в рублях.
        companions_count: Количество участников.
        child_age: Возраст ребёнка в годах, если планируется поход с ребёнком.
        maximum_travel_minutes: Максимальное время перемещения между площадками в минутах.

    Returns:
        JSON-строку с построенным маршрутом или предложениями по смягчению ограничений.
    """
    arguments: dict[str, object] = {
        "city": city,
        "target_date": target_date,
        "window_start_time": window_start_time,
        "window_end_time": window_end_time,
        "budget_rubles": budget_rubles,
        "companions_count": companions_count,
        "child_age": child_age,
        "maximum_travel_minutes": maximum_travel_minutes,
    }
    if (limit_error := ctx.context.try_consume_step(tool_name="plan_weekend", arguments=arguments)) is not None:
        return limit_error

    try:
        request = WeekendPlanRequest(
            city=city,
            target_date=_parse_required_date(target_date, field_name="target_date"),
            window_start_time=_parse_required_time(window_start_time, field_name="window_start_time"),
            window_end_time=_parse_required_time(window_end_time, field_name="window_end_time"),
            budget_rubles=budget_rubles,
            companions_count=companions_count,
            child_age=child_age,
            maximum_travel_minutes=maximum_travel_minutes,
        )
        plan_result = await ctx.context.callbacks.plan_weekend(request)
    except (ValueError, DomainException) as error:
        ctx.context.record_failure(tool_name="plan_weekend", arguments=arguments, error_message=str(error))
        return _dumps_json({"error": str(error)})

    step_ids: list[UUID] = []
    for step in plan_result.steps:
        step_ids.append(step.event_identifier)
        if step.event_identifier not in ctx.context.seen_events:
            try:
                loaded_event = await ctx.context.callbacks.get_event(step.event_identifier)
            except DomainException:
                continue
            ctx.context.seen_events[loaded_event.identifier] = loaded_event

    ctx.context.record_success(tool_name="plan_weekend", arguments=arguments, event_identifiers=tuple(step_ids))
    return _dumps_json(_serialize_weekend_plan(plan_result))


@function_tool
async def prepare_subscribe(
    ctx: RunContextWrapper[AgentRunContext], city: str | None = None, category: str | None = None
) -> str:
    """Создаёт запрос на подтверждение подписки на уведомления о новых мероприятиях.

    Arguments:
        ctx: Контекст выполнения агента.
        city: Город для фильтрации подписки.
        category: Категория (`concert`, `spectacle`, `exhibition`, `festival`, `cinema`, `excursion`, `other`).

    Returns:
        JSON-строку с текстом запроса подтверждения для пользователя.
    """
    arguments: dict[str, object] = {"city": city, "category": category}
    if (limit_error := ctx.context.try_consume_step(tool_name="prepare_subscribe", arguments=arguments)) is not None:
        return limit_error

    try:
        parsed_category = _parse_optional_category(category)
        pending_action = await ctx.context.callbacks.prepare_subscribe(city, parsed_category)
    except (ValueError, DomainException) as error:
        ctx.context.record_failure(tool_name="prepare_subscribe", arguments=arguments, error_message=str(error))
        return _dumps_json({"error": str(error)})

    ctx.context.pending_action = pending_action
    ctx.context.record_success(tool_name="prepare_subscribe", arguments=arguments)
    return _dumps_json(
        {
            "status": "pending_confirmation",
            "action_type": pending_action.action_type,
            "confirmation_prompt": sanitize_control_characters(pending_action.confirmation_prompt, maximum_length=500),
        }
    )


@function_tool
async def prepare_feedback(
    ctx: RunContextWrapper[AgentRunContext],
    message_text: str,
    event_id: str | None = None,
    contact_info: str | None = None,
) -> str:
    """Создаёт запрос на подтверждение отправки сообщения организатору или модератору.

    Arguments:
        ctx: Контекст выполнения агента.
        message_text: Текст обращения или отзыва пользователя.
        event_id: Необязательный UUID мероприятия, к которому относится обращение.
        contact_info: Необязательная контактная информация (не передаётся в логи).

    Returns:
        JSON-строку с текстом запроса подтверждения для пользователя.
    """
    arguments: dict[str, object] = {"event_id": event_id, "message_text_length": len(message_text)}
    if (limit_error := ctx.context.try_consume_step(tool_name="prepare_feedback", arguments=arguments)) is not None:
        return limit_error

    try:
        parsed_uuid = _parse_optional_uuid(event_id, field_name="event_id")
        pending_action = await ctx.context.callbacks.prepare_feedback(parsed_uuid, message_text, contact_info)
    except (ValueError, DomainException) as error:
        ctx.context.record_failure(tool_name="prepare_feedback", arguments=arguments, error_message=str(error))
        return _dumps_json({"error": str(error)})

    ctx.context.pending_action = pending_action
    ctx.context.record_success(
        tool_name="prepare_feedback",
        arguments=arguments,
        event_identifiers=(parsed_uuid,) if parsed_uuid is not None else (),
    )
    return _dumps_json(
        {
            "status": "pending_confirmation",
            "action_type": pending_action.action_type,
            "confirmation_prompt": sanitize_control_characters(pending_action.confirmation_prompt, maximum_length=500),
        }
    )


def build_agent_tools() -> list[Tool]:
    """Возвращает полный список из 7 инструментов ИИ-агента в соответствии с контрактом."""
    return [
        search_events,
        get_event,
        find_similar,
        get_venue_schedule,
        plan_weekend,
        prepare_subscribe,
        prepare_feedback,
    ]
