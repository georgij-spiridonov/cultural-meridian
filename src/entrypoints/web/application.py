"""Фабрика веб-приложения FastAPI, OpenAPI 3.1, статика, шаблоны и жизненный цикл."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.entrypoints.bootstrap import ApplicationContainer, build_application_container
from src.entrypoints.web.administration import create_administration_router
from src.entrypoints.web.api import create_api_router, register_exception_handlers, register_observability_routes
from src.entrypoints.web.middleware import InMemoryRateLimiter, RequestContextMiddleware, RequestMetricsCollector
from src.entrypoints.web.pages import create_pages_router
from src.entrypoints.web.widget import create_widget_router

__all__ = ["create_default_application", "create_web_application"]

_WEB_DIRECTORY: Final[Path] = Path(__file__).resolve().parent
_STATIC_DIRECTORY: Final[Path] = _WEB_DIRECTORY / "static"
_TEMPLATES_DIRECTORY: Final[Path] = _WEB_DIRECTORY / "templates"


def create_web_application(container: ApplicationContainer | None = None) -> FastAPI:
    """Создаёт настроенный экземпляр FastAPI со всеми роутерами, OpenAPI 3.1 и middleware.

    Arguments:
        container: Опциональный готовый контейнер зависимостей (удобен для тестов).

    Returns:
        Готовое ASGI-приложение FastAPI.
    """
    resolved_container = container or build_application_container()
    manage_lifecycle = container is None

    @asynccontextmanager
    async def application_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if manage_lifecycle:
            await resolved_container.startup()
        try:
            yield
        finally:
            if manage_lifecycle:
                await resolved_container.shutdown()

    application = FastAPI(
        title="Культурный Меридиан — Афиша Республики Башкортостан API",
        description=(
            "Открытый версионированный REST API и веб-сервис афиши культурных мероприятий "
            "Республики Башкортостан. Предназначен для встраивания на официальные и партнёрские "
            "порталы через нейтральный JSON-контракт и виджет без сторонних cookies."
        ),
        version="1.0.0",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=application_lifespan,
    )

    metrics_collector = RequestMetricsCollector()
    rate_limiter = InMemoryRateLimiter(
        general_limit_per_minute=resolved_container.web_settings.RATE_LIMIT_REQUESTS_PER_MINUTE,
        feedback_limit_per_minute=resolved_container.web_settings.FEEDBACK_RATE_LIMIT_PER_MINUTE,
    )

    application.state.container = resolved_container
    application.state.metrics_collector = metrics_collector
    application.state.rate_limiter = rate_limiter

    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_container.web_settings.ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID", "If-None-Match"],
        expose_headers=["ETag", "X-Request-ID"],
    )
    application.add_middleware(RequestContextMiddleware, metrics_collector=metrics_collector, rate_limiter=rate_limiter)

    register_exception_handlers(application)
    register_observability_routes(application)

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIRECTORY))

    application.mount("/static", StaticFiles(directory=str(_STATIC_DIRECTORY)), name="static")
    application.include_router(create_api_router())
    application.include_router(create_widget_router(templates=templates))
    application.include_router(create_administration_router(templates=templates))
    application.include_router(create_pages_router(templates=templates))

    return application


def create_default_application() -> FastAPI:
    """Фабрика приложения по умолчанию для запуска через `uvicorn --factory`."""
    return create_web_application()
