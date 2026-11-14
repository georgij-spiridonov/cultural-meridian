"""Инлайн-клавиатуры и типизированные фабрики callback-данных Telegram-бота."""

import hashlib
from datetime import date, datetime, time, timedelta
from typing import Final, Literal
from uuid import UUID

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.core.types import EventCategory, RealCollection
from src.domain.entities import (
    AgentTurnResult,
    CityReference,
    Event,
    EventSearchFilter,
    EventSearchPage,
    SubscriptionRecord,
    WeekendPlanRequest,
    WeekendPlanResult,
)
from src.domain.services import BASHKORTOSTAN_TIMEZONE, DEFAULT_BASHKORTOSTAN_CITIES
from src.entrypoints.telegram_bot.formatting import format_subscription_label, sanitize_public_url, truncate_plain_text
from src.entrypoints.web._formatting import CATEGORY_LABELS

__all__ = [
    "ALL_CATEGORIES_TOKEN",
    "ALL_CITIES_TOKEN",
    "MAXIMUM_CALLBACK_DATA_BYTES",
    "EventCardCallback",
    "FilterCallback",
    "MenuCallback",
    "PageCallback",
    "PendingActionCallback",
    "RelaxFilterCallback",
    "SimilarEventsCallback",
    "SubscriptionCallback",
    "WeekendPlanCallback",
    "build_agent_turn_keyboard",
    "build_categories_keyboard",
    "build_cities_keyboard",
    "build_empty_search_keyboard",
    "build_event_card_keyboard",
    "build_main_menu_keyboard",
    "build_pending_action_keyboard",
    "build_search_results_keyboard",
    "build_similar_events_keyboard",
    "build_subscriptions_keyboard",
    "build_weekend_plan_error_keyboard",
    "build_weekend_plan_result_keyboard",
    "build_weekend_planner_cities_keyboard",
    "build_weekend_planner_keyboard",
    "compute_city_token",
    "resolve_city_from_token",
    "resolve_upcoming_weekend_dates",
]

MAXIMUM_CALLBACK_DATA_BYTES: Final[int] = 64
ALL_CITIES_TOKEN: Final[str] = "all"
ALL_CATEGORIES_TOKEN: Final[str] = "all"
_MAXIMUM_EVENT_BUTTON_TITLE_LENGTH: Final[int] = 36
_MAXIMUM_SUBSCRIPTION_BUTTON_LENGTH: Final[int] = 28
_CITY_BUTTON_LIMIT: Final[int] = 12


class MenuCallback(CallbackData, prefix="m"):
    """Навигационные действия главного меню и разделов бота.

    Attributes:
        action_name: Код целевого раздела (`home`, `cities`, `categories`, `subscriptions`, `help`, `reset`).
    """

    action_name: str


class FilterCallback(CallbackData, prefix="f"):
    """Быстрое применение поискового фильтра с инлайн-кнопки.

    Attributes:
        filter_kind: Тип ограничения (`period`, `category`, `city`, `free`, `age`).
        filter_value: Компактное значение фильтра (ASCII-код или токен города).
    """

    filter_kind: str
    filter_value: str


class PageCallback(CallbackData, prefix="pg"):
    """Навигация по страницам сохранённого поискового фильтра.

    Attributes:
        filter_token: Короткий 8-символьный идентификатор фильтра в состоянии сессии.
        page_number: Целевой номер страницы выдачи (начиная с 1).
    """

    filter_token: str
    page_number: int


class EventCardCallback(CallbackData, prefix="event"):
    """Открытие подробной карточки мероприятия (`event:{uuid}`).

    Attributes:
        event_identifier: Строковый UUID карточки мероприятия.
    """

    event_identifier: str


class SimilarEventsCallback(CallbackData, prefix="sim"):
    """Запрос похожих мероприятий для открытой карточки.

    Attributes:
        event_identifier: Строковый UUID исходной карточки.
    """

    event_identifier: str


class RelaxFilterCallback(CallbackData, prefix="rx"):
    """Быстрое ослабление мешающего ограничения при пустой выдаче.

    Attributes:
        filter_token: Короткий 8-символьный идентификатор фильтра в состоянии сессии.
        relaxation_kind: Код снимаемого ограничения (`free`, `price`, `age`, `category`, `date`, `query`, `city`).
    """

    filter_token: str
    relaxation_kind: str


