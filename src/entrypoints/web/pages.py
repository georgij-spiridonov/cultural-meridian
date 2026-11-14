"""SSR и HTMX-маршруты публичного веб-каталога, карточки события, подписок и обратной связи."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Form, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.application import EventSearchService, WeekendPlannerService
from src.core.exceptions import ApplicationException
from src.core.types import DialogChannel, EventCategory, EventStatus
from src.domain.entities import (
    CityReference,
    ConstraintRelaxationSuggestion,
    EmptySearchExplanation,
    Event,
    EventSearchFilter,
    EventSortField,
    SortDirection,
    WeekendPlanRequest,
    WeekendPlanResult,
)
from src.domain.exceptions import EventNotFoundError, InvalidSearchFilterError, InvalidWeekendPlanRequestError
from src.domain.services import BASHKORTOSTAN_TIMEZONE, normalize_optional_text
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
    parse_time_value,
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
        languages = await container.search_service.list_languages()
        language_options = tuple((language, language[:1].upper() + language[1:]) for language in languages)
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
                language_options=language_options,
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

    @router.get("/planner", response_class=HTMLResponse)
    async def planner_page(
        request: Request,
        city: Annotated[str | None, Query()] = None,
        target_date: Annotated[str | None, Query()] = None,
        window_start_time: Annotated[str | None, Query()] = None,
        window_end_time: Annotated[str | None, Query()] = None,
        budget_rubles: Annotated[str | None, Query()] = None,
        companions_count: Annotated[str | None, Query()] = None,
        child_age: Annotated[str | None, Query()] = None,
        maximum_travel_minutes: Annotated[str | None, Query()] = None,
    ) -> HTMLResponse:
        """Рендерит страницу планировщика «Выходной день» с формой и SSR/HTMX-результатом."""
        container = _require_container(request)
        cities = await container.event_repository.list_cities()
        weekend_dates = _resolve_upcoming_weekend_dates()
        default_city = _resolve_default_planner_city(cities)
        form_values = _normalize_planner_form_values(
            city=city,
            target_date=target_date,
            window_start_time=window_start_time,
            window_end_time=window_end_time,
            budget_rubles=budget_rubles,
            companions_count=companions_count,
            child_age=child_age,
            maximum_travel_minutes=maximum_travel_minutes,
            default_city=default_city,
            default_date=weekend_dates["default_date_iso"],
        )

        has_planner_query = any(key in request.query_params for key in _PLANNER_QUERY_KEYS)
        if has_planner_query:
            plan_context = await _execute_weekend_plan(
                weekend_planner=container.weekend_planner, form_values=form_values
            )
        else:
            plan_context = {
                "form_values": form_values,
                "plan_result": None,
                "planner_error": None,
                "relaxation_actions": (),
                "share_url": "/planner",
                "has_submitted": False,
            }

        if request.headers.get("HX-Request") == "true" and request.headers.get("HX-Target") == "planner-results":
            return templates.TemplateResponse(
                request=request,
                name="partials/planner_results.html",
                context=_base_page_context(
                    request=request,
                    active_nav="planner",
                    **weekend_dates,
                    **plan_context,
                    format_rubles=_format_rubles,
                    format_step_time=_format_step_time,
                ),
            )

        return templates.TemplateResponse(
            request=request,
            name="planner.html",
            context=_base_page_context(
                request=request,
                active_nav="planner",
                cities=cities,
                **weekend_dates,
                **plan_context,
                format_rubles=_format_rubles,
                format_step_time=_format_step_time,
            ),
        )

    @router.get("/partials/weekend-plan", response_class=HTMLResponse)
    async def planner_results_get_partial(
        request: Request,
        city: Annotated[str | None, Query()] = None,
        target_date: Annotated[str | None, Query()] = None,
        window_start_time: Annotated[str | None, Query()] = None,
        window_end_time: Annotated[str | None, Query()] = None,
        budget_rubles: Annotated[str | None, Query()] = None,
        companions_count: Annotated[str | None, Query()] = None,
        child_age: Annotated[str | None, Query()] = None,
        maximum_travel_minutes: Annotated[str | None, Query()] = None,
    ) -> HTMLResponse:
        """Возвращает HTMX-фрагмент расчёта маршрута выходного дня по GET-параметрам."""
        return await _render_planner_partial(
            request=request,
            templates=templates,
            city=city,
            target_date=target_date,
            window_start_time=window_start_time,
            window_end_time=window_end_time,
            budget_rubles=budget_rubles,
            companions_count=companions_count,
            child_age=child_age,
            maximum_travel_minutes=maximum_travel_minutes,
        )

    @router.post("/partials/weekend-plan", response_class=HTMLResponse)
    async def planner_results_post_partial(
        request: Request,
        city: Annotated[str | None, Form()] = None,
        target_date: Annotated[str | None, Form()] = None,
        window_start_time: Annotated[str | None, Form()] = None,
        window_end_time: Annotated[str | None, Form()] = None,
        budget_rubles: Annotated[str | None, Form()] = None,
        companions_count: Annotated[str | None, Form()] = None,
        child_age: Annotated[str | None, Form()] = None,
        maximum_travel_minutes: Annotated[str | None, Form()] = None,
    ) -> HTMLResponse:
        """Возвращает HTMX-фрагмент расчёта маршрута выходного дня по POST-форме."""
        return await _render_planner_partial(
            request=request,
            templates=templates,
            city=city,
            target_date=target_date,
            window_start_time=window_start_time,
            window_end_time=window_end_time,
            budget_rubles=budget_rubles,
            companions_count=companions_count,
            child_age=child_age,
            maximum_travel_minutes=maximum_travel_minutes,
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


_PLANNER_QUERY_KEYS: tuple[str, ...] = (
    "city",
    "target_date",
    "window_start_time",
    "window_end_time",
    "budget_rubles",
    "companions_count",
    "child_age",
    "maximum_travel_minutes",
)
_BUDGET_RELAXATION_PATTERN = re.compile(r"(\d[\d\s\u00a0]*)\s*₽")
_WINDOW_RELAXATION_PATTERN = re.compile(r"(\d{2}:\d{2})\s*[–—-]\s*(\d{2}:\d{2})")
_TRAVEL_RELAXATION_PATTERN = re.compile(r"(\d+)\s*мин")
_CONSTRAINT_LABELS: dict[str, str] = {
    "budget": "Бюджет",
    "time_window": "Временное окно",
    "travel": "Время в пути",
    "age": "Возраст ребёнка",
    "count": "Доступность событий",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class _PlannerFormValues:
    """Значения полей формы планировщика «Выходной день» для SSR и HTMX."""

    city: str
    target_date: str
    window_start_time: str
    window_end_time: str
    budget_rubles: str
    companions_count: str
    child_age: str
    maximum_travel_minutes: str


@dataclass(frozen=True, slots=True, kw_only=True)
class _RelaxationActionView:
    """Представление подсказки ослабления ограничения с готовым патчем формы."""

    constraint_name: str
    constraint_label: str
    explanation: str
    suggested_change: str
    is_actionable: bool
    action_label: str | None
    patch_fields: dict[str, str]
    fallback_href: str
    partial_href: str


async def _render_planner_partial(
    *,
    request: Request,
    templates: Jinja2Templates,
    city: str | None,
    target_date: str | None,
    window_start_time: str | None,
    window_end_time: str | None,
    budget_rubles: str | None,
    companions_count: str | None,
    child_age: str | None,
    maximum_travel_minutes: str | None,
) -> HTMLResponse:
    container = _require_container(request)
    cities = await container.event_repository.list_cities()
    weekend_dates = _resolve_upcoming_weekend_dates()
    default_city = _resolve_default_planner_city(cities)
    form_values = _normalize_planner_form_values(
        city=city,
        target_date=target_date,
        window_start_time=window_start_time,
        window_end_time=window_end_time,
        budget_rubles=budget_rubles,
        companions_count=companions_count,
        child_age=child_age,
        maximum_travel_minutes=maximum_travel_minutes,
        default_city=default_city,
        default_date=weekend_dates["default_date_iso"],
    )
    plan_context = await _execute_weekend_plan(weekend_planner=container.weekend_planner, form_values=form_values)
    return templates.TemplateResponse(
        request=request,
        name="partials/planner_results.html",
        headers={"HX-Push-Url": str(plan_context["share_url"])},
        context=_base_page_context(
            request=request,
            active_nav="planner",
            **weekend_dates,
            **plan_context,
            format_rubles=_format_rubles,
            format_step_time=_format_step_time,
        ),
    )


def _resolve_upcoming_weekend_dates() -> dict[str, str]:
    today = datetime.now(tz=BASHKORTOSTAN_TIMEZONE).date()
    days_until_saturday = (5 - today.weekday()) % 7
    nearest_saturday = today + timedelta(days=days_until_saturday)
    days_until_sunday = (6 - today.weekday()) % 7
    nearest_sunday = today + timedelta(days=days_until_sunday)
    default_date = today if today.weekday() in {5, 6} else nearest_saturday
    return {
        "nearest_saturday_iso": nearest_saturday.isoformat(),
        "nearest_saturday_label": nearest_saturday.strftime("%d.%m"),
        "nearest_sunday_iso": nearest_sunday.isoformat(),
        "nearest_sunday_label": nearest_sunday.strftime("%d.%m"),
        "default_date_iso": default_date.isoformat(),
    }


def _resolve_default_planner_city(cities: Sequence[CityReference]) -> str:
    for item in cities:
        if item.name == "Уфа":
            return item.name
    if cities:
        return cities[0].name
    return "Уфа"


def _normalize_planner_form_values(
    *,
    city: str | None,
    target_date: str | None,
    window_start_time: str | None,
    window_end_time: str | None,
    budget_rubles: str | None,
    companions_count: str | None,
    child_age: str | None,
    maximum_travel_minutes: str | None,
    default_city: str,
    default_date: str,
) -> _PlannerFormValues:
    resolved_city = default_city if city is None else city.strip()
    resolved_date = normalize_optional_text(target_date) or default_date
    resolved_start = normalize_optional_text(window_start_time) or "10:00"
    resolved_end = normalize_optional_text(window_end_time) or "21:00"
    resolved_budget = normalize_optional_text(budget_rubles)
    resolved_companions = normalize_optional_text(companions_count)
    resolved_child_age = normalize_optional_text(child_age) or ""
    resolved_travel = normalize_optional_text(maximum_travel_minutes)
    return _PlannerFormValues(
        city=resolved_city,
        target_date=resolved_date,
        window_start_time=resolved_start,
        window_end_time=resolved_end,
        budget_rubles="2000" if resolved_budget is None else resolved_budget,
        companions_count="1" if resolved_companions is None else resolved_companions,
        child_age=resolved_child_age,
        maximum_travel_minutes="20" if resolved_travel is None else resolved_travel,
    )


def _build_planner_url_query(values: _PlannerFormValues, *, overrides: Mapping[str, str] | None = None) -> str:
    effective = replace(values, **overrides) if overrides else values
    pairs: list[tuple[str, str]] = [
        ("city", effective.city),
        ("target_date", effective.target_date),
        ("window_start_time", effective.window_start_time),
        ("window_end_time", effective.window_end_time),
        ("budget_rubles", effective.budget_rubles),
        ("companions_count", effective.companions_count),
    ]
    if effective.child_age:
        pairs.append(("child_age", effective.child_age))
    pairs.append(("maximum_travel_minutes", effective.maximum_travel_minutes))
    return f"?{urlencode(pairs)}"


def _parse_planner_request(values: _PlannerFormValues) -> WeekendPlanRequest:
    cleaned_city = normalize_optional_text(values.city)
    if cleaned_city is None:
        raise InvalidWeekendPlanRequestError(
            "Выберите город для подбора маршрута.", parameter_name="city", reason="Не указан город"
        )
    try:
        parsed_date = date.fromisoformat(values.target_date)
    except ValueError as error:
        raise InvalidWeekendPlanRequestError(
            "Некорректная дата маршрута (ожидается формат ГГГГ-ММ-ДД).",
            parameter_name="target_date",
            reason="Ошибка разбора даты",
        ) from error

    try:
        parsed_start = parse_time_value(values.window_start_time)
    except ValueError as error:
        raise InvalidWeekendPlanRequestError(
            "Некорректное время начала свободного окна (ожидается ЧЧ:ММ).",
            parameter_name="window_start_time",
            reason="Ошибка разбора времени начала",
        ) from error
    if parsed_start is None:
        raise InvalidWeekendPlanRequestError(
            "Укажите время начала свободного окна.",
            parameter_name="window_start_time",
            reason="Отсутствует время начала",
        )

    try:
        parsed_end = parse_time_value(values.window_end_time)
    except ValueError as error:
        raise InvalidWeekendPlanRequestError(
            "Некорректное время окончания свободного окна (ожидается ЧЧ:ММ).",
            parameter_name="window_end_time",
            reason="Ошибка разбора времени окончания",
        ) from error
    if parsed_end is None:
        raise InvalidWeekendPlanRequestError(
            "Укажите время окончания свободного окна.",
            parameter_name="window_end_time",
            reason="Отсутствует время окончания",
        )

    try:
        parsed_budget = int(values.budget_rubles)
    except ValueError as error:
        raise InvalidWeekendPlanRequestError(
            "Общий бюджет должен быть целым числом в рублях.",
            parameter_name="budget_rubles",
            reason="Ошибка разбора бюджета",
        ) from error

    try:
        parsed_companions = int(values.companions_count)
    except ValueError as error:
        raise InvalidWeekendPlanRequestError(
            "Число участников должно быть целым положительным числом.",
            parameter_name="companions_count",
            reason="Ошибка разбора числа участников",
        ) from error

    parsed_child_age: int | None = None
    if values.child_age:
        try:
            parsed_child_age = int(values.child_age)
        except ValueError as error:
            raise InvalidWeekendPlanRequestError(
                "Возраст ребёнка должен быть целым числом лет.",
                parameter_name="child_age",
                reason="Ошибка разбора возраста ребёнка",
            ) from error

    try:
        parsed_travel = int(values.maximum_travel_minutes)
    except ValueError as error:
        raise InvalidWeekendPlanRequestError(
            "Допустимое время на дорогу должно быть целым числом минут.",
            parameter_name="maximum_travel_minutes",
            reason="Ошибка разбора времени в пути",
        ) from error

    return WeekendPlanRequest(
        city=cleaned_city,
        target_date=parsed_date,
        window_start_time=parsed_start,
        window_end_time=parsed_end,
        budget_rubles=parsed_budget,
        companions_count=parsed_companions,
        child_age=parsed_child_age,
        maximum_travel_minutes=parsed_travel,
    )


async def _execute_weekend_plan(
    *, weekend_planner: WeekendPlannerService, form_values: _PlannerFormValues
) -> dict[str, Any]:
    plan_result: WeekendPlanResult | None = None
    planner_error: str | None = None
    relaxation_actions: tuple[_RelaxationActionView, ...] = ()

    try:
        plan_request = _parse_planner_request(form_values)
        plan_result = await weekend_planner.plan_weekend(plan_request)
        if not plan_result.is_feasible:
            relaxation_actions = _build_relaxation_actions(
                form_values=form_values, suggestions=plan_result.relaxation_suggestions
            )
    except InvalidWeekendPlanRequestError as error:
        planner_error = error.message

    share_url = f"/planner{_build_planner_url_query(form_values)}"
    return {
        "form_values": form_values,
        "plan_result": plan_result,
        "planner_error": planner_error,
        "relaxation_actions": relaxation_actions,
        "share_url": share_url,
        "has_submitted": True,
    }


def _build_relaxation_actions(
    *, form_values: _PlannerFormValues, suggestions: Sequence[ConstraintRelaxationSuggestion]
) -> tuple[_RelaxationActionView, ...]:
    actions: list[_RelaxationActionView] = []
    for suggestion in suggestions:
        patch_fields, action_label = _extract_relaxation_patch(form_values=form_values, suggestion=suggestion)
        is_actionable = bool(patch_fields and action_label)
        query_suffix = _build_planner_url_query(form_values, overrides=patch_fields) if is_actionable else ""
        actions.append(
            _RelaxationActionView(
                constraint_name=suggestion.constraint_name,
                constraint_label=_CONSTRAINT_LABELS.get(suggestion.constraint_name, "Ограничение"),
                explanation=suggestion.explanation,
                suggested_change=suggestion.suggested_change,
                is_actionable=is_actionable,
                action_label=action_label,
                patch_fields=patch_fields,
                fallback_href=f"/planner{query_suffix}" if is_actionable else "/planner",
                partial_href=f"/partials/weekend-plan{query_suffix}" if is_actionable else "",
            )
        )
    return tuple(actions)


def _extract_relaxation_patch(
    *, form_values: _PlannerFormValues, suggestion: ConstraintRelaxationSuggestion
) -> tuple[dict[str, str], str | None]:
    name = suggestion.constraint_name
    text = suggestion.suggested_change

    if name == "budget":
        match = _BUDGET_RELAXATION_PATTERN.search(text)
        if match is not None:
            digits = re.sub(r"\s+", "", match.group(1))
            if digits.isdigit():
                amount = int(digits)
                return {"budget_rubles": str(amount)}, f"Увеличить бюджет до {_format_rubles(amount)}"

    if name == "time_window":
        match = _WINDOW_RELAXATION_PATTERN.search(text)
        if match is not None:
            start_str, end_str = match.group(1), match.group(2)
            return (
                {"window_start_time": start_str, "window_end_time": end_str},
                f"Расширить окно до {start_str}–{end_str}",
            )

    if name == "travel":
        match = _TRAVEL_RELAXATION_PATTERN.search(text)
        if match is not None:
            minutes = match.group(1)
            return {"maximum_travel_minutes": minutes}, f"Увеличить дорогу до {minutes} мин"

    if name == "age" and form_values.child_age:
        return {"child_age": ""}, "Снять детский фильтр"

    return {}, None


def _format_rubles(amount: int) -> str:
    return f"{amount:,} ₽".replace(",", " ")


def _format_step_time(moment: datetime) -> str:
    return moment.astimezone(BASHKORTOSTAN_TIMEZONE).strftime("%H:%M")
