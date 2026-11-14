"""Реестр источников афиш, строгая YAML-валидация и общий транспорт загрузчиков."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, Self
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import url2pathname

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.core.configuration import PROJECT_ROOT
from src.core.exceptions import (
    AccessPermissionDeniedError,
    ServiceRateLimitExceededError,
    ServiceUnavailableError,
    ValidationInputError,
    ValidationSchemaError,
)
from src.core.logger import get_logger
from src.core.types import EventCategory, SourceType
from src.domain.entities import FetchedSourceItem, SourceDefinition
from src.domain.exceptions import UnsupportedSourceFormatError
from src.domain.ports import SourceFetcherPort
from src.domain.services import compute_payload_hash
from src.infrastructure.sources.http_client import PoliteHttpClient

__all__ = [
    "CompositeSourceFetcher",
    "ParserConfiguration",
    "ParserName",
    "SourceParserSchema",
    "SourceRegistryItemSchema",
    "SourcesRegistrySchema",
    "load_parser_configurations",
    "load_sources_registry",
]

type ParserName = Literal["wordpress_events", "quicktickets", "rusdramteatr", "bashopera"]

logger = get_logger(__name__)

DEFAULT_SOURCE_USER_AGENT: Final[str] = "cultural-meridian-bot/1.0 (+https://cultural-meridian.local)"
_SUPPORTED_FEED_SOURCE_TYPES: Final[frozenset[SourceType]] = frozenset(
    {SourceType.RSS, SourceType.API, SourceType.WEBPAGE}
)
_SUPPORTED_FEED_TYPE_VALUES: Final[frozenset[str]] = frozenset(item.value for item in _SUPPORTED_FEED_SOURCE_TYPES)
_HTTP_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
_CANCELED_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    {"canceled", "cancelled", "отменено", "отменен", "отменён", "eventcancelled"}
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ParserConfiguration:
    """Именной парсер разметки конкретного сайта; живёт в инфраструктуре и не попадает в домен.

    Attributes:
        parser: Имя парсера разметки.
        options: Строковые параметры парсера (например, город страницы афиши).
    """

    parser: ParserName
    options: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        """Делает параметры парсера неизменяемыми."""
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


class SourceParserSchema(BaseModel):
    """Схема секции `parser` источника в `sources.yaml`.

    Attributes:
        name: Имя именного парсера разметки сайта.
        options: Строковые параметры парсера.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ParserName
    options: dict[str, str] = Field(default_factory=dict)


class SourceRegistryItemSchema(BaseModel):
    """Строгая Pydantic-схема одного источника в файле `configuration/sources.yaml`.

    Attributes:
        name: Уникальное имя источника.
        source_type: Тип протокола (`rss`, `api` или `webpage`).
        source_url: HTTP(S)-адрес, `file://`-URI или относительный путь к локальному фиду.
        default_city: Город по умолчанию для записей без явного указания города.
        default_category: Категория по умолчанию для специализированных источников.
        default_venue: Площадка источника в его городе, как она названа в справочнике площадок.
        is_enabled: Признак активности источника при плановом сборе.
        request_timeout_seconds: Таймаут сетевого запроса в секундах.
        parser: Именной парсер разметки сайта; без него HTML разбирается общим разбором JSON-LD и карточек.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    source_type: Literal[SourceType.RSS, SourceType.API, SourceType.WEBPAGE]
    source_url: str = Field(min_length=1)
    default_city: str | None = None
    default_category: EventCategory | None = None
    default_venue: str | None = None
    is_enabled: bool = True
    request_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
    parser: SourceParserSchema | None = None

    @model_validator(mode="after")
    def _check_parser_source_type(self) -> Self:
        """Разрешает именной парсер только для HTML-источников."""
        if self.parser is not None and self.source_type != SourceType.WEBPAGE:
            raise ValueError("Именной парсер задаётся только для источников типа webpage.")
        return self

    @field_validator("name", "source_url", mode="before")
    @classmethod
    def _validate_required_text(cls, value: object) -> object:
        """Удаляет крайние пробелы и отклоняет пустые обязательные строки."""
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Обязательное текстовое поле источника не может быть пустым.")
        return cleaned

    @field_validator("default_city", mode="before")
    @classmethod
    def _normalize_optional_city(cls, value: object) -> object:
        """Преобразует пустую строку города по умолчанию в `None`."""
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        return cleaned or None

    @field_validator("default_category", mode="before")
    @classmethod
    def _normalize_optional_category(cls, value: object) -> object:
        """Преобразует пустую строку категории по умолчанию в `None`."""
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        return cleaned or None


class SourcesRegistrySchema(BaseModel):
    """Строгая Pydantic-схема корневого объекта реестра `sources.yaml`.

    Attributes:
        sources: Список описаний настроенных источников афиш.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sources: tuple[SourceRegistryItemSchema, ...]