class SubscriptionCallback(CallbackData, prefix="sub"):
    """Операции управления подписками пользователя.

    Attributes:
        action_name: Действие (`list`, `new`, `city`, `create`, `delete`, `unsubscribe_all`).
        first_argument: Токен города или hex-представление UUID подписки.
        second_argument: Код категории при создании подписки.
    """

    action_name: str
    first_argument: str = ""
    second_argument: str = ""


class PendingActionCallback(CallbackData, prefix="pa"):
    """Подтверждение или отмена отложенного изменяющего действия.

    Attributes:
        decision: Решение пользователя (`confirm` или `cancel`).
        action_identifier: Hex-представление UUID ожидающего действия.
    """

    decision: str
    action_identifier: str


class WeekendPlanCallback(CallbackData, prefix="wp"):
    """Навигация, пресеты и ослабление ограничений планировщика «Выходной день».

    Attributes:
        action: Код действия (`menu`, `preset`, `run`, `cities`, `city`, `day`,
            `win`, `bud`, `comp`, `age`, `trv`, `relax`, `edit`, `show`).
        value: Компактное значение параметра или 8-символьный токен плана/города.
        extra: Дополнительный код (например, имя ослабляемого ограничения).
    """

    action: str
    value: str = ""
    extra: str = ""


def resolve_upcoming_weekend_dates(reference_now: datetime | None = None) -> tuple[date, date]:
    """Возвращает ближайшие субботу и воскресенье в часовом поясе Башкортостана.

    Arguments:
        reference_now: Опорное время (по умолчанию текущее время в часовом поясе РБ).

    Returns:
        Кортеж `(nearest_saturday, nearest_sunday)`.
    """
    now = reference_now or datetime.now(tz=BASHKORTOSTAN_TIMEZONE)
    local_now = (
        now.replace(tzinfo=BASHKORTOSTAN_TIMEZONE) if now.tzinfo is None else now.astimezone(BASHKORTOSTAN_TIMEZONE)
    )
    today = local_now.date()
    weekday = today.weekday()
    if weekday == 5:
        return today, today + timedelta(days=1)
    if weekday == 6:
        return today + timedelta(days=6), today
    saturday = today + timedelta(days=5 - weekday)
    return saturday, saturday + timedelta(days=1)


def compute_city_token(city_name: str) -> str:
    """Вычисляет компактный детерминированный 8-символьный токен города для `callback_data`.

    Arguments:
        city_name: Каноническое название города.

    Returns:
        Строка из 8 шестнадцатеричных ASCII-символов.
    """
    return hashlib.blake2b(city_name.strip().lower().encode("utf-8"), digest_size=4).hexdigest()


def resolve_city_from_token(city_token: str, cities: RealCollection[CityReference] = ()) -> str | None:
    """Восстанавливает каноническое название города по компактному токену из кнопки.

    Arguments:
        city_token: Токен города из `callback_data` либо `"all"`.
        cities: Справочник городов из каталога.

    Returns:
        Название города или `None` для режима «все города».
    """
    if city_token == ALL_CITIES_TOKEN:
        return None
    for city_reference in cities:
        if compute_city_token(city_reference.name) == city_token:
            return city_reference.name
    for canonical_city in DEFAULT_BASHKORTOSTAN_CITIES:
        if compute_city_token(canonical_city) == city_token:
            return canonical_city
    return None


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Строит стартовое инлайн-меню быстрого подбора, планировщика, подписок и справки."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Сегодня", callback_data=FilterCallback(filter_kind="period", filter_value="today"))
    builder.button(text="🌤 На выходные", callback_data=FilterCallback(filter_kind="period", filter_value="weekend"))
    builder.button(text="🆓 Бесплатные", callback_data=FilterCallback(filter_kind="free", filter_value="1"))
    builder.button(text="👶 Детям (0–6+)", callback_data=FilterCallback(filter_kind="age", filter_value="6"))
    builder.button(text="🏙 Выбрать город", callback_data=MenuCallback(action_name="cities"))
    builder.button(text="🎭 Категории", callback_data=MenuCallback(action_name="categories"))
    builder.button(text="🗓 Выходной день", callback_data=MenuCallback(action_name="planner"))
    builder.button(text="🔔 Подписки", callback_data=MenuCallback(action_name="subscriptions"))
    builder.button(text="❓ Помощь", callback_data=MenuCallback(action_name="help"))
    builder.adjust(2, 2, 2, 1, 2)
    return builder.as_markup()


