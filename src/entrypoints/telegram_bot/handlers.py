"""Обработчики команд, текстовых запросов и инлайн-кнопок Telegram-бота."""

import contextlib
import hashlib
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from difflib import SequenceMatcher
from typing import Any, Final, Literal, override
from uuid import UUID, uuid4

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject, Update

from src.application import EventSearchService, SubscriptionService, WeekendPlannerService
from src.core.configuration import TelegramSettings
from src.core.exceptions import ApplicationException, ValidationException
from src.core.logger import get_logger, reset_request_identifier, set_request_identifier
from src.core.types import DialogChannel, EventCategory, RealCollection
from src.domain.entities import (
    CityReference,
    DialogMessage,
    EventSearchFilter,
    PendingAction,
    WeekendPlanRequest,
    WeekendPlanResult,
)
from src.domain.exceptions import (
    EventNotFoundError,
    InvalidSearchFilterError,
    InvalidWeekendPlanRequestError,
    PendingActionExpiredError,
    PendingActionNotFoundError,
    PromptInjectionDetectedError,
    SubscriptionNotFoundError,
)
from src.domain.ports import DialogRepositoryPort
from src.domain.services import (
    BASHKORTOSTAN_TIMEZONE,
    DEFAULT_BASHKORTOSTAN_CITIES,
    inspect_untrusted_text,
    normalize_optional_text,
    normalize_text_for_matching,
    sanitize_control_characters,
)
from src.entrypoints.bootstrap import ApplicationContainer
from src.entrypoints.telegram_bot._search_parser import (
    TelegramSessionFilterStore,
    has_active_search_constraints,
    parse_search_query_to_filter,
    relax_search_filter,
)
from src.entrypoints.telegram_bot.formatting import (
    format_active_filter_summary,
    format_cancel_message,
    format_empty_search_message,
    format_event_card_message,
    format_events_page_message,
    format_help_message,
    format_similar_events_message,
    format_start_message,
    format_subscriptions_message,
    format_weekend_plan_error_message,
    format_weekend_plan_result_message,
    format_weekend_planner_menu_message,
    format_weekend_planner_unavailable_message,
)
from src.entrypoints.telegram_bot.keyboards import (
    ALL_CATEGORIES_TOKEN,
    ALL_CITIES_TOKEN,
    EventCardCallback,
    FilterCallback,
    MenuCallback,
    PageCallback,
    PendingActionCallback,
    RelaxFilterCallback,
    SimilarEventsCallback,
    SubscriptionCallback,
    WeekendPlanCallback,
    build_categories_keyboard,
    build_cities_keyboard,
    build_empty_search_keyboard,
    build_event_card_keyboard,
    build_main_menu_keyboard,
    build_pending_action_keyboard,
    build_search_results_keyboard,
    build_similar_events_keyboard,
    build_subscriptions_keyboard,
    build_weekend_plan_error_keyboard,
    build_weekend_plan_result_keyboard,
    build_weekend_planner_cities_keyboard,
    build_weekend_planner_keyboard,
    resolve_city_from_token,
    resolve_upcoming_weekend_dates,
)
from src.entrypoints.web._formatting import CATEGORY_LABELS, compute_period_range

__all__ = ["TelegramBotDependencies", "TelegramUpdateMiddleware", "WeekendPlanSessionStore", "create_telegram_router"]

logger = get_logger(__name__)

_MAXIMUM_SEEN_EVENT_KEYS: Final[int] = 4096
_DEFAULT_CALLBACK_COOLDOWN_SECONDS: Final[float] = 1.0
_DEFAULT_PLAN_CITY: Final[str] = "Уфа"
_DEFAULT_PLAN_BUDGET_RUBLES: Final[int] = 2000
_DEFAULT_PLAN_MAX_TRAVEL_MINUTES: Final[int] = 20
_DEFAULT_PLAN_WINDOW_START: Final[datetime_time] = datetime_time(10, 0)
_DEFAULT_PLAN_WINDOW_END: Final[datetime_time] = datetime_time(21, 0)


class WeekendPlanSessionStore:
    """Ограниченный кэш черновиков и сохранённых маршрутов планировщика для инлайн-кнопок."""

    def __init__(self, *, maximum_sessions: int = 2048, maximum_tokens: int = 4096) -> None:
        """Инициализирует хранилище состояния планировщика по сессиям и токенам."""
        self._maximum_sessions = maximum_sessions
        self._maximum_tokens = maximum_tokens
        self._drafts: OrderedDict[str, WeekendPlanRequest] = OrderedDict()
        self._plans: OrderedDict[str, tuple[str, WeekendPlanRequest, WeekendPlanResult]] = OrderedDict()
        self._last_modes: OrderedDict[str, tuple[Literal["search", "plan"], str | None]] = OrderedDict()

    def get_draft(self, session_identifier: str, *, reference_now: datetime | None = None) -> WeekendPlanRequest:
        """Возвращает текущий черновик запроса планировщика или создаёт настройки по умолчанию."""
        existing = self._drafts.get(session_identifier)
        if existing is not None:
            self._drafts.move_to_end(session_identifier)
            return existing
        return self._build_default_draft(reference_now=reference_now)

    def save_draft(self, session_identifier: str, draft: WeekendPlanRequest) -> WeekendPlanRequest:
        """Сохраняет обновлённый черновик параметров маршрута в сессии пользователя."""
        self._drafts[session_identifier] = draft
        self._drafts.move_to_end(session_identifier)
        while len(self._drafts) > self._maximum_sessions:
            self._drafts.popitem(last=False)
        return draft

    def save_plan(self, session_identifier: str, request: WeekendPlanRequest, result: WeekendPlanResult) -> str:
        """Сохраняет построенный результат планировщика и возвращает компактный 8-символьный токен."""
        self.save_draft(session_identifier, request)
        payload = f"{session_identifier}:{request!r}:{result.is_feasible}:{len(result.steps)}"
        token = hashlib.blake2b(payload.encode("utf-8"), digest_size=4).hexdigest()
        self._plans[token] = (session_identifier, request, result)
        self._plans.move_to_end(token)
        while len(self._plans) > self._maximum_tokens:
            self._plans.popitem(last=False)
        self.set_last_mode(session_identifier, "plan", plan_token=token)
        return token

    def resolve_plan(
        self, session_identifier: str, plan_token: str
    ) -> tuple[WeekendPlanRequest, WeekendPlanResult] | None:
        """Возвращает сохранённые запрос и результат по токену только для владельца сессии."""
        entry = self._plans.get(plan_token)
        if entry is None or entry[0] != session_identifier:
            return None
        self._plans.move_to_end(plan_token)
        return entry[1], entry[2]

    def set_last_mode(
        self, session_identifier: str, mode: Literal["search", "plan"], *, plan_token: str | None = None
    ) -> None:
        """Запоминает последний активный экран выдачи для кнопки возврата из карточки события."""
        self._last_modes[session_identifier] = (mode, plan_token)
        self._last_modes.move_to_end(session_identifier)
        while len(self._last_modes) > self._maximum_sessions:
            self._last_modes.popitem(last=False)

    def get_last_plan_token(self, session_identifier: str) -> str | None:
        """Возвращает токен маршрута, если последним активным экраном был планировщик."""
        entry = self._last_modes.get(session_identifier)
        if entry is not None and entry[0] == "plan":
            return entry[1]
        return None

    def reset_session(self, session_identifier: str) -> None:
        """Сбрасывает черновик и активный режим планировщика при `/start` или `/cancel`."""
        self._drafts.pop(session_identifier, None)
        self._last_modes.pop(session_identifier, None)

    @staticmethod
    def _build_default_draft(*, reference_now: datetime | None = None) -> WeekendPlanRequest:
        now = reference_now or datetime.now(tz=BASHKORTOSTAN_TIMEZONE)
        local_now = (
            now.replace(tzinfo=BASHKORTOSTAN_TIMEZONE) if now.tzinfo is None else now.astimezone(BASHKORTOSTAN_TIMEZONE)
        )
        saturday, _ = resolve_upcoming_weekend_dates(reference_now=local_now)
        default_date = local_now.date() if local_now.weekday() in (5, 6) else saturday
        return WeekendPlanRequest(
            city=_DEFAULT_PLAN_CITY,
            target_date=default_date,
            window_start_time=_DEFAULT_PLAN_WINDOW_START,
            window_end_time=_DEFAULT_PLAN_WINDOW_END,
            budget_rubles=_DEFAULT_PLAN_BUDGET_RUBLES,
            companions_count=1,
            child_age=None,
            maximum_travel_minutes=_DEFAULT_PLAN_MAX_TRAVEL_MINUTES,
        )


