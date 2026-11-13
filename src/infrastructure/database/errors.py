"""Трансляция ошибок SQLAlchemy и asyncpg в таксономию исключений приложения."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError, SQLAlchemyError

from src.core.exceptions import (
    NetworkConnectionFailureError,
    NetworkConnectionTimeoutError,
    ServiceUnavailableError,
    ValidationException,
)
from src.domain.exceptions import DuplicateEventConflictError

__all__ = ["extract_sqlstate", "handle_database_errors", "translate_database_error"]

_INTEGRITY_VALIDATION_SQLSTATES: frozenset[str] = frozenset({"22001", "22003", "22P02", "23502", "23503", "23514"})


def extract_sqlstate(error: SQLAlchemyError) -> str | None:
    """Извлекает код состояния PostgreSQL (`SQLSTATE`) из исключения SQLAlchemy.

    Arguments:
        error: Исключение SQLAlchemy, потенциально оборачивающее ошибку драйвера `asyncpg`.

    Returns:
        Пятисимвольный код `SQLSTATE` либо `None`, если исходное исключение его не содержит.
    """
    original_error = getattr(error, "orig", None)
    if original_error is None:
        return None
    for attribute_name in ("sqlstate", "pgcode"):
        value: object = getattr(original_error, attribute_name, None)
        if isinstance(value, str) and value:
            return value
    return None


def translate_database_error(
    error: TimeoutError | OSError | SQLAlchemyError, *, operation: str, duplicate_conflict_message: str | None = None
) -> (
    NetworkConnectionTimeoutError
    | NetworkConnectionFailureError
    | ServiceUnavailableError
    | ValidationException
    | DuplicateEventConflictError
):
    """Преобразует исключение драйвера или ORM в типизированную ошибку ядра или домена.

    Arguments:
        error: Пойманное исключение ввода-вывода, тайм-аута или SQLAlchemy.
        operation: Человекочитаемое описание операции для диагностического сообщения.
        duplicate_conflict_message: Доменное сообщение при конфликте уникальности (`23505`).

    Returns:
        Типизированное исключение `ApplicationException`, готовое к повторному выбросу через `raise ... from error`.
    """
    if isinstance(error, TimeoutError):
        return NetworkConnectionTimeoutError(f"Превышено время ожидания PostgreSQL при операции «{operation}».")
    if isinstance(error, OSError):
        return NetworkConnectionFailureError(f"Сетевая ошибка подключения к PostgreSQL при операции «{operation}».")

    if isinstance(error, IntegrityError):
        sqlstate = extract_sqlstate(error)
        if sqlstate == "23505":
            if duplicate_conflict_message is not None:
                return DuplicateEventConflictError(duplicate_conflict_message)
            return ValidationException(f"Нарушена уникальность записи в базе данных при операции «{operation}».")
        if sqlstate in _INTEGRITY_VALIDATION_SQLSTATES:
            return ValidationException(
                f"Нарушены ограничения целостности данных при операции «{operation}» (SQLSTATE={sqlstate})."
            )
        return ValidationException(f"Ошибка целостности данных при операции «{operation}».")

    if isinstance(error, (OperationalError, InterfaceError)):
        return ServiceUnavailableError(f"Сервер PostgreSQL недоступен при операции «{operation}».")

    return ServiceUnavailableError(f"Сбой базы данных PostgreSQL при операции «{operation}».")


@asynccontextmanager
async def handle_database_errors(
    *, operation: str, duplicate_conflict_message: str | None = None
) -> AsyncIterator[None]:
    # noinspection GrazieStyle
    """Оборачивает блок работы с PostgreSQL в единый транслятор исключений.

    Исключения `TimeoutError`, `OSError` и `SQLAlchemyError` транслируются в
    `ApplicationException`, тогда как доменные ошибки и `BaseException`
    (`asyncio.CancelledError`) пробрасываются без искажений.

    Arguments:
        operation: Человекочитаемое название операции в базе данных.
        duplicate_conflict_message: Необязательное сообщение для `DuplicateEventConflictError` при коде `23505`.

    Yields:
        Управление телу асинхронного контекстного менеджера.

    Raises:
        NetworkConnectionTimeoutError: При превышении времени ожидания базы данных.
        NetworkConnectionFailureError: При сетевом отказе подключения.
        ServiceUnavailableError: При недоступности или внутреннем сбое PostgreSQL.
        ValidationException: При нарушении ограничений `CHECK`, `NOT NULL` или `FOREIGN KEY`.
        DuplicateEventConflictError: При нарушении уникальности с переданным конфликтным сообщением.
    """
    try:
        yield
    except (TimeoutError, OSError, SQLAlchemyError) as error:
        raise translate_database_error(
            error, operation=operation, duplicate_conflict_message=duplicate_conflict_message
        ) from error
