"""Маршруты встраиваемого виджета `/widget`, изолированного скрипта `/widget.js` и демо-стенда."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from src.domain.entities import EventSearchFilter
from src.domain.exceptions import InvalidSearchFilterError
from src.domain.services import normalize_optional_text
from src.entrypoints.bootstrap import ApplicationContainer
from src.entrypoints.web._formatting import (
    CATEGORY_LABELS,
    PERIOD_LABELS,
    SERVICE_DISCLAIMER_TEXT,
    CatalogQueryParameters,
    build_search_filter_from_parameters,
    format_date_range_label,
    format_price_label,
)

__all__ = ["create_widget_router"]

_WIDGET_STATIC_SCRIPT_PATH = Path(__file__).resolve().parent / "static" / "widget.js"


def create_widget_router(*, templates: Jinja2Templates) -> APIRouter:
    """Создаёт маршрутизатор встраиваемого виджета, совместимого с сайтом Министерства культуры."""
    router = APIRouter(include_in_schema=False)

    @router.get("/widget", response_class=HTMLResponse)
    async def embedded_widget_frame(
        request: Request,
        city: Annotated[str | None, Query()] = None,
        category: Annotated[str | None, Query()] = None,
        period: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=12)] = 5,
        height: Annotated[int, Query(ge=280, le=1200)] = 520,
    ) -> HTMLResponse:
        """Рендерит изолированный HTML-виджет для встраивания в `<iframe>` без сторонних cookies."""
        container = _require_container(request)
        parameters = CatalogQueryParameters(
            city=normalize_optional_text(city),
            category=normalize_optional_text(category),
            period=normalize_optional_text(period),
            page=1,
            page_size=limit,
        )

        error_message: str | None = None
        try:
            search_filter = build_search_filter_from_parameters(parameters)
            search_page = await container.search_service.search_events(
                search_filter=search_filter, include_empty_explanation=True
            )
        except InvalidSearchFilterError as error:
            error_message = error.message
            search_page = await container.search_service.search_events(
                search_filter=EventSearchFilter(page=1, page_size=limit)
            )

        base_url = str(request.base_url).rstrip("/")
        full_catalog_query = request.url.query
        full_catalog_url = f"{base_url}/?{full_catalog_query}" if full_catalog_query else f"{base_url}/"

        response = templates.TemplateResponse(
            request=request,
            name="widget.html",
            headers={"Cache-Control": "public, max-age=60"},
            context={
                "request": request,
                "events": search_page.items,
                "total_count": search_page.total_count,
                "empty_explanation": search_page.empty_explanation,
                "error_message": error_message,
                "city": parameters.city,
                "category": parameters.category,
                "period": parameters.period,
                "widget_height": height,
                "base_url": base_url,
                "full_catalog_url": full_catalog_url,
                "disclaimer_text": SERVICE_DISCLAIMER_TEXT,
                "category_labels": CATEGORY_LABELS,
                "period_labels": PERIOD_LABELS,
                "format_price": format_price_label,
                "format_date_range": format_date_range_label,
            },
        )
        return response

    @router.get("/widget.js")
    async def embedded_widget_script() -> FileResponse:
        """Отдаёт изолированный клиентский скрипт инициализации виджета без cookies."""
        return FileResponse(
            path=_WIDGET_STATIC_SCRIPT_PATH,
            media_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @router.get("/widget/demo", response_class=HTMLResponse)
    async def widget_integration_demo(
        request: Request,
        city: Annotated[str | None, Query()] = "Уфа",
        category: Annotated[str | None, Query()] = None,
        period: Annotated[str | None, Query()] = "weekend",
        limit: Annotated[int, Query(ge=1, le=12)] = 5,
        height: Annotated[int, Query(ge=280, le=1200)] = 540,
    ) -> HTMLResponse:
        """Демонстрационная страница стороннего портала с подключённым `/widget.js` и конструктором кода."""
        container = _require_container(request)
        cities = await container.search_service.list_cities()
        base_url = str(request.base_url).rstrip("/")

        return templates.TemplateResponse(
            request=request,
            name="widget_demo.html",
            context={
                "request": request,
                "active_nav": "widget",
                "disclaimer_text": SERVICE_DISCLAIMER_TEXT,
                "cities": cities,
                "category_labels": CATEGORY_LABELS,
                "period_labels": PERIOD_LABELS,
                "selected_city": normalize_optional_text(city) or "",
                "selected_category": normalize_optional_text(category) or "",
                "selected_period": normalize_optional_text(period) or "",
                "selected_limit": limit,
                "selected_height": height,
                "base_url": base_url,
            },
        )

    return router


def _require_container(request: Request) -> ApplicationContainer:
    container = getattr(request.app.state, "container", None)
    if not isinstance(container, ApplicationContainer):
        raise RuntimeError("Контейнер зависимостей веб-приложения не инициализирован.")
    return container