def build_cities_keyboard(
    cities: RealCollection[CityReference], *, mode: Literal["search", "subscribe"]
) -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора города для поиска или оформления подписки.

    Arguments:
        cities: Записи справочника городов из базы.
        mode: Сценарий выбора (`search` для поиска, `subscribe` для подписки).

    Returns:
        Инлайн-клавиатура с кнопками городов.
    """
    builder = InlineKeyboardBuilder()
    city_names = _collect_display_city_names(cities)[:_CITY_BUTTON_LIMIT]

    if mode == "search":
        builder.button(
            text="🗺 Вся республика", callback_data=FilterCallback(filter_kind="city", filter_value=ALL_CITIES_TOKEN)
        )
        for city_name in city_names:
            builder.button(
                text=city_name,
                callback_data=FilterCallback(filter_kind="city", filter_value=compute_city_token(city_name)),
            )
        builder.button(text="🏠 Главное меню", callback_data=MenuCallback(action_name="home"))
        builder.adjust(1, *([2] * ((len(city_names) + 1) // 2)), 1)
        return builder.as_markup()

    builder.button(
        text="🗺 Любой город", callback_data=SubscriptionCallback(action_name="city", first_argument=ALL_CITIES_TOKEN)
    )
    for city_name in city_names:
        builder.button(
            text=city_name,
            callback_data=SubscriptionCallback(action_name="city", first_argument=compute_city_token(city_name)),
        )
    builder.button(text="⬅️ К подпискам", callback_data=SubscriptionCallback(action_name="list"))
    builder.adjust(1, *([2] * ((len(city_names) + 1) // 2)), 1)
    return builder.as_markup()


def build_categories_keyboard(
    *, mode: Literal["search", "subscribe"], city_token: str = ALL_CITIES_TOKEN
) -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора категории мероприятий для поиска или подписки.

    Arguments:
        mode: Сценарий выбора (`search` или `subscribe`).
        city_token: Компактный токен города (используется при создании подписки).

    Returns:
        Инлайн-клавиатура с категориями афиши.
    """
    builder = InlineKeyboardBuilder()
    if mode == "search":
        builder.button(
            text="✨ Все категории",
            callback_data=FilterCallback(filter_kind="category", filter_value=ALL_CATEGORIES_TOKEN),
        )
        for category in EventCategory:
            builder.button(
                text=CATEGORY_LABELS[category],
                callback_data=FilterCallback(filter_kind="category", filter_value=category.value),
            )
        builder.button(text="🏠 Главное меню", callback_data=MenuCallback(action_name="home"))
        builder.adjust(1, 2, 2, 2, 1, 1)
        return builder.as_markup()

    builder.button(
        text="✨ Любая категория",
        callback_data=SubscriptionCallback(
            action_name="create", first_argument=city_token, second_argument=ALL_CATEGORIES_TOKEN
        ),
    )
    for category in EventCategory:
        builder.button(
            text=CATEGORY_LABELS[category],
            callback_data=SubscriptionCallback(
                action_name="create", first_argument=city_token, second_argument=category.value
            ),
        )
    builder.button(text="⬅️ Назад к городам", callback_data=SubscriptionCallback(action_name="new"))
    builder.adjust(1, 2, 2, 2, 1, 1)
    return builder.as_markup()


