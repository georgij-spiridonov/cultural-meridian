"""Неизменяемые доменные сущности каталога мероприятий, источников, поиска и импорта."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import StrEnum
from typing import Literal
from uuid import UUID

from src.core.types import EventCategory, EventStatus, SourceType
from src.domain.entities._helpers import (
    EMPTY_OBJECT_MAPPING,
    freeze_mapping,
    restore_dataclass_state,
    serialize_dataclass_state,
)
from src.domain.entities.planning import Coordinates

__all__ = [
    "CityReference",
    "EmptySearchExplanation",
    "Event",
    "EventSearchFilter",
    "EventSearchPage",
    "EventSortField",
    "EventSourceReference",
    "ExtractedEventFields",
    "FetchedSourceItem",
    "ImportReport",
    "ImportRowFailure",
    "IngestionReport",
    "LinkCheckResult",
    "RawEventRecord",
    "SortDirection",
    "SourceDefinition",
    "VenueReference",
]


class EventSortField(StrEnum):
    """Поле, по которому выполняется сортировка выдачи мероприятий.

    Attributes:
        DATE_START: Дата и время начала мероприятия.
        PRICE: Минимальная стоимость посещения (бесплатные события имеют цену 0).
        RELEVANCE: Релевантность текстовому поисковому запросу.
        TITLE: Алфавитный порядок по названию мероприятия.
        DURATION: Длительность мероприятия в минутах.
        AGE_LIMIT: Возрастное ограничение.
        CREATED_AT: Дата добавления карточки в каталог.
    """

    DATE_START = "date_start"
    PRICE = "price"
    RELEVANCE = "relevance"
    TITLE = "title"
    DURATION = "duration"
    AGE_LIMIT = "age_limit"
    CREATED_AT = "created_at"


class SortDirection(StrEnum):
    """Направление сортировки результатов поиска.

    Attributes:
        ASCENDING: По возрастанию значений.
        DESCENDING: По убыванию значений.
    """

    ASCENDING = "ascending"
    DESCENDING = "descending"


@dataclass(frozen=True, slots=True, kw_only=True)
class EventSourceReference:
    """Сведения об источнике, подтвердившем карточку мероприятия.

    Используется при дедупликации для отображения всех источников на одной карточке.

    Attributes:
        source_name: Человекочитаемое название источника.
        source_type: Тип источника (`manual`, `import_file`, `rss`, `api`, `webpage`).
        source_url: Ссылка на конкретную публикацию у источника.
        source_hash: Контрольная сумма исходной записи для идемпотентности.
        verified_at: Временная метка последней успешной проверки доступности ссылки.
    """

    source_name: str
    source_type: SourceType
    source_url: str
    source_hash: str
    verified_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """Нормализованная карточка культурного мероприятия Республики Башкортостан.

    Полностью покрывает обязательные и рекомендуемые поля схемы из раздела 8.1 ТЗ.

    Attributes:
        identifier: Уникальный идентификатор мероприятия.
        title: Название мероприятия в формулировке первоисточника.
        normalized_title: Нормализованное название для поиска и дедупликации.
        category: Категория мероприятия.
        tags: Неизменяемый кортеж тематических меток (`джаз`, `дети`, `бесплатно` и др.).
        date_start: Дата и время начала мероприятия.
        date_end: Дата и время окончания мероприятия, если известны.
        duration_minutes: Длительность мероприятия в минутах, если известна.
        city: Город или населённый пункт проведения.
        venue: Название площадки проведения.
        address: Адрес площадки проведения.
        latitude: Географическая широта площадки, если известна.
        longitude: Географическая долгота площадки, если известна.
        description: Краткое описание мероприятия.
        price_minimum: Минимальная стоимость билета в рублях (`0` — бесплатно).
        price_maximum: Максимальная стоимость билета в рублях (`0` — бесплатно).
        is_free: Признак бесплатного входа.
        age_limit: Возрастное ограничение (`0`, `6`, `12`, `16`, `18`).
        language: Язык проведения мероприятия, если указан.
        organization_link: Ссылка на страницу организатора или покупку билетов.
        source_url: Основная ссылка на источник сведений.
        source_type: Тип основного источника.
        source_hash: Хэш исходной записи для идемпотентного обновления.
        verified_at: Время последней проверки актуальности источника.
        confidence: Оценка уверенности извлечения и нормализации в диапазоне `0.0..1.0`.
        status: Статус публикации (`published`, `draft`, `canceled`).
        deduplication_key: Детерминированный ключ дедупликации.
        created_at: Временная метка создания записи.
        updated_at: Временная метка последнего обновления записи.
        sources: Кортеж всех подтверждённых источников после дедупликации.
    """

    identifier: UUID
    title: str
    normalized_title: str
    category: EventCategory
    tags: tuple[str, ...] = ()
    date_start: datetime
    date_end: datetime | None = None
    duration_minutes: int | None = None
    city: str
    venue: str
    address: str
    latitude: float | None = None
    longitude: float | None = None
    description: str = ""
    price_minimum: int | None = None
    price_maximum: int | None = None
    is_free: bool = False
    age_limit: int = 0
    language: str | None = None
    organization_link: str | None = None
    source_url: str
    source_type: SourceType
    source_hash: str
    verified_at: datetime | None = None
    confidence: float = 1.0
    status: EventStatus = EventStatus.PUBLISHED
    deduplication_key: str
    created_at: datetime
    updated_at: datetime
    sources: tuple[EventSourceReference, ...] = ()

    def __post_init__(self) -> None:
        """Преобразует кортежи меток и источников в неизменяемые структуры."""
        object.__setattr__(self, "tags", tuple(self.tags))
        object.__setattr__(self, "sources", tuple(self.sources))


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceDefinition:
    """Декларативное описание внешнего или локального источника афиш.

    Attributes:
        name: Уникальное имя источника в реестре конфигурации.
        source_type: Тип протокола источника (`rss`, `api`, `webpage`).
        source_url: URL или путь к локальному фиду источника.
        default_city: Город по умолчанию, если он не указан в отдельной записи фида.
        default_category: Категория по умолчанию для специализированных площадок.
        default_venue: Площадка по умолчанию для мероприятий в городе источника, как она названа в справочнике.
        is_enabled: Признак активности источника при плановом сборе.
        request_timeout_seconds: Максимальное время ожидания сетевого ответа в секундах.
    """

    name: str
    source_type: SourceType
    source_url: str
    default_city: str | None = None
    default_category: EventCategory | None = None
    default_venue: str | None = None
    is_enabled: bool = True
    request_timeout_seconds: float = 5.0


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchedSourceItem:
    """Необработанная карточка мероприятия, полученная загрузчиком из источника.

    Attributes:
        source_name: Имя источника из конфигурации.
        source_type: Тип источника.
        external_identifier: Внешний идентификатор записи у источника.
        source_url: Прямая ссылка на страницу или запись мероприятия.
        raw_title: Исходный заголовок события.
        raw_description: Исходный текст описания.
        raw_date_text: Исходная строка даты и времени.
        raw_price_text: Исходная строка стоимости.
        raw_city: Исходное название города.
        raw_venue: Исходное название площадки.
        raw_address: Исходный адрес площадки.
        raw_category: Исходная категория у источника.
        raw_age_text: Исходное возрастное ограничение текстом.
        raw_language: Язык мероприятия, если указан в источнике.
        organization_link: Ссылка на организатора или билетную страницу.
        raw_payload: Неизменяемая копия исходного словаря полей.
    """

    source_name: str
    source_type: SourceType
    external_identifier: str
    source_url: str
    raw_title: str
    raw_description: str | None = None
    raw_date_text: str | None = None
    raw_price_text: str | None = None
    raw_city: str | None = None
    raw_venue: str | None = None
    raw_address: str | None = None
    raw_category: str | None = None
    raw_age_text: str | None = None
    raw_language: str | None = None
    organization_link: str | None = None
    raw_payload: Mapping[str, object] = field(default=EMPTY_OBJECT_MAPPING, hash=False)

    def __post_init__(self) -> None:
        """Обеспечивает неизменяемость словаря полей и нормализует пустые строки в `None`."""
        object.__setattr__(self, "raw_payload", freeze_mapping(self.raw_payload))
        for field_name in (
            "raw_description",
            "raw_date_text",
            "raw_price_text",
            "raw_city",
            "raw_venue",
            "raw_address",
            "raw_category",
            "raw_age_text",
            "raw_language",
            "organization_link",
        ):
            value: object = getattr(self, field_name)
            if isinstance(value, str) and not value.strip():
                object.__setattr__(self, field_name, None)

    def __getstate__(self) -> dict[str, object]:
        """Подготавливает состояние объекта для сериализации."""
        return serialize_dataclass_state(self)

    def __setstate__(self, state: Mapping[str, object]) -> None:
        """Восстанавливает неизменяемое состояние объекта после десериализации."""
        restore_dataclass_state(self, state, self.__post_init__)


@dataclass(frozen=True, slots=True, kw_only=True)
class RawEventRecord:
    """Запись сырых данных источника в хранилище для повторной нормализации.

    Attributes:
        identifier: Внутренний идентификатор сырой записи.
        source_name: Имя источника.
        source_type: Тип источника.
        external_identifier: Внешний идентификатор у источника.
        source_url: Ссылка на первоисточник.
        source_hash: Хэш исходного содержимого для контроля идемпотентности.
        raw_payload: Неизменяемое представление исходного содержимого источника.
        fetched_at: Временная метка получения записи.
        normalization_status: Текущий статус обработки (`pending`, `normalized`, `failed`).
        error_message: Причина ошибки нормализации, если она возникла.
    """

    identifier: UUID
    source_name: str
    source_type: SourceType
    external_identifier: str
    source_url: str
    source_hash: str
    raw_payload: Mapping[str, object] = field(default=EMPTY_OBJECT_MAPPING, hash=False)
    fetched_at: datetime
    normalization_status: Literal["pending", "normalized", "failed"] = "pending"
    error_message: str | None = None

    def __post_init__(self) -> None:
        """Обеспечивает глубокую неизменяемость сырого содержимого."""
        object.__setattr__(self, "raw_payload", freeze_mapping(self.raw_payload))

    def __getstate__(self) -> dict[str, object]:
        """Подготавливает состояние объекта для сериализации."""
        return serialize_dataclass_state(self)

    def __setstate__(self, state: Mapping[str, object]) -> None:
        """Восстанавливает неизменяемое состояние объекта после десериализации."""
        restore_dataclass_state(self, state, self.__post_init__)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractedEventFields:
    """Результат детерминированной или гибридной нормализации полей мероприятия.

    Attributes:
        title: Очищенный заголовок мероприятия.
        category: Распознанная категория мероприятия.
        date_start: Распознанные дата и время начала.
        date_end: Распознанные дата и время окончания, если определены.
        duration_minutes: Длительность мероприятия в минутах.
        city: Нормализованное название города из справочника.
        venue: Нормализованное название площадки.
        address: Уточнённый адрес площадки.
        price_minimum: Минимальная цена в рублях.
        price_maximum: Максимальная цена в рублях.
        is_free: Признак бесплатного входа.
        age_limit: Возрастное ограничение (`0`, `6`, `12`, `16`, `18`).
        language: Язык проведения мероприятия, если определён.
        tags: Извлечённые тематические метки.
        confidence: Итоговая уверенность извлечения в диапазоне `0.0..1.0`.
    """

    title: str | None = None
    category: EventCategory | None = None
    date_start: datetime | None = None
    date_end: datetime | None = None
    duration_minutes: int | None = None
    city: str | None = None
    venue: str | None = None
    address: str | None = None
    price_minimum: int | None = None
    price_maximum: int | None = None
    is_free: bool | None = None
    age_limit: int | None = None
    language: str | None = None
    tags: tuple[str, ...] = ()
    confidence: float = 0.0

    def __post_init__(self) -> None:
        """Фиксирует кортеж меток."""
        object.__setattr__(self, "tags", tuple(self.tags))


@dataclass(frozen=True, slots=True, kw_only=True)
class LinkCheckResult:
    """Результат проверки доступности внешней ссылки на источник события.

    Attributes:
        target_url: Проверяемый адрес источника.
        is_reachable: Признак успешного ответа сервера (`2xx` или `3xx`).
        http_status_code: Полученный HTTP-код ответа, если соединение установлено.
        final_url: Итоговый адрес после перенаправлений.
        error_reason: Описание ошибки при недоступности ресурса.
        checked_at: Временная метка выполнения проверки.
    """

    target_url: str
    is_reachable: bool
    http_status_code: int | None = None
    final_url: str | None = None
    error_reason: str | None = None
    checked_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class CityReference:
    """Элемент справочника городов и населённых пунктов Республики Башкортостан.

    Attributes:
        name: Каноническое название города.
        normalized_name: Название в нижнем регистре для сопоставления.
        aliases: Допустимые словоформы и распространённые опечатки названия.
        center_coordinates: Координаты центра города для оценки близости.
    """

    name: str
    normalized_name: str
    aliases: tuple[str, ...] = ()
    center_coordinates: Coordinates | None = None

    def __post_init__(self) -> None:
        """Фиксирует кортеж синонимов города."""
        object.__setattr__(self, "aliases", tuple(self.aliases))


@dataclass(frozen=True, slots=True, kw_only=True)
class VenueReference:
    """Элемент справочника площадок проведения культурных мероприятий.

    Attributes:
        name: Каноническое название площадки.
        normalized_name: Нормализованное название для поиска и дедупликации.
        city: Город расположения площадки.
        address: Основной адрес площадки.
        aliases: Альтернативные написания и сокращения названия площадки.
        coordinates: Координаты площадки для расчёта времени перемещения.
    """

    name: str
    normalized_name: str
    city: str
    address: str
    aliases: tuple[str, ...] = ()
    coordinates: Coordinates | None = None

    def __post_init__(self) -> None:
        """Фиксирует кортеж альтернативных названий площадки."""
        object.__setattr__(self, "aliases", tuple(self.aliases))


@dataclass(frozen=True, slots=True, kw_only=True)
class EventSearchFilter:
    """Параметры поиска и фильтрации мероприятий в каталоге.

    Attributes:
        query: Свободная текстовая строка поиска (название, описание, теги).
        city: Фильтр по городу или населённому пункту.
        category: Фильтр по категории мероприятия.
        date_from: Нижняя граница даты и времени начала события.
        date_to: Верхняя граница даты и времени начала события.
        time_from: Минимальное время начала в течение дня.
        time_to: Максимальное время окончания или начала в течение дня.
        maximum_price: Максимальная допустимая цена билета в рублях.
        only_free: Показывать только бесплатные мероприятия.
        maximum_age_limit: Максимально допустимый возрастной ценз (подходит ребёнку указанного возраста).
        maximum_duration_minutes: Максимальная длительность мероприятия в минутах.
        language: Фильтр по языку проведения мероприятия.
        near_center: Приоритет или фильтрация по близости к центру города.
        venue: Фильтр по конкретной площадке.
        sort_field: Поле сортировки выдачи (`EventSortField`).
        sort_direction: Направление сортировки (`SortDirection`).
        page: Номер страницы выдачи (начиная с 1).
        page_size: Количество карточек на странице.
    """

    query: str | None = None
    city: str | None = None
    category: EventCategory | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    time_from: time | None = None
    time_to: time | None = None
    maximum_price: int | None = None
    only_free: bool = False
    maximum_age_limit: int | None = None
    maximum_duration_minutes: int | None = None
    language: str | None = None
    near_center: bool = False
    venue: str | None = None
    sort_field: EventSortField = EventSortField.DATE_START
    sort_direction: SortDirection = SortDirection.ASCENDING
    page: int = 1
    page_size: int = 5


@dataclass(frozen=True, slots=True, kw_only=True)
class EmptySearchExplanation:
    """Пояснение пустой выдачи с указанием мешающего ограничения.

    Attributes:
        summary: Краткое человекочитаемое сообщение о том, почему ничего не найдено.
        blocking_constraints: Список ограничений, отсеявших доступные события.
        suggested_relaxations: Конкретные варианты ослабления фильтров для получения выдачи.
    """

    summary: str
    blocking_constraints: tuple[str, ...] = ()
    suggested_relaxations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Фиксирует кортежи ограничений и предложений."""
        object.__setattr__(self, "blocking_constraints", tuple(self.blocking_constraints))
        object.__setattr__(self, "suggested_relaxations", tuple(self.suggested_relaxations))