def is_http_url(raw_url: str) -> bool:
    """Проверяет, является ли строка адресом протокола HTTP или HTTPS.

    Arguments:
        raw_url: Проверяемая строка адреса или пути.

    Returns:
        `True`, если схема URL равна `http` или `https`.
    """
    return urlsplit(raw_url.strip()).scheme.lower() in _HTTP_SCHEMES


def is_file_uri(raw_url: str) -> bool:
    """Проверяет, является ли строка локальным URI со схемой `file://`.

    Arguments:
        raw_url: Проверяемая строка адреса или пути.

    Returns:
        `True`, если схема URI равна `file`.
    """
    return urlsplit(raw_url.strip()).scheme.lower() == "file"


def _is_within_allowed_roots(candidate: Path, *allowed_roots: Path | None) -> bool:
    """Проверяет, что разрешённый путь находится внутри одного из допустимых корней."""
    return any(root is not None and candidate.is_relative_to(root) for root in allowed_roots)


def resolve_local_source_path(
    raw_url: str, *, project_root: Path = PROJECT_ROOT, local_feeds_directory: Path | None = None
) -> Path:
    """Безопасно преобразует локальный путь или `file://` URI в путь внутри корня проекта.

    Отбрасывает `#fragment` и `?query` и блокирует выход за пределы `project_root`
    и `local_feeds_directory`.

    Arguments:
        raw_url: Локальный путь или `file://` URI из конфигурации или карточки события.
        project_root: Корневая директория проекта.
        local_feeds_directory: Дополнительная директория локальных фидов.

    Returns:
        Абсолютный разрешённый путь к локальному файлу.

    Raises:
        AccessPermissionDeniedError: При попытке выхода за пределы допустимых директорий.
        UnsupportedSourceFormatError: Если указана неподдерживаемая схема URL.
        ValidationInputError: Если путь пустой или синтаксически некорректен.
    """
    cleaned = raw_url.strip()
    if not cleaned:
        raise ValidationInputError("Пустой путь к локальному источнику.")

    resolved_root = project_root.resolve()
    resolved_feeds_dir = local_feeds_directory.resolve() if local_feeds_directory is not None else None
    parsed = urlsplit(cleaned)
    scheme = parsed.scheme.lower()

    if scheme == "file":
        if parsed.netloc not in ("", "localhost"):
            raise AccessPermissionDeniedError(f"Недопустимый узел '{parsed.netloc}' в file:// URI: {raw_url}")
        local_pathname = url2pathname(unquote(parsed.path))
        if not local_pathname.strip():
            raise ValidationInputError(f"Пустой путь в file:// URI: {raw_url}")
        resolved_candidate = Path(local_pathname).resolve()
        if not _is_within_allowed_roots(resolved_candidate, resolved_root, resolved_feeds_dir):
            raise AccessPermissionDeniedError(f"Доступ к файлу за пределами корня проекта запрещён: {raw_url}")
        return resolved_candidate

    # Однобуквенная схема на Windows является буквой диска (например, C:\...).
    if scheme and not (len(scheme) == 1 and scheme.isalpha()):
        raise UnsupportedSourceFormatError(
            f"Неподдерживаемая схема адреса источника '{scheme}': {raw_url}", format_name=scheme, source_name=raw_url
        )

    path_without_query = cleaned.split("#", 1)[0].split("?", 1)[0].strip()
    if not path_without_query:
        raise ValidationInputError(f"Некорректный локальный путь источника: {raw_url}")

    candidate_path = Path(path_without_query)
    if candidate_path.is_absolute():
        resolved_candidate = candidate_path.resolve()
        if not _is_within_allowed_roots(resolved_candidate, resolved_root, resolved_feeds_dir):
            raise AccessPermissionDeniedError(f"Доступ к файлу за пределами корня проекта запрещён: {raw_url}")
        return resolved_candidate

    primary_candidate = (resolved_root / candidate_path).resolve()
    if not _is_within_allowed_roots(primary_candidate, resolved_root, resolved_feeds_dir):
        raise AccessPermissionDeniedError(f"Попытка выхода за пределы корня проекта: {raw_url}")
    if primary_candidate.is_file() or resolved_feeds_dir is None:
        return primary_candidate

    secondary_candidate = (resolved_feeds_dir / candidate_path).resolve()
    if not _is_within_allowed_roots(secondary_candidate, resolved_root, resolved_feeds_dir):
        raise AccessPermissionDeniedError(f"Попытка выхода за пределы директории фидов: {raw_url}")
    if secondary_candidate.is_file():
        return secondary_candidate
    return primary_candidate


