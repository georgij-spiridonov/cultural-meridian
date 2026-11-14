"""Форматирование сообщений Telegram-бота в безопасном HTML-представлении."""

from datetime import date, datetime
from html import escape
from typing import Final

from src.core.types import EventCategory, EventStatus
from src.domain.entities import (
    EmptySearchExplanation,
    Event,
    EventSearchFilter,
    EventSearchPage,
    SubscriptionRecord,
    WeekendPlanRequest,
    WeekendPlanResult,
    WeekendPlanStep,
)
from src.domain.services import BASHKORTOSTAN_TIMEZONE, normalize_optional_text
from src.entrypoints.web._formatting import (
    CATEGORY_LABELS,
    SERVICE_DISCLAIMER_TEXT,
    SOURCE_TYPE_LABELS,
    format_date_range_label,
    format_price_label,
    format_status_label,
)

__all__ = [
    "MAXIMUM_TELEGRAM_MESSAGE_LENGTH",
    "escape_html",
    "format_active_filter_summary",
    "format_cancel_message",
    "format_empty_search_message",
    "format_event_card_message",
    "format_events_page_message",
    "format_help_message",
    "format_similar_events_message",
    "format_start_message",
    "format_subscription_label",
    "format_subscriptions_message",
    "format_telegram_datetime",
    "format_weekend_plan_error_message",
    "format_weekend_plan_result_message",
    "format_weekend_planner_menu_message",
    "format_weekend_planner_unavailable_message",
    "sanitize_public_url",
    "truncate_plain_text",
]

MAXIMUM_TELEGRAM_MESSAGE_LENGTH: Final[int] = 4096
_MAXIMUM_LIST_TITLE_LENGTH: Final[int] = 110
_MAXIMUM_CARD_DESCRIPTION_LENGTH: Final[int] = 1200
_MAXIMUM_PLAN_STEP_TITLE_LENGTH: Final[int] = 90
_WEEKDAYS_SHORT: Final[tuple[str, ...]] = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def escape_html(value: str) -> str:
    """Экранирует пользовательские и внешние данные для режима Telegram HTML.

    Arguments:
        value: Исходная строка текста.

    Returns:
        Безопасная строка с экранированными спецсимволами HTML.
    """
    return escape(value, quote=True)


def sanitize_public_url(raw_url: str | None) -> str | None:
    """Проверяет, что ссылка использует безопасный протокол `http` или `https`.

    Arguments:
        raw_url: Исходная строка URL или `None`.

    Returns:
        Очищенный URL либо `None`, если протокол недопустим или строка пуста.
    """
    cleaned = normalize_optional_text(raw_url)
    if cleaned is None:
        return None
    lowered = cleaned.lower()
    if lowered.startswith(("https://", "http://")):
        return cleaned
    return None


def truncate_plain_text(value: str, *, maximum_length: int) -> str:
    """Аккуратно обрезает длинный текст по границе слова с многоточием.

    Arguments:
        value: Исходный текст до HTML-экранирования.
        maximum_length: Максимальная длина итоговой строки.

    Returns:
        Строка длиной не более `maximum_length`.
    """
    cleaned = " ".join(value.split())
    if len(cleaned) <= maximum_length:
        return cleaned
    if maximum_length <= 1:
        return cleaned[:maximum_length]
    truncated = cleaned[: maximum_length - 1].rsplit(" ", maxsplit=1)[0].strip()
    if not truncated:
        truncated = cleaned[: maximum_length - 1].strip()
    return f"{truncated}…"


def format_telegram_datetime(event: Event) -> str:
    """Форматирует дату и время события в часовом поясе Республики Башкортостан (`+05:00`).

    Arguments:
        event: Карточка культурного мероприятия.

    Returns:
        Локализованная строка даты и времени с явным указанием смещения `+05:00`.
    """
    return f"{format_date_range_label(event)} (+05:00)"


def format_start_message() -> str:
    """Формирует компактное приветствие для команды `/start` с обязательным дисклеймером."""
    return (
        "<b>Культурный Меридиан — афиша Башкортостана</b>\n\n"
        "Помогу подобрать концерты, спектакли, выставки, фестивали, кинопоказы и экскурсии по городам республики.\n\n"
        "Выберите быстрый фильтр кнопками ниже или просто напишите запрос, например:\n"
        "• <i>Бесплатные выставки в Уфе на выходные</i>\n"
        "• <i>Концерт в Стерлитамаке до 1500 рублей</i>\n"
        "• <i>Куда сходить с ребёнком 7 лет</i>\n\n"
        f"<i>ℹ️ {escape_html(SERVICE_DISCLAIMER_TEXT)}</i>"
    )


