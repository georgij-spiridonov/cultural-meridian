"""Маршрутизатор панели модератора `/admin` с авторизацией, импортом, модерацией и аудитом."""

import hmac
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.application import ManualEventInput
from src.core.exceptions import ApplicationException
from src.core.types import EventCategory, EventStatus
from src.domain.entities import Event, EventSearchFilter, EventSortField, ImportReport, SortDirection
from src.domain.services import normalize_optional_text
from src.entrypoints.bootstrap import ApplicationContainer
from src.entrypoints.web._formatting import (
    ADMIN_SESSION_COOKIE_NAME,
    ALLOWED_AGE_OPTIONS,
    CATEGORY_LABELS,
    SERVICE_DISCLAIMER_TEXT,
    SOURCE_TYPE_LABELS,
    format_date_range_label,
    format_price_label,
    format_status_label,
    parse_date_boundary,
    sign_admin_session,
    verify_admin_session,
)

__all__ = ["create_administration_router"]

_MAXIMUM_IMPORT_FILE_BYTES = 5 * 1024 * 1024
_ADMIN_TABS: frozenset[str] = frozenset({"catalog", "import", "moderation", "audit", "feedback"})


def create_administration_router(*, templates: Jinja2Templates) -> APIRouter:
    """Создаёт маршрутизатор административной панели и модерации каталога."""
    router = APIRouter(prefix="/admin", include_in_schema=False)

    @router.get("/login", response_class=HTMLResponse)
    async def admin_login_page(request: Request) -> Response:
        """Отображает форму входа в панель модератора."""
        container = _require_container(request)
        if _authenticated_username(request, container=container) is not None:
            return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

        return templates.TemplateResponse(
            request=request,
            name="admin_login.html",
            context={
                "request": request,
                "active_nav": "admin",
                "disclaimer_text": SERVICE_DISCLAIMER_TEXT,
                "error_message": request.query_params.get("error"),
                "current_year": datetime.now().year,
            },
        )

    @router.post("/login", response_class=HTMLResponse)
    async def admin_login_submit(
        request: Request, username: Annotated[str, Form()] = "", password: Annotated[str, Form()] = ""
    ) -> Response:
        """Проверяет учётные данные модератора и выдаёт HMAC-подписанную сессию."""
        container = _require_container(request)
        expected_username = container.web_settings.ADMIN_USERNAME
        expected_password = container.web_settings.ADMIN_PASSWORD

        username_valid = hmac.compare_digest(username.strip().encode("utf-8"), expected_username.encode("utf-8"))
        password_valid = hmac.compare_digest(password.encode("utf-8"), expected_password.encode("utf-8"))
        if not (username_valid and password_valid):
            return templates.TemplateResponse(
                request=request,
                name="admin_login.html",
                status_code=status.HTTP_401_UNAUTHORIZED,
                context={
                    "request": request,
                    "active_nav": "admin",
                    "disclaimer_text": SERVICE_DISCLAIMER_TEXT,
                    "error_message": "Неверный логин или пароль модератора.",
                    "current_year": datetime.now().year,
                },
            )

        token = sign_admin_session(
            username=expected_username,
            secret=container.web_settings.SESSION_SECRET,
            ttl_seconds=container.web_settings.SESSION_TTL_SECONDS,
        )
        redirect = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
        redirect.set_cookie(
            key=ADMIN_SESSION_COOKIE_NAME,
            value=token,
            max_age=container.web_settings.SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
        )
        return redirect

    @router.post("/logout")
    async def admin_logout_submit() -> Response:
        """Завершает сессию модератора и удаляет cookie авторизации."""
        redirect = RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        redirect.delete_cookie(key=ADMIN_SESSION_COOKIE_NAME)
        return redirect

    @router.get("", response_class=HTMLResponse)
    async def admin_dashboard_page(
        request: Request,
        tab: Annotated[str, Query()] = "catalog",
        status_filter: Annotated[str, Query(alias="status")] = "all",
        query: Annotated[str | None, Query()] = None,
        edit_event: Annotated[str | None, Query()] = None,
    ) -> Response:
        """Отображает панель модератора с вкладками каталога, импорта, модерации, аудита и обращений."""
        container = _require_container(request)
        moderator_username = _authenticated_username(request, container=container)
        if moderator_username is None:
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        active_tab = tab if tab in _ADMIN_TABS else "catalog"
        return await _render_admin_dashboard(
            request=request,
            templates=templates,
            container=container,
            moderator_username=moderator_username,
            active_tab=active_tab,
            status_filter=status_filter,
            search_query=query,
            edit_event_identifier=edit_event,
            notice_message=request.query_params.get("notice"),
            error_message=request.query_params.get("error"),
        )

    @router.post("/events", response_class=HTMLResponse)
    async def admin_create_event_action(
        request: Request,
        title: Annotated[str, Form()] = "",
        category: Annotated[str, Form()] = EventCategory.CONCERT.value,
        date_start: Annotated[str, Form()] = "",
        date_end: Annotated[str | None, Form()] = None,
        duration_minutes: Annotated[str | None, Form()] = None,
        city: Annotated[str, Form()] = "",
        venue: Annotated[str, Form()] = "",
        address: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
        price_minimum: Annotated[str | None, Form()] = None,
        price_maximum: Annotated[str | None, Form()] = None,
        is_free: Annotated[str | None, Form()] = None,
        age_limit: Annotated[str, Form()] = "0",
        language: Annotated[str | None, Form()] = None,
        tags: Annotated[str | None, Form()] = None,
        organization_link: Annotated[str | None, Form()] = None,
        source_url: Annotated[str, Form()] = "",
        event_status: Annotated[str, Form(alias="status")] = EventStatus.PUBLISHED.value,
    ) -> Response:
        """Создаёт новое событие вручную через панель администратора."""
        container = _require_container(request)
        moderator_username = _authenticated_username(request, container=container)
        if moderator_username is None:
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            manual_input = _build_manual_event_input_from_form(
                title=title,
                category=category,
                date_start=date_start,
                date_end=date_end,
                duration_minutes=duration_minutes,
                city=city,
                venue=venue,
                address=address,
                description=description,
                price_minimum=price_minimum,
                price_maximum=price_maximum,
                is_free=is_free,
                age_limit=age_limit,
                language=language,
                tags=tags,
                organization_link=organization_link,
                source_url=source_url,
                event_status=event_status,
            )
            created_event = await container.administration_service.create_manual_event(
                event_input=manual_input, actor=moderator_username
            )
            redirect_query = urlencode({"tab": "catalog", "notice": f"Карточка «{created_event.title}» сохранена."})
            return RedirectResponse(url=f"/admin?{redirect_query}", status_code=status.HTTP_303_SEE_OTHER)
        except (ApplicationException, ValueError) as error:
            return await _render_admin_dashboard(
                request=request,
                templates=templates,
                container=container,
                moderator_username=moderator_username,
                active_tab="catalog",
                status_filter="all",
                error_message=str(error),
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

    @router.post("/events/{event_identifier}/edit", response_class=HTMLResponse)
    async def admin_edit_event_action(
        request: Request,
        event_identifier: UUID,
        title: Annotated[str, Form()] = "",
        category: Annotated[str, Form()] = EventCategory.CONCERT.value,
        date_start: Annotated[str, Form()] = "",
        date_end: Annotated[str | None, Form()] = None,
        duration_minutes: Annotated[str | None, Form()] = None,
        city: Annotated[str, Form()] = "",
        venue: Annotated[str, Form()] = "",
        address: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
        price_minimum: Annotated[str | None, Form()] = None,
        price_maximum: Annotated[str | None, Form()] = None,
        is_free: Annotated[str | None, Form()] = None,
        age_limit: Annotated[str, Form()] = "0",
        language: Annotated[str | None, Form()] = None,
        tags: Annotated[str | None, Form()] = None,
        organization_link: Annotated[str | None, Form()] = None,
        source_url: Annotated[str, Form()] = "",
        event_status: Annotated[str, Form(alias="status")] = EventStatus.PUBLISHED.value,
    ) -> Response:
        """Обновляет существующую карточку мероприятия с записью изменений в аудит."""
        container = _require_container(request)
        moderator_username = _authenticated_username(request, container=container)
        if moderator_username is None:
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            manual_input = _build_manual_event_input_from_form(
                title=title,
                category=category,
                date_start=date_start,
                date_end=date_end,
                duration_minutes=duration_minutes,
                city=city,
                venue=venue,
                address=address,
                description=description,
                price_minimum=price_minimum,
                price_maximum=price_maximum,
                is_free=is_free,
                age_limit=age_limit,
                language=language,
                tags=tags,
                organization_link=organization_link,
                source_url=source_url,
                event_status=event_status,
            )
            updated_event = await container.administration_service.update_manual_event(
                event_identifier=event_identifier, event_input=manual_input, actor=moderator_username
            )
            redirect_query = urlencode({"tab": "catalog", "notice": f"Изменения в «{updated_event.title}» сохранены."})
            return RedirectResponse(url=f"/admin?{redirect_query}", status_code=status.HTTP_303_SEE_OTHER)
        except (ApplicationException, ValueError) as error:
            return await _render_admin_dashboard(
                request=request,
                templates=templates,
                container=container,
                moderator_username=moderator_username,
                active_tab="catalog",
                status_filter="all",
                edit_event_identifier=str(event_identifier),
                error_message=str(error),
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

    @router.post("/events/{event_identifier}/cancel")
    async def admin_cancel_event_action(
        request: Request, event_identifier: UUID, reason: Annotated[str | None, Form()] = None
    ) -> Response:
        """Переводит мероприятие в статус `CANCELED` (исключается из выдачи, доступно по ссылке)."""
        container = _require_container(request)
        moderator_username = _authenticated_username(request, container=container)
        if moderator_username is None:
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            canceled = await container.administration_service.cancel_event(
                event_identifier=event_identifier, actor=moderator_username, reason=reason
            )
            redirect_query = urlencode(
                {"tab": "catalog", "notice": f"Мероприятие «{canceled.title}» отменено и скрыто из поиска."}
            )
        except ApplicationException as error:
            redirect_query = urlencode({"tab": "catalog", "error": error.message})
        return RedirectResponse(url=f"/admin?{redirect_query}", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/import", response_class=HTMLResponse)
    async def admin_import_file_action(
        request: Request, upload_file: Annotated[UploadFile, File()], file_format: Annotated[str | None, Form()] = None
    ) -> Response:
        """Выполняет идемпотентный импорт CSV или JSON и отображает отчёт с ошибками по строкам."""
        container = _require_container(request)
        moderator_username = _authenticated_username(request, container=container)
        if moderator_username is None:
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        filename = upload_file.filename or "import.csv"
        raw_bytes = await upload_file.read(_MAXIMUM_IMPORT_FILE_BYTES + 1)
        if len(raw_bytes) > _MAXIMUM_IMPORT_FILE_BYTES:
            return await _render_admin_dashboard(
                request=request,
                templates=templates,
                container=container,
                moderator_username=moderator_username,
                active_tab="import",
                status_filter="all",
                error_message="Размер файла импорта превышает допустимые 5 МБ.",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

        explicit_format: Literal["csv", "json"] | None = None
        cleaned_format = normalize_optional_text(file_format)
        if cleaned_format == "csv":
            explicit_format = "csv"
        elif cleaned_format == "json":
            explicit_format = "json"

        try:
            import_report = await container.import_service.import_events(
                raw_content=raw_bytes, file_format=explicit_format, filename=filename, actor=moderator_username
            )
            return await _render_admin_dashboard(
                request=request,
                templates=templates,
                container=container,
                moderator_username=moderator_username,
                active_tab="import",
                status_filter="all",
                import_report=import_report,
                notice_message=f"Файл «{filename}» обработан.",
            )
        except ApplicationException as error:
            return await _render_admin_dashboard(
                request=request,
                templates=templates,
                container=container,
                moderator_username=moderator_username,
                active_tab="import",
                status_filter="all",
                error_message=error.message,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

    @router.post("/moderation/{moderation_identifier}/approve")
    async def admin_approve_moderation_action(request: Request, moderation_identifier: UUID) -> Response:
        """Одобряет спорную карточку в очереди модерации и публикует её в каталоге."""
        container = _require_container(request)
        moderator_username = _authenticated_username(request, container=container)
        if moderator_username is None:
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            _, updated_event = await container.administration_service.resolve_moderation_decision(
                moderation_identifier=moderation_identifier, decision="approved", moderator_username=moderator_username
            )
            redirect_query = urlencode(
                {"tab": "moderation", "notice": f"Карточка «{updated_event.title}» одобрена и опубликована."}
            )
        except ApplicationException as error:
            redirect_query = urlencode({"tab": "moderation", "error": error.message})
        return RedirectResponse(url=f"/admin?{redirect_query}", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/moderation/{moderation_identifier}/reject")
    async def admin_reject_moderation_action(request: Request, moderation_identifier: UUID) -> Response:
        """Отклоняет спорную или инъекционную карточку в очереди модерации."""
        container = _require_container(request)
        moderator_username = _authenticated_username(request, container=container)
        if moderator_username is None:
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            _, updated_event = await container.administration_service.resolve_moderation_decision(
                moderation_identifier=moderation_identifier, decision="rejected", moderator_username=moderator_username
            )
            redirect_query = urlencode(
                {"tab": "moderation", "notice": f"Карточка «{updated_event.title}» отклонена и снята с публикации."}
            )
        except ApplicationException as error:
            redirect_query = urlencode({"tab": "moderation", "error": error.message})
        return RedirectResponse(url=f"/admin?{redirect_query}", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/feedback/{feedback_identifier}/review")
    async def admin_mark_feedback_reviewed_action(request: Request, feedback_identifier: UUID) -> Response:
        """Помечает обращение организатора или жителя как рассмотренное."""
        container = _require_container(request)
        moderator_username = _authenticated_username(request, container=container)
        if moderator_username is None:
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        reviewed = await container.subscription_service.mark_feedback_reviewed(feedback_identifier=feedback_identifier)
        if reviewed is None:
            redirect_query = urlencode({"tab": "feedback", "error": "Обращение не найдено."})
        else:
            redirect_query = urlencode({"tab": "feedback", "notice": "Обращение отмечено как рассмотренное."})
        return RedirectResponse(url=f"/admin?{redirect_query}", status_code=status.HTTP_303_SEE_OTHER)

    return router


async def _render_admin_dashboard(
    *,
    request: Request,
    templates: Jinja2Templates,
    container: ApplicationContainer,
    moderator_username: str,
    active_tab: str,
    status_filter: str,
    search_query: str | None = None,
    edit_event_identifier: str | None = None,
    import_report: ImportReport | None = None,
    notice_message: str | None = None,
    error_message: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    target_statuses = _resolve_status_filter(status_filter)
    events_page = await container.administration_service.list_events(
        search_filter=EventSearchFilter(
            query=normalize_optional_text(search_query),
            sort_field=EventSortField.CREATED_AT,
            sort_direction=SortDirection.DESCENDING,
            page=1,
            page_size=40,
        ),
        statuses=target_statuses,
    )

    published_count = await container.event_repository.count_events(search_filter=EventSearchFilter())
    pending_moderation = await container.administration_service.list_moderation_queue(status="pending", limit=100)
    resolved_moderation = await container.administration_service.list_moderation_queue(status="approved", limit=20)
    audit_logs = await container.administration_service.list_audit_logs(limit=60)
    feedback_requests = await container.subscription_service.list_feedback_requests(limit=50)
    cities = await container.search_service.list_cities()

    editing_event: Event | None = None
    if edit_event_identifier is not None:
        try:
            editing_event = await container.administration_service.get_event_details(
                event_identifier=UUID(edit_event_identifier)
            )
        except ValueError, ApplicationException:
            editing_event = None

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        status_code=status_code,
        context={
            "request": request,
            "active_nav": "admin",
            "moderator_username": moderator_username,
            "active_tab": active_tab,
            "status_filter": status_filter,
            "search_query": search_query or "",
            "events_page": events_page,
            "published_count": published_count,
            "pending_moderation": pending_moderation,
            "resolved_moderation": resolved_moderation,
            "audit_logs": audit_logs,
            "feedback_requests": feedback_requests,
            "cities": cities,
            "editing_event": editing_event,
            "import_report": import_report,
            "notice_message": notice_message,
            "error_message": error_message,
            "disclaimer_text": SERVICE_DISCLAIMER_TEXT,
            "category_labels": CATEGORY_LABELS,
            "source_type_labels": SOURCE_TYPE_LABELS,
            "age_options": ALLOWED_AGE_OPTIONS,
            "format_price": format_price_label,
            "format_date_range": format_date_range_label,
            "format_status": format_status_label,
            "current_year": datetime.now().year,
        },
    )


def _require_container(request: Request) -> ApplicationContainer:
    container = getattr(request.app.state, "container", None)
    if not isinstance(container, ApplicationContainer):
        raise RuntimeError("Контейнер зависимостей веб-приложения не инициализирован.")
    return container


def _authenticated_username(request: Request, *, container: ApplicationContainer) -> str | None:
    token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME)
    return verify_admin_session(token=token, secret=container.web_settings.SESSION_SECRET)


def _resolve_status_filter(raw_status: str) -> tuple[EventStatus, ...]:
    if raw_status == EventStatus.PUBLISHED.value:
        return (EventStatus.PUBLISHED,)
    if raw_status == EventStatus.DRAFT.value:
        return (EventStatus.DRAFT,)
    if raw_status == EventStatus.CANCELED.value:
        return (EventStatus.CANCELED,)
    return (EventStatus.PUBLISHED, EventStatus.DRAFT, EventStatus.CANCELED)


def _parse_optional_form_int(raw_value: str | None, *, field_label: str) -> int | None:
    cleaned = normalize_optional_text(raw_value)
    if cleaned is None:
        return None
    try:
        return int(cleaned)
    except ValueError as error:
        raise ValueError(f"Поле «{field_label}» должно содержать целое число.") from error


def _build_manual_event_input_from_form(
    *,
    title: str,
    category: str,
    date_start: str,
    date_end: str | None,
    duration_minutes: str | None,
    city: str,
    venue: str,
    address: str,
    description: str,
    price_minimum: str | None,
    price_maximum: str | None,
    is_free: str | None,
    age_limit: str,
    language: str | None,
    tags: str | None,
    organization_link: str | None,
    source_url: str,
    event_status: str,
) -> ManualEventInput:
    try:
        resolved_category = EventCategory(category.strip().lower())
    except ValueError as error:
        raise ValueError(f"Неизвестная категория «{category}».") from error

    try:
        resolved_status = EventStatus(event_status.strip().lower())
    except ValueError as error:
        raise ValueError(f"Неизвестный статус «{event_status}».") from error

    parsed_start = parse_date_boundary(date_start, end_of_day=False)
    if parsed_start is None:
        raise ValueError("Необходимо указать дату и время начала мероприятия.")
    parsed_end = parse_date_boundary(date_end, end_of_day=False)
    parsed_duration = _parse_optional_form_int(duration_minutes, field_label="Длительность")
    parsed_min_price = _parse_optional_form_int(price_minimum, field_label="Минимальная цена")
    parsed_max_price = _parse_optional_form_int(price_maximum, field_label="Максимальная цена")
    parsed_age = _parse_optional_form_int(age_limit, field_label="Возрастное ограничение")
    parsed_is_free = (is_free or "").strip().lower() in {"1", "true", "on", "yes", "да"}

    tag_items = tuple(
        cleaned for raw_token in (tags or "").split(",") if (cleaned := normalize_optional_text(raw_token)) is not None
    )

    return ManualEventInput(
        title=title,
        category=resolved_category,
        date_start=parsed_start,
        date_end=parsed_end,
        duration_minutes=parsed_duration,
        city=city,
        venue=venue,
        address=address,
        description=description,
        price_minimum=parsed_min_price,
        price_maximum=parsed_max_price,
        is_free=parsed_is_free,
        age_limit=parsed_age if parsed_age is not None else 0,
        language=language,
        tags=tag_items,
        organization_link=organization_link,
        source_url=source_url,
        status=resolved_status,
    )
