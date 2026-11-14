"""Детерминированный резервный поиск и планирование при недоступности языковой модели.

Архитектурное решение:
    Модуль обеспечивает полностью автономную работу диалогового сценария при
    отключении языковой модели, исчерпании суточного/диалогового бюджета или
    сетевых сбоях провайдера. Разбор свободного текста пользователя выполняется
    детерминированными регулярными выражениями и справочниками доменного слоя
    без обращения к внешним API и без импорта модулей `src/entrypoints`.
    Все ответы формируются строго по подтверждённым записям `EventRepositoryPort`
    и `WeekendPlannerService` без раскрытия технических идентификаторов UUID.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Final
from uuid import UUID

from src.application._helpers import attach_default_timezone
from src.application.weekend_planner import WeekendPlannerService
from src.core.exceptions import ApplicationException
from src.core.types import EventCategory, EventStatus
from src.domain.entities import (
    AgentTurnResult,
    CityReference,
    DialogSessionState,
    Event,
    EventSearchFilter,
    EventSortField,
    SortDirection,
    WeekendPlanRequest,
    WeekendPlanResult,
)
from src.domain.ports import EventRepositoryPort
from src.domain.services import (
    BASHKORTOSTAN_TIMEZONE,
    DEFAULT_BASHKORTOSTAN_CITIES,
    inspect_untrusted_text,
    normalize_city,
    normalize_text_for_matching,
    parse_category,
    parse_event_datetime,
    sanitize_control_characters,
)

__all__ = ["FallbackSearchService"]

_MAXIMUM_RECOMMENDED_EVENTS = 5
_DEFAULT_PLAN_WINDOW_START = time(10, 0)
_DEFAULT_PLAN_WINDOW_END = time(21, 0)
_DEFAULT_PLAN_BUDGET_RUBLES = 5000
_DEFAULT_PLAN_MAX_TRAVEL_MINUTES = 30

_CATEGORY_LABELS: Final[dict[EventCategory, str]] = {
    EventCategory.CONCERT: "концерт",
    EventCategory.SPECTACLE: "спектакль",
    EventCategory.EXHIBITION: "выставка",
    EventCategory.FESTIVAL: "фестиваль",
    EventCategory.CINEMA: "кино",
    EventCategory.EXCURSION: "экскурсия",
    EventCategory.OTHER: "событие",
}

_FREE_QUERY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:бесплатн\w*|даром|вход\s+свободн\w*|без\s+оплат\w*)", flags=re.IGNORECASE
)
_PRICE_CEILING_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:до|не\s+дороже|бюджет\s*(?:до)?|максимум)\s*(\d{1,6})\s*(?:₽|руб\.?|рублей|рубля|р\.?)?", flags=re.IGNORECASE
)
_CHILD_AGE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:реб[её]н\w*|дет\w*|сын\w*|доч\w*|малыш\w*)[^\d]{0,16}(\d{1,2})\s*(?:лет|года|год)", flags=re.IGNORECASE
)
_GENERIC_CHILD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:с\s+детьми|с\s+реб[её]нк\w*|для\s+детей|детск\w*|семейн\w*)", flags=re.IGNORECASE
)
_COMPANIONS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:на|для)\s*(\d{1,2})\s*(?:человек\w*|чел\.?|персон\w*)", flags=re.IGNORECASE
)
_PLAN_INTENT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:маршрут|спланируй\w*|составь\w*\s+план|план\s+(?:на\s+)?выходн\w*|выходной\s+день|программ\w*\s+на\s+день)",
    flags=re.IGNORECASE,
)
_CITY_PREPOSITION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)(?:в|во|по|из|г\.?|город)\s+([а-яёa-z-]{3,})", flags=re.IGNORECASE
)
_WEEKEND_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:на\s+выходн\w*|в\s+выходн\w*|выходн\w*\s+дн\w*|суббот\w*|воскресен\w*)", flags=re.IGNORECASE
)
_WEEK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:на\s+(?:этой\s+)?недел\w*|ближайш\w*\s+7\s+дн\w*|на\s+неделю)", flags=re.IGNORECASE
)

_STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        "в",
        "во",
        "на",
        "по",
        "с",
        "со",
        "для",
        "до",
        "от",
        "из",
        "и",
        "или",
        "а",
        "что",
        "где",
        "когда",
        "куда",
        "какие",
        "какой",
        "можно",
        "есть",
        "покажи",
        "найди",
        "подбери",
        "посоветуй",
        "хочу",
        "нужно",
        "сходить",
        "пойти",
        "посетить",
        "мероприятия",
        "мероприятие",
        "события",
        "событие",
        "афиша",
        "афишу",
        "сегодня",
        "завтра",
        "послезавтра",
        "выходные",
        "выходных",
        "выходной",
        "день",
        "субботу",
        "суббота",
        "воскресенье",
        "неделю",
        "неделе",
        "этой",
        "ближайшие",
        "бесплатно",
        "бесплатные",
        "бесплатный",
        "бесплатная",
        "бесплатное",
        "руб",
        "рублей",
        "рубля",
        "ребенком",
        "ребенка",
        "ребенку",
        "детей",
        "детьми",
        "лет",
        "года",
        "год",
        "маршрут",
        "спланируй",
        "составь",
        "план",
        "город",
        "городе",
        "концерт",
        "концерты",
        "спектакль",
        "спектакли",
        "театр",
        "выставка",
        "выставки",
        "музей",
        "фестиваль",
        "фестивали",
        "кино",
        "экскурсия",
        "экскурсии",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class _ParsedFallbackQuery:
    """Структурированные параметры, извлечённые детерминированным парсером."""

    city: str | None
    category: EventCategory | None
    only_free: bool
    maximum_price: int | None
    date_from: datetime | None
    date_to: datetime | None
    target_date: date | None
    child_age: int | None
    maximum_age_limit: int | None
    companions_count: int
    keywords_query: str | None
    is_weekend_plan_request: bool


class FallbackSearchService:
    """Прикладной сервис детерминированного резервного поиска без языковой модели."""

    def __init__(
        self, event_repository: EventRepositoryPort, weekend_planner: WeekendPlannerService | None = None
    ) -> None:
        """Инициализирует сервис резервного поиска.

        Arguments:
            event_repository: Порт каталога мероприятий и справочника городов.
            weekend_planner: Необязательный планировщик маршрута выходного дня.
        """
        self._event_repository = event_repository
        self._weekend_planner = weekend_planner

    async def handle_fallback(
        self,
        *,
        session_state: DialogSessionState,
        user_message: str,
        current_datetime: datetime,
        reason: str,
        trace_identifier: UUID | None = None,
        maximum_results: int = 5,
    ) -> AgentTurnResult:
        """Обрабатывает пользовательский запрос детерминированно при отказе или отключении модели.

        Arguments:
            session_state: Текущее состояние диалоговой сессии.
            user_message: Входящее сообщение пользователя.
            current_datetime: Текущее опорное время для разрешения относительных дат.
            reason: Причина перехода в резервный режим.
            trace_identifier: Необязательный идентификатор трассы диалога.
            maximum_results: Максимальное количество карточек в ответе (1..5).

        Returns:
            Итоговый результат ответа с `fallback_used=True` и `model_name="deterministic-fallback"`.
        """
        effective_limit = max(1, min(maximum_results, _MAXIMUM_RECOMMENDED_EVENTS))
        reference_now = attach_default_timezone(current_datetime)
        inspection = inspect_untrusted_text(user_message)

        if inspection.has_suspicious_instructions:
            return AgentTurnResult(
                answer_text=(
                    "Сейчас сервис работает в резервном режиме поиска по каталогу. "
                    "Пожалуйста, уточните город Башкортостана, дату и желаемую категорию мероприятий "
                    "(например: «бесплатные концерты в Уфе на выходных»)."
                ),
                clarifying_question="В каком городе и на какую дату подобрать культурные мероприятия?",
                fallback_used=True,
                fallback_reason=reason,
                trace_identifier=trace_identifier,
                model_name="deterministic-fallback",
            )

        cities = await self._event_repository.list_cities()
        parsed = self._parse_query(
            cleaned_message=inspection.cleaned_text,
            session_state=session_state,
            cities=cities,
            reference_now=reference_now,
        )

        if (
            parsed.is_weekend_plan_request
            and self._weekend_planner is not None
            and parsed.city is not None
            and parsed.target_date is not None
        ):
            planner_turn = await self._try_plan_weekend(
                parsed=parsed, reason=reason, trace_identifier=trace_identifier, maximum_results=effective_limit
            )
            if planner_turn is not None:
                return planner_turn

        return await self._execute_catalog_search(
            parsed=parsed, reason=reason, trace_identifier=trace_identifier, maximum_results=effective_limit
        )

    def _parse_query(
        self,
        *,
        cleaned_message: str,
        session_state: DialogSessionState,
        cities: tuple[CityReference, ...],
        reference_now: datetime,
    ) -> _ParsedFallbackQuery:
        """Извлекает фильтры поиска и намерение планировщика из текста сообщения."""
        city = self._extract_city(cleaned_message, cities=cities)
        if city is None:
            city = self._extract_city_from_history(session_state, cities=cities)

        detected_category = parse_category(None, fallback_text=cleaned_message)
        category = detected_category if detected_category is not EventCategory.OTHER else None

        only_free = _FREE_QUERY_PATTERN.search(cleaned_message) is not None
        maximum_price: int | None = None
        if not only_free:
            price_match = _PRICE_CEILING_PATTERN.search(cleaned_message)
            if price_match is not None:
                maximum_price = int(price_match.group(1))
                if maximum_price == 0:
                    only_free = True
                    maximum_price = None

        date_from, date_to, target_date = self._extract_date_range(cleaned_message, reference_now=reference_now)

        child_age: int | None = None
        maximum_age_limit: int | None = None
        age_match = _CHILD_AGE_PATTERN.search(cleaned_message)
        if age_match is not None:
            child_age = int(age_match.group(1))
            maximum_age_limit = child_age
        elif _GENERIC_CHILD_PATTERN.search(cleaned_message) is not None:
            maximum_age_limit = 12

        companions_count = 1
        companions_match = _COMPANIONS_PATTERN.search(cleaned_message)
        if companions_match is not None:
            companions_count = max(1, int(companions_match.group(1)))
        elif child_age is not None or _GENERIC_CHILD_PATTERN.search(cleaned_message) is not None:
            companions_count = 2

        is_weekend_plan_request = _PLAN_INTENT_PATTERN.search(cleaned_message) is not None
        if is_weekend_plan_request and target_date is None:
            saturday, _ = self._resolve_weekend_dates(reference_now.date())
            target_date = saturday

        keywords_query = self._extract_keywords_query(
            cleaned_message=cleaned_message,
            city=city,
            has_structured_filters=any(
                (category is not None, only_free, maximum_price is not None, date_from is not None)
            ),
        )

        return _ParsedFallbackQuery(
            city=city,
            category=category,
            only_free=only_free,
            maximum_price=maximum_price,
            date_from=date_from,
            date_to=date_to,
            target_date=target_date,
            child_age=child_age,
            maximum_age_limit=maximum_age_limit,
            companions_count=companions_count,
            keywords_query=keywords_query,
            is_weekend_plan_request=is_weekend_plan_request,
        )

    @staticmethod
    def _extract_city(text: str, *, cities: tuple[CityReference, ...]) -> str | None:
        """Извлекает каноническое название города из свободного текста."""
        normalized_text = normalize_text_for_matching(text)
        if not normalized_text:
            return None

        alias_pairs: list[tuple[str, str]] = []
        for canonical_name, aliases in DEFAULT_BASHKORTOSTAN_CITIES.items():
            alias_pairs.append((normalize_text_for_matching(canonical_name), canonical_name))
            for alias in aliases:
                alias_pairs.append((normalize_text_for_matching(alias), canonical_name))
        for city_ref in cities:
            alias_pairs.append((normalize_text_for_matching(city_ref.name), city_ref.name))
            for alias in city_ref.aliases:
                alias_pairs.append((normalize_text_for_matching(alias), city_ref.name))

        tokens = normalized_text.split()
        alias_lookup = dict(alias_pairs)
        for token in tokens:
            if token in alias_lookup:
                return alias_lookup[token]

        for prep_match in _CITY_PREPOSITION_PATTERN.finditer(text):
            candidate = prep_match.group(1)
            resolved = normalize_city(candidate, cities=cities)
            if resolved is not None and resolved in {pair[1] for pair in alias_pairs}:
                return resolved

        return None

    def _extract_city_from_history(
        self, session_state: DialogSessionState, *, cities: tuple[CityReference, ...]
    ) -> str | None:
        """Пытается восстановить город из последних реплик пользователя в сессии."""
        for message in reversed(session_state.recent_messages):
            if message.role != "user":
                continue
            found_city = self._extract_city(message.content, cities=cities)
            if found_city is not None:
                return found_city
        return None

    def _extract_date_range(
        self, text: str, *, reference_now: datetime
    ) -> tuple[datetime | None, datetime | None, date | None]:
        """Определяет диапазон дат поиска по относительным и явным формулировкам."""
        lowered = text.lower()
        today = reference_now.date()

        if "послезавтра" in lowered:
            target = today + timedelta(days=2)
            return (*self._day_bounds(target, tzinfo=reference_now.tzinfo or BASHKORTOSTAN_TIMEZONE), target)
        if "завтра" in lowered:
            target = today + timedelta(days=1)
            return (*self._day_bounds(target, tzinfo=reference_now.tzinfo or BASHKORTOSTAN_TIMEZONE), target)
        if "сегодня" in lowered:
            return (*self._day_bounds(today, tzinfo=reference_now.tzinfo or BASHKORTOSTAN_TIMEZONE), today)

        if _WEEKEND_PATTERN.search(text) is not None:
            saturday, sunday = self._resolve_weekend_dates(today)
            tz = reference_now.tzinfo or BASHKORTOSTAN_TIMEZONE
            if "воскресен" in lowered and "суббот" not in lowered:
                return (*self._day_bounds(sunday, tzinfo=tz), sunday)
            if "суббот" in lowered and "воскресен" not in lowered:
                return (*self._day_bounds(saturday, tzinfo=tz), saturday)
            return (
                datetime.combine(saturday, time(0, 0), tzinfo=tz),
                datetime.combine(sunday, time(23, 59, 59), tzinfo=tz),
                saturday,
            )

        if _WEEK_PATTERN.search(text) is not None:
            tz = reference_now.tzinfo or BASHKORTOSTAN_TIMEZONE
            end_date = today + timedelta(days=7)
            return (reference_now, datetime.combine(end_date, time(23, 59, 59), tzinfo=tz), today)

        parsed_start, _, _ = parse_event_datetime(text, reference_datetime=reference_now)
        if parsed_start is not None:
            target = parsed_start.date()
            return (*self._day_bounds(target, tzinfo=reference_now.tzinfo or BASHKORTOSTAN_TIMEZONE), target)

        return None, None, None

    @staticmethod
    def _day_bounds(target_date: date, *, tzinfo: object) -> tuple[datetime, datetime]:
        from datetime import tzinfo as tzinfo_type

        assert isinstance(tzinfo, tzinfo_type)
        return (
            datetime.combine(target_date, time(0, 0), tzinfo=tzinfo),
            datetime.combine(target_date, time(23, 59, 59), tzinfo=tzinfo),
        )

    @staticmethod
    def _resolve_weekend_dates(today: date) -> tuple[date, date]:
        """Возвращает ближайшие субботу и воскресенье относительно заданной даты."""
        weekday = today.weekday()
        if weekday == 5:
            return today, today + timedelta(days=1)
        if weekday == 6:
            return today, today
        saturday = today + timedelta(days=(5 - weekday))
        return saturday, saturday + timedelta(days=1)

    @staticmethod
    def _extract_keywords_query(*, cleaned_message: str, city: str | None, has_structured_filters: bool) -> str | None:
        """Выделяет значимые ключевые слова, отбрасывая распознанные служебные токены."""
        normalized = normalize_text_for_matching(cleaned_message)
        if not normalized:
            return None

        city_tokens = set(normalize_text_for_matching(city).split()) if city else set()
        kept_tokens: list[str] = []
        for token in normalized.split():
            if len(token) < 3 or token.isdigit():
                continue
            if token in _STOP_WORDS or token in city_tokens:
                continue
            kept_tokens.append(token)

        if not kept_tokens:
            return None
        if has_structured_filters and len(kept_tokens) > 3:
            return None
        return " ".join(kept_tokens[:4])

    async def _try_plan_weekend(
        self, *, parsed: _ParsedFallbackQuery, reason: str, trace_identifier: UUID | None, maximum_results: int
    ) -> AgentTurnResult | None:
        """Пытается построить маршрут выходного дня через `WeekendPlannerService`."""
        assert self._weekend_planner is not None
        assert parsed.city is not None
        assert parsed.target_date is not None

        budget = (
            0
            if parsed.only_free
            else (parsed.maximum_price if parsed.maximum_price is not None else _DEFAULT_PLAN_BUDGET_RUBLES)
        )
        plan_request = WeekendPlanRequest(
            city=parsed.city,
            target_date=parsed.target_date,
            window_start_time=_DEFAULT_PLAN_WINDOW_START,
            window_end_time=_DEFAULT_PLAN_WINDOW_END,
            budget_rubles=budget,
            companions_count=parsed.companions_count,
            child_age=parsed.child_age,
            maximum_travel_minutes=_DEFAULT_PLAN_MAX_TRAVEL_MINUTES,
        )
        plan_result: WeekendPlanResult = await self._weekend_planner.plan_weekend(plan_request)

        if not plan_result.is_feasible:
            suggestions_lines = [
                f"• {suggestion.explanation} Рекомендация: {suggestion.suggested_change}"
                for suggestion in plan_result.relaxation_suggestions
            ]
            details = "\n".join(suggestions_lines)
            failure_text = plan_result.failure_reason or "Не удалось собрать маршрут из 2–4 мероприятий."
            answer_text = f"Сервис работает в резервном режиме. {failure_text}" + (
                f"\n\nВозможные изменения параметров:\n{details}" if details else ""
            )
            return AgentTurnResult(
                answer_text=answer_text,
                clarifying_question="Изменить бюджет, временное окно или дату маршрута?",
                fallback_used=True,
                fallback_reason=reason,
                trace_identifier=trace_identifier,
                model_name="deterministic-fallback",
            )

        events: list[Event] = []
        explanations: dict[UUID, str] = {}
        step_lines: list[str] = []

        for step_number, step in enumerate(plan_result.steps[:maximum_results], start=1):
            try:
                event = await self._event_repository.get_event_by_identifier(
                    event_identifier=step.event_identifier, include_non_published=False
                )
            except ApplicationException:
                continue

            events.append(event)
            clean_title = sanitize_control_characters(step.title, maximum_length=120)
            clean_venue = sanitize_control_characters(step.venue, maximum_length=100)
            travel_note = f"переезд {step.travel_from_previous_minutes} мин." if step_number > 1 else "старт маршрута"
            cost_note = "бесплатно" if step.total_cost_rubles == 0 else f"от {step.total_cost_rubles} ₽"
            explanations[event.identifier] = (
                f"Шаг {step_number} маршрута: {step.starts_at:%H:%M}–{step.ends_at:%H:%M}, "
                f"площадка «{clean_venue}», {cost_note} ({travel_note})."
            )
            step_lines.append(
                f"{step_number}. {clean_title} — {step.starts_at:%d.%m %H:%M}–{step.ends_at:%H:%M}, "
                f"«{clean_venue}», {cost_note}."
            )

        if not events:
            return None

        summary = (
            f"Сервис работает в резервном режиме. Подобран маршрут в городе {parsed.city} "
            f"на {parsed.target_date:%d.%m.%Y} (переезды: {plan_result.total_travel_minutes} мин., "
            f"стоимость от {plan_result.total_minimum_cost_rubles} ₽):\n" + "\n".join(step_lines)
        )
        return AgentTurnResult(
            answer_text=summary,
            recommended_events=tuple(events),
            explanations_by_event=explanations,
            fallback_used=True,
            fallback_reason=reason,
            trace_identifier=trace_identifier,
            model_name="deterministic-fallback",
        )

    async def _execute_catalog_search(
        self, *, parsed: _ParsedFallbackQuery, reason: str, trace_identifier: UUID | None, maximum_results: int
    ) -> AgentTurnResult:
        """Выполняет детерминированный поиск по каталогу и формирует безопасный ответ."""
        search_filter = EventSearchFilter(
            query=parsed.keywords_query,
            city=parsed.city,
            category=parsed.category,
            date_from=parsed.date_from,
            date_to=parsed.date_to,
            maximum_price=parsed.maximum_price,
            only_free=parsed.only_free,
            maximum_age_limit=parsed.maximum_age_limit,
            sort_field=EventSortField.DATE_START,
            sort_direction=SortDirection.ASCENDING,
            page=1,
            page_size=maximum_results,
        )
        page = await self._event_repository.search_events(
            search_filter=search_filter, statuses=(EventStatus.PUBLISHED,)
        )

        if page.total_count == 0 and parsed.keywords_query is not None:
            relaxed_filter = EventSearchFilter(
                city=parsed.city,
                category=parsed.category,
                date_from=parsed.date_from,
                date_to=parsed.date_to,
                maximum_price=parsed.maximum_price,
                only_free=parsed.only_free,
                maximum_age_limit=parsed.maximum_age_limit,
                sort_field=EventSortField.DATE_START,
                sort_direction=SortDirection.ASCENDING,
                page=1,
                page_size=maximum_results,
            )
            page = await self._event_repository.search_events(
                search_filter=relaxed_filter, statuses=(EventStatus.PUBLISHED,)
            )

        recommended = tuple(page.items[:maximum_results])
        explanations: dict[UUID, str] = {}
        lines: list[str] = []

        for index, event in enumerate(recommended, start=1):
            clean_title = sanitize_control_characters(event.title, maximum_length=120)
            clean_venue = sanitize_control_characters(event.venue, maximum_length=100)
            clean_city = sanitize_control_characters(event.city, maximum_length=60)
            event_start = attach_default_timezone(event.date_start)
            category_label = _CATEGORY_LABELS.get(event.category, "событие")
            price_label = self._format_price_label(event)

            explanations[event.identifier] = (
                f"Подходит по параметрам поиска ({category_label} в городе {clean_city}, "
                f"{event_start:%d.%m.%Y в %H:%M}, площадка «{clean_venue}», {price_label})."
            )
            lines.append(
                f"{index}. {clean_title} — {event_start:%d.%m %H:%M}, {clean_city}, «{clean_venue}» ({price_label})."
            )

        clarifying_question = self._build_clarifying_question(parsed=parsed, total_count=page.total_count)

        if not recommended:
            answer_text = (
                "Сервис работает в резервном режиме поиска по каталогу. "
                "По вашему запросу подходящих опубликованных мероприятий не найдено. "
                "Попробуйте расширить диапазон дат, убрать ограничение по цене или указать другой город."
            )
        else:
            answer_text = (
                "Сервис работает в резервном режиме поиска по каталогу. "
                "Вот подходящие мероприятия:\n" + "\n".join(lines)
            )
            if clarifying_question is not None:
                answer_text = f"{answer_text}\n\n{clarifying_question}"

        return AgentTurnResult(
            answer_text=answer_text,
            recommended_events=recommended,
            explanations_by_event=explanations,
            clarifying_question=clarifying_question,
            fallback_used=True,
            fallback_reason=reason,
            trace_identifier=trace_identifier,
            model_name="deterministic-fallback",
        )

    @staticmethod
    def _format_price_label(event: Event) -> str:
        """Форматирует стоимость билета без технических деталей."""
        if event.is_free or event.price_minimum == 0:
            return "бесплатно"
        if event.price_minimum is not None:
            return f"от {event.price_minimum} ₽"
        return "стоимость уточняется у организатора"

    @staticmethod
    def _build_clarifying_question(*, parsed: _ParsedFallbackQuery, total_count: int) -> str | None:
        """Формирует один уточняющий вопрос при неоднозначности запроса."""
        if parsed.city is None:
            return "Уточните, пожалуйста, в каком городе Башкортостана подобрать мероприятия?"
        if total_count == 0:
            return "Расширить поиск на соседние даты или убрать ограничение по категории и бюджету?"
        if parsed.category is None and parsed.date_from is None and total_count > _MAXIMUM_RECOMMENDED_EVENTS:
            return "Уточните, пожалуйста, интересующую категорию (концерты, театр, выставки) или желаемую дату?"
        return None