def format_help_message() -> str:
    """Формирует краткую справку по командам и способам поиска в боте."""
    return (
        "<b>Справка по боту «Культурный Меридиан»</b>\n\n"
        "<b>Доступные команды:</b>\n"
        "• /start — главное меню и быстрые фильтры\n"
        "• /plan — планировщик маршрута «Выходной день» (например: <code>/plan Уфа 2000</code>)\n"
        "• /subscribe — подписка на уведомления по городу и категории\n"
        "• /unsubscribe — просмотр и удаление подписок\n"
        "• /cancel — отмена текущего шага или подтверждения\n"
        "• /help — эта справка\n\n"
        "<b>Поиск текстом:</b>\n"
        "Напишите запрос свободным текстом с указанием города, периода, категории, бюджета или возраста — "
        "например: <i>«Концерт в Салавате завтра до 1000 руб»</i> или <i>«Спектакли для детей 6+ в Уфе»</i>.\n\n"
        "Время всех мероприятий указано в часовом поясе Уфы (<code>Asia/Yekaterinburg</code>, +05:00).\n\n"
        f"<i>ℹ️ {escape_html(SERVICE_DISCLAIMER_TEXT)}</i>"
    )


def format_cancel_message(*, had_pending_action: bool) -> str:
    """Формирует ответ для команды `/cancel` при сбросе шага или подтверждения.

    Arguments:
        had_pending_action: Признак того, что было отменено активное ожидающее подтверждение.

    Returns:
        HTML-текст ответа пользователю.
    """
    if had_pending_action:
        return (
            "Ожидающее подтверждение действие отменено, а текущие шаги сброшены.\n"
            "Можете начать новый поиск или выбрать раздел в меню."
        )
    return "Текущий шаг сброшен. Выберите фильтр или напишите новый поисковый запрос."


def format_active_filter_summary(search_filter: EventSearchFilter) -> str:
    """Собирает краткое описание активных параметров поиска для заголовка выдачи.

    Arguments:
        search_filter: Применённый фильтр поиска.

    Returns:
        Строка вида `Уфа · Концерты · Бесплатно` либо `Все события`.
    """
    parts: list[str] = []
    if search_filter.city:
        parts.append(search_filter.city)
    if search_filter.category is not None:
        parts.append(CATEGORY_LABELS.get(search_filter.category, search_filter.category.value))
    if search_filter.date_from is not None and search_filter.date_to is not None:
        local_from = search_filter.date_from.astimezone(BASHKORTOSTAN_TIMEZONE)
        local_to = search_filter.date_to.astimezone(BASHKORTOSTAN_TIMEZONE)
        if local_from.date() == local_to.date():
            parts.append(f"{local_from:%d.%m.%Y}")
        else:
            parts.append(f"{local_from:%d.%m}–{local_to:%d.%m.%Y}")
    elif search_filter.date_from is not None:
        parts.append(f"с {search_filter.date_from.astimezone(BASHKORTOSTAN_TIMEZONE):%d.%m.%Y}")
    if search_filter.only_free:
        parts.append("Бесплатно")
    elif search_filter.maximum_price is not None:
        parts.append(f"до {search_filter.maximum_price} ₽")
    if search_filter.maximum_age_limit is not None:
        parts.append(f"до {search_filter.maximum_age_limit}+")
    if search_filter.language:
        parts.append(f"язык: {search_filter.language}")
    if search_filter.venue:
        parts.append(f"площадка: {search_filter.venue}")
    if search_filter.query:
        parts.append(f"«{search_filter.query}»")
    return " · ".join(parts) if parts else "Все опубликованные события"