def build_search_results_keyboard(
    search_page: EventSearchPage, *, filter_token: str, has_active_filters: bool, public_web_base_url: str = ""
) -> InlineKeyboardMarkup:
    """Строит клавиатуру страницы выдачи с карточками, пагинацией и фильтрами.

    Arguments:
        search_page: Текущая страница результатов поиска.
        filter_token: Короткий токен фильтра в сессии пользователя.
        has_active_filters: Признак наличия хотя бы одного активного ограничения.
        public_web_base_url: Публичный URL веб-каталога (если настроен).

    Returns:
        Инлайн-клавиатура результатов поиска.
    """
    builder = InlineKeyboardBuilder()
    start_number = (search_page.page - 1) * search_page.page_size + 1

    for index, event in enumerate(search_page.items, start=start_number):
        short_title = truncate_plain_text(event.title, maximum_length=_MAXIMUM_EVENT_BUTTON_TITLE_LENGTH)
        builder.row(
            InlineKeyboardButton(
                text=f"🎟 {index}. {short_title}",
                callback_data=EventCardCallback(event_identifier=str(event.identifier)).pack(),
            )
        )

    pagination_buttons: list[InlineKeyboardButton] = []
    if search_page.page > 1:
        pagination_buttons.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=PageCallback(filter_token=filter_token, page_number=search_page.page - 1).pack(),
            )
        )
    if search_page.has_more:
        remaining = max(0, search_page.total_count - (search_page.page * search_page.page_size))
        next_label = f"Показать ещё ({remaining}) ➡️" if remaining > 0 else "Показать ещё ➡️"
        pagination_buttons.append(
            InlineKeyboardButton(
                text=next_label,
                callback_data=PageCallback(filter_token=filter_token, page_number=search_page.page + 1).pack(),
            )
        )
    if pagination_buttons:
        builder.row(*pagination_buttons)

    filter_controls: list[InlineKeyboardButton] = [
        InlineKeyboardButton(text="🏙 Город", callback_data=MenuCallback(action_name="cities").pack()),
        InlineKeyboardButton(text="🎭 Категория", callback_data=MenuCallback(action_name="categories").pack()),
    ]
    if has_active_filters:
        filter_controls.append(
            InlineKeyboardButton(text="🔄 Сбросить", callback_data=MenuCallback(action_name="reset").pack())
        )
    builder.row(*filter_controls)

    footer_buttons: list[InlineKeyboardButton] = [
        InlineKeyboardButton(text="🔔 Подписки", callback_data=MenuCallback(action_name="subscriptions").pack()),
        InlineKeyboardButton(text="🏠 Меню", callback_data=MenuCallback(action_name="home").pack()),
    ]
    clean_web_url = sanitize_public_url(public_web_base_url)
    if clean_web_url is not None:
        footer_buttons.insert(0, InlineKeyboardButton(text="🌐 Веб-каталог", url=clean_web_url))
    builder.row(*footer_buttons)

    return builder.as_markup()


def build_agent_turn_keyboard(turn_result: AgentTurnResult, *, public_web_base_url: str = "") -> InlineKeyboardMarkup:
    """Строит клавиатуру ответа ИИ-агента: подтверждение действия, карточки событий или главное меню.

    Arguments:
        turn_result: Итоговый результат диалогового хода.
        public_web_base_url: Публичный URL веб-каталога (если настроен).

    Returns:
        Инлайн-клавиатура для сообщения с ответом ИИ-агента.
    """
    if turn_result.pending_action is not None:
        return build_pending_action_keyboard(turn_result.pending_action.action_identifier)

    if not turn_result.recommended_events:
        return build_main_menu_keyboard()

    builder = InlineKeyboardBuilder()
    for index, event in enumerate(turn_result.recommended_events, start=1):
        short_title = truncate_plain_text(event.title, maximum_length=_MAXIMUM_EVENT_BUTTON_TITLE_LENGTH)
        builder.row(
            InlineKeyboardButton(
                text=f"🎟 {index}. {short_title}",
                callback_data=EventCardCallback(event_identifier=str(event.identifier)).pack(),
            )
        )

    builder.row(
        InlineKeyboardButton(text="🏙 Город", callback_data=MenuCallback(action_name="cities").pack()),
        InlineKeyboardButton(text="🎭 Категория", callback_data=MenuCallback(action_name="categories").pack()),
    )

    footer_buttons: list[InlineKeyboardButton] = [
        InlineKeyboardButton(text="🔔 Подписки", callback_data=MenuCallback(action_name="subscriptions").pack()),
        InlineKeyboardButton(text="🏠 Меню", callback_data=MenuCallback(action_name="home").pack()),
    ]
    clean_web_url = sanitize_public_url(public_web_base_url)
    if clean_web_url is not None:
        footer_buttons.insert(0, InlineKeyboardButton(text="🌐 Веб-каталог", url=clean_web_url))
    builder.row(*footer_buttons)

    return builder.as_markup()


