"""Инфраструктурный пакет работы с базой данных PostgreSQL."""

from src.infrastructure.database.administration_repository import PostgreSqlAdministrationRepository
from src.infrastructure.database.dialog_repository import PostgreSqlDialogRepository
from src.infrastructure.database.engine import DatabaseEngineManager, DatabaseLifecycleState
from src.infrastructure.database.errors import handle_database_errors, translate_database_error
from src.infrastructure.database.event_repository import PostgreSqlEventRepository
from src.infrastructure.database.raw_event_repository import PostgreSqlRawEventRepository
from src.infrastructure.database.tables import DatabaseBase

__all__ = [
    "DatabaseBase",
    "DatabaseEngineManager",
    "DatabaseLifecycleState",
    "PostgreSqlAdministrationRepository",
    "PostgreSqlDialogRepository",
    "PostgreSqlEventRepository",
    "PostgreSqlRawEventRepository",
    "handle_database_errors",
    "translate_database_error",
]