def format_events_page_message(
    search_page: EventSearchPage, *, search_filter: EventSearchFilter, heading: str = "Афиша культуры Башкортостана"
) -> str:
    """Форматирует компактный список карточек текущей страницы без длинного текста.

    Arguments:
        search_page: Страница результатов поиска.
        search_filter: Фильтр, по которому получена страница.
        heading: Заголовок подборки.

    Returns:
        Готовый HTML-текст выдачи длиной в пределах лимита Telegram.
    """
    start_index = (search_page.page - 1) * search_page.page_size + 1
    end_index = start_index + len(search_page.items) - 1
    filter_summary = format_active_filter_summary(search_filter)

    lines: list[str] = [
        f"<b>{escape_html(heading)}</b>",
        f"<i>Фильтр: {escape_html(filter_summary)}</i>",
        f"Найдено: <b>{search_page.total_count}</b> (показаны {start_index}–{end_index})",
        "",
    ]

    for offset, event in enumerate(search_page.items, start=start_index):
        category_label = CATEGORY_LABELS.get(event.category, event.category.value)
        short_title = truncate_plain_text(event.title, maximum_length=_MAXIMUM_LIST_TITLE_LENGTH)
        datetime_label = format_telegram_datetime(event)
        price_label = format_price_label(event)
        lines.append(f"<b>{offset}. {escape_html(short_title)}</b> · <i>{escape_html(category_label)}</i>")
        lines.append(f"📅 {escape_html(datetime_label)}")
        lines.append(
            f"📍 {escape_html(event.city)}, {escape_html(event.venue)} · "
            f"💳 {escape_html(price_label)} · 🔞 {event.age_limit}+"
        )
        lines.append("")

    lines.append("Нажмите на кнопку с номером и названием ниже, чтобы открыть подробную карточку.")
    lines.append(f"<i>ℹ️ {escape_html(SERVICE_DISCLAIMER_TEXT)}</i>")
    return _fit_telegram_message("\n".join(lines))


def format_empty_search_message(explanation: EmptySearchExplanation, *, search_filter: EventSearchFilter) -> str:
    """Форматирует объяснение пустой выдачи и рекомендации по ослаблению ограничений.

    Arguments:
        explanation: Структурированное объяснение от `EventSearchService`.
        search_filter: Фильтр, не давший результатов.

    Returns:
        HTML-сообщение с указанием мешающих ограничений и вариантов ослабления.
    """
    filter_summary = format_active_filter_summary(search_filter)
    lines: list[str] = [
        "<b>По вашему запросу мероприятия не найдены</b>",
        f"<i>Фильтр: {escape_html(filter_summary)}</i>",
        "",
        escape_html(explanation.summary),
    ]
    if explanation.blocking_constraints:
        lines.append("")
        lines.append("<b>Мешающие ограничения:</b>")
        for constraint in explanation.blocking_constraints:
            lines.append(f"• {escape_html(constraint)}")

    if explanation.suggested_relaxations:
        lines.append("")
        lines.append("<b>Что можно ослабить:</b>")
        for suggestion in explanation.suggested_relaxations:
            lines.append(f"• {escape_html(suggestion)}")

    lines.append("")
    lines.append("Выберите кнопку ниже для быстрого ослабления фильтра или введите другой запрос.")
    return _fit_telegram_message("\n".join(lines))


def format_event_card_message(event: Event) -> str:
    """Формирует полную карточку мероприятия со статусом, описанием и всеми подтверждёнными источниками.

    Arguments:
        event: Карточка культурного мероприятия.

    Returns:
        HTML-текст подробной карточки мероприятия.
    """
    category_label = CATEGORY_LABELS.get(event.category, event.category.value)
    status_label = format_status_label(event.status)
    datetime_label = format_telegram_datetime(event)
    price_label = format_price_label(event)

    lines: list[str] = []
    if event.status is EventStatus.CANCELED:
        lines.append("⚠️ <b>ВНИМАНИЕ: МЕРОПРИЯТИЕ ОТМЕНЕНО</b>")
        lines.append("")
    elif event.status is EventStatus.DRAFT:
        lines.append("ℹ️ <b>Черновик (карточка находится на проверке)</b>")
        lines.append("")

    lines.extend(
        [
            f"<b>{escape_html(event.title)}</b>",
            f"🎭 Категория: {escape_html(category_label)} · Статус: <b>{escape_html(status_label)}</b>",
            f"📅 Дата и время: {escape_html(datetime_label)} (<code>Asia/Yekaterinburg</code>)",
            f"📍 Место: {escape_html(event.city)}, {escape_html(event.venue)}",
            f"🗺 Адрес: {escape_html(event.address)}",
            f"💳 Стоимость: <b>{escape_html(price_label)}</b> · 🔞 Возраст: <b>{event.age_limit}+</b>",
        ]
    )

    if event.language:
        lines.append(f"🗣 Язык: {escape_html(event.language)}")
    if event.tags:
        tags_text = ", ".join(escape_html(tag) for tag in event.tags)
        lines.append(f"🏷 Метки: {tags_text}")

    cleaned_description = normalize_optional_text(event.description)
    if cleaned_description is not None:
        safe_description = truncate_plain_text(cleaned_description, maximum_length=_MAXIMUM_CARD_DESCRIPTION_LENGTH)
        lines.extend(["", "<b>Описание:</b>", escape_html(safe_description)])

    organization_url = sanitize_public_url(event.organization_link)
    if organization_url is not None:
        lines.extend(["", f'🎫 Организатор / билеты: <a href="{escape_html(organization_url)}">перейти</a>'])

    lines.extend(["", "<b>Подтверждённые источники:</b>"])
    if event.sources:
        for source in event.sources:
            source_type_label = SOURCE_TYPE_LABELS.get(source.source_type, source.source_type.value)
            lines.append(_format_source_line(source.source_name, source_type_label, source.source_url))
    else:
        fallback_source_label = SOURCE_TYPE_LABELS.get(event.source_type, event.source_type.value)
        lines.append(_format_source_line(fallback_source_label, fallback_source_label, event.source_url))

    lines.extend(["", f"<i>ℹ️ {escape_html(SERVICE_DISCLAIMER_TEXT)}</i>"])
    return _fit_telegram_message("\n".join(lines))


