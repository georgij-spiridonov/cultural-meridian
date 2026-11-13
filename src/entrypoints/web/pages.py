"""SSR и HTMX-маршруты публичного веб-каталога, карточки события, подписок и обратной связи."""

from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Form, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.application import EventSearchService
from src.core.exceptions import ApplicationException
from src.core.types import DialogChannel, EventCategory, EventStatus
from src.domain.entities import EmptySearchExplanation, Event, EventSearchFilter, EventSortField, SortDirection
from src.domain.exceptions import EventNotFoundError, InvalidSearchFilterError
from src.domain.services import normalize_optional_text
from src.entrypoints.bootstrap import ApplicationContainer
from src.entrypoints.web._formatting import (
    ALLOWED_AGE_OPTIONS,
    CATEGORY_LABELS,
    PERIOD_LABELS,
    SERVICE_DISCLAIMER_TEXT,
    SOURCE_TYPE_LABELS,
    VISITOR_COOKIE_NAME,
    CatalogQueryParameters,
    build_catalog_url_query,
    build_search_filter_from_parameters,
    format_date_range_label,
    format_price_label,
    format_status_label,
    generate_visitor_identifier,
)
from src.entrypoints.web.middleware import InMemoryRateLimiter

__all__ = ["create_pages_router"]

_CATEGORY_FILTER_CHIPS: tuple[tuple[str, str], ...] = tuple(
    (category.value, CATEGORY_LABELS[category]) for category in EventCategory
)
_PERIOD_FILTER_CHIPS: tuple[tuple[str, str], ...] = tuple(PERIOD_LABELS.items())


