"""Глобальная конфигурация ядра приложения."""

import json
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
    "LanguageModelSettings",
    "LoggingSettings",
    "TelegramSettings",
    "WebSettings",
    "get_application_settings",
    "get_database_settings",
    "get_language_model_settings",
    "get_logging_settings",
    "get_telegram_settings",
    "get_web_settings",
]

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
DEFAULT_LANGUAGE_MODEL_PRICING_JSON: Final[str] = json.dumps(
    {
        "google/gemini-3.5-flash-lite": {"input": 24.0, "output": 200.0},
        "google/gemini-4-hydrogen": {"input": 135.0, "output": 675.0},
    },
    ensure_ascii=False,
    sort_keys=True,
)


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


def _extract_tariff_rate(tariffs: dict[object, object], primary_key: str, fallback_key: str, model_name: str) -> float:
    """Извлекает и валидирует неотрицательный тариф модели из словаря тарифов.

    Arguments:
        tariffs: Словарь тарифов конкретной модели.
        primary_key: Основной ключ тарифа (`input` или `output`).
        fallback_key: Резервный ключ тарифа (`input_rubles_per_million` или `output_rubles_per_million`).
        model_name: Имя модели для информативного сообщения об ошибке.

    Returns:
        Неотрицательная стоимость за миллион токенов в рублях.

    Raises:
        ValueError: Если ключ отсутствует или значение не является неотрицательным числом.
    """
    raw_value = tariffs.get(primary_key, tariffs.get(fallback_key))
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
        raise ValueError(f"Для модели '{model_name}' отсутствует числовой тариф '{primary_key}'.")
    rate = float(raw_value)
    if rate < 0.0:
        raise ValueError(f"Тариф '{primary_key}' для модели '{model_name}' не может быть отрицательным.")
    return rate


def _parse_pricing_payload(value: str | dict[str, dict[str, float]]) -> dict[str, tuple[float, float]]:
    """Разбирает и валидирует таблицу тарифов языковых моделей в рублях за 1M токенов.

    Arguments:
        value: JSON-строка или словарь с тарифами моделей.

    Returns:
        Словарь вида `{model_name: (input_rubles_per_million, output_rubles_per_million)}`.

    Raises:
        ValueError: При некорректном JSON, неверной структуре или отрицательных тарифах.
    """
    raw_payload: object
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}
        try:
            raw_payload = json.loads(stripped)
        except json.JSONDecodeError as error:
            raise ValueError(f"Некорректный JSON в LANGUAGE_MODEL_PRICING_JSON: {error}") from error
    else:
        raw_payload = value

    if not isinstance(raw_payload, dict):
        raise ValueError("LANGUAGE_MODEL_PRICING_JSON должен быть JSON-объектом или словарём.")

    pricing_map: dict[str, tuple[float, float]] = {}
    for raw_model_name, raw_tariffs in raw_payload.items():
        if not isinstance(raw_model_name, str) or not raw_model_name.strip():
            raise ValueError("Имя модели в LANGUAGE_MODEL_PRICING_JSON должно быть непустой строкой.")
        if not isinstance(raw_tariffs, dict):
            raise ValueError(f"Тарифы модели '{raw_model_name}' должны быть словарём.")
        model_name = raw_model_name.strip()
        input_rate = _extract_tariff_rate(raw_tariffs, "input", "input_rubles_per_million", model_name)
        output_rate = _extract_tariff_rate(raw_tariffs, "output", "output_rubles_per_million", model_name)
        pricing_map[model_name] = (input_rate, output_rate)
    return pricing_map