def build_empty_search_keyboard(search_filter: EventSearchFilter, *, filter_token: str) -> InlineKeyboardMarkup:
    """Строит клавиатуру быстрого ослабления ограничений при пустой выдаче.

    Arguments:
        search_filter: Текущий фильтр, не давший результатов.
        filter_token: Короткий токен фильтра в сессии пользователя.

    Returns:
        Инлайн-клавиатура с кнопками снятия активных ограничений.
    """
    builder = InlineKeyboardBuilder()
    relaxations: list[tuple[str, str]] = []
    if search_filter.only_free:
        relaxations.append(("💳 Разрешить платные", "free"))
    if search_filter.maximum_price is not None:
        relaxations.append(("💳 Снять лимит цены", "price"))
    if search_filter.maximum_age_limit is not None:
        relaxations.append(("🔞 Снять возрастной ценз", "age"))
    if search_filter.maximum_duration_minutes is not None:
        relaxations.append(("⏱ Любая длительность", "duration"))
    if search_filter.time_from is not None or search_filter.time_to is not None:
        relaxations.append(("🕒 Любое время суток", "time"))
    if search_filter.venue is not None:
        relaxations.append(("🏛 Любая площадка", "venue"))
    if search_filter.language is not None:
        relaxations.append(("🗣 Любой язык", "language"))
    if search_filter.category is not None:
        relaxations.append(("🎭 Все категории", "category"))
    if search_filter.date_from is not None or search_filter.date_to is not None:
        relaxations.append(("📅 Любые даты", "date"))
    if search_filter.query is not None:
        relaxations.append(("🔎 Убрать текстовый запрос", "query"))
    if search_filter.city is not None:
        relaxations.append(("🗺 Вся республика", "city"))

    for label, relaxation_kind in relaxations[:6]:
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=RelaxFilterCallback(filter_token=filter_token, relaxation_kind=relaxation_kind).pack(),
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🔄 Сбросить все фильтры",
            callback_data=RelaxFilterCallback(filter_token=filter_token, relaxation_kind="all").pack(),
        ),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data=MenuCallback(action_name="home").pack()),
    )
    return builder.as_markup()


def build_event_card_keyboard(
    event: Event,
    *,
    filter_token: str | None = None,
    return_page: int = 1,
    plan_token: str | None = None,
    public_web_base_url: str = "",
) -> InlineKeyboardMarkup:
    """Строит инлайн-клавиатуру для подробной карточки мероприятия.

    Arguments:
        event: Открытая карточка мероприятия.
        filter_token: Токен предыдущей выдачи поиска для кнопки возврата.
        return_page: Номер страницы выдачи для возврата.
        plan_token: Токен маршрута «Выходной день» для кнопки возврата к плану.
        public_web_base_url: Публичный URL веб-каталога (если настроен).

    Returns:
        Инлайн-клавиатура с действиями карточки.
    """
    builder = InlineKeyboardBuilder()

    external_links: list[InlineKeyboardButton] = []
    clean_web_url = sanitize_public_url(public_web_base_url)
    if clean_web_url is not None:
        external_links.append(
            InlineKeyboardButton(text="🌐 Страница на сайте", url=f"{clean_web_url}/events/{event.identifier}")
        )

    source_target_url = sanitize_public_url(event.organization_link) or sanitize_public_url(event.source_url)
    if source_target_url is not None:
        external_links.append(InlineKeyboardButton(text="🔗 Источник / билеты", url=source_target_url))

    if external_links:
        builder.row(*external_links)

    builder.row(
        InlineKeyboardButton(
            text="🔍 Похожие события",
            callback_data=SimilarEventsCallback(event_identifier=str(event.identifier)).pack(),
        )
    )

    navigation_buttons: list[InlineKeyboardButton] = []
    if plan_token:
        navigation_buttons.append(
            InlineKeyboardButton(
                text="⬅️ К маршруту", callback_data=WeekendPlanCallback(action="show", value=plan_token).pack()
            )
        )
    elif filter_token:
        navigation_buttons.append(
            InlineKeyboardButton(
                text="⬅️ К результатам",
                callback_data=PageCallback(filter_token=filter_token, page_number=max(1, return_page)).pack(),
            )
        )
    navigation_buttons.append(
        InlineKeyboardButton(text="🏠 Главное меню", callback_data=MenuCallback(action_name="home").pack())
    )
    builder.row(*navigation_buttons)
    return builder.as_markup()