def load_sources_registry(
    config_path: Path, *, project_root: Path = PROJECT_ROOT, only_enabled: bool = False
) -> tuple[SourceDefinition, ...]:
    """Читает, валидирует по строгой Pydantic-схеме и возвращает источники из YAML-реестра.

    Arguments:
        config_path: Путь к YAML-файлу реестра источников (`configuration/sources.yaml`).
        project_root: Корень проекта для разрешения относительного пути к конфигурации и проверки путей.
        only_enabled: Возвращать только источники с `is_enabled=True`.

    Returns:
        Кортеж неизменяемых доменных описаний `SourceDefinition`.

    Raises:
        AccessPermissionDeniedError: Если локальный `source_url` выходит за пределы `project_root`.
        UnsupportedSourceFormatError: Если в реестре указан неподдерживаемый `source_type` или схема URL.
        ValidationInputError: Если файл реестра не найден или не читается.
        ValidationSchemaError: При ошибке YAML или нарушении строгой схемы реестра.
    """
    validated_registry = _load_validated_registry(config_path, project_root=project_root)

    definitions: list[SourceDefinition] = []
    for item in validated_registry.sources:
        if only_enabled and not item.is_enabled:
            continue
        definitions.append(
            SourceDefinition(
                name=item.name,
                source_type=item.source_type,
                source_url=item.source_url,
                default_city=item.default_city,
                default_category=item.default_category,
                default_venue=item.default_venue,
                is_enabled=item.is_enabled,
                request_timeout_seconds=item.request_timeout_seconds,
            )
        )
    return tuple(definitions)


def load_parser_configurations(
    config_path: Path, *, project_root: Path = PROJECT_ROOT
) -> Mapping[str, ParserConfiguration]:
    """Возвращает именные парсеры источников из YAML-реестра по имени источника.

    Arguments:
        config_path: Путь к YAML-файлу реестра источников.
        project_root: Корень проекта для разрешения относительного пути.

    Returns:
        Неизменяемое отображение «имя источника → конфигурация парсера» только для источников с парсером.

    Raises:
        ValidationInputError: Если файл реестра не найден или не читается.
        ValidationSchemaError: При ошибке YAML или нарушении строгой схемы реестра.
    """
    validated_registry = _load_validated_registry(config_path, project_root=project_root)
    return MappingProxyType(
        {
            item.name: ParserConfiguration(parser=item.parser.name, options=item.parser.options)
            for item in validated_registry.sources
            if item.parser is not None
        }
    )