class LanguageModelSettings(BaseSettings):
    """Настройки языковой модели, провайдера OpenRouter и бюджетных ограничений ИИ-слоя.

    Attributes:
        BASE_URL: Базовый URL OpenAI-совместимого API провайдера (по умолчанию OpenRouter).
        API_KEY: Секретный ключ доступа к API языковой модели.
        PRIMARY: Идентификатор основной модели для рассуждений и диалога.
        FAST: Идентификатор быстрой модели для структурированного извлечения и классификации.
        PRICING_JSON: Таблица тарифов моделей (JSON-строка или словарь в рублях за 1M токенов).
        REQUEST_TIMEOUT_SECONDS: Таймаут одного HTTP-запроса к языковой модели в секундах.
        MAX_RETRIES: Максимальное число повторных попыток при временных сбоях сети/API.
        MAX_AGENT_STEPS: Максимальное число шагов (вызовов инструментов) агента за один ход.
        MAX_OUTPUT_TOKENS: Максимальное число выходных токенов в одном ответе модели.
        USER_DAILY_BUDGET_RUBLES: Дневной лимит расходов на одного пользователя в рублях.
        GLOBAL_DAILY_BUDGET_RUBLES: Общий дневной лимит расходов всего сервиса в рублях.
        USD_TO_RUB_RATE: Курс пересчёта USD в RUB при использовании резервного биллинга из `raw_usage`.
        HTTP_REFERER: Заголовок `HTTP-Referer` для атрибуции запросов в OpenRouter.
        APP_TITLE: Заголовок `X-Title` для атрибуции приложения в OpenRouter.
        PROMPT_VERSION: Версия системного промпта агента для аудита и трассировки.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="LANGUAGE_MODEL_",
        extra="ignore",
        env_ignore_empty=True,
        frozen=True,
    )

    BASE_URL: str = "https://openrouter.ai/api/v1"
    API_KEY: str = ""
    PRIMARY: str = "google/gemini-4-hydrogen"
    FAST: str = "google/gemini-3.5-flash-lite"
    PRICING_JSON: str | dict[str, dict[str, float]] = DEFAULT_LANGUAGE_MODEL_PRICING_JSON
    REQUEST_TIMEOUT_SECONDS: float = Field(default=20.0, gt=0.0)
    MAX_RETRIES: int = Field(default=1, ge=0, le=5)
    MAX_AGENT_STEPS: int = Field(default=5, ge=1, le=5)
    MAX_OUTPUT_TOKENS: int = Field(default=800, ge=64, le=4096)
    USER_DAILY_BUDGET_RUBLES: float = Field(default=15.0, ge=0.0)
    GLOBAL_DAILY_BUDGET_RUBLES: float = Field(default=250.0, ge=0.0)
    USD_TO_RUB_RATE: float = Field(default=90.0, gt=0.0)
    HTTP_REFERER: str = "https://cultural-meridian.local"
    APP_TITLE: str = "Cultural Meridian"
    PROMPT_VERSION: str = "v1.0"

    @field_validator("BASE_URL", "HTTP_REFERER", mode="after")
    @classmethod
    def strip_url_option(cls, value: str) -> str:
        """Удаляет крайние пробелы и завершающий слеш у URL-параметров конфигурации.

        Arguments:
            value: Исходное строковое значение URL.

        Returns:
            Нормализованный URL без крайних пробелов и завершающего слеша.
        """
        return value.strip().rstrip("/")

    @field_validator("API_KEY", "PRIMARY", "FAST", "APP_TITLE", "PROMPT_VERSION", mode="after")
    @classmethod
    def strip_plain_text_option(cls, value: str) -> str:
        """Удаляет крайние пробелы у текстовых параметров конфигурации языковой модели.

        Arguments:
            value: Исходное строковое значение параметра.

        Returns:
            Строка без крайних пробельных символов.
        """
        return value.strip()

    @field_validator("PRICING_JSON", mode="after")
    @classmethod
    def normalize_pricing_json(cls, value: str | dict[str, dict[str, float]]) -> str:
        """Валидирует и нормализует таблицу тарифов в неизменяемую JSON-строку.

        Arguments:
            value: Исходная JSON-строка или словарь тарифов.

        Returns:
            Каноническая JSON-строка с отсортированными ключами моделей.
        """
        pricing_map = _parse_pricing_payload(value)
        normalized: dict[str, dict[str, float]] = {
            model_name: {"input": input_rate, "output": output_rate}
            for model_name, (input_rate, output_rate) in sorted(pricing_map.items())
        }
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True)

    def get_pricing_map(self) -> dict[str, tuple[float, float]]:
        """Возвращает словарь тарифов моделей `(input_rubles_per_million, output_rubles_per_million)`.

        Returns:
            Словарь тарифов в рублях за один миллион токенов для каждой настроенной модели.
        """
        return _parse_pricing_payload(self.PRICING_JSON)


@lru_cache
def get_language_model_settings() -> LanguageModelSettings:
    """Возвращает кэшированный экземпляр настроек языковой модели и ИИ-слоя.

    Returns:
        Неизменяемые настройки языковой модели.
    """
    return LanguageModelSettings()


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
