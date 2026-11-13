"""Глобальная конфигурация ядра приложения."""

from functools import lru_cache
from pathlib import Path
from typing import Final
from urllib.parse import quote_plus

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.types import EnvironmentType, LoggingFileModeType, LoggingLevelType

__all__ = [
    "PROJECT_ROOT",
    "ApplicationSettings",
    "DatabaseSettings",
    "LoggingSettings",
    "TelegramSettings",
    "WebSettings",
    "get_application_settings",
    "get_database_settings",
    "get_logging_settings",
    "get_telegram_settings",
    "get_web_settings",
]

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


class DatabaseSettings(BaseSettings):
    """Настройки подключения к PostgreSQL и параметров пула соединений.

    Attributes:
        HOST: Хост сервера PostgreSQL.
        PORT: TCP-порт сервера PostgreSQL.
        NAME: Имя базы данных.
        USER: Имя пользователя базы данных.
        PASSWORD: Пароль пользователя базы данных.
        POOL_SIZE: Базовый размер пула постоянных подключений.
        MAXIMUM_OVERFLOW: Максимальное число дополнительных подключений сверх базового пула.
        POOL_TIMEOUT_SECONDS: Максимальное время ожидания свободного подключения из пула в секундах.
        ECHO_SQL: Признак логирования выполняемых SQL-запросов движком SQLAlchemy.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="DATABASE_",
        extra="ignore",
        env_ignore_empty=True,
        frozen=True,
    )

    HOST: str = Field(default="localhost", min_length=1)
    PORT: int = Field(default=5432, ge=1, le=65535)
    NAME: str = Field(default="cultural_meridian", min_length=1)
    USER: str = Field(default="cultural_meridian", min_length=1)
    PASSWORD: str = Field(default="cultural_meridian_dev", min_length=1)
    POOL_SIZE: int = Field(default=5, ge=1)
    MAXIMUM_OVERFLOW: int = Field(default=10, ge=0)
    POOL_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0.0)
    ECHO_SQL: bool = False

    @property
    def connection_url(self) -> str:
        """Формирует асинхронную строку подключения SQLAlchemy для драйвера asyncpg.

        Returns:
            Строка подключения вида `postgresql+asyncpg://...` с экранированными реквизитами.
        """
        encoded_user = quote_plus(self.USER)
        encoded_password = quote_plus(self.PASSWORD)
        encoded_database_name = quote_plus(self.NAME)
        return f"postgresql+asyncpg://{encoded_user}:{encoded_password}@{self.HOST}:{self.PORT}/{encoded_database_name}"


class WebSettings(BaseSettings):
    """Настройки веб-сервера, REST API, ограничений частоты запросов и админ-сессий.

    Attributes:
        HOST: Сетевой адрес прослушивания HTTP-сервера.
        PORT: TCP-порт веб-сервера.
        PUBLIC_BASE_URL: Публичный базовый URL сервиса для генерации ссылок встраиваемого виджета.
        ADMIN_USERNAME: Логин встроенного модератора веб-панели.
        ADMIN_PASSWORD: Пароль встроенного модератора для демонстрационного и локального запуска.
        SESSION_SECRET: Секрет для HMAC-подписи сессионных cookie администратора.
        SESSION_TTL_SECONDS: Время жизни сессии модератора в секундах.
        RATE_LIMIT_REQUESTS_PER_MINUTE: Общий лимит запросов в минуту с одного IP-адреса.
        FEEDBACK_RATE_LIMIT_PER_MINUTE: Лимит отправок формы обратной связи с одного IP в минуту.
        ALLOWED_ORIGINS: Список разрешённых источников для CORS открытого JSON API.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="WEB_",
        extra="ignore",
        env_ignore_empty=True,
        frozen=True,
    )

    HOST: str = Field(default="0.0.0.0", min_length=1)
    PORT: int = Field(default=8000, ge=1, le=65535)
    PUBLIC_BASE_URL: str = Field(default="http://localhost:8000", min_length=1)
    ADMIN_USERNAME: str = Field(default="moderator", min_length=1)
    ADMIN_PASSWORD: str = Field(default="meridian_admin_dev", min_length=4)
    SESSION_SECRET: str = Field(default="cultural-meridian-dev-session-secret-change-me", min_length=16)
    SESSION_TTL_SECONDS: int = Field(default=43200, ge=300)
    RATE_LIMIT_REQUESTS_PER_MINUTE: int = Field(default=120, ge=10)
    FEEDBACK_RATE_LIMIT_PER_MINUTE: int = Field(default=10, ge=1)
    ALLOWED_ORIGINS: list[str] = Field(default_factory=lambda: ["*"])


class TelegramSettings(BaseSettings):
    """Настройки Telegram-бота на базе библиотеки `aiogram` v3 (long polling).

    Attributes:
        BOT_TOKEN: Токен Telegram-бота (при пустом значении бот запускается в режиме безопасного ожидания).
        PAGE_SIZE: Количество карточек мероприятий на одной странице выдачи в чате.
        IDLE_POLL_SECONDS: Интервал проверки сигнала остановки в режиме безопасного ожидания без токена.
        PUBLIC_WEB_BASE_URL: Публичный базовый URL веб-каталога для кнопок перехода на сайт (если настроен).
        DIALOG_HISTORY_LIMIT: Количество последних реплик, загружаемых из диалоговой сессии.
        PENDING_ACTION_LIFETIME_SECONDS: Время жизни запроса на подтверждение изменяющего действия в секундах.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="TELEGRAM_",
        extra="ignore",
        env_ignore_empty=True,
        frozen=True,
    )

    BOT_TOKEN: str = ""
    PAGE_SIZE: int = Field(default=5, ge=1, le=20)
    IDLE_POLL_SECONDS: float = Field(default=5.0, gt=0.0)
    PUBLIC_WEB_BASE_URL: str = ""
    DIALOG_HISTORY_LIMIT: int = Field(default=10, ge=5, le=50)
    PENDING_ACTION_LIFETIME_SECONDS: int = Field(default=600, ge=60, le=86400)

    @field_validator("BOT_TOKEN", "PUBLIC_WEB_BASE_URL", mode="after")
    @classmethod
    def strip_text_option(cls, value: str) -> str:
        """Удаляет крайние пробелы и завершающий слеш у строковых параметров конфигурации.

        Arguments:
            value: Исходное строковое значение параметра.

        Returns:
            Нормализованная строка без крайних пробелов.
        """
        return value.strip().rstrip("/")


@lru_cache
def get_telegram_settings() -> TelegramSettings:
    """Возвращает кэшированный экземпляр настроек Telegram-бота.

    Returns:
        Неизменяемые настройки Telegram-бота.
    """
    return TelegramSettings()


@lru_cache
def get_web_settings() -> WebSettings:
    """Возвращает кэшированный экземпляр настроек веб-сервиса.

    Returns:
        Неизменяемые настройки веб-сервера и API.
    """
    return WebSettings()


@lru_cache
def get_application_settings() -> ApplicationSettings:
    """Возвращает кэшированный экземпляр общих настроек приложения.

    Returns:
        Неизменяемые настройки приложения.
    """
    return ApplicationSettings()


@lru_cache
def get_database_settings() -> DatabaseSettings:
    """Возвращает кэшированный экземпляр настроек базы данных.

    Returns:
        Неизменяемые настройки базы данных.
    """
    return DatabaseSettings()


@lru_cache
def get_logging_settings() -> LoggingSettings:
    """Возвращает кэшированный экземпляр настроек логирования.

    Returns:
        Неизменяемые настройки логирования.
    """
    return LoggingSettings()