def format_similar_events_message(source_event: Event, similar_events: tuple[Event, ...]) -> str:
    """Форматирует список похожих мероприятий для выбранной карточки.

    Arguments:
        source_event: Исходная карточка мероприятия.
        similar_events: Найденные похожие события.

    Returns:
        HTML-сообщение со списком похожих событий или сообщением об их отсутствии.
    """
    if not similar_events:
        return (
            f"Похожих опубликованных мероприятий для «{escape_html(source_event.title)}» сейчас не найдено.\n"
            "Попробуйте изменить город или выбрать другой период поиска."
        )

    lines: list[str] = [
        f"<b>Похожие мероприятия для «{escape_html(truncate_plain_text(source_event.title, maximum_length=80))}»</b>",
        "",
    ]
    for index, event in enumerate(similar_events, start=1):
        short_title = truncate_plain_text(event.title, maximum_length=_MAXIMUM_LIST_TITLE_LENGTH)
        lines.append(f"<b>{index}. {escape_html(short_title)}</b>")
        lines.append(f"📅 {escape_html(format_telegram_datetime(event))}")
        lines.append(
            f"📍 {escape_html(event.city)}, {escape_html(event.venue)} · "
            f"💳 {escape_html(format_price_label(event))} · 🔞 {event.age_limit}+"
        )
        lines.append("")

    lines.append(f"<i>ℹ️ {escape_html(SERVICE_DISCLAIMER_TEXT)}</i>")
    return _fit_telegram_message("\n".join(lines))


def format_subscription_label(subscription: SubscriptionRecord) -> str:
    """Формирует краткое описание фильтров одной подписки.

    Arguments:
        subscription: Запись подписки пользователя.

    Returns:
        Человекочитаемая строка вида `Уфа · Концерты`.
    """
    city_label = subscription.city or "Все города"
    category_label = (
        CATEGORY_LABELS.get(subscription.category, subscription.category.value)
        if isinstance(subscription.category, EventCategory)
        else "Все категории"
    )
    return f"{city_label} · {category_label}"


def format_subscriptions_message(subscriptions: tuple[SubscriptionRecord, ...]) -> str:
    """Формирует экран управления активными подписками пользователя.

    Arguments:
        subscriptions: Кортеж активных подписок пользователя в Telegram.

    Returns:
        HTML-текст со списком подписок и пояснением политики хранения данных.
    """
    lines: list[str] = ["<b>Ваши подписки на афишу</b>", ""]
    if not subscriptions:
        lines.append("У вас пока нет активных подписок.")
        lines.append("Нажмите «➕ Оформить подписку», чтобы получать подборки по нужному городу и категории.")
    else:
        for index, subscription in enumerate(subscriptions, start=1):
            lines.append(f"{index}. {escape_html(format_subscription_label(subscription))}")
        lines.append("")
        lines.append("Нажмите кнопку удаления напротив подписки, чтобы немедленно удалить её из базы.")

    lines.append("")
    lines.append(
        "<i>Мы храним только обезличенный идентификатор пользователя и выбранные фильтры без телефонов и ФИО. "
        "При отписке запись удаляется физически.</i>"
    )
    return _fit_telegram_message("\n".join(lines))


