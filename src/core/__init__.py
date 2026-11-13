"""Ядро приложения: конфигурация, таксономия исключений, логирование и базовые типы."""

from src.core.configuration import (
    PROJECT_ROOT,
    ApplicationSettings,
    DatabaseSettings,
    LoggingSettings,
    get_application_settings,
    get_database_settings,
    get_logging_settings,
)
from src.core.exceptions import ApplicationException, DomainException, InfrastructureException
from src.core.logger import (
    get_logger,
    get_request_identifier,
    reset_request_identifier,
    set_request_identifier,
    setup_logger,
)
from src.core.types import DialogChannel, EventCategory, EventStatus, RealCollection, SourceType

__all__ = [
    "PROJECT_ROOT",
    "ApplicationException",
    "ApplicationSettings",
    "DatabaseSettings",
    "DialogChannel",
    "DomainException",
    "EventCategory",
    "EventStatus",
    "InfrastructureException",
    "LoggingSettings",
    "RealCollection",
    "SourceType",
    "get_application_settings",
    "get_database_settings",
    "get_logger",
    "get_logging_settings",
    "get_request_identifier",
    "reset_request_identifier",
    "set_request_identifier",
    "setup_logger",
]