def create_pages_router(*, templates: Jinja2Templates) -> APIRouter:
    """Создаёт маршрутизатор HTML-страниц и HTMX-фрагментов публичной афиши."""
    router = APIRouter(include_in_schema=False)

    @router.get("/", response_class=HTMLResponse)
    async def catalog_page(
        request: Request,
        query: Annotated[str | None, Query()] = None,
        city: Annotated[str | None, Query()] = None,
        category: Annotated[str | None, Query()] = None,
        period: Annotated[str | None, Query()] = None,
        date_from: Annotated[str | None, Query()] = None,
        date_to: Annotated[str | None, Query()] = None,
        maximum_price: Annotated[str | None, Query()] = None,
        only_free: Annotated[str | None, Query()] = None,
        maximum_age_limit: Annotated[str | None, Query()] = None,
        language: Annotated[str | None, Query()] = None,
        venue: Annotated[str | None, Query()] = None,
        near_center: Annotated[str | None, Query()] = None,
        sort_field: Annotated[str | None, Query()] = None,
        sort_direction: Annotated[str | None, Query()] = None,
        page: Annotated[str | None, Query()] = None,
        page_size: Annotated[str | None, Query()] = None,
    ) -> HTMLResponse:
        """Рендерит главную страницу каталога или HTMX-фрагмент выдачи с синхронизацией URL."""
        container = _require_container(request)
        parameters = _parse_catalog_query_parameters(
            query=query,
            city=city,
            category=category,
            period=period,
            date_from=date_from,
            date_to=date_to,
            maximum_price=maximum_price,
            only_free=only_free,
            maximum_age_limit=maximum_age_limit,
            language=language,
            venue=venue,
            near_center=near_center,
            sort_field=sort_field,
            sort_direction=sort_direction,
            page=page,
            page_size=page_size,
        )
        search_context = await _execute_catalog_search(search_service=container.search_service, parameters=parameters)

        if request.headers.get("HX-Request") == "true" and request.headers.get("HX-Target") == "catalog-results":
            return templates.TemplateResponse(
                request=request,
                name="partials/events_results.html",
                context=_base_page_context(request=request, active_nav="catalog", **search_context),
            )

        cities = await container.search_service.list_cities()
        return templates.TemplateResponse(
            request=request,
            name="catalog.html",
            context=_base_page_context(
                request=request,
                active_nav="catalog",
                cities=cities,
                category_options=_CATEGORY_FILTER_CHIPS,
                period_options=_PERIOD_FILTER_CHIPS,
                age_options=ALLOWED_AGE_OPTIONS,
                **search_context,
            ),
        )

    @router.get("/partials/events", response_class=HTMLResponse)
    async def catalog_results_partial(
        request: Request,
        query: Annotated[str | None, Query()] = None,
        city: Annotated[str | None, Query()] = None,
        category: Annotated[str | None, Query()] = None,
        period: Annotated[str | None, Query()] = None,
        date_from: Annotated[str | None, Query()] = None,
        date_to: Annotated[str | None, Query()] = None,
        maximum_price: Annotated[str | None, Query()] = None,
        only_free: Annotated[str | None, Query()] = None,
        maximum_age_limit: Annotated[str | None, Query()] = None,
        language: Annotated[str | None, Query()] = None,
        venue: Annotated[str | None, Query()] = None,
        near_center: Annotated[str | None, Query()] = None,
        sort_field: Annotated[str | None, Query()] = None,
        sort_direction: Annotated[str | None, Query()] = None,
        page: Annotated[str | None, Query()] = None,
        page_size: Annotated[str | None, Query()] = None,
    ) -> HTMLResponse:
        """Возвращает HTML-фрагмент выдачи мероприятий и обновляет строку URL браузера."""
        container = _require_container(request)
        parameters = _parse_catalog_query_parameters(
            query=query,
            city=city,
            category=category,
            period=period,
            date_from=date_from,
            date_to=date_to,
            maximum_price=maximum_price,
            only_free=only_free,
            maximum_age_limit=maximum_age_limit,
            language=language,
            venue=venue,
            near_center=near_center,
            sort_field=sort_field,
            sort_direction=sort_direction,
            page=page,
            page_size=page_size,
        )
        search_context = await _execute_catalog_search(search_service=container.search_service, parameters=parameters)
        share_url = f"/{build_catalog_url_query(parameters)}"
        return templates.TemplateResponse(
            request=request,
            name="partials/events_results.html",
            headers={"HX-Push-Url": share_url},
            context=_base_page_context(request=request, active_nav="catalog", **search_context),
        )

    @router.get("/events/{event_identifier}", response_class=HTMLResponse)
    async def event_detail_page(request: Request, event_identifier: UUID) -> HTMLResponse:
        """Рендерит постоянную страницу карточки мероприятия со всеми подтверждёнными источниками."""
        container = _require_container(request)
        try:
            event = await container.search_service.get_event(event_identifier=event_identifier)
        except EventNotFoundError:
            return templates.TemplateResponse(
                request=request,
                name="not_found.html",
                status_code=status.HTTP_404_NOT_FOUND,
                context=_base_page_context(
                    request=request,
                    active_nav="catalog",
                    missing_message="Мероприятие не найдено или находится на проверке модератором.",
                ),
            )

        similar_events: tuple[Event, ...] = ()
        if event.status is EventStatus.PUBLISHED:
            similar_events = await container.search_service.find_similar_events(
                event_identifier=event.identifier, limit=3
            )

        return templates.TemplateResponse(
            request=request,
            name="event_detail.html",
            context=_base_page_context(
                request=request,
                active_nav="catalog",
                event=event,
                similar_events=similar_events,
                feedback_submitted=request.query_params.get("feedback") == "ok",
            ),
        )

    @router.get("/subscriptions", response_class=HTMLResponse)
    async def subscriptions_page(request: Request) -> Response:
        """Рендерит страницу управления подписками (без ПДн) и общей формы обратной связи."""
        container = _require_container(request)
        visitor_identifier, should_set_cookie = _resolve_visitor_identifier(request)
        cities = await container.search_service.list_cities()
        active_subscriptions = await container.subscription_service.list_subscriptions(
            channel=DialogChannel.WEB, subscriber_identifier=visitor_identifier
        )
        response = templates.TemplateResponse(
            request=request,
            name="subscriptions.html",
            context=_base_page_context(
                request=request,
                active_nav="subscriptions",
                visitor_identifier=visitor_identifier,
                cities=cities,
                category_options=_CATEGORY_FILTER_CHIPS,
                subscriptions=active_subscriptions,
                notice_message=request.query_params.get("notice"),
                error_message=request.query_params.get("error"),
            ),
        )
        if should_set_cookie:
            _attach_visitor_cookie(response, visitor_identifier=visitor_identifier)
        return response

    @router.post("/subscriptions", response_class=HTMLResponse)
    async def create_subscription_action(
        request: Request, city: Annotated[str | None, Form()] = None, category: Annotated[str | None, Form()] = None
    ) -> Response:
        """Оформляет подписку браузерной сессии на город и категорию."""
        container = _require_container(request)
        visitor_identifier, should_set_cookie = _resolve_visitor_identifier(request)
        parsed_category = _parse_optional_category(category)

        error_message: str | None = None
        notice_message: str | None = None
        try:
            await container.subscription_service.subscribe(
                channel=DialogChannel.WEB,
                subscriber_identifier=visitor_identifier,
                city=normalize_optional_text(city),
                category=parsed_category,
            )
            notice_message = "Подписка оформлена. Личные контакты и телефоны не сохраняются."
        except ApplicationException as error:
            error_message = error.message

        if request.headers.get("HX-Request") == "true":
            active_subscriptions = await container.subscription_service.list_subscriptions(
                channel=DialogChannel.WEB, subscriber_identifier=visitor_identifier
            )
            response: Response = templates.TemplateResponse(
                request=request,
                name="partials/subscriptions_list.html",
                context=_base_page_context(
                    request=request,
                    active_nav="subscriptions",
                    subscriptions=active_subscriptions,
                    notice_message=notice_message,
                    error_message=error_message,
                ),
            )
        else:
            redirect_query = urlencode(
                {"notice": notice_message} if notice_message else {"error": error_message or "Ошибка оформления."}
            )
            response = RedirectResponse(url=f"/subscriptions?{redirect_query}", status_code=status.HTTP_303_SEE_OTHER)

        if should_set_cookie:
            _attach_visitor_cookie(response, visitor_identifier=visitor_identifier)
        return response

    @router.post("/subscriptions/{subscription_identifier}/delete", response_class=HTMLResponse)
    async def delete_subscription_action(request: Request, subscription_identifier: UUID) -> Response:
        """Физически удаляет выбранную подписку текущего браузера."""
        container = _require_container(request)
        visitor_identifier, should_set_cookie = _resolve_visitor_identifier(request)
        error_message: str | None = None
        notice_message: str | None = None

        try:
            await container.subscription_service.unsubscribe(
                subscription_identifier=subscription_identifier, subscriber_identifier=visitor_identifier
            )
            notice_message = "Подписка полностью удалена из базы сервиса."
        except ApplicationException as error:
            error_message = error.message

        if request.headers.get("HX-Request") == "true":
            active_subscriptions = await container.subscription_service.list_subscriptions(
                channel=DialogChannel.WEB, subscriber_identifier=visitor_identifier
            )
            response: Response = templates.TemplateResponse(
                request=request,
                name="partials/subscriptions_list.html",
                context=_base_page_context(
                    request=request,
                    active_nav="subscriptions",
                    subscriptions=active_subscriptions,
                    notice_message=notice_message,
                    error_message=error_message,
                ),
            )
        else:
            redirect_query = urlencode(
                {"notice": notice_message} if notice_message else {"error": error_message or "Не удалось отписаться."}
            )
            response = RedirectResponse(url=f"/subscriptions?{redirect_query}", status_code=status.HTTP_303_SEE_OTHER)

        if should_set_cookie:
            _attach_visitor_cookie(response, visitor_identifier=visitor_identifier)
        return response

    @router.post("/subscriptions/delete-all", response_class=HTMLResponse)
    async def delete_all_subscriptions_action(request: Request) -> Response:
        """Удаляет все подписки текущей браузерной сессии."""
        container = _require_container(request)
        visitor_identifier, should_set_cookie = _resolve_visitor_identifier(request)
        removed_count = await container.subscription_service.unsubscribe_all(
            channel=DialogChannel.WEB, subscriber_identifier=visitor_identifier
        )
        notice_message = f"Удалено подписок: {removed_count}. В базе данных больше нет ваших фильтров."
        if request.headers.get("HX-Request") == "true":
            response: Response = templates.TemplateResponse(
                request=request,
                name="partials/subscriptions_list.html",
                context=_base_page_context(
                    request=request, active_nav="subscriptions", subscriptions=(), notice_message=notice_message
                ),
            )
        else:
            response = RedirectResponse(
                url=f"/subscriptions?{urlencode({'notice': notice_message})}", status_code=status.HTTP_303_SEE_OTHER
            )
        if should_set_cookie:
            _attach_visitor_cookie(response, visitor_identifier=visitor_identifier)
        return response

    @router.post("/feedback", response_class=HTMLResponse)
    async def submit_feedback_action(
        request: Request,
        message_text: Annotated[str, Form()] = "",
        contact_hint: Annotated[str | None, Form()] = None,
        event_identifier: Annotated[str | None, Form()] = None,
        honeypot_website: Annotated[str, Form()] = "",
    ) -> Response:
        """Принимает заявку организатора или уточнение с защитой от спама и инъекций."""
        container = _require_container(request)
        rate_limiter = getattr(request.app.state, "rate_limiter", None)
        client_address = request.client.host if request.client is not None else "unknown"

        error_message: str | None = None
        notice_message: str | None = None

        if isinstance(rate_limiter, InMemoryRateLimiter):
            allowed, retry_after = rate_limiter.check_and_consume(client_address=client_address, scope="feedback")
            if not allowed:
                error_message = f"Слишком много отправок. Пожалуйста, подождите {retry_after} сек."

        if error_message is None and normalize_optional_text(honeypot_website) is not None:
            error_message = "Обращение отклонено встроенной защитой от автоматического спама."

        parsed_event_id: UUID | None = None
        cleaned_event_id = normalize_optional_text(event_identifier)
        if error_message is None and cleaned_event_id is not None:
            try:
                parsed_event_id = UUID(cleaned_event_id)
            except ValueError:
                error_message = "Некорректный идентификатор связанного мероприятия."

        if error_message is None:
            try:
                await container.subscription_service.submit_feedback(
                    channel=DialogChannel.WEB,
                    message_text=message_text,
                    event_identifier=parsed_event_id,
                    contact_hint=contact_hint,
                )
                notice_message = "Обращение зарегистрировано и передано модератору сервиса."
            except ApplicationException as error:
                error_message = error.message

        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(
                request=request,
                name="partials/feedback_result.html",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT if error_message else status.HTTP_200_OK,
                context=_base_page_context(
                    request=request,
                    active_nav="subscriptions",
                    feedback_notice=notice_message,
                    feedback_error=error_message,
                ),
            )

        if parsed_event_id is not None:
            target = f"/events/{parsed_event_id}?feedback=ok" if notice_message else f"/events/{parsed_event_id}"
            return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)

        redirect_query = urlencode(
            {"notice": notice_message} if notice_message else {"error": error_message or "Ошибка отправки."}
        )
        return RedirectResponse(url=f"/subscriptions?{redirect_query}", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/assistant", response_class=HTMLResponse)
    async def assistant_preview_page(request: Request) -> HTMLResponse:
        """Рендерит обзорную страницу сценариев ИИ-помощника и быстрых пресетов каталога."""
        return templates.TemplateResponse(
            request=request,
            name="assistant.html",
            context=_base_page_context(
                request=request,
                active_nav="assistant",
                quick_scenarios=(
                    {
                        "title": "С ребёнком в Стерлитамаке до 1000 ₽ на выходных",
                        "description": "Быстрый отбор семейных мероприятий для детей с ограничением бюджета.",
                        "url": "/?city=Стерлитамак&period=weekend&maximum_age_limit=6&maximum_price=1000",
                    },
                    {
                        "title": "Бесплатные выставки в Уфе до конца месяца",
                        "description": "Подборка музеев и галерей со свободным входом.",
                        "url": "/?city=Уфа&category=exhibition&period=month&only_free=true",
                    },
                    {
                        "title": "Вечерние концерты рядом с центром Уфы",
                        "description": "Сортировка по близости площадок к центру города.",
                        "url": "/?city=Уфа&category=concert&near_center=true",
                    },
                    {
                        "title": "Мероприятия на башкирском языке",
                        "description": "Спектакли и программы с указанием языка в метаданных.",
                        "url": "/?language=башкирский",
                    },
                ),
            ),
        )

    return router


def _require_container(request: Request) -> ApplicationContainer:
    container = getattr(request.app.state, "container", None)
    if not isinstance(container, ApplicationContainer):
        raise RuntimeError("Контейнер зависимостей веб-приложения не инициализирован.")
    return container


def _base_page_context(*, request: Request, active_nav: str, **extra: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "request": request,
        "active_nav": active_nav,
        "disclaimer_text": SERVICE_DISCLAIMER_TEXT,
        "category_labels": CATEGORY_LABELS,
        "period_labels": PERIOD_LABELS,
        "source_type_labels": SOURCE_TYPE_LABELS,
        "format_price": format_price_label,
        "format_date_range": format_date_range_label,
        "format_status": format_status_label,
        "current_year": datetime.now().year,
    }
    context.update(extra)
    return context


def _parse_optional_query_int(raw_value: str | None) -> int | None:
    cleaned = normalize_optional_text(raw_value)
    if cleaned is None:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_optional_query_bool(raw_value: str | None) -> bool:
    cleaned = normalize_optional_text(raw_value)
    if cleaned is None:
        return False
    return cleaned.lower() in {"1", "true", "on", "yes", "да"}


def _parse_optional_category(raw_value: str | None) -> EventCategory | None:
    cleaned = normalize_optional_text(raw_value)
    if cleaned is None:
        return None
    try:
        return EventCategory(cleaned.lower())
    except ValueError:
        return None


def _parse_catalog_query_parameters(
    *,
    query: str | None,
    city: str | None,
    category: str | None,
    period: str | None,
    date_from: str | None,
    date_to: str | None,
    maximum_price: str | None,
    only_free: str | None,
    maximum_age_limit: str | None,
    language: str | None,
    venue: str | None,
    near_center: str | None,
    sort_field: str | None,
    sort_direction: str | None,
    page: str | None,
    page_size: str | None,
) -> CatalogQueryParameters:
    parsed_page = _parse_optional_query_int(page) or 1
    parsed_page_size = _parse_optional_query_int(page_size) or 9
    resolved_sort_field = normalize_optional_text(sort_field) or EventSortField.DATE_START.value
    resolved_sort_direction = normalize_optional_text(sort_direction) or SortDirection.ASCENDING.value
    return CatalogQueryParameters(
        query=normalize_optional_text(query),
        city=normalize_optional_text(city),
        category=normalize_optional_text(category),
        period=normalize_optional_text(period),
        date_from=normalize_optional_text(date_from),
        date_to=normalize_optional_text(date_to),
        maximum_price=_parse_optional_query_int(maximum_price),
        only_free=_parse_optional_query_bool(only_free),
        maximum_age_limit=_parse_optional_query_int(maximum_age_limit),
        language=normalize_optional_text(language),
        venue=normalize_optional_text(venue),
        near_center=_parse_optional_query_bool(near_center),
        sort_field=resolved_sort_field,
        sort_direction=resolved_sort_direction,
        page=max(1, parsed_page),
        page_size=min(50, max(1, parsed_page_size)),
    )


async def _execute_catalog_search(
    *, search_service: EventSearchService, parameters: CatalogQueryParameters
) -> dict[str, Any]:
    filter_error: str | None = None
    try:
        search_filter = build_search_filter_from_parameters(parameters)
        search_page = await search_service.search_events(search_filter=search_filter)
    except InvalidSearchFilterError as error:
        filter_error = error.message
        fallback_filter = EventSearchFilter(page=1, page_size=parameters.page_size)
        search_page = await search_service.search_events(search_filter=fallback_filter)

    total_pages = max(1, (search_page.total_count + search_page.page_size - 1) // search_page.page_size)
    previous_page_url = (
        f"/{build_catalog_url_query(parameters, overrides={'page': parameters.page - 1})}"
        if parameters.page > 1
        else None
    )
    next_page_url = (
        f"/{build_catalog_url_query(parameters, overrides={'page': parameters.page + 1})}"
        if search_page.has_more
        else None
    )

    relaxation_links = _build_relaxation_links(parameters=parameters, explanation=search_page.empty_explanation)
    active_filter_chips = _build_active_filter_chips(parameters)
    current_query_string = build_catalog_url_query(parameters)

    return {
        "parameters": parameters,
        "search_page": search_page,
        "filter_error": filter_error,
        "total_pages": total_pages,
        "previous_page_url": previous_page_url,
        "next_page_url": next_page_url,
        "relaxation_links": relaxation_links,
        "active_filter_chips": active_filter_chips,
        "current_query_string": current_query_string,
        "reset_url": "/",
    }


def _build_relaxation_links(
    *, parameters: CatalogQueryParameters, explanation: EmptySearchExplanation | None
) -> tuple[dict[str, str], ...]:
    if explanation is None:
        return ()

    relaxation_rules: tuple[tuple[bool, str, dict[str, str | int | bool | None]], ...] = (
        (parameters.only_free, "Разрешить платные мероприятия", {"only_free": None, "page": None}),
        (
            parameters.maximum_price is not None,
            f"Убрать ограничение цены (до {parameters.maximum_price} ₽)",
            {"maximum_price": None, "page": None},
        ),
        (
            parameters.maximum_age_limit is not None,
            f"Убрать возрастной ценз ({parameters.maximum_age_limit}+)",
            {"maximum_age_limit": None, "page": None},
        ),
        (parameters.category is not None, "Показать все категории", {"category": None, "page": None}),
        (
            parameters.period is not None or parameters.date_from is not None or parameters.date_to is not None,
            "Сбросить даты и период",
            {"period": None, "date_from": None, "date_to": None, "page": None},
        ),
        (
            parameters.language is not None,
            f"Убрать фильтр по языку ({parameters.language})",
            {"language": None, "page": None},
        ),
        (parameters.venue is not None, "Искать на всех площадках", {"venue": None, "page": None}),
        (
            parameters.query is not None,
            f"Очистить поисковый запрос «{parameters.query}»",
            {"query": None, "page": None},
        ),
        (parameters.city is not None, "Искать во всех городах Башкортостана", {"city": None, "page": None}),
    )

    links: list[dict[str, str]] = []
    for is_active, label, overrides in relaxation_rules:
        if is_active:
            target_query = build_catalog_url_query(parameters, overrides=overrides)
            links.append(
                {"label": label, "href": f"/{target_query}", "partial_href": f"/partials/events{target_query}"}
            )
    return tuple(links)


def _build_active_filter_chips(parameters: CatalogQueryParameters) -> tuple[dict[str, str], ...]:
    chips: list[dict[str, str]] = []
    parsed_category = _parse_optional_category(parameters.category)
    category_label = (
        CATEGORY_LABELS.get(parsed_category, parameters.category or "")
        if parsed_category is not None
        else (parameters.category or "")
    )
    candidate_chips: list[tuple[bool, str, dict[str, str | int | bool | None]]] = [
        (parameters.query is not None, f"Поиск: «{parameters.query}»", {"query": None, "page": None}),
        (parameters.city is not None, f"Город: {parameters.city}", {"city": None, "page": None}),
        (parameters.category is not None, f"Категория: {category_label}", {"category": None, "page": None}),
        (
            parameters.period is not None,
            f"Период: {PERIOD_LABELS.get(parameters.period or '', parameters.period or '')}",
            {"period": None, "page": None},
        ),
        (
            parameters.date_from is not None or parameters.date_to is not None,
            f"Даты: {parameters.date_from or '...'} — {parameters.date_to or '...'}",
            {"date_from": None, "date_to": None, "page": None},
        ),
        (parameters.only_free, "Только бесплатно", {"only_free": None, "page": None}),
        (
            parameters.maximum_price is not None,
            f"До {parameters.maximum_price} ₽",
            {"maximum_price": None, "page": None},
        ),
        (
            parameters.maximum_age_limit is not None,
            f"Возраст до {parameters.maximum_age_limit}+",
            {"maximum_age_limit": None, "page": None},
        ),
        (parameters.language is not None, f"Язык: {parameters.language}", {"language": None, "page": None}),
        (parameters.venue is not None, f"Площадка: {parameters.venue}", {"venue": None, "page": None}),
        (parameters.near_center, "Ближе к центру", {"near_center": None, "page": None}),
    ]
    for active, label, overrides in candidate_chips:
        if active:
            query_suffix = build_catalog_url_query(parameters, overrides=overrides)
            chips.append(
                {"label": label, "href": f"/{query_suffix}", "partial_href": f"/partials/events{query_suffix}"}
            )
    return tuple(chips)


def _resolve_visitor_identifier(request: Request) -> tuple[str, bool]:
    existing_cookie = normalize_optional_text(request.cookies.get(VISITOR_COOKIE_NAME))
    if existing_cookie is not None and len(existing_cookie) <= 120:
        return existing_cookie, False
    return generate_visitor_identifier(), True


def _attach_visitor_cookie(response: Response, *, visitor_identifier: str) -> None:
    response.set_cookie(
        key=VISITOR_COOKIE_NAME, value=visitor_identifier, max_age=60 * 60 * 24 * 180, httponly=True, samesite="lax"
    )