def format_weekend_planner_menu_message(draft: WeekendPlanRequest) -> str:
    """Формирует экран конструктора и готовых пресетов планировщика «Выходной день».

    Arguments:
        draft: Текущий черновик параметров маршрута пользователя.

    Returns:
        HTML-сообщение с параметрами маршрута и подсказкой по команде `/plan`.
    """
    date_label = _format_short_date(draft.target_date)
    budget_label = "Бесплатно (0 ₽)" if draft.budget_rubles == 0 else f"до {draft.budget_rubles} ₽"
    child_label = f"до {draft.child_age}+" if draft.child_age is not None else "без ограничений"

    lines = [
        "<b>Планировщик маршрута «Выходной день»</b>",
        "Подберёт 2–4 непересекающихся события с учётом бюджета, возраста и времени в пути.",
        "",
        "<b>Текущие параметры:</b>",
        f"• Город: <b>{escape_html(draft.city)}</b>",
        f"• Дата: <b>{escape_html(date_label)}</b>",
        f"• Свободное окно: <b>{draft.window_start_time:%H:%M}–{draft.window_end_time:%H:%M}</b> (+05:00)",
        f"• Бюджет на компанию: <b>{escape_html(budget_label)}</b> · Участники: <b>{draft.companions_count} чел.</b>",
        f"• Детский фильтр: <b>{escape_html(child_label)}</b> · Переезд: <b>до {draft.maximum_travel_minutes} мин.</b>",
        "",
        "Выберите готовый пресет, настройте параметры кнопками ниже или отправьте команду:",
        "<code>/plan Уфа 2000</code> или <code>/plan Стерлитамак 1500 2</code>",
    ]
    return _fit_telegram_message("\n".join(lines))


def format_weekend_plan_result_message(request: WeekendPlanRequest, result: WeekendPlanResult) -> str:
    """Форматирует результат планировщика «Выходной день» без перегрузки текстом.

    Arguments:
        request: Запрос, по которому строился маршрут.
        result: Итоговый результат работы `WeekendPlannerService`.

    Returns:
        Готовое HTML-сообщение в пределах лимита Telegram.
    """
    date_label = _format_short_date(request.target_date)
    budget_label = "Бесплатно" if request.budget_rubles == 0 else f"до {request.budget_rubles} ₽"
    age_suffix = f" · 👶 {request.child_age}+" if request.child_age is not None else ""
    summary_meta = (
        f"📅 {escape_html(date_label)} · 🕒 {request.window_start_time:%H:%M}–{request.window_end_time:%H:%M} "
        f"(+05:00) · 👥 {request.companions_count} чел. · 💳 {escape_html(budget_label)}{age_suffix}"
    )

    if not result.is_feasible:
        lines: list[str] = [
            f"<b>Маршрут на выходной день не собран · {escape_html(request.city)}</b>",
            f"<i>{summary_meta} · 🚌 ≤{request.maximum_travel_minutes} мин.</i>",
            "",
            escape_html(
                result.failure_reason
                or "Не удалось подобрать от 2 до 4 непересекающихся мероприятий при заданных ограничениях."
            ),
        ]
        if result.relaxation_suggestions:
            lines.extend(["", "<b>Что можно ослабить:</b>"])
            for suggestion in result.relaxation_suggestions:
                lines.append(
                    f"• {escape_html(suggestion.explanation)} <i>({escape_html(suggestion.suggested_change)})</i>"
                )
        lines.extend(["", "Нажмите кнопку ниже, чтобы сразу перестроить маршрут с ослабленным ограничением."])
        lines.append(f"<i>ℹ️ {escape_html(SERVICE_DISCLAIMER_TEXT)}</i>")
        return _fit_telegram_message("\n".join(lines))

    lines = [f"<b>Маршрут выходного дня · {escape_html(request.city)}</b>", f"<i>{summary_meta}</i>", ""]

    for index, step in enumerate(result.steps, start=1):
        lines.extend(_format_weekend_plan_step_lines(step, index=index, companions_count=request.companions_count))
        lines.append("")

    total_cost_label = _format_cost_range_label(
        minimum_rubles=result.total_minimum_cost_rubles,
        maximum_rubles=result.total_maximum_cost_rubles,
        companions_count=request.companions_count,
    )
    lines.append(
        f"<b>Итого:</b> {len(result.steps)} события · 💳 {escape_html(total_cost_label)} · "
        f"🚌 в пути ~{result.total_travel_minutes} мин."
    )
    lines.append("Нажмите на кнопку события ниже, чтобы открыть подробную карточку.")
    lines.append(f"<i>ℹ️ {escape_html(SERVICE_DISCLAIMER_TEXT)}</i>")
    return _fit_telegram_message("\n".join(lines))


