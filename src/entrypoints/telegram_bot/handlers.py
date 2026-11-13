"""Обработчики команд, текстовых запросов и инлайн-кнопок Telegram-бота."""

import contextlib
import hashlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final, override
from uuid import UUID, uuid4

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject, Update

from src.application import EventSearchService, SubscriptionService
from src.core.configuration import TelegramSettings
from src.core.exceptions import ApplicationException, ValidationException
from src.core.logger import get_logger, reset_request_identifier, set_request_identifier
from src.core.types import DialogChannel, EventCategory
from src.domain.entities import DialogMessage, EventSearchFilter, PendingAction
from src.domain.exceptions import (
    EventNotFoundError,
    InvalidSearchFilterError,
    PendingActionExpiredError,
    PendingActionNotFoundError,
    PromptInjectionDetectedError,
    SubscriptionNotFoundError,
)
from src.domain.ports import DialogRepositoryPort
from src.domain.services import (
    BASHKORTOSTAN_TIMEZONE,
    inspect_untrusted_text,
    normalize_optional_text,
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
    build_categories_keyboard,
    build_cities_keyboard,
    build_empty_search_keyboard,
    build_event_card_keyboard,
    build_main_menu_keyboard,
    build_pending_action_keyboard,
    build_search_results_keyboard,
    build_similar_events_keyboard,
    build_subscriptions_keyboard,
    resolve_city_from_token,
)
from src.entrypoints.web._formatting import CATEGORY_LABELS, compute_period_range

__all__ = ["TelegramBotDependencies", "TelegramUpdateMiddleware", "create_telegram_router"]

logger = get_logger(__name__)

_MAXIMUM_SEEN_EVENT_KEYS: Final[int] = 4096
_DEFAULT_CALLBACK_COOLDOWN_SECONDS: Final[float] = 1.0


@dataclass(slots=True, kw_only=True)
class TelegramBotDependencies:
    """Набор прикладных зависимостей и состояния сессий для обработчиков бота.

    Attributes:
        search_service: Прикладной сервис поиска и карточек мероприятий.
        subscription_service: Прикладной сервис управления подписками.
        dialog_repository: Порт хранения истории диалога и ожидающих подтверждений.
        telegram_settings: Конфигурация Telegram-бота.
        filter_store: Ограниченный кэш фильтров пагинации и ослабления.
    """

    search_service: EventSearchService
    subscription_service: SubscriptionService
    dialog_repository: DialogRepositoryPort
    telegram_settings: TelegramSettings
    filter_store: TelegramSessionFilterStore


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
    )

    @router.message(CommandStart())
    async def handle_start_command(message: Message) -> None:
        """Обрабатывает команду `/start`: отправляет приветствие с дисклеймером и меню."""
        if message.from_user is None:
            return
        session_identifier = _build_session_identifier(message.from_user.id)
        dependencies.filter_store.reset_session(session_identifier)
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
        active_filter = dependencies.filter_store.get_session_filter(
            session_identifier, default_page_size=dependencies.telegram_settings.PAGE_SIZE
        )
        filter_token = (
            dependencies.filter_store.save_session_filter(session_identifier, active_filter)
            if has_active_search_constraints(active_filter) or active_filter.page > 1
            else None
        )

        await _safe_edit_or_send(
            callback_query.message,
            text=format_event_card_message(event),
            reply_markup=build_event_card_keyboard(
                event,
                filter_token=filter_token,
                return_page=active_filter.page,
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