@dataclass(slots=True, kw_only=True)
class TelegramBotDependencies:
    """Набор прикладных зависимостей и состояния сессий для обработчиков бота.

    Attributes:
        search_service: Прикладной сервис поиска и карточек мероприятий.
        subscription_service: Прикладной сервис управления подписками.
        dialog_repository: Порт хранения истории диалога и ожидающих подтверждений.
        telegram_settings: Конфигурация Telegram-бота.
        filter_store: Ограниченный кэш фильтров пагинации и ослабления.
        weekend_planner: Опциональный прикладной сервис планировщика «Выходной день».
        plan_store: Ограниченный кэш черновиков и маршрутов планировщика.
    """

    search_service: EventSearchService
    subscription_service: SubscriptionService
    dialog_repository: DialogRepositoryPort
    telegram_settings: TelegramSettings
    filter_store: TelegramSessionFilterStore
    weekend_planner: WeekendPlannerService | None = None
    plan_store: WeekendPlanSessionStore = field(default_factory=WeekendPlanSessionStore)


class TelegramUpdateMiddleware(BaseMiddleware):
    """Промежуточный слой трассировки, защиты от дублей и безопасной обработки доменных ошибок."""

    def __init__(self, *, duplicate_callback_cooldown_seconds: float = _DEFAULT_CALLBACK_COOLDOWN_SECONDS) -> None:
        """Инициализирует middleware защиты от дублирования событий.

        Arguments:
            duplicate_callback_cooldown_seconds: Окно защиты от повторного быстрого нажатия одной кнопки.
        """
        super().__init__()
        self._duplicate_callback_cooldown_seconds = duplicate_callback_cooldown_seconds
        self._seen_callback_identifiers: OrderedDict[str, float] = OrderedDict()
        self._seen_message_keys: OrderedDict[tuple[int, int], float] = OrderedDict()
        self._recent_callback_clicks: OrderedDict[tuple[int, int, str], float] = OrderedDict()

    @override
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Обрабатывает входящее обновление с установкой `request_id`, дедупликацией и обработкой ошибок."""
        request_identifier = f"tg-{uuid4().hex[:12]}"
        token = set_request_identifier(request_identifier)
        try:
            if await self._is_duplicate_event(event):
                return None

            logger.info("Обработка обновления Telegram (%s).", _describe_update_anonymously(event))
            try:
                return await handler(event, data)
            except ApplicationException as error:
                logger.warning("Прикладная ошибка при обработке запроса Telegram: %s", type(error).__name__)
                await _reply_with_friendly_error(event, _map_application_error_to_text(error))
                return None
        finally:
            reset_request_identifier(token)

    async def _is_duplicate_event(self, event: TelegramObject) -> bool:
        now_monotonic = time.monotonic()
        if isinstance(event, Update):
            if event.callback_query is not None:
                return await self._is_duplicate_callback(event.callback_query, now_monotonic=now_monotonic)
            if event.message is not None:
                return self._is_duplicate_message(event.message, now_monotonic=now_monotonic)
        elif isinstance(event, CallbackQuery):
            return await self._is_duplicate_callback(event, now_monotonic=now_monotonic)
        elif isinstance(event, Message):
            return self._is_duplicate_message(event, now_monotonic=now_monotonic)
        return False

    async def _is_duplicate_callback(self, callback_query: CallbackQuery, *, now_monotonic: float) -> bool:
        if callback_query.id and callback_query.id in self._seen_callback_identifiers:
            await _safe_answer_callback(callback_query)
            return True
        if callback_query.id:
            _remember_bounded_key(self._seen_callback_identifiers, callback_query.id, now_monotonic)

        if (
            self._duplicate_callback_cooldown_seconds > 0
            and callback_query.from_user is not None
            and isinstance(callback_query.message, Message)
            and callback_query.data is not None
        ):
            click_key = (callback_query.from_user.id, callback_query.message.message_id, callback_query.data)
            last_clicked = self._recent_callback_clicks.get(click_key)
            if last_clicked is not None and (now_monotonic - last_clicked) < self._duplicate_callback_cooldown_seconds:
                await _safe_answer_callback(callback_query)
                return True
            _remember_bounded_key(self._recent_callback_clicks, click_key, now_monotonic)
        return False

    def _is_duplicate_message(self, message: Message, *, now_monotonic: float) -> bool:
        if message.message_id <= 0 or message.chat is None:
            return False
        message_key = (message.chat.id, message.message_id)
        if message_key in self._seen_message_keys:
            return True
        _remember_bounded_key(self._seen_message_keys, message_key, now_monotonic)
        return False


def create_telegram_router(
    container: ApplicationContainer, *, duplicate_callback_cooldown_seconds: float = _DEFAULT_CALLBACK_COOLDOWN_SECONDS
) -> Router:
    """Создаёт и настраивает роутер `aiogram` v3 поверх прикладных сервисов контейнера.

    Arguments:
        container: Собранный контейнер зависимостей приложения.
        duplicate_callback_cooldown_seconds: Окно защиты от двойных нажатий инлайн-кнопок.

    Returns:
        Готовый к подключению в `Dispatcher` роутер `aiogram`.
    """
    router = Router(name="cultural_meridian_bot")
    middleware = TelegramUpdateMiddleware(duplicate_callback_cooldown_seconds=duplicate_callback_cooldown_seconds)
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)

    dependencies = TelegramBotDependencies(
        search_service=container.search_service,
        subscription_service=container.subscription_service,
        dialog_repository=container.dialog_repository,
        telegram_settings=container.telegram_settings,
        filter_store=TelegramSessionFilterStore(),
        weekend_planner=_resolve_weekend_planner(container),
        plan_store=WeekendPlanSessionStore(),
    )

    @router.message(CommandStart())
    async def handle_start_command(message: Message) -> None:
        """Обрабатывает команду `/start`: отправляет приветствие с дисклеймером и меню."""
        if message.from_user is None:
            return
        session_identifier = _build_session_identifier(message.from_user.id)
        dependencies.filter_store.reset_session(session_identifier)
        dependencies.plan_store.reset_session(session_identifier)
        await message.answer(format_start_message(), reply_markup=build_main_menu_keyboard())

    @router.message(Command("help"))
    async def handle_help_command(message: Message) -> None:
        """Обрабатывает команду `/help`: выводит краткую справку по командам и поиску."""
        await message.answer(format_help_message(), reply_markup=build_main_menu_keyboard())

    @router.message(Command("cancel"))
    async def handle_cancel_command(message: Message) -> None:
        """Обрабатывает команду `/cancel`: сбрасывает шаг и отменяет активное подтверждение."""
        if message.from_user is None:
            return
        user_identifier = message.from_user.id
        session_identifier = _build_session_identifier(user_identifier)
        subscriber_identifier = str(user_identifier)
        dependencies.filter_store.reset_session(session_identifier)
        dependencies.plan_store.reset_session(session_identifier)

        session_state = await dependencies.dialog_repository.get_session_state(
            session_identifier=session_identifier,
            channel=DialogChannel.TELEGRAM,
            subscriber_identifier=subscriber_identifier,
            message_limit=dependencies.telegram_settings.DIALOG_HISTORY_LIMIT,
        )
        had_pending_action = False
        if session_state.active_pending_action is not None:
            try:
                await dependencies.dialog_repository.cancel_pending_action(
                    action_identifier=session_state.active_pending_action.action_identifier,
                    session_identifier=session_identifier,
                )
                had_pending_action = True
            except PendingActionNotFoundError:
                had_pending_action = False

        await message.answer(
            format_cancel_message(had_pending_action=had_pending_action), reply_markup=build_main_menu_keyboard()
        )

    @router.message(Command("subscribe"))
    async def handle_subscribe_command(message: Message, command: CommandObject) -> None:
        """Обрабатывает команду `/subscribe` (меню подписок или подтверждение подписки по параметрам)."""
        if message.from_user is None:
            return
        user_identifier = message.from_user.id
        session_identifier = _build_session_identifier(user_identifier)
        subscriber_identifier = str(user_identifier)
        raw_arguments = normalize_optional_text(command.args)

        if raw_arguments is None:
            await _send_subscriptions_screen(
                target=message, dependencies=dependencies, subscriber_identifier=subscriber_identifier
            )
            return

        cities = await dependencies.search_service.list_cities()
        parsed_filter = parse_search_query_to_filter(
            raw_arguments, cities=cities, page_size=dependencies.telegram_settings.PAGE_SIZE
        )
        city_label = parsed_filter.city or "Все города"
        category_label = (
            CATEGORY_LABELS[parsed_filter.category] if parsed_filter.category is not None else "Все категории"
        )
        now = datetime.now(tz=BASHKORTOSTAN_TIMEZONE)
        pending_action = PendingAction(
            action_identifier=uuid4(),
            session_identifier=session_identifier,
            channel=DialogChannel.TELEGRAM,
            action_type="subscribe",
            payload={
                "operation": "subscribe",
                "subscriber_identifier": subscriber_identifier,
                "city": parsed_filter.city,
                "category": parsed_filter.category.value if parsed_filter.category is not None else None,
            },
            confirmation_prompt=f"Подтвердите оформление подписки: {city_label} · {category_label}.",
            created_at=now,
            expires_at=now + timedelta(seconds=dependencies.telegram_settings.PENDING_ACTION_LIFETIME_SECONDS),
        )
        saved_action = await dependencies.dialog_repository.create_pending_action(pending_action=pending_action)
        await message.answer(
            f"<b>Подтверждение подписки</b>\n\n{saved_action.confirmation_prompt}",
            reply_markup=build_pending_action_keyboard(saved_action.action_identifier),
        )

    @router.message(Command("unsubscribe"))
    async def handle_unsubscribe_command(message: Message, command: CommandObject) -> None:
        """Обрабатывает команду `/unsubscribe` (список подписок или запрос удаления всех подписок)."""
        if message.from_user is None:
            return
        user_identifier = message.from_user.id
        session_identifier = _build_session_identifier(user_identifier)
        subscriber_identifier = str(user_identifier)
        raw_arguments = (normalize_optional_text(command.args) or "").lower()

        if raw_arguments in {"all", "все", "всё"}:
            await _prompt_unsubscribe_all(
                target=message,
                dependencies=dependencies,
                session_identifier=session_identifier,
                subscriber_identifier=subscriber_identifier,
            )
            return

        await _send_subscriptions_screen(
            target=message, dependencies=dependencies, subscriber_identifier=subscriber_identifier
        )

    @router.message(Command("plan"))
    async def handle_plan_command(message: Message, command: CommandObject) -> None:
        """Обрабатывает команду `/plan` (интерактивный конструктор или быстрый расчёт по аргументам)."""
        if message.from_user is None:
            return
        if dependencies.weekend_planner is None:
            await message.answer(format_weekend_planner_unavailable_message(), reply_markup=build_main_menu_keyboard())
            return

        user_identifier = message.from_user.id
        session_identifier = _build_session_identifier(user_identifier)
        subscriber_identifier = str(user_identifier)
        raw_arguments = sanitize_control_characters(command.args or "", maximum_length=300)

        if not raw_arguments or raw_arguments.lower() in {"меню", "настройка", "настройки", "пресеты"}:
            await _send_weekend_planner_menu(
                target=message, dependencies=dependencies, session_identifier=session_identifier
            )
            return

        inspection = inspect_untrusted_text(raw_arguments, maximum_length=300)
        if inspection.has_suspicious_instructions:
            raise PromptInjectionDetectedError(
                "Запрос отклонён: укажите город, бюджет и количество участников (например: /plan Уфа 2000).",
                source_context="telegram_plan_command",
                matched_patterns=inspection.matched_patterns,
            )

        cities = await dependencies.search_service.list_cities()
        plan_request = _parse_plan_command_arguments(raw_arguments, cities=cities)
        await _execute_and_render_weekend_plan(
            target=message,
            dependencies=dependencies,
            session_identifier=session_identifier,
            subscriber_identifier=subscriber_identifier,
            plan_request=plan_request,
            user_query_text=f"/plan {raw_arguments}",
        )

    @router.message(F.text)
    async def handle_text_search(message: Message) -> None:
        """Обрабатывает свободный текстовый запрос Уровня 1–2 поверх `EventSearchService`."""
        if message.from_user is None or message.text is None:
            return
        cleaned_input = sanitize_control_characters(message.text, maximum_length=500)
        if not cleaned_input:
            await message.answer(
                "Введите текстовый запрос, например: <i>«Концерт в Уфе на выходные»</i>.",
                reply_markup=build_main_menu_keyboard(),
            )
            return

        if cleaned_input.startswith("/"):
            await message.answer(
                "Неизвестная команда. Используйте /help, чтобы посмотреть список доступных команд.",
                reply_markup=build_main_menu_keyboard(),
            )
            return

        inspection = inspect_untrusted_text(cleaned_input, maximum_length=500)
        if inspection.has_suspicious_instructions:
            raise PromptInjectionDetectedError(
                "Запрос отклонён: используйте только формулировки поиска афиши (город, дата, категория, цена).",
                source_context="telegram_user_message",
                matched_patterns=inspection.matched_patterns,
            )

        user_identifier = message.from_user.id
        session_identifier = _build_session_identifier(user_identifier)
        subscriber_identifier = str(user_identifier)

        cities = await dependencies.search_service.list_cities()
        parsed_filter = parse_search_query_to_filter(
            cleaned_input, cities=cities, page_size=dependencies.telegram_settings.PAGE_SIZE
        )
        await _execute_and_render_search(
            target=message,
            dependencies=dependencies,
            session_identifier=session_identifier,
            subscriber_identifier=subscriber_identifier,
            search_filter=parsed_filter,
            user_query_text=cleaned_input,
        )

    @router.callback_query(MenuCallback.filter())
    async def handle_menu_callback(callback_query: CallbackQuery, callback_data: MenuCallback) -> None:
        """Обрабатывает навигационные кнопки главного меню и разделов."""
        if callback_query.from_user is None or not isinstance(callback_query.message, Message):
            await _safe_answer_callback(callback_query)
            return

        user_identifier = callback_query.from_user.id
        session_identifier = _build_session_identifier(user_identifier)
        subscriber_identifier = str(user_identifier)
        action_name = callback_data.action_name

        if action_name == "home":
            await _safe_edit_or_send(
                callback_query.message, text=format_start_message(), reply_markup=build_main_menu_keyboard()
            )
        elif action_name == "help":
            await _safe_edit_or_send(
                callback_query.message, text=format_help_message(), reply_markup=build_main_menu_keyboard()
            )
        elif action_name == "cities":
            cities = await dependencies.search_service.list_cities()
            await _safe_edit_or_send(
                callback_query.message,
                text="<b>Выберите город или населённый пункт:</b>",
                reply_markup=build_cities_keyboard(cities, mode="search"),
            )
        elif action_name == "categories":
            await _safe_edit_or_send(
                callback_query.message,
                text="<b>Выберите категорию мероприятий:</b>",
                reply_markup=build_categories_keyboard(mode="search"),
            )
        elif action_name == "subscriptions":
            await _send_subscriptions_screen(
                target=callback_query.message,
                dependencies=dependencies,
                subscriber_identifier=subscriber_identifier,
                edit_existing=True,
            )
        elif action_name == "planner":
            if dependencies.weekend_planner is None:
                await _safe_edit_or_send(
                    callback_query.message,
                    text=format_weekend_planner_unavailable_message(),
                    reply_markup=build_main_menu_keyboard(),
                )
            else:
                await _send_weekend_planner_menu(
                    target=callback_query.message,
                    dependencies=dependencies,
                    session_identifier=session_identifier,
                    edit_existing=True,
                )
        elif action_name == "reset":
            dependencies.filter_store.reset_session(session_identifier)
            reset_filter = EventSearchFilter(page=1, page_size=dependencies.telegram_settings.PAGE_SIZE)
            await _execute_and_render_search(
                target=callback_query.message,
                dependencies=dependencies,
                session_identifier=session_identifier,
                subscriber_identifier=subscriber_identifier,
                search_filter=reset_filter,
                edit_existing=True,
            )

        await _safe_answer_callback(callback_query)

    @router.callback_query(FilterCallback.filter())
    async def handle_filter_callback(callback_query: CallbackQuery, callback_data: FilterCallback) -> None:
        """Обрабатывает кнопки быстрого подбора и комбинирования фильтров."""
        if callback_query.from_user is None or not isinstance(callback_query.message, Message):
            await _safe_answer_callback(callback_query)
            return

        user_identifier = callback_query.from_user.id
        session_identifier = _build_session_identifier(user_identifier)
        subscriber_identifier = str(user_identifier)
        current_filter = dependencies.filter_store.get_session_filter(
            session_identifier, default_page_size=dependencies.telegram_settings.PAGE_SIZE
        )

        updated_filter = await _apply_quick_filter(
            current_filter=current_filter,
            filter_kind=callback_data.filter_kind,
            filter_value=callback_data.filter_value,
            search_service=dependencies.search_service,
        )
        await _execute_and_render_search(
            target=callback_query.message,
            dependencies=dependencies,
            session_identifier=session_identifier,
            subscriber_identifier=subscriber_identifier,
            search_filter=updated_filter,
            edit_existing=True,
        )
        await _safe_answer_callback(callback_query)

    @router.callback_query(PageCallback.filter())
    async def handle_pagination_callback(callback_query: CallbackQuery, callback_data: PageCallback) -> None:
        """Обрабатывает кнопки пагинации «Показать ещё» / «Назад» по токену сессии."""
        if callback_query.from_user is None or not isinstance(callback_query.message, Message):
            await _safe_answer_callback(callback_query)
            return

        user_identifier = callback_query.from_user.id
        session_identifier = _build_session_identifier(user_identifier)
        subscriber_identifier = str(user_identifier)

        resolved = dependencies.filter_store.resolve_token(session_identifier, callback_data.filter_token)
        if resolved is None:
            await _safe_answer_callback(
                callback_query, text="Кнопка устарела — повторите поисковый запрос, пожалуйста."
            )
            await _safe_edit_or_send(
                callback_query.message, text=format_start_message(), reply_markup=build_main_menu_keyboard()
            )
            return

        base_filter, _ = resolved
        target_filter = replace(base_filter, page=max(1, callback_data.page_number))
        await _execute_and_render_search(
            target=callback_query.message,
            dependencies=dependencies,
            session_identifier=session_identifier,
            subscriber_identifier=subscriber_identifier,
            search_filter=target_filter,
            edit_existing=True,
        )
        await _safe_answer_callback(callback_query)

    @router.callback_query(RelaxFilterCallback.filter())
    async def handle_relax_filter_callback(callback_query: CallbackQuery, callback_data: RelaxFilterCallback) -> None:
        """Обрабатывает быстрое ослабление мешающего ограничения при пустой выдаче."""
        if callback_query.from_user is None or not isinstance(callback_query.message, Message):
            await _safe_answer_callback(callback_query)
            return

        user_identifier = callback_query.from_user.id
        session_identifier = _build_session_identifier(user_identifier)
        subscriber_identifier = str(user_identifier)

        resolved = dependencies.filter_store.resolve_token(session_identifier, callback_data.filter_token)
        if resolved is None:
            await _safe_answer_callback(callback_query, text="Кнопка устарела — выберите новый фильтр в меню.")
            await _safe_edit_or_send(
                callback_query.message, text=format_start_message(), reply_markup=build_main_menu_keyboard()
            )
            return

        base_filter, _ = resolved
        relaxed_filter = relax_search_filter(base_filter, callback_data.relaxation_kind)
        await _execute_and_render_search(
            target=callback_query.message,
            dependencies=dependencies,
            session_identifier=session_identifier,
            subscriber_identifier=subscriber_identifier,
            search_filter=relaxed_filter,
            edit_existing=True,
        )
        await _safe_answer_callback(callback_query)

    @router.callback_query(EventCardCallback.filter())
    async def handle_event_card_callback(callback_query: CallbackQuery, callback_data: EventCardCallback) -> None:
        """Открывает подробную карточку мероприятия по идентификатору `event:{uuid}`."""
        if callback_query.from_user is None or not isinstance(callback_query.message, Message):
            await _safe_answer_callback(callback_query)
            return

        try:
            event_identifier = UUID(callback_data.event_identifier)
        except ValueError:
            await _safe_answer_callback(callback_query, text="Некорректный идентификатор события.")
            return

        event = await dependencies.search_service.get_event(event_identifier=event_identifier)
        session_identifier = _build_session_identifier(callback_query.from_user.id)
        last_plan_token = dependencies.plan_store.get_last_plan_token(session_identifier)
        active_filter = dependencies.filter_store.get_session_filter(
            session_identifier, default_page_size=dependencies.telegram_settings.PAGE_SIZE
        )
        filter_token = (
            dependencies.filter_store.save_session_filter(session_identifier, active_filter)
            if last_plan_token is None and (has_active_search_constraints(active_filter) or active_filter.page > 1)
            else None
        )

        await _safe_edit_or_send(
            callback_query.message,
            text=format_event_card_message(event),
            reply_markup=build_event_card_keyboard(
                event,
                filter_token=filter_token,
                return_page=active_filter.page,
                plan_token=last_plan_token,
                public_web_base_url=dependencies.telegram_settings.PUBLIC_WEB_BASE_URL,
            ),
        )
        await _safe_answer_callback(callback_query)

    @router.callback_query(SimilarEventsCallback.filter())
    async def handle_similar_events_callback(
        callback_query: CallbackQuery, callback_data: SimilarEventsCallback
    ) -> None:
        """Показывает похожие опубликованные мероприятия для выбранной карточки."""
        if callback_query.from_user is None or not isinstance(callback_query.message, Message):
            await _safe_answer_callback(callback_query)
            return

        try:
            event_identifier = UUID(callback_data.event_identifier)
        except ValueError:
            await _safe_answer_callback(callback_query, text="Некорректный идентификатор события.")
            return

        source_event = await dependencies.search_service.get_event(event_identifier=event_identifier)
        similar_events = await dependencies.search_service.find_similar_events(
            event_identifier=event_identifier, limit=dependencies.telegram_settings.PAGE_SIZE
        )
        await _safe_edit_or_send(
            callback_query.message,
            text=format_similar_events_message(source_event, similar_events),
            reply_markup=build_similar_events_keyboard(similar_events, source_event_identifier=event_identifier),
        )
        await _safe_answer_callback(callback_query)

    @router.callback_query(SubscriptionCallback.filter())
    async def handle_subscription_callback(callback_query: CallbackQuery, callback_data: SubscriptionCallback) -> None:
        """Обрабатывает создание, просмотр, удаление одной подписки и запрос массовой отписки."""
        if callback_query.from_user is None or not isinstance(callback_query.message, Message):
            await _safe_answer_callback(callback_query)
            return

        user_identifier = callback_query.from_user.id
        session_identifier = _build_session_identifier(user_identifier)
        subscriber_identifier = str(user_identifier)
        action_name = callback_data.action_name

        if action_name == "list":
            await _send_subscriptions_screen(
                target=callback_query.message,
                dependencies=dependencies,
                subscriber_identifier=subscriber_identifier,
                edit_existing=True,
            )
            await _safe_answer_callback(callback_query)
            return

        if action_name == "new":
            cities = await dependencies.search_service.list_cities()
            await _safe_edit_or_send(
                callback_query.message,
                text="<b>Новая подписка — шаг 1 из 2: выберите город</b>",
                reply_markup=build_cities_keyboard(cities, mode="subscribe"),
            )
            await _safe_answer_callback(callback_query)
            return

        if action_name == "city":
            city_token = callback_data.first_argument or ALL_CITIES_TOKEN
            await _safe_edit_or_send(
                callback_query.message,
                text="<b>Новая подписка — шаг 2 из 2: выберите категорию</b>",
                reply_markup=build_categories_keyboard(mode="subscribe", city_token=city_token),
            )
            await _safe_answer_callback(callback_query)
            return

        if action_name == "create":
            cities = await dependencies.search_service.list_cities()
            selected_city = resolve_city_from_token(callback_data.first_argument or ALL_CITIES_TOKEN, cities)
            selected_category: EventCategory | None = None
            if callback_data.second_argument and callback_data.second_argument != ALL_CATEGORIES_TOKEN:
                selected_category = EventCategory(callback_data.second_argument)

            await dependencies.subscription_service.subscribe(
                channel=DialogChannel.TELEGRAM,
                subscriber_identifier=subscriber_identifier,
                city=selected_city,
                category=selected_category,
            )
            await _send_subscriptions_screen(
                target=callback_query.message,
                dependencies=dependencies,
                subscriber_identifier=subscriber_identifier,
                edit_existing=True,
                notice_text="Подписка успешно оформлена.",
            )
            await _safe_answer_callback(callback_query, text="Подписка оформлена")
            return

        if action_name == "delete":
            try:
                subscription_identifier = UUID(callback_data.first_argument)
            except ValueError:
                await _safe_answer_callback(callback_query, text="Некорректный идентификатор подписки.")
                return

            await dependencies.subscription_service.unsubscribe(
                subscription_identifier=subscription_identifier, subscriber_identifier=subscriber_identifier
            )
            await _send_subscriptions_screen(
                target=callback_query.message,
                dependencies=dependencies,
                subscriber_identifier=subscriber_identifier,
                edit_existing=True,
                notice_text="Подписка физически удалена из базы.",
            )
            await _safe_answer_callback(callback_query, text="Подписка удалена")
            return

        if action_name == "unsubscribe_all":
            await _prompt_unsubscribe_all(
                target=callback_query.message,
                dependencies=dependencies,
                session_identifier=session_identifier,
                subscriber_identifier=subscriber_identifier,
                edit_existing=True,
            )
            await _safe_answer_callback(callback_query)

    @router.callback_query(PendingActionCallback.filter())
    async def handle_pending_action_callback(
        callback_query: CallbackQuery, callback_data: PendingActionCallback
    ) -> None:
        """Обрабатывает подтверждение или отмену двухшагового `PendingAction`."""
        if callback_query.from_user is None or not isinstance(callback_query.message, Message):
            await _safe_answer_callback(callback_query)
            return

        user_identifier = callback_query.from_user.id
        session_identifier = _build_session_identifier(user_identifier)
        subscriber_identifier = str(user_identifier)

        try:
            action_identifier = UUID(callback_data.action_identifier)
        except ValueError:
            await _safe_answer_callback(callback_query, text="Некорректный идентификатор действия.")
            return

        if callback_data.decision == "cancel":
            with contextlib.suppress(PendingActionNotFoundError):
                await dependencies.dialog_repository.cancel_pending_action(
                    action_identifier=action_identifier, session_identifier=session_identifier
                )
            await _safe_edit_or_send(
                callback_query.message,
                text="Действие отменено. Ваши данные не изменены.",
                reply_markup=build_main_menu_keyboard(),
            )
            await _safe_answer_callback(callback_query, text="Отменено")
            return

        consumed = await dependencies.dialog_repository.consume_pending_action(
            action_identifier=action_identifier,
            session_identifier=session_identifier,
            current_time=datetime.now(tz=BASHKORTOSTAN_TIMEZONE),
        )
        operation = consumed.payload.get("operation")
        if operation == "unsubscribe_all":
            removed_count = await dependencies.subscription_service.unsubscribe_all(
                channel=DialogChannel.TELEGRAM, subscriber_identifier=subscriber_identifier
            )
            await _send_subscriptions_screen(
                target=callback_query.message,
                dependencies=dependencies,
                subscriber_identifier=subscriber_identifier,
                edit_existing=True,
                notice_text=f"Все ваши подписки физически удалены ({removed_count} шт.).",
            )
            await _safe_answer_callback(callback_query, text="Подписки удалены")
            return

        if operation == "subscribe":
            raw_city = consumed.payload.get("city")
            raw_category = consumed.payload.get("category")
            city = raw_city if isinstance(raw_city, str) else None
            category = EventCategory(raw_category) if isinstance(raw_category, str) else None
            await dependencies.subscription_service.subscribe(
                channel=DialogChannel.TELEGRAM,
                subscriber_identifier=subscriber_identifier,
                city=city,
                category=category,
            )
            await _send_subscriptions_screen(
                target=callback_query.message,
                dependencies=dependencies,
                subscriber_identifier=subscriber_identifier,
                edit_existing=True,
                notice_text="Подписка успешно оформлена.",
            )
            await _safe_answer_callback(callback_query, text="Подписка оформлена")
            return

        await _safe_edit_or_send(
            callback_query.message, text="Действие выполнено.", reply_markup=build_main_menu_keyboard()
        )
        await _safe_answer_callback(callback_query)

    @router.callback_query(WeekendPlanCallback.filter())
    async def handle_weekend_plan_callback(callback_query: CallbackQuery, callback_data: WeekendPlanCallback) -> None:
        """Обрабатывает пресеты, настройку параметров, перестроение и ослабление ограничений `/plan`."""
        if callback_query.from_user is None or not isinstance(callback_query.message, Message):
            await _safe_answer_callback(callback_query)
            return

        if dependencies.weekend_planner is None:
            await _safe_edit_or_send(
                callback_query.message,
                text=format_weekend_planner_unavailable_message(),
                reply_markup=build_main_menu_keyboard(),
            )
            await _safe_answer_callback(callback_query)
            return

        user_identifier = callback_query.from_user.id
        session_identifier = _build_session_identifier(user_identifier)
        subscriber_identifier = str(user_identifier)
        action = callback_data.action
        value = callback_data.value

        if action == "menu":
            await _send_weekend_planner_menu(
                target=callback_query.message,
                dependencies=dependencies,
                session_identifier=session_identifier,
                edit_existing=True,
            )
            await _safe_answer_callback(callback_query)
            return

        if action == "cities":
            cities = await dependencies.search_service.list_cities()
            await _safe_edit_or_send(
                callback_query.message,
                text="<b>Планировщик «Выходной день» — выберите город:</b>",
                reply_markup=build_weekend_planner_cities_keyboard(cities),
            )
            await _safe_answer_callback(callback_query)
            return

        if action == "preset":
            preset_request = _build_preset_weekend_plan_request(value)
            await _execute_and_render_weekend_plan(
                target=callback_query.message,
                dependencies=dependencies,
                session_identifier=session_identifier,
                subscriber_identifier=subscriber_identifier,
                plan_request=preset_request,
                user_query_text=f"Пресет планировщика: {value}",
                edit_existing=True,
            )
            await _safe_answer_callback(callback_query)
            return

        if action == "run":
            draft = dependencies.plan_store.get_draft(session_identifier)
            await _execute_and_render_weekend_plan(
                target=callback_query.message,
                dependencies=dependencies,
                session_identifier=session_identifier,
                subscriber_identifier=subscriber_identifier,
                plan_request=draft,
                user_query_text=f"/plan {draft.city} {draft.budget_rubles} {draft.companions_count}",
                edit_existing=True,
            )
            await _safe_answer_callback(callback_query)
            return

        if action == "show":
            resolved_plan = dependencies.plan_store.resolve_plan(session_identifier, value)
            if resolved_plan is None:
                await _safe_answer_callback(
                    callback_query, text="Маршрут устарел — откройте планировщик и постройте заново."
                )
                await _send_weekend_planner_menu(
                    target=callback_query.message,
                    dependencies=dependencies,
                    session_identifier=session_identifier,
                    edit_existing=True,
                )
                return
            saved_request, saved_result = resolved_plan
            dependencies.plan_store.set_last_mode(session_identifier, "plan", plan_token=value)
            await _safe_edit_or_send(
                callback_query.message,
                text=format_weekend_plan_result_message(saved_request, saved_result),
                reply_markup=build_weekend_plan_result_keyboard(
                    saved_request,
                    saved_result,
                    plan_token=value,
                    public_web_base_url=dependencies.telegram_settings.PUBLIC_WEB_BASE_URL,
                ),
            )
            await _safe_answer_callback(callback_query)
            return

        if action == "edit":
            resolved_plan = dependencies.plan_store.resolve_plan(session_identifier, value)
            if resolved_plan is not None:
                dependencies.plan_store.save_draft(session_identifier, resolved_plan[0])
            await _send_weekend_planner_menu(
                target=callback_query.message,
                dependencies=dependencies,
                session_identifier=session_identifier,
                edit_existing=True,
            )
            await _safe_answer_callback(callback_query)
            return

        if action == "relax":
            resolved_plan = dependencies.plan_store.resolve_plan(session_identifier, value)
            if resolved_plan is None:
                await _safe_answer_callback(
                    callback_query, text="Кнопка устарела — настройте маршрут в планировщике заново."
                )
                await _send_weekend_planner_menu(
                    target=callback_query.message,
                    dependencies=dependencies,
                    session_identifier=session_identifier,
                    edit_existing=True,
                )
                return
            base_request, previous_result = resolved_plan
            relaxed_request = _apply_weekend_plan_relaxation(
                base_request, previous_result=previous_result, constraint_name=callback_data.extra
            )
            await _execute_and_render_weekend_plan(
                target=callback_query.message,
                dependencies=dependencies,
                session_identifier=session_identifier,
                subscriber_identifier=subscriber_identifier,
                plan_request=relaxed_request,
                user_query_text=f"Ослабление ограничения маршрута: {callback_data.extra}",
                edit_existing=True,
            )
            await _safe_answer_callback(callback_query)
            return

        current_draft = dependencies.plan_store.get_draft(session_identifier)
        cities = await dependencies.search_service.list_cities() if action == "city" else ()
        updated_draft = _update_draft_from_callback(draft=current_draft, action=action, value=value, cities=cities)
        dependencies.plan_store.save_draft(session_identifier, updated_draft)
        await _send_weekend_planner_menu(
            target=callback_query.message,
            dependencies=dependencies,
            session_identifier=session_identifier,
            edit_existing=True,
        )
        await _safe_answer_callback(callback_query)

    return router


async def _execute_and_render_search(
    *,
    target: Message,
    dependencies: TelegramBotDependencies,
    session_identifier: str,
    subscriber_identifier: str,
    search_filter: EventSearchFilter,
    user_query_text: str | None = None,
    edit_existing: bool = False,
) -> None:
    search_page = await dependencies.search_service.search_events(
        search_filter=search_filter, include_empty_explanation=True
    )
    filter_token = dependencies.filter_store.save_session_filter(
        session_identifier, search_filter, current_page=search_page.page
    )
    dependencies.plan_store.set_last_mode(session_identifier, "search")

    if search_page.items:
        response_text = format_events_page_message(search_page, search_filter=search_filter)
        reply_markup = build_search_results_keyboard(
            search_page,
            filter_token=filter_token,
            has_active_filters=has_active_search_constraints(search_filter),
            public_web_base_url=dependencies.telegram_settings.PUBLIC_WEB_BASE_URL,
        )
        assistant_summary = (
            f"Выдача ({format_active_filter_summary(search_filter)}): "
            f"найдено {search_page.total_count}, страница {search_page.page}."
        )
    else:
        assert search_page.empty_explanation is not None
        response_text = format_empty_search_message(search_page.empty_explanation, search_filter=search_filter)
        reply_markup = build_empty_search_keyboard(search_filter, filter_token=filter_token)
        assistant_summary = f"Пустая выдача ({format_active_filter_summary(search_filter)})."

    await _record_dialog_messages(
        dialog_repository=dependencies.dialog_repository,
        session_identifier=session_identifier,
        subscriber_identifier=subscriber_identifier,
        history_limit=dependencies.telegram_settings.DIALOG_HISTORY_LIMIT,
        user_text=user_query_text or f"Фильтр: {format_active_filter_summary(search_filter)}",
        assistant_text=assistant_summary,
    )

    if edit_existing:
        await _safe_edit_or_send(target, text=response_text, reply_markup=reply_markup)
    else:
        await target.answer(response_text, reply_markup=reply_markup)


async def _record_dialog_messages(
    *,
    dialog_repository: DialogRepositoryPort,
    session_identifier: str,
    subscriber_identifier: str,
    history_limit: int,
    user_text: str,
    assistant_text: str,
) -> None:
    await dialog_repository.get_session_state(
        session_identifier=session_identifier,
        channel=DialogChannel.TELEGRAM,
        subscriber_identifier=subscriber_identifier,
        message_limit=history_limit,
    )
    now = datetime.now(tz=BASHKORTOSTAN_TIMEZONE)
    await dialog_repository.append_messages(
        session_identifier=session_identifier,
        messages=(
            DialogMessage(role="user", content=user_text, created_at=now),
            DialogMessage(role="assistant", content=assistant_text, created_at=now),
        ),
    )


async def _send_subscriptions_screen(
    *,
    target: Message,
    dependencies: TelegramBotDependencies,
    subscriber_identifier: str,
    edit_existing: bool = False,
    notice_text: str | None = None,
) -> None:
    subscriptions = await dependencies.subscription_service.list_subscriptions(
        channel=DialogChannel.TELEGRAM, subscriber_identifier=subscriber_identifier
    )
    body_text = format_subscriptions_message(subscriptions)
    full_text = f"✅ <b>{notice_text}</b>\n\n{body_text}" if notice_text else body_text
    reply_markup = build_subscriptions_keyboard(subscriptions)
    if edit_existing:
        await _safe_edit_or_send(target, text=full_text, reply_markup=reply_markup)
    else:
        await target.answer(full_text, reply_markup=reply_markup)


async def _prompt_unsubscribe_all(
    *,
    target: Message,
    dependencies: TelegramBotDependencies,
    session_identifier: str,
    subscriber_identifier: str,
    edit_existing: bool = False,
) -> None:
    subscriptions = await dependencies.subscription_service.list_subscriptions(
        channel=DialogChannel.TELEGRAM, subscriber_identifier=subscriber_identifier
    )
    if not subscriptions:
        text = "У вас нет активных подписок для удаления."
        markup = build_subscriptions_keyboard(subscriptions)
        if edit_existing:
            await _safe_edit_or_send(target, text=text, reply_markup=markup)
        else:
            await target.answer(text, reply_markup=markup)
        return

    now = datetime.now(tz=BASHKORTOSTAN_TIMEZONE)
    pending_action = PendingAction(
        action_identifier=uuid4(),
        session_identifier=session_identifier,
        channel=DialogChannel.TELEGRAM,
        action_type="subscribe",
        payload={
            "operation": "unsubscribe_all",
            "subscriber_identifier": subscriber_identifier,
            "expected_count": len(subscriptions),
        },
        confirmation_prompt=f"Вы уверены, что хотите физически удалить все ваши подписки ({len(subscriptions)} шт.)?",
        created_at=now,
        expires_at=now + timedelta(seconds=dependencies.telegram_settings.PENDING_ACTION_LIFETIME_SECONDS),
    )
    saved_action = await dependencies.dialog_repository.create_pending_action(pending_action=pending_action)
    confirmation_text = f"<b>Подтверждение массовой отписки</b>\n\n{saved_action.confirmation_prompt}"
    confirmation_markup = build_pending_action_keyboard(saved_action.action_identifier)
    if edit_existing:
        await _safe_edit_or_send(target, text=confirmation_text, reply_markup=confirmation_markup)
    else:
        await target.answer(confirmation_text, reply_markup=confirmation_markup)


async def _apply_quick_filter(
    *, current_filter: EventSearchFilter, filter_kind: str, filter_value: str, search_service: EventSearchService
) -> EventSearchFilter:
    base_filter = replace(current_filter, page=1)
    if filter_kind == "period":
        date_from, date_to = compute_period_range(filter_value)
        return replace(base_filter, date_from=date_from, date_to=date_to)
    if filter_kind == "category":
        if filter_value == ALL_CATEGORIES_TOKEN:
            return replace(base_filter, category=None)
        return replace(base_filter, category=EventCategory(filter_value))
    if filter_kind == "city":
        if filter_value == ALL_CITIES_TOKEN:
            return replace(base_filter, city=None)
        cities = await search_service.list_cities()
        resolved_city = resolve_city_from_token(filter_value, cities)
        return replace(base_filter, city=resolved_city)
    if filter_kind == "free":
        return replace(base_filter, only_free=(filter_value == "1"))
    if filter_kind == "age":
        return replace(base_filter, maximum_age_limit=int(filter_value))
    return base_filter


def _build_session_identifier(user_identifier: int) -> str:
    return f"telegram:{user_identifier}"


def _remember_bounded_key[KeyType](storage: OrderedDict[KeyType, float], key: KeyType, timestamp: float) -> None:
    storage[key] = timestamp
    storage.move_to_end(key)
    while len(storage) > _MAXIMUM_SEEN_EVENT_KEYS:
        storage.popitem(last=False)


def _describe_update_anonymously(event: TelegramObject) -> str:
    user_identifier: int | None = None
    event_kind = type(event).__name__
    if isinstance(event, Update):
        if event.callback_query is not None:
            event_kind = "callback_query"
            user_identifier = event.callback_query.from_user.id if event.callback_query.from_user else None
        elif event.message is not None:
            event_kind = "message"
            user_identifier = event.message.from_user.id if event.message.from_user else None
        return f"update_id={event.update_id}, type={event_kind}, actor={_hash_user_identifier(user_identifier)}"
    if isinstance(event, CallbackQuery):
        user_identifier = event.from_user.id if event.from_user else None
        return f"type=callback_query, actor={_hash_user_identifier(user_identifier)}"
    if isinstance(event, Message):
        user_identifier = event.from_user.id if event.from_user else None
        return f"type=message, actor={_hash_user_identifier(user_identifier)}"
    return f"type={event_kind}"


def _hash_user_identifier(user_identifier: int | None) -> str:
    if user_identifier is None:
        return "anonymous"
    return hashlib.blake2b(str(user_identifier).encode("utf-8"), digest_size=6).hexdigest()


def _map_application_error_to_text(error: ApplicationException) -> str:
    if isinstance(error, EventNotFoundError):
        return "Запрошенное мероприятие не найдено или скрыто в черновиках."
    if isinstance(error, SubscriptionNotFoundError):
        return "Указанная подписка уже удалена или не найдена."
    if isinstance(error, PendingActionExpiredError):
        return "Срок действия подтверждения истёк. Запустите действие заново."
    if isinstance(error, PendingActionNotFoundError):
        return "Запрос на подтверждение уже выполнен, отменён или устарел."
    if isinstance(error, InvalidSearchFilterError):
        return f"Не удалось применить фильтр: {error.message}"
    if isinstance(error, InvalidWeekendPlanRequestError):
        return f"Некорректные параметры маршрута: {error.message}"
    if isinstance(error, PromptInjectionDetectedError):
        return error.message
    if isinstance(error, ValidationException):
        return f"Проверьте введённые данные: {error.message}"
    return "Сервис временно не смог обработать запрос. Попробуйте повторить чуть позже."


async def _reply_with_friendly_error(event: TelegramObject, error_text: str) -> None:
    target_message: Message | None = None
    target_callback: CallbackQuery | None = None

    if isinstance(event, Update):
        target_callback = event.callback_query
        if event.message is not None:
            target_message = event.message
        elif target_callback is not None and isinstance(target_callback.message, Message):
            target_message = target_callback.message
    elif isinstance(event, CallbackQuery):
        target_callback = event
        if isinstance(event.message, Message):
            target_message = event.message
    elif isinstance(event, Message):
        target_message = event

    if target_callback is not None:
        await _safe_answer_callback(target_callback, text=error_text[:180])
    if target_message is not None:
        await target_message.answer(error_text, reply_markup=build_main_menu_keyboard())


async def _safe_answer_callback(callback_query: CallbackQuery, *, text: str | None = None) -> None:
    try:
        await callback_query.answer(text=text)
    except TelegramBadRequest:
        return


async def _safe_edit_or_send(message: Message, *, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text=text, reply_markup=reply_markup)
    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            return
        await message.answer(text=text, reply_markup=reply_markup)


def _resolve_weekend_planner(container: ApplicationContainer) -> WeekendPlannerService | None:
    candidate = getattr(container, "weekend_planner", None)
    if isinstance(candidate, WeekendPlannerService):
        return candidate
    return None


async def _send_weekend_planner_menu(
    *, target: Message, dependencies: TelegramBotDependencies, session_identifier: str, edit_existing: bool = False
) -> None:
    draft = dependencies.plan_store.get_draft(session_identifier)
    dependencies.plan_store.save_draft(session_identifier, draft)
    text = format_weekend_planner_menu_message(draft)
    reply_markup = build_weekend_planner_keyboard(draft)
    if edit_existing:
        await _safe_edit_or_send(target, text=text, reply_markup=reply_markup)
    else:
        await target.answer(text, reply_markup=reply_markup)


async def _execute_and_render_weekend_plan(
    *,
    target: Message,
    dependencies: TelegramBotDependencies,
    session_identifier: str,
    subscriber_identifier: str,
    plan_request: WeekendPlanRequest,
    user_query_text: str,
    edit_existing: bool = False,
) -> None:
    planner = dependencies.weekend_planner
    if planner is None:
        unavailable_text = format_weekend_planner_unavailable_message()
        if edit_existing:
            await _safe_edit_or_send(target, text=unavailable_text, reply_markup=build_main_menu_keyboard())
        else:
            await target.answer(unavailable_text, reply_markup=build_main_menu_keyboard())
        return

    try:
        plan_result = await planner.plan_weekend(plan_request)
    except InvalidWeekendPlanRequestError as error:
        error_text = format_weekend_plan_error_message(error.message)
        error_markup = build_weekend_plan_error_keyboard()
        if edit_existing:
            await _safe_edit_or_send(target, text=error_text, reply_markup=error_markup)
        else:
            await target.answer(error_text, reply_markup=error_markup)
        return

    plan_token = dependencies.plan_store.save_plan(session_identifier, plan_request, plan_result)
    response_text = format_weekend_plan_result_message(plan_request, plan_result)
    reply_markup = build_weekend_plan_result_keyboard(
        plan_request,
        plan_result,
        plan_token=plan_token,
        public_web_base_url=dependencies.telegram_settings.PUBLIC_WEB_BASE_URL,
    )

    assistant_summary = (
        f"Маршрут выходного дня ({plan_request.city}, {plan_request.target_date:%d.%m.%Y}): "
        f"{len(plan_result.steps)} событий."
        if plan_result.is_feasible
        else f"Маршрут выходного дня ({plan_request.city}, {plan_request.target_date:%d.%m.%Y}) не собран."
    )
    await _record_dialog_messages(
        dialog_repository=dependencies.dialog_repository,
        session_identifier=session_identifier,
        subscriber_identifier=subscriber_identifier,
        history_limit=dependencies.telegram_settings.DIALOG_HISTORY_LIMIT,
        user_text=user_query_text,
        assistant_text=assistant_summary,
    )

    if edit_existing:
        await _safe_edit_or_send(target, text=response_text, reply_markup=reply_markup)
    else:
        await target.answer(response_text, reply_markup=reply_markup)


def _build_preset_weekend_plan_request(
    preset_code: str, *, reference_now: datetime | None = None
) -> WeekendPlanRequest:
    saturday, sunday = resolve_upcoming_weekend_dates(reference_now=reference_now)
    if preset_code == "kids6":
        return WeekendPlanRequest(
            city=_DEFAULT_PLAN_CITY,
            target_date=saturday,
            window_start_time=datetime_time(10, 0),
            window_end_time=datetime_time(20, 0),
            budget_rubles=2500,
            companions_count=2,
            child_age=6,
            maximum_travel_minutes=_DEFAULT_PLAN_MAX_TRAVEL_MINUTES,
        )
    if preset_code == "ufafree":
        return WeekendPlanRequest(
            city=_DEFAULT_PLAN_CITY,
            target_date=sunday,
            window_start_time=_DEFAULT_PLAN_WINDOW_START,
            window_end_time=_DEFAULT_PLAN_WINDOW_END,
            budget_rubles=0,
            companions_count=1,
            child_age=None,
            maximum_travel_minutes=_DEFAULT_PLAN_MAX_TRAVEL_MINUTES,
        )
    return WeekendPlanRequest(
        city=_DEFAULT_PLAN_CITY,
        target_date=saturday,
        window_start_time=_DEFAULT_PLAN_WINDOW_START,
        window_end_time=_DEFAULT_PLAN_WINDOW_END,
        budget_rubles=_DEFAULT_PLAN_BUDGET_RUBLES,
        companions_count=1,
        child_age=None,
        maximum_travel_minutes=_DEFAULT_PLAN_MAX_TRAVEL_MINUTES,
    )


def _update_draft_from_callback(
    *,
    draft: WeekendPlanRequest,
    action: str,
    value: str,
    cities: RealCollection[CityReference],
    reference_now: datetime | None = None,
) -> WeekendPlanRequest:
    saturday, sunday = resolve_upcoming_weekend_dates(reference_now=reference_now)
    if action == "city":
        resolved_city = resolve_city_from_token(value, cities)
        return replace(draft, city=resolved_city) if resolved_city else draft
    if action == "day":
        return replace(draft, target_date=sunday if value == "sun" else saturday)
    if action == "bud":
        with contextlib.suppress(ValueError):
            return replace(draft, budget_rubles=max(0, int(value)))
    if action == "trv":
        with contextlib.suppress(ValueError):
            return replace(draft, maximum_travel_minutes=max(5, int(value)))
    if action == "age":
        if value == "none":
            return replace(draft, child_age=None)
        with contextlib.suppress(ValueError):
            return replace(draft, child_age=max(0, int(value)))
    if action == "comp":
        with contextlib.suppress(ValueError):
            return replace(draft, companions_count=max(1, int(value)))
    if action == "win":
        windows: dict[str, tuple[datetime_time, datetime_time]] = {
            "full": (datetime_time(10, 0), datetime_time(21, 0)),
            "day": (datetime_time(11, 0), datetime_time(18, 0)),
            "eve": (datetime_time(15, 0), datetime_time(22, 0)),
        }
        if value in windows:
            start_t, end_t = windows[value]
            return replace(draft, window_start_time=start_t, window_end_time=end_t)
    return draft


def _apply_weekend_plan_relaxation(
    request: WeekendPlanRequest,
    *,
    previous_result: WeekendPlanResult,
    constraint_name: str,
    reference_now: datetime | None = None,
) -> WeekendPlanRequest:
    suggestion_text = next(
        (
            item.suggested_change
            for item in previous_result.relaxation_suggestions
            if item.constraint_name == constraint_name
        ),
        "",
    )
    if constraint_name == "budget":
        matched = re.search(r"(\d+)\s*₽", suggestion_text)
        suggested_budget = int(matched.group(1)) if matched is not None else 0
        fallback_budget = (
            3000 if request.budget_rubles <= 0 else max(request.budget_rubles * 2, request.budget_rubles + 2000)
        )
        return replace(request, budget_rubles=max(suggested_budget, fallback_budget))
    if constraint_name == "time_window":
        return replace(request, window_start_time=datetime_time(0, 0), window_end_time=datetime_time(23, 59))
    if constraint_name == "travel":
        matched = re.search(r"(\d+)\s*мин", suggestion_text)
        suggested_travel = int(matched.group(1)) if matched is not None else 0
        return replace(request, maximum_travel_minutes=max(suggested_travel, request.maximum_travel_minutes + 30, 60))
    if constraint_name == "age":
        return replace(request, child_age=None)
    if constraint_name == "count":
        saturday, sunday = resolve_upcoming_weekend_dates(reference_now=reference_now)
        next_date = sunday if request.target_date == saturday else saturday
        return replace(request, target_date=next_date)
    return request


def _parse_plan_command_arguments(
    raw_arguments: str, *, cities: RealCollection[CityReference], reference_now: datetime | None = None
) -> WeekendPlanRequest:
    now = reference_now or datetime.now(tz=BASHKORTOSTAN_TIMEZONE)
    local_now = (
        now.replace(tzinfo=BASHKORTOSTAN_TIMEZONE) if now.tzinfo is None else now.astimezone(BASHKORTOSTAN_TIMEZONE)
    )
    saturday, sunday = resolve_upcoming_weekend_dates(reference_now=local_now)
    target_date: date = local_now.date() if local_now.weekday() in (5, 6) else saturday

    working = raw_arguments.strip()

    iso_date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", working)
    if iso_date_match is not None:
        with contextlib.suppress(ValueError):
            target_date = date.fromisoformat(iso_date_match.group(1))
            working = f"{working[: iso_date_match.start()]} {working[iso_date_match.end() :]}"
    else:
        dot_date_match = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b", working)
        if dot_date_match is not None:
            day_num = int(dot_date_match.group(1))
            month_num = int(dot_date_match.group(2))
            year_num = int(dot_date_match.group(3)) if dot_date_match.group(3) else local_now.year
            with contextlib.suppress(ValueError):
                target_date = date(year_num, month_num, day_num)
                working = f"{working[: dot_date_match.start()]} {working[dot_date_match.end() :]}"
        else:
            date_keyword_patterns: tuple[tuple[date, re.Pattern[str]], ...] = (
                (local_now.date(), re.compile(r"\bсегодня\b", flags=re.IGNORECASE)),
                (local_now.date() + timedelta(days=1), re.compile(r"\bзавтра\b", flags=re.IGNORECASE)),
                (saturday, re.compile(r"\b(?:в\s+|на\s+)?(?:сб|суббот\w*)\b", flags=re.IGNORECASE)),
                (sunday, re.compile(r"\b(?:в\s+|на\s+)?(?:вс|воскресен\w*)\b", flags=re.IGNORECASE)),
            )
            for candidate_date, pattern in date_keyword_patterns:
                match = pattern.search(working)
                if match is not None:
                    target_date = candidate_date
                    working = f"{working[: match.start()]} {working[match.end() :]}"
                    break

    budget_rubles: int | None = None
    free_pattern = re.compile(r"\b(?:бесплатн\w*|даром|free)\b", flags=re.IGNORECASE)
    if free_pattern.search(working):
        budget_rubles = 0
        working = free_pattern.sub(" ", working)

    child_age: int | None = None
    child_explicit_pattern = re.compile(
        r"(?:(?:с\s+реб[её]нком|реб[её]нк\w*|дет\w*)\s+(\d{1,2})(?:\+|(?:\s*(?:лет|года|год)))?|\b(\d{1,2})\+)",
        flags=re.IGNORECASE,
    )
    child_match = child_explicit_pattern.search(working)
    if child_match is not None:
        age_group = child_match.group(1) or child_match.group(2)
        if age_group is not None:
            child_age = int(age_group)
        working = f"{working[: child_match.start()]} {working[child_match.end() :]}"
    else:
        child_generic_pattern = re.compile(r"\b(?:с\s+детьми|с\s+реб[её]нк\w*|детск\w*)\b", flags=re.IGNORECASE)
        if child_generic_pattern.search(working):
            child_age = 6
            working = child_generic_pattern.sub(" ", working)

    maximum_travel_minutes: int | None = None
    travel_pattern = re.compile(r"\b(\d{1,3})\s*мин(?:ут\w*)?\b", flags=re.IGNORECASE)
    travel_match = travel_pattern.search(working)
    if travel_match is not None:
        maximum_travel_minutes = int(travel_match.group(1))
        working = f"{working[: travel_match.start()]} {working[travel_match.end() :]}"

    city_lookup = _build_planner_city_lookup(cities)
    selected_city: str | None = None
    noise_words = frozenset(
        {"в", "во", "на", "до", "от", "за", "чел", "человек", "человека", "руб", "рублей", "рубля", "р", "₽"}
    )
    tokens = [token for token in working.split() if token]
    remaining_tokens: list[str] = []

    for token in tokens:
        cleaned_token = normalize_text_for_matching(token)
        if (
            selected_city is None
            and cleaned_token
            and cleaned_token not in noise_words
            and not re.fullmatch(r"-?\d+", token)
        ):
            matched_city = _match_known_city(cleaned_token, city_lookup)
            if matched_city is not None:
                selected_city = matched_city
                continue
        remaining_tokens.append(token)

    companions_count: int | None = None
    unmatched_words: list[str] = []
    for token in remaining_tokens:
        stripped = token.rstrip(",.;:!?₽")
        if re.fullmatch(r"-?\d+", stripped):
            number = int(stripped)
            if budget_rubles is None:
                budget_rubles = number
            elif companions_count is None:
                companions_count = number
            continue
        cleaned = normalize_text_for_matching(stripped)
        if cleaned and cleaned not in noise_words:
            unmatched_words.append(stripped)

    if selected_city is None and unmatched_words:
        selected_city = unmatched_words[0]

    return WeekendPlanRequest(
        city=selected_city or _DEFAULT_PLAN_CITY,
        target_date=target_date,
        window_start_time=_DEFAULT_PLAN_WINDOW_START,
        window_end_time=_DEFAULT_PLAN_WINDOW_END,
        budget_rubles=budget_rubles if budget_rubles is not None else _DEFAULT_PLAN_BUDGET_RUBLES,
        companions_count=companions_count if companions_count is not None else 1,
        child_age=child_age,
        maximum_travel_minutes=maximum_travel_minutes
        if maximum_travel_minutes is not None
        else _DEFAULT_PLAN_MAX_TRAVEL_MINUTES,
    )


def _build_planner_city_lookup(cities: RealCollection[CityReference]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical_name, aliases in DEFAULT_BASHKORTOSTAN_CITIES.items():
        lookup[normalize_text_for_matching(canonical_name)] = canonical_name
        for alias in aliases:
            lookup[normalize_text_for_matching(alias)] = canonical_name
    for city_reference in cities:
        lookup[normalize_text_for_matching(city_reference.name)] = city_reference.name
        lookup[normalize_text_for_matching(city_reference.normalized_name)] = city_reference.name
        for alias in city_reference.aliases:
            lookup[normalize_text_for_matching(alias)] = city_reference.name
    return lookup


def _match_known_city(cleaned_token: str, city_lookup: dict[str, str]) -> str | None:
    if cleaned_token in city_lookup:
        return city_lookup[cleaned_token]
    if len(cleaned_token) >= 5:
        best_city: str | None = None
        best_ratio = 0.0
        for alias_key, canonical_name in city_lookup.items():
            if abs(len(alias_key) - len(cleaned_token)) > 3:
                continue
            ratio = SequenceMatcher(None, cleaned_token, alias_key).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_city = canonical_name
        if best_city is not None and best_ratio >= 0.8:
            return best_city
    return None
