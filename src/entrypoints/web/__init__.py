"""Пакет веб-сервиса, REST API `/api/v1`, встраиваемого виджета и админ-панели."""

from src.entrypoints.web.application import create_default_application, create_web_application

__all__ = ["create_default_application", "create_web_application"]