def build_similar_events_keyboard(
    similar_events: tuple[Event, ...], *, source_event_identifier: UUID
) -> InlineKeyboardMarkup:
    """Строит клавиатуру со списком похожих событий и возвратом к исходной карточке.

    Arguments:
        similar_events: Подобранные похожие мероприятия.
        source_event_identifier: Идентификатор исходной карточки.

    Returns:
        Инлайн-клавиатура похожих событий.
    """
    builder = InlineKeyboardBuilder()
    for index, event in enumerate(similar_events, start=1):
        short_title = truncate_plain_text(event.title, maximum_length=_MAXIMUM_EVENT_BUTTON_TITLE_LENGTH)
        builder.row(
            InlineKeyboardButton(
                text=f"🎟 {index}. {short_title}",
                callback_data=EventCardCallback(event_identifier=str(event.identifier)).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад к карточке",
            callback_data=EventCardCallback(event_identifier=str(source_event_identifier)).pack(),
        ),
        InlineKeyboardButton(text="🏠 Меню", callback_data=MenuCallback(action_name="home").pack()),
    )
    return builder.as_markup()


def build_subscriptions_keyboard(subscriptions: tuple[SubscriptionRecord, ...]) -> InlineKeyboardMarkup:
    """Строит клавиатуру управления подписками пользователя.

    Arguments:
        subscriptions: Активные подписки пользователя в Telegram.

    Returns:
        Инлайн-клавиатура подписок.
    """
    builder = InlineKeyboardBuilder()
    for subscription in subscriptions:
        label = truncate_plain_text(
            format_subscription_label(subscription), maximum_length=_MAXIMUM_SUBSCRIPTION_BUTTON_LENGTH
        )
        builder.row(
            InlineKeyboardButton(
                text=f"🗑 Удалить: {label}",
                callback_data=SubscriptionCallback(
                    action_name="delete", first_argument=subscription.subscription_identifier.hex
                ).pack(),
            )
        )

    builder.row(
        InlineKeyboardButton(text="➕ Оформить подписку", callback_data=SubscriptionCallback(action_name="new").pack())
    )
    if subscriptions:
        builder.row(
            InlineKeyboardButton(
                text="⚠️ Отписаться от всех", callback_data=SubscriptionCallback(action_name="unsubscribe_all").pack()
            )
        )
    builder.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data=MenuCallback(action_name="home").pack()))
    return builder.as_markup()


