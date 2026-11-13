"""Глобальная конфигурация ядра приложения."""

from functools import lru_cache
from pathlib import Path
from typing import Final

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.types import EnvironmentType, LoggingFileModeType, LoggingLevelType

__all__ = ["PROJECT_ROOT", "ApplicationSettings", "LoggingSettings", "get_application_settings", "get_logging_settings"]

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent


class ApplicationSettings(BaseSettings):
    """Общие настройки среды и приложения.

    Attributes:
        NAME: Наименование сервиса.
        ENVIRONMENT: Среда выполнения (`development`, `production` или `testing`).
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="APPLICATION_",
        extra="ignore",
        env_ignore_empty=True,
        frozen=True,
    )

    NAME: str = "cultural-meridian"
    ENVIRONMENT: EnvironmentType = "development"


class LoggingSettings(BaseSettings):
    # noinspection GrazieInspection
    """Настройки подсистемы структурного логирования.

    Attributes:
        CONSOLE_LEVEL: Минимальный уровень сообщений для вывода в стандартный поток вывода.
        DIRECTORY: Абсолютный путь к директории журналов.
        FILE_APPLICATION: Имя файла общего журнала приложения.
        FILE_ERRORS: Имя файла журнала ошибок.
        FILE_MODE: Режим открытия файлов журналов (`w` — перезапись, `a` — дозапись).
        TO_FILE: Признак включения записи журналов в файлы.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="LOGGING_",
        extra="ignore",
        env_ignore_empty=True,
        frozen=True,
    )

    CONSOLE_LEVEL: LoggingLevelType = "INFO"
    DIRECTORY: Path = PROJECT_ROOT / "logs"
    FILE_APPLICATION: str = "application.log"
    FILE_ERRORS: str = "errors.log"
    FILE_MODE: LoggingFileModeType = "a"
    TO_FILE: bool = True

    @field_validator("DIRECTORY", mode="after")
    @classmethod
    def resolve_directory_path(cls, directory_path: Path) -> Path:
        """Преобразует относительный путь к директории логов в абсолютный от корня проекта.

        Arguments:
            directory_path: Путь к директории журналов из конфигурации.

        Returns:
            Абсолютный путь к директории журналов.
        """
        if directory_path.is_absolute():
            return directory_path
        return PROJECT_ROOT / directory_path


@lru_cache
def get_application_settings() -> ApplicationSettings:
    """Возвращает кэшированный экземпляр общих настроек приложения.

    Returns:
        Неизменяемые настройки приложения.
    """
    return ApplicationSettings()


@lru_cache
def get_logging_settings() -> LoggingSettings:
    """Возвращает кэшированный экземпляр настроек логирования.

    Returns:
        Неизменяемые настройки логирования.
    """
    return LoggingSettings()