def format_weekend_plan_error_message(error_message: str) -> str:
    """Форматирует сообщение об ошибке параметров планировщика `InvalidWeekendPlanRequestError`."""
    return (
        "<b>Не удалось построить маршрут по заданным параметрам</b>\n\n"
        f"{escape_html(error_message)}\n\n"
        "Проверьте параметры или настройте маршрут через интерактивное меню ниже.\n"
        "Пример команды: <code>/plan Уфа 2000</code>"
    )


def format_weekend_planner_unavailable_message() -> str:
    """Форматирует понятное сообщение при недоступности сервиса планировщика."""
    return (
        "<b>Планировщик «Выходной день» временно недоступен</b>\n\n"
        "Вы можете подобрать мероприятия через быстрые фильтры в главном меню "
        "или обычным текстовым запросом, например: <i>«Концерт в Уфе на выходные»</i>."
    )


def _format_weekend_plan_step_lines(step: WeekendPlanStep, *, index: int, companions_count: int) -> list[str]:
    local_start = _to_bashkortostan_datetime(step.starts_at)
    local_end = _to_bashkortostan_datetime(step.ends_at)
    short_title = truncate_plain_text(step.title, maximum_length=_MAXIMUM_PLAN_STEP_TITLE_LENGTH)
    category_label = CATEGORY_LABELS.get(step.category, step.category.value)

    step_max_total = step.price_maximum_rubles * companions_count
    price_label = _format_cost_range_label(
        minimum_rubles=step.total_cost_rubles, maximum_rubles=step_max_total, companions_count=companions_count
    )
    location_part = (
        f"{escape_html(step.venue)}, {escape_html(step.address)}" if step.address else escape_html(step.venue)
    )

    step_lines = [
        f"<b>{index}. {local_start:%H:%M}–{local_end:%H:%M} — {escape_html(short_title)}</b> "
        f"<i>({escape_html(category_label)})</i>",
        f"📍 {location_part}",
    ]
    if index > 1:
        travel_label = _format_step_travel_label(step.travel_from_previous_minutes)
        step_lines.append(f"{travel_label} · 💳 {escape_html(price_label)}")
    else:
        step_lines.append(f"💳 {escape_html(price_label)}")
    return step_lines


def _format_step_travel_label(travel_minutes: int) -> str:
    if travel_minutes <= 0:
        return "📍 на той же площадке"
    if travel_minutes <= 12:
        return f"🚶 {travel_minutes} мин"
    return f"🚌 {travel_minutes} мин"


def _format_cost_range_label(*, minimum_rubles: int, maximum_rubles: int, companions_count: int) -> str:
    suffix = f" (на {companions_count} чел.)" if companions_count > 1 else ""
    if minimum_rubles <= 0 and maximum_rubles <= 0:
        return "Бесплатно"
    if maximum_rubles <= minimum_rubles:
        return f"{minimum_rubles} ₽{suffix}"
    return f"от {minimum_rubles} до {maximum_rubles} ₽{suffix}"


def _format_short_date(target_date: date) -> str:
    weekday_label = _WEEKDAYS_SHORT[target_date.weekday()]
    return f"{weekday_label}, {target_date:%d.%m.%Y}"


def _to_bashkortostan_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BASHKORTOSTAN_TIMEZONE)
    return value.astimezone(BASHKORTOSTAN_TIMEZONE)


def _format_source_line(source_name: str, source_type_label: str, source_url: str) -> str:
    safe_url = sanitize_public_url(source_url)
    safe_name = escape_html(source_name)
    safe_type = escape_html(source_type_label)
    if safe_url is not None:
        return f'• <a href="{escape_html(safe_url)}">{safe_name}</a> ({safe_type})'
    return f"• {safe_name} ({safe_type}): {escape_html(source_url)}"


def _fit_telegram_message(text: str) -> str:
    if len(text) <= MAXIMUM_TELEGRAM_MESSAGE_LENGTH:
        return text
    lines = text.splitlines()
    while len("\n".join(lines)) > MAXIMUM_TELEGRAM_MESSAGE_LENGTH - 32 and len(lines) > 3:
        lines.pop(-2)
    fitted = "\n".join(lines)
    if len(fitted) <= MAXIMUM_TELEGRAM_MESSAGE_LENGTH:
        return fitted
    return fitted[: MAXIMUM_TELEGRAM_MESSAGE_LENGTH - 1] + "…"