@dataclass(frozen=True, slots=True, kw_only=True)
class EventSearchPage:
    """Страница результатов поиска мероприятий.

    Attributes:
        items: Кортеж найденных карточек мероприятий на текущей странице.
        total_count: Общее количество подходящих мероприятий по фильтру.
        page: Номер текущей страницы.
        page_size: Размер страницы.
        has_more: Признак наличия следующей страницы выдачи.
        empty_explanation: Объяснение пустого результата, если `items` пуст.
    """

    items: tuple[Event, ...]
    total_count: int
    page: int
    page_size: int
    has_more: bool
    empty_explanation: EmptySearchExplanation | None = None

    def __post_init__(self) -> None:
        """Фиксирует кортеж карточек мероприятий."""
        object.__setattr__(self, "items", tuple(self.items))


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportRowFailure:
    """Описание отклонённой строки при импорте файла CSV или JSON.

    Attributes:
        row_number: Порядковый номер строки или элемента в файле (начиная с 1).
        external_identifier: Внешний идентификатор или заголовок проблемной записи.
        reason: Понятная причина отклонения записи при валидации.
    """

    row_number: int
    external_identifier: str | None = None
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportReport:
    """Итоговый отчёт об идемпотентном импорте файла мероприятий.

    Attributes:
        rows_total: Общее количество обработанных записей во входном файле.
        rows_added: Количество впервые добавленных мероприятий.
        rows_updated: Количество обновлённых мероприятий при изменении данных.
        rows_skipped_unchanged: Количество пропущенных записей без изменений (повторная загрузка).
        rows_rejected: Количество отклонённых записей из-за ошибок валидации.
        failures: Подробный список отклонённых записей и причин.
    """

    rows_total: int
    rows_added: int
    rows_updated: int
    rows_skipped_unchanged: int
    rows_rejected: int
    failures: tuple[ImportRowFailure, ...] = ()

    def __post_init__(self) -> None:
        """Фиксирует кортеж описаний ошибок импорта."""
        object.__setattr__(self, "failures", tuple(self.failures))


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestionReport:
    """Сводный отчёт фонового прогона сбора и проверки внешних источников.

    Attributes:
        sources_processed: Количество успешно опрошенных источников.
        sources_failed: Количество источников, завершившихся ошибкой сети или разбора.
        items_fetched: Общее число полученных из источников записей.
        items_published: Количество карточек, прошедших проверку и опубликованных в выдаче.
        items_sent_to_moderation: Количество карточек, направленных в очередь модерации.
        items_deduplicated: Количество записей, объединённых с существующими карточками.
        items_unchanged: Количество записей, пропущенных как уже актуальные по `source_hash`.
        errors: Список диагностических сообщений по сбойным источникам.
    """

    sources_processed: int
    sources_failed: int
    items_fetched: int
    items_published: int
    items_sent_to_moderation: int
    items_deduplicated: int
    items_unchanged: int
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Фиксирует кортеж диагностических ошибок."""
        object.__setattr__(self, "errors", tuple(self.errors))