def _load_validated_registry(config_path: Path, *, project_root: Path) -> SourcesRegistrySchema:
    """Читает YAML-реестр, проверяет его строгой схемой, уникальность имён и локальные пути."""
    resolved_config = config_path if config_path.is_absolute() else (project_root / config_path)
    if not resolved_config.is_file():
        raise ValidationInputError(f"Файл реестра источников не найден: {resolved_config}")

    try:
        raw_content = resolved_config.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationInputError(f"Не удалось прочитать файл реестра источников '{resolved_config}': {exc}") from exc

    try:
        raw_payload: object = yaml.safe_load(raw_content)
    except yaml.YAMLError as exc:
        raise ValidationSchemaError(f"Некорректный YAML в реестре источников '{resolved_config}': {exc}") from exc

    if not isinstance(raw_payload, Mapping):
        raise ValidationSchemaError(
            f"Корневая структура реестра источников '{resolved_config}' должна быть отображением."
        )

    raw_sources = raw_payload.get("sources")
    if isinstance(raw_sources, list):
        for raw_item in raw_sources:
            if isinstance(raw_item, Mapping) and "source_type" in raw_item:
                raw_type = raw_item.get("source_type")
                if not isinstance(raw_type, str) or raw_type.strip().lower() not in _SUPPORTED_FEED_TYPE_VALUES:
                    raise UnsupportedSourceFormatError(
                        f"Неподдерживаемый тип источника '{raw_type}' в реестре '{resolved_config}'.",
                        format_name=str(raw_type),
                        source_name=str(raw_item.get("name", "")),
                    )

    try:
        validated_registry = SourcesRegistrySchema.model_validate(raw_payload)
    except ValidationError as exc:
        raise ValidationSchemaError(f"Ошибка валидации схемы реестра источников '{resolved_config}': {exc}") from exc

    seen_names: set[str] = set()
    for item in validated_registry.sources:
        if item.name in seen_names:
            raise ValidationSchemaError(f"Обнаружено дублирующееся имя источника в реестре: '{item.name}'.")
        seen_names.add(item.name)

        if not is_http_url(item.source_url):
            resolve_local_source_path(item.source_url, project_root=project_root)

    return validated_registry


def raise_for_http_status(response: httpx.Response, *, source_name: str, source_url: str) -> None:
    """Преобразует неуспешный HTTP-статус ответа в типизированное исключение приложения.

    Arguments:
        response: Ответ библиотеки `httpx`.
        source_name: Имя источника для диагностического сообщения.
        source_url: Запрошенный URL источника.

    Raises:
        AccessPermissionDeniedError: При ответе `401` или `403`.
        ServiceRateLimitExceededError: При ответе `429`.
        ServiceUnavailableError: При ответе `5xx` или неожиданном статусе перенаправления.
        ValidationInputError: При прочих ошибках клиента `4xx`.
    """
    status_code = response.status_code
    if 200 <= status_code < 300:
        return

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if status_code == 429:
            raise ServiceRateLimitExceededError(
                f"Превышен лимит запросов к источнику '{source_name}' ({source_url}): HTTP {status_code}."
            ) from exc
        if 500 <= status_code <= 599:
            raise ServiceUnavailableError(
                f"Внешний источник '{source_name}' ({source_url}) временно недоступен: HTTP {status_code}."
            ) from exc
        if status_code in (401, 403):
            raise AccessPermissionDeniedError(
                f"Отказано в доступе к источнику '{source_name}' ({source_url}): HTTP {status_code}."
            ) from exc
        if 400 <= status_code <= 499:
            raise ValidationInputError(
                f"Источник '{source_name}' ({source_url}) отклонил запрос: HTTP {status_code}."
            ) from exc
        raise ServiceUnavailableError(
            f"Неожиданный HTTP-статус {status_code} от источника '{source_name}' ({source_url})."
        ) from exc