def build_pending_action_keyboard(action_identifier: UUID) -> InlineKeyboardMarkup:
    """Строит клавиатуру двухшагового подтверждения для изменяющего или массового действия.

    Arguments:
        action_identifier: Уникальный идентификатор ожидающего действия в БД.

    Returns:
        Инлайн-клавиатура с кнопками подтверждения и отмены.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Подтвердить",
        callback_data=PendingActionCallback(decision="confirm", action_identifier=action_identifier.hex),
    )
    builder.button(
        text="❌ Отмена",
        callback_data=PendingActionCallback(decision="cancel", action_identifier=action_identifier.hex),
    )
    builder.adjust(2)
    return builder.as_markup()


def build_weekend_planner_keyboard(
    draft: WeekendPlanRequest, *, reference_now: datetime | None = None
) -> InlineKeyboardMarkup:
    """Строит инлайн-клавиатуру конструктора и пресетов планировщика «Выходной день».

    Arguments:
        draft: Текущий черновик параметров планирования.
        reference_now: Опорное время для расчёта ближайших выходных.

    Returns:
        Инлайн-клавиатура с пресетами и быстрой настройкой ограничений.
    """
    saturday, sunday = resolve_upcoming_weekend_dates(reference_now=reference_now)
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=f"🎯 Уфа · Сб ({saturday:%d.%m}) · до 2000 ₽",
            callback_data=WeekendPlanCallback(action="preset", value="ufa2k").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=f"👶 С ребёнком 6+ · Сб ({saturday:%d.%m})",
            callback_data=WeekendPlanCallback(action="preset", value="kids6").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=f"🆓 Бесплатный день в Уфе · Вс ({sunday:%d.%m})",
            callback_data=WeekendPlanCallback(action="preset", value="ufafree").pack(),
        )
    )

    builder.row(
        InlineKeyboardButton(
            text=f"🏙 {truncate_plain_text(draft.city, maximum_length=16)}",
            callback_data=WeekendPlanCallback(action="cities").pack(),
        ),
        InlineKeyboardButton(
            text=f"{'✓ ' if draft.target_date == saturday else ''}Сб {saturday:%d.%m}",
            callback_data=WeekendPlanCallback(action="day", value="sat").pack(),
        ),
        InlineKeyboardButton(
            text=f"{'✓ ' if draft.target_date == sunday else ''}Вс {sunday:%d.%m}",
            callback_data=WeekendPlanCallback(action="day", value="sun").pack(),
        ),
    )

    builder.row(
        *[
            InlineKeyboardButton(
                text=f"{'✓ ' if draft.budget_rubles == budget else ''}{'0 ₽' if budget == 0 else f'{budget} ₽'}",
                callback_data=WeekendPlanCallback(action="bud", value=str(budget)).pack(),
            )
            for budget in (0, 1000, 2000, 5000)
        ]
    )

    builder.row(
        *[
            InlineKeyboardButton(
                text=f"{'✓ ' if draft.maximum_travel_minutes == minutes else ''}≤{minutes}м",
                callback_data=WeekendPlanCallback(action="trv", value=str(minutes)).pack(),
            )
            for minutes in (15, 20, 30, 45)
        ]
    )

    age_options: tuple[tuple[str, str, int | None], ...] = (
        ("Все", "none", None),
        ("0+", "0", 0),
        ("6+", "6", 6),
        ("12+", "12", 12),
    )
    builder.row(
        *[
            InlineKeyboardButton(
                text=f"{'✓ ' if draft.child_age == age_value else '👶 ' if age_value is not None else ''}{label}",
                callback_data=WeekendPlanCallback(action="age", value=code).pack(),
            )
            for label, code, age_value in age_options
        ]
    )

    builder.row(
        *[
            InlineKeyboardButton(
                text=f"{'✓ ' if draft.companions_count == count else ''}{count} чел",
                callback_data=WeekendPlanCallback(action="comp", value=str(count)).pack(),
            )
            for count in (1, 2, 3, 4)
        ]
    )

    window_options: tuple[tuple[str, str, time, time], ...] = (
        ("🕙 10–21", "full", time(10, 0), time(21, 0)),
        ("🌞 11–18", "day", time(11, 0), time(18, 0)),
        ("🌆 15–22", "eve", time(15, 0), time(22, 0)),
    )
    builder.row(
        *[
            InlineKeyboardButton(
                text=f"{'✓ ' if (draft.window_start_time, draft.window_end_time) == (start_t, end_t) else ''}{label}",
                callback_data=WeekendPlanCallback(action="win", value=code).pack(),
            )
            for label, code, start_t, end_t in window_options
        ]
    )

    builder.row(
        InlineKeyboardButton(text="🚀 Построить маршрут", callback_data=WeekendPlanCallback(action="run").pack()),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data=MenuCallback(action_name="home").pack()),
    )
    return builder.as_markup()


def build_weekend_planner_cities_keyboard(cities: RealCollection[CityReference]) -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора города для планировщика «Выходной день»."""
    builder = InlineKeyboardBuilder()
    city_names = _collect_display_city_names(cities)[:_CITY_BUTTON_LIMIT]
    for city_name in city_names:
        builder.button(
            text=city_name, callback_data=WeekendPlanCallback(action="city", value=compute_city_token(city_name))
        )
    builder.button(text="⬅️ Назад к параметрам", callback_data=WeekendPlanCallback(action="menu"))
    builder.adjust(*([2] * ((len(city_names) + 1) // 2)), 1)
    return builder.as_markup()


def build_weekend_plan_result_keyboard(
    request: WeekendPlanRequest, result: WeekendPlanResult, *, plan_token: str, public_web_base_url: str = ""
) -> InlineKeyboardMarkup:
    """Строит клавиатуру результата планировщика: карточки событий или ослабление ограничений.

    Arguments:
        request: Запрос, по которому построен маршрут.
        result: Результат планировщика `WeekendPlannerService`.
        plan_token: Короткий токен маршрута в сессии пользователя.
        public_web_base_url: Публичный URL веб-каталога (если настроен).

    Returns:
        Инлайн-клавиатура с карточками шагов либо кнопками ослабления ограничений.
    """
    del request
    builder = InlineKeyboardBuilder()

    if result.is_feasible:
        for index, step in enumerate(result.steps, start=1):
            short_title = truncate_plain_text(step.title, maximum_length=_MAXIMUM_EVENT_BUTTON_TITLE_LENGTH)
            builder.row(
                InlineKeyboardButton(
                    text=f"🎟 {index}. {short_title}",
                    callback_data=EventCardCallback(event_identifier=str(step.event_identifier)).pack(),
                )
            )
        builder.row(
            InlineKeyboardButton(
                text="⚙️ Изменить параметры", callback_data=WeekendPlanCallback(action="edit", value=plan_token).pack()
            ),
            InlineKeyboardButton(text="🔄 Пресеты", callback_data=WeekendPlanCallback(action="menu").pack()),
        )
        footer_buttons: list[InlineKeyboardButton] = []
        clean_web_url = sanitize_public_url(public_web_base_url)
        if clean_web_url is not None:
            footer_buttons.append(InlineKeyboardButton(text="🌐 Веб-каталог", url=clean_web_url))
        footer_buttons.append(
            InlineKeyboardButton(text="🏠 Главное меню", callback_data=MenuCallback(action_name="home").pack())
        )
        builder.row(*footer_buttons)
        return builder.as_markup()

    relax_labels: dict[str, str] = {
        "budget": "💳 Увеличить бюджет",
        "time_window": "🕒 Расширить окно (весь день)",
        "travel": "🚌 Больше время переезда",
        "age": "👶 Убрать детский фильтр",
        "count": "📅 Попробовать другой день",
    }
    seen_constraints: set[str] = set()
    for suggestion in result.relaxation_suggestions:
        constraint_name = suggestion.constraint_name
        if constraint_name in seen_constraints or constraint_name not in relax_labels:
            continue
        seen_constraints.add(constraint_name)
        builder.row(
            InlineKeyboardButton(
                text=relax_labels[constraint_name],
                callback_data=WeekendPlanCallback(action="relax", value=plan_token, extra=constraint_name).pack(),
            )
        )

    builder.row(
        InlineKeyboardButton(text="🏙 Другой город", callback_data=WeekendPlanCallback(action="cities").pack()),
        InlineKeyboardButton(
            text="⚙️ Параметры", callback_data=WeekendPlanCallback(action="edit", value=plan_token).pack()
        ),
    )
    builder.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data=MenuCallback(action_name="home").pack()))
    return builder.as_markup()


def build_weekend_plan_error_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру восстановления после ошибки параметров `/plan`."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⚙️ Настроить маршрут", callback_data=WeekendPlanCallback(action="menu").pack()),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data=MenuCallback(action_name="home").pack()),
    )
    return builder.as_markup()


def _collect_display_city_names(cities: RealCollection[CityReference]) -> list[str]:
    seen_normalized: set[str] = set()
    ordered_names: list[str] = []
    for city_reference in cities:
        lowered = city_reference.name.strip().lower()
        if lowered and lowered not in seen_normalized:
            seen_normalized.add(lowered)
            ordered_names.append(city_reference.name.strip())
    for default_city in DEFAULT_BASHKORTOSTAN_CITIES:
        lowered = default_city.strip().lower()
        if lowered not in seen_normalized:
            seen_normalized.add(lowered)
            ordered_names.append(default_city)
    return ordered_names
