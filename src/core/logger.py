"""Конфигурация структурного логгера приложения."""

import logging
import logging.config
from contextvars import ContextVar, Token
from typing import Final, TypedDict, cast, override

from src.core.configuration import LoggingSettings, get_logging_settings

__all__ = [
    "LoggingConfiguration",
    "RequestIdentifierFilter",
    "RootLoggerConfiguration",
    "create_logging_configuration",
    "get_logger",
    "get_request_identifier",
    "reset_request_identifier",
    "set_request_identifier",
    "setup_logger",
]

_REQUEST_IDENTIFIER_CONTEXT: Final[ContextVar[str]] = ContextVar("request_identifier", default="-")

_STANDARD_LOG_FORMAT: Final[str] = (
    "[%(asctime)s] [%(levelname)s] [%(request_identifier)s] [%(name)s:%(lineno)d]: %(message)s"
)
_STANDARD_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"
_DETAILED_LOG_FORMAT: Final[str] = (
    "%(asctime)s | %(levelname)-8s | %(request_identifier)s | %(threadName)s | "
    "%(name)s:%(funcName)s:%(lineno)d - %(message)s"
)


class RequestIdentifierFilter(logging.Filter):
    """Фильтр логирования, добавляющий идентификатор текущего запроса без персональных данных."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        """Обогащает запись лога полем `request_identifier` из контекста выполнения.

        Arguments:
            record: Запись стандартного модуля `logging`.

        Returns:
            Всегда `True`, чтобы не отбрасывать сообщения.
        """
        record.request_identifier = _REQUEST_IDENTIFIER_CONTEXT.get()
        return True


def set_request_identifier(request_identifier: str) -> Token[str]:
    """Устанавливает идентификатор текущего запроса в контекст выполнения.

    Arguments:
        request_identifier: Уникальный идентификатор запроса или обновления.

    Returns:
        Токен для последующего восстановления предыдущего значения контекста.
    """
    return _REQUEST_IDENTIFIER_CONTEXT.set(request_identifier)


def reset_request_identifier(token: Token[str]) -> None:
    """Восстанавливает предыдущее значение идентификатора запроса в контексте.

    Arguments:
        token: Токен, возвращённый функцией `set_request_identifier`.
    """
    _REQUEST_IDENTIFIER_CONTEXT.reset(token)


def get_request_identifier() -> str:
    """Возвращает идентификатор запроса из текущего контекста выполнения.

    Returns:
        Идентификатор текущего запроса либо `"-"` вне контекста запроса.
    """
    return _REQUEST_IDENTIFIER_CONTEXT.get()


class RootLoggerConfiguration(TypedDict):
    """Словарь конфигурации корневого логгера.

    Attributes:
        level: Минимальный уровень сообщений корневого логгера.
        handlers: Имена обработчиков, подключённых к корневому логгеру.
    """

    level: str
    handlers: list[str]


class LoggingConfiguration(TypedDict, total=False):
    """Словарь конфигурации подсистемы логирования для `logging.config.dictConfig`.

    Attributes:
        version: Версия схемы конфигурации `logging.config`.
        disable_existing_loggers: Признак отключения ранее созданных логгеров.
        filters: Описание фильтров записей лога.
        formatters: Описание форматтеров по их именам.
        handlers: Описание обработчиков по их именам.
        loggers: Описание именованных логгеров.
        root: Конфигурация корневого логгера.
    """

    version: int
    disable_existing_loggers: bool
    filters: dict[str, dict[str, object]]
    formatters: dict[str, dict[str, str]]
    handlers: dict[str, dict[str, object]]
    loggers: dict[str, dict[str, object]]
    root: RootLoggerConfiguration


def create_logging_configuration(settings: LoggingSettings) -> LoggingConfiguration:
    """Генерирует словарь конфигурации логирования на основе настроек приложения.

    Arguments:
        settings: Неизменяемые настройки логирования.

    Returns:
        Словарь конфигурации для передачи в `logging.config.dictConfig`.
    """
    handlers: dict[str, dict[str, object]] = {
        "console": {
            "class": "logging.StreamHandler",
            "level": settings.CONSOLE_LEVEL,
            "formatter": "standard",
            "filters": ["request_identifier"],
            "stream": "ext://sys.stdout",
        }
    }
    root_handlers = ["console"]

    if settings.TO_FILE:
        settings.DIRECTORY.mkdir(parents=True, exist_ok=True)
        handlers["file_debug"] = {
            "class": "logging.FileHandler",
            "level": "DEBUG",
            "formatter": "detailed",
            "filters": ["request_identifier"],
            "filename": str(settings.DIRECTORY / settings.FILE_APPLICATION),
            "mode": settings.FILE_MODE,
            "encoding": "utf-8",
        }
        handlers["file_errors"] = {
            "class": "logging.FileHandler",
            "level": "ERROR",
            "formatter": "detailed",
            "filters": ["request_identifier"],
            "filename": str(settings.DIRECTORY / settings.FILE_ERRORS),
            "mode": settings.FILE_MODE,
            "encoding": "utf-8",
        }
        root_handlers.extend(["file_debug", "file_errors"])

    # noinspection bad-typed-dict
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"request_identifier": {"()": "src.core.logger.RequestIdentifierFilter"}},
        "formatters": {
            "standard": {"format": _STANDARD_LOG_FORMAT, "datefmt": _STANDARD_DATE_FORMAT},
            "detailed": {"format": _DETAILED_LOG_FORMAT},
        },
        "handlers": handlers,
        "loggers": {},
        "root": {"level": "DEBUG", "handlers": root_handlers},
    }


def setup_logger(settings: LoggingSettings | None = None) -> None:
    """Инициализирует конфигурацию логирования и при необходимости создаёт директорию журналов.

    Arguments:
        settings: Настройки логирования. Если не переданы, используются кэшированные настройки.
    """
    effective_settings = settings or get_logging_settings()
    configuration = create_logging_configuration(effective_settings)
    logging.config.dictConfig(cast(dict[str, object], cast(object, configuration)))


def get_logger(module_name: str) -> logging.Logger:
    """Возвращает именованный логгер для указанного модуля.

    Arguments:
        module_name: Имя модуля, обычно `__name__`.

    Returns:
        Экземпляр логгера стандартной библиотеки `logging`.
    """
    return logging.getLogger(module_name)