def create_standalone_polite_client(
    *,
    user_agent: str = DEFAULT_SOURCE_USER_AGENT,
    http_client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PoliteHttpClient:
    """Создаёт HTTP-клиент для загрузчика, собранного без общего клиента сборщика.

    Такой клиент соблюдает `robots.txt` и `User-Agent`, но делает одну попытку без пауз.
    Паузы, повторы и общий лимит запросов к сайту даёт клиент, который корень композиции
    создаёт один на все загрузчики и проверку ссылок.

    Arguments:
        user_agent: Значение HTTP-заголовка `User-Agent`.
        http_client: Внешний `httpx.AsyncClient`.
        transport: Транспорт `httpx` для изолированного тестирования.

    Returns:
        Клиент с одной попыткой и без пауз между запросами.
    """
    return PoliteHttpClient(
        user_agent=user_agent,
        request_delay_seconds=0.0,
        maximum_attempts=1,
        http_client=http_client,
        transport=transport,
    )


async def fetch_source_bytes(
    *,
    source: SourceDefinition,
    http_client: PoliteHttpClient,
    project_root: Path = PROJECT_ROOT,
    local_feeds_directory: Path | None = None,
) -> bytes:
    """Загружает сырые байты источника по HTTP(S), `file://` или из локального файла.

    Сетевой запрос идёт через `PoliteHttpClient`: адрес, запрещённый `robots.txt`, не загружается.

    Arguments:
        source: Описание загружаемого источника.
        http_client: Вежливый HTTP-клиент сборщика.
        project_root: Корневая директория проекта.
        local_feeds_directory: Дополнительная директория локальных фидов.

    Returns:
        Необработанные байты содержимого источника.

    Raises:
        AccessPermissionDeniedError: При запрете `robots.txt`, ошибке прав доступа
            или выходе локального пути за пределы проекта.
        NetworkConnectionFailureError: При сетевом сбое соединения.
        NetworkConnectionTimeoutError: При превышении таймаута.
        ServiceRateLimitExceededError: При ответе HTTP 429.
        ServiceUnavailableError: При ответе HTTP 5xx.
        UnsupportedSourceFormatError: При неподдерживаемой схеме URL.
        ValidationInputError: При отсутствии локального файла, некорректном URL или статусе 4xx.
    """
    source_url = source.source_url.strip()
    if is_http_url(source_url):
        parsed = urlsplit(source_url)
        if not parsed.netloc:
            raise ValidationInputError(f"Некорректный HTTP-адрес источника '{source.name}': {source_url}")
        if not await http_client.is_allowed(source_url):
            raise AccessPermissionDeniedError(f"robots.txt запрещает обход источника '{source.name}' ({source_url}).")
        response = await http_client.request("GET", source_url, timeout_seconds=source.request_timeout_seconds)
        raise_for_http_status(response, source_name=source.name, source_url=source_url)
        return response.content

    resolved_path = resolve_local_source_path(
        source_url, project_root=project_root, local_feeds_directory=local_feeds_directory
    )

    def _read_local_file() -> bytes:
        if not resolved_path.is_file():
            raise ValidationInputError(f"Локальный файл источника '{source.name}' не найден: {resolved_path}")
        try:
            return resolved_path.read_bytes()
        except PermissionError as exc:
            raise AccessPermissionDeniedError(
                f"Нет прав на чтение файла источника '{source.name}': {resolved_path}"
            ) from exc
        except OSError as exc:
            raise ValidationInputError(
                f"Ошибка чтения локального файла источника '{source.name}' ({resolved_path}): {exc}"
            ) from exc

    return await asyncio.to_thread(_read_local_file)


def decode_utf8_content(raw_bytes: bytes, *, source_name: str) -> str:
    """Декодирует байты источника в строку UTF-8 (с поддержкой UTF-8 BOM).

    Arguments:
        raw_bytes: Сырые байты содержимого источника.
        source_name: Имя источника для диагностического сообщения.

    Returns:
        Декодированный текст.

    Raises:
        ValidationSchemaError: Если содержимое не удаётся декодировать как UTF-8.
    """
    try:
        return raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationSchemaError(f"Некорректная кодировка UTF-8 в источнике '{source_name}': {exc}") from exc


def resolve_item_source_url(raw_link: str | None, *, base_source_url: str, project_root: Path = PROJECT_ROOT) -> str:
    """Разрешает ссылку отдельного мероприятия относительно адреса или пути источника.

    Arguments:
        raw_link: Ссылка из карточки или записи фида.
        base_source_url: Базовый `source_url` источника.
        project_root: Корневая директория проекта для проверки локальных путей.

    Returns:
        Нормализованный URL или относительный путь карточки мероприятия.
    """
    cleaned_link = (raw_link or "").strip()
    cleaned_base = base_source_url.strip()
    if not cleaned_link:
        return cleaned_base

    if is_http_url(cleaned_link) or is_file_uri(cleaned_link):
        return cleaned_link

    if is_http_url(cleaned_base):
        return urljoin(cleaned_base, cleaned_link)

    if cleaned_link.startswith("#"):
        base_without_fragment = cleaned_base.split("#", 1)[0]
        return f"{base_without_fragment}{cleaned_link}"

    if Path(cleaned_link).is_absolute():
        return cleaned_link

    # Если путь уже задан относительно корня проекта (например data/local_feeds/...), сохраняем его.
    direct_candidate = (project_root / cleaned_link.split("#", 1)[0].split("?", 1)[0]).resolve()
    if direct_candidate.is_relative_to(project_root.resolve()) and direct_candidate.is_file():
        return cleaned_link

    if not is_file_uri(cleaned_base):
        base_path_part = cleaned_base.split("#", 1)[0].split("?", 1)[0]
        base_parent = Path(base_path_part).parent
        if str(base_parent) not in ("", "."):
            relative_to_base = base_parent / cleaned_link.split("#", 1)[0].split("?", 1)[0]
            resolved_relative = (project_root / relative_to_base).resolve()
            if resolved_relative.is_relative_to(project_root.resolve()) and resolved_relative.is_file():
                suffix = ""
                if "#" in cleaned_link:
                    suffix = f"#{cleaned_link.split('#', 1)[1]}"
                elif "?" in cleaned_link:
                    suffix = f"?{cleaned_link.split('?', 1)[1]}"
                return f"{relative_to_base.as_posix()}{suffix}"

    return cleaned_link


def extract_scalar_text(value: object) -> str | None:
    """Безопасно извлекает непустую строку из скалярного значения поля.

    Arguments:
        value: Исходное значение из словаря или атрибута разметки.

    Returns:
        Очищенная строка или `None`.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        cleaned = " ".join(value.split()).strip()
        return cleaned or None
    if isinstance(value, int | float):
        return str(value)
    return None


def extract_age_text(value: object) -> str | None:
    """Преобразует возрастное ограничение в строку формата, распознаваемого доменным парсером.

    Arguments:
        value: Числовое или строковое возрастное ограничение из источника.

    Returns:
        Строка вида `6+` / исходный текст либо `None`.
    """
    text = extract_scalar_text(value)
    if text is None:
        return None
    if text.isdigit():
        return f"{text}+"
    return text


def format_price_text_from_parts(
    *, raw_price: object = None, price_minimum: object = None, price_maximum: object = None, is_free: object = None
) -> str | None:
    """Формирует текстовую строку цены из произвольных числовых или булевых полей.

    Arguments:
        raw_price: Основное значение цены (`str`, `int` или `float`).
        price_minimum: Минимальная цена билета.
        price_maximum: Максимальная цена билета.
        is_free: Явный признак бесплатного входа.

    Returns:
        Текстовое представление стоимости для доменной нормализации либо `None`.
    """
    direct = extract_scalar_text(raw_price)
    if direct is not None:
        return direct
    if is_free is True:
        return "Бесплатно"

    min_text = extract_scalar_text(price_minimum)
    max_text = extract_scalar_text(price_maximum)
    if min_text is not None and max_text is not None:
        if min_text == "0" and max_text == "0":
            return "Бесплатно"
        return f"{min_text}-{max_text} ₽"
    if min_text is not None:
        return "Бесплатно" if min_text == "0" else f"от {min_text} ₽"
    if max_text is not None:
        return "Бесплатно" if max_text == "0" else f"до {max_text} ₽"
    return None


def normalize_status_marker(raw_status: object) -> str | None:
    """Приводит статус отмены мероприятия из различных форматов к единому маркеру `canceled`.

    Arguments:
        raw_status: Исходное значение статуса из XML, JSON или HTML.

    Returns:
        Строка `"canceled"`, исходный очищенный статус или `None`.
    """
    status_text = extract_scalar_text(raw_status)
    if status_text is None:
        return None
    lowered = status_text.lower().rstrip("/")
    tail = lowered.rsplit("/", 1)[-1]
    if lowered in _CANCELED_STATUS_VALUES or tail in _CANCELED_STATUS_VALUES:
        return "canceled"
    return status_text


def resolve_external_identifier(
    explicit_id: str | None, *, source_url: str, fallback_payload: Mapping[str, object]
) -> str:
    """Определяет детерминированный внешний идентификатор записи источника.

    Arguments:
        explicit_id: Явный идентификатор (`id`, `guid`, `external_id`) из источника.
        source_url: Разрешённый URL карточки мероприятия.
        fallback_payload: Исходный словарь полей для расчёта детерминированного хэша.

    Returns:
        Непустой строковый идентификатор записи.
    """
    if explicit_id:
        return explicit_id
    if source_url:
        return compute_payload_hash({"source_url": source_url, **dict(fallback_payload)})[:24]
    return compute_payload_hash(fallback_payload)[:24]


class CompositeSourceFetcher:
    """Маршрутизирующий загрузчик событий, объединяющий адаптеры RSS, JSON API и HTML."""

    def __init__(
        self,
        *,
        rss_fetcher: SourceFetcherPort | None = None,
        json_fetcher: SourceFetcherPort | None = None,
        html_fetcher: SourceFetcherPort | None = None,
        project_root: Path = PROJECT_ROOT,
        local_feeds_directory: Path | None = None,
        user_agent: str = DEFAULT_SOURCE_USER_AGENT,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
        polite_client: PoliteHttpClient | None = None,
        sources_config_path: Path | None = None,
    ) -> None:
        """Инициализирует составной загрузчик с готовыми или созданными по умолчанию адаптерами.

        Arguments:
            rss_fetcher: Пользовательский или тестовый загрузчик RSS/Atom.
            json_fetcher: Пользовательский или тестовый загрузчик JSON API.
            html_fetcher: Пользовательский или тестовый загрузчик HTML-страниц.
            project_root: Корневая директория проекта для локальных путей.
            local_feeds_directory: Директория локальных фидов.
            user_agent: Заголовок `User-Agent` для HTTP-запросов.
            transport: Опциональный транспорт `httpx` для тестов.
            http_client: Опциональный внешний `httpx.AsyncClient`.
            polite_client: Общий вежливый HTTP-клиент сборщика; закрывается его владельцем, а не фасадом.
            sources_config_path: Реестр источников для выбора именных парсеров сайтов.
        """
        self._owned_closers: list[Callable[[], Awaitable[None]]] = []

        if rss_fetcher is None:
            from src.infrastructure.sources.rss_fetcher import RssSourceFetcher

            default_rss = RssSourceFetcher(
                http_client=http_client,
                transport=transport,
                user_agent=user_agent,
                project_root=project_root,
                local_feeds_directory=local_feeds_directory,
                polite_client=polite_client,
            )
            self._owned_closers.append(default_rss.close)
            rss_fetcher = default_rss

        if json_fetcher is None:
            from src.infrastructure.sources.json_fetcher import JsonSourceFetcher

            default_json = JsonSourceFetcher(
                http_client=http_client,
                transport=transport,
                user_agent=user_agent,
                project_root=project_root,
                local_feeds_directory=local_feeds_directory,
                polite_client=polite_client,
            )
            self._owned_closers.append(default_json.close)
            json_fetcher = default_json

        if html_fetcher is None:
            from src.infrastructure.sources.html_fetcher import HtmlSourceFetcher

            default_html = HtmlSourceFetcher(
                http_client=http_client,
                transport=transport,
                user_agent=user_agent,
                project_root=project_root,
                local_feeds_directory=local_feeds_directory,
                polite_client=polite_client,
                sources_config_path=sources_config_path,
            )
            self._owned_closers.append(default_html.close)
            html_fetcher = default_html

        self._fetchers: dict[SourceType, SourceFetcherPort] = {
            SourceType.RSS: rss_fetcher,
            SourceType.API: json_fetcher,
            SourceType.WEBPAGE: html_fetcher,
        }

    async def fetch_items(self, *, source: SourceDefinition) -> tuple[FetchedSourceItem, ...]:
        """Маршрутизирует загрузку мероприятий к адаптеру согласно `source.source_type`.

        Arguments:
            source: Декларативное описание источника из реестра конфигурации.

        Returns:
            Кортеж сырых элементов мероприятий для конвейера нормализации.

        Raises:
            UnsupportedSourceFormatError: Если тип источника не поддерживается.
        """
        fetcher = self._fetchers.get(source.source_type)
        if fetcher is None:
            raise UnsupportedSourceFormatError(
                f"Тип источника '{source.source_type}' не поддерживается составным загрузчиком.",
                format_name=source.source_type.value,
                source_name=source.name,
            )
        return await fetcher.fetch_items(source=source)

    async def close(self) -> None:
        """Закрывает внутренние HTTP-клиенты всех созданных этим фасадом адаптеров."""
        for close_callback in self._owned_closers:
            await close_callback()
        self._owned_closers.clear()
