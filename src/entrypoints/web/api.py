"""Версионированный REST API `/api/v1`, OpenAPI 3.1 схемы и единый обработчик ошибок."""

from datetime import date, datetime, time
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from src.application import EventSearchService, SubscriptionService, WeekendPlannerService
from src.core.exceptions import (
    ApplicationException,
    InfrastructureException,
    LifecycleException,
    ServiceAvailabilityException,
    ValidationException,
)
from src.core.logger import get_logger
from src.core.types import DialogChannel, EventCategory, EventStatus, SourceType
from src.domain.entities import (
    CityReference,
    ConstraintRelaxationSuggestion,
    EmptySearchExplanation,
    Event,
    EventSearchFilter,
    EventSearchPage,
    EventSortField,
    EventSourceReference,
    FeedbackRequestRecord,
    FeedbackStatus,
    SortDirection,
    SubscriptionRecord,
    VenueReference,
    WeekendPlanRequest,
    WeekendPlanResult,
    WeekendPlanStep,
)
from src.domain.exceptions import (
    DuplicateEventConflictError,
    EventNotFoundError,
    InvalidEventDataError,
    InvalidSearchFilterError,
    InvalidWeekendPlanRequestError,
    ModerationItemNotFoundError,
    PromptInjectionDetectedError,
    SubscriptionNotFoundError,
    UnsupportedSourceFormatError,
)
from src.domain.services import normalize_optional_text
from src.entrypoints.bootstrap import ApplicationContainer
from src.entrypoints.web._formatting import (
    CATEGORY_LABELS,
    build_entity_etag,
    compute_period_range,
    parse_date_boundary,
    parse_time_value,
)
from src.entrypoints.web.middleware import InMemoryRateLimiter, RequestMetricsCollector

__all__ = [
    "CityReferenceSchema",
    "ConstraintRelaxationSchema",
    "EmptySearchExplanationSchema",
    "ErrorResponse",
    "EventDetailSchema",
    "EventSearchPageSchema",
    "EventSourceSchema",
    "EventSummarySchema",
    "FeedbackCreateRequest",
    "FeedbackSchema",
    "HealthComponentSchema",
    "HealthResponse",
    "MetricsResponse",
    "SubscriptionCreateRequest",
    "SubscriptionDeletedSchema",
    "SubscriptionSchema",
    "VenueReferenceSchema",
    "WeekendPlanApiRequest",
    "WeekendPlanApiResponse",
    "WeekendPlanStepSchema",
    "create_api_router",
    "register_exception_handlers",
    "register_observability_routes",
]

logger = get_logger(__name__)


class ErrorResponse(BaseModel):
    """Единый формат JSON-ответа об ошибке для всех конечных точек API."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "example": {
                "code": "event_not_found",
                "message": "Мероприятие не найдено в каталоге.",
                "details": {"event_identifier": "66c51c98-97f9-473c-8b42-9382f84a83de"},
                "request_id": "a4f9d8c1e2b34f10",
            }
        },
    )

    code: str
    message: str
    details: dict[str, Any] | list[Any] | None = None
    request_id: str | None = None


class EventSourceSchema(BaseModel):
    """Подтверждённый источник публикации карточки мероприятия."""

    model_config = ConfigDict(frozen=True)

    source_name: str
    source_type: SourceType
    source_url: str
    verified_at: datetime | None = None

    @classmethod
    def from_domain(cls, source_reference: EventSourceReference) -> EventSourceSchema:
        """Преобразует доменную ссылку на источник в API-схему."""
        return cls(
            source_name=source_reference.source_name,
            source_type=source_reference.source_type,
            source_url=source_reference.source_url,
            verified_at=source_reference.verified_at,
        )


class EventSummarySchema(BaseModel):
    """Краткая карточка мероприятия в поисковой выдаче."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "example": {
                "identifier": "66c51c98-97f9-473c-8b42-9382f84a83de",
                "title": "Щелкунчик",
                "category": "spectacle",
                "category_label": "Спектакли",
                "tags": ["балет", "классика", "дети"],
                "date_start": "2026-11-15T18:00:00+05:00",
                "date_end": "2026-11-15T20:10:00+05:00",
                "duration_minutes": 130,
                "city": "Уфа",
                "venue": "Башкирский государственный театр оперы и балета",
                "address": "ул. Ленина, 5/1",
                "price_minimum": 500,
                "price_maximum": 1800,
                "is_free": False,
                "age_limit": 6,
                "language": "русский",
                "status": "published",
                "source_url": "https://example.org/events/nutcracker",
                "organization_link": "https://bashopera.ru",
                "sources_count": 2,
            }
        },
    )

    identifier: UUID
    title: str
    category: EventCategory
    category_label: str
    tags: list[str]
    date_start: datetime
    date_end: datetime | None = None
    duration_minutes: int | None = None
    city: str
    venue: str
    address: str
    price_minimum: int | None = None
    price_maximum: int | None = None
    is_free: bool
    age_limit: int
    language: str | None = None
    status: EventStatus
    source_url: str
    organization_link: str | None = None
    sources_count: int

    @classmethod
    def from_domain(cls, event: Event) -> EventSummarySchema:
        """Преобразует доменную карточку в краткое представление выдачи."""
        return cls(
            identifier=event.identifier,
            title=event.title,
            category=event.category,
            category_label=CATEGORY_LABELS.get(event.category, event.category.value),
            tags=list(event.tags),
            date_start=event.date_start,
            date_end=event.date_end,
            duration_minutes=event.duration_minutes,
            city=event.city,
            venue=event.venue,
            address=event.address,
            price_minimum=event.price_minimum,
            price_maximum=event.price_maximum,
            is_free=event.is_free,
            age_limit=event.age_limit,
            language=event.language,
            status=event.status,
            source_url=event.source_url,
            organization_link=event.organization_link,
            sources_count=max(1, len(event.sources)),
        )


class EventDetailSchema(EventSummarySchema):
    """Полная карточка мероприятия со всеми подтверждёнными источниками и координатами."""

    model_config = ConfigDict(frozen=True)

    description: str
    latitude: float | None = None
    longitude: float | None = None
    source_type: SourceType
    verified_at: datetime | None = None
    confidence: float
    created_at: datetime
    updated_at: datetime
    sources: list[EventSourceSchema]

    @classmethod
    def from_domain_detail(cls, event: Event) -> EventDetailSchema:
        """Преобразует доменную сущность `Event` в детальную схему карточки."""
        summary = EventSummarySchema.from_domain(event)
        return cls(
            **summary.model_dump(),
            description=event.description,
            latitude=event.latitude,
            longitude=event.longitude,
            source_type=event.source_type,
            verified_at=event.verified_at,
            confidence=event.confidence,
            created_at=event.created_at,
            updated_at=event.updated_at,
            sources=[EventSourceSchema.from_domain(item) for item in event.sources],
        )


class EmptySearchExplanationSchema(BaseModel):
    """Диагностика пустой выдачи с указанием мешающих ограничений и подсказок ослабления."""

    model_config = ConfigDict(frozen=True)

    summary: str
    blocking_constraints: list[str]
    suggested_relaxations: list[str]

    @classmethod
    def from_domain(cls, explanation: EmptySearchExplanation) -> EmptySearchExplanationSchema:
        """Преобразует доменное пояснение пустого результата в API-схему."""
        return cls(
            summary=explanation.summary,
            blocking_constraints=list(explanation.blocking_constraints),
            suggested_relaxations=list(explanation.suggested_relaxations),
        )


class EventSearchPageSchema(BaseModel):
    """Страница результатов поиска мероприятий в каталоге."""

    model_config = ConfigDict(frozen=True)

    items: list[EventSummarySchema]
    total_count: int
    page: int
    page_size: int
    has_more: bool
    empty_explanation: EmptySearchExplanationSchema | None = None

    @classmethod
    def from_domain(cls, search_page: EventSearchPage) -> EventSearchPageSchema:
        """Преобразует доменную страницу поиска в схему REST API."""
        return cls(
            items=[EventSummarySchema.from_domain(item) for item in search_page.items],
            total_count=search_page.total_count,
            page=search_page.page,
            page_size=search_page.page_size,
            has_more=search_page.has_more,
            empty_explanation=(
                EmptySearchExplanationSchema.from_domain(search_page.empty_explanation)
                if search_page.empty_explanation is not None
                else None
            ),
        )


class CityReferenceSchema(BaseModel):
    """Элемент справочника городов Республики Башкортостан."""

    model_config = ConfigDict(frozen=True)

    name: str
    normalized_name: str
    aliases: list[str]
    center_latitude: float | None = None
    center_longitude: float | None = None

    @classmethod
    def from_domain(cls, city: CityReference) -> CityReferenceSchema:
        """Преобразует запись справочника городов в схему API."""
        return cls(
            name=city.name,
            normalized_name=city.normalized_name,
            aliases=list(city.aliases),
            center_latitude=city.center_coordinates.latitude if city.center_coordinates else None,
            center_longitude=city.center_coordinates.longitude if city.center_coordinates else None,
        )


class VenueReferenceSchema(BaseModel):
    """Элемент справочника площадок проведения культурных мероприятий."""

    model_config = ConfigDict(frozen=True)

    name: str
    normalized_name: str
    city: str
    address: str
    aliases: list[str]
    latitude: float | None = None
    longitude: float | None = None

    @classmethod
    def from_domain(cls, venue: VenueReference) -> VenueReferenceSchema:
        """Преобразует запись справочника площадок в схему API."""
        return cls(
            name=venue.name,
            normalized_name=venue.normalized_name,
            city=venue.city,
            address=venue.address,
            aliases=list(venue.aliases),
            latitude=venue.coordinates.latitude if venue.coordinates else None,
            longitude=venue.coordinates.longitude if venue.coordinates else None,
        )


class CategoryReferenceSchema(BaseModel):
    """Элемент справочника категорий культурных мероприятий."""

    model_config = ConfigDict(frozen=True)

    code: EventCategory
    label: str


class SubscriptionCreateRequest(BaseModel):
    """Запрос на оформление минималистичной подписки без персональных контактов."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={
            "example": {
                "channel": "web",
                "subscriber_identifier": "web-visitor-demo-001",
                "city": "Уфа",
                "category": "concert",
            }
        },
    )

    channel: DialogChannel = DialogChannel.WEB
    subscriber_identifier: str = Field(min_length=1, max_length=128)
    city: str | None = Field(default=None, max_length=120)
    category: EventCategory | None = None


class SubscriptionSchema(BaseModel):
    """Запись активной подписки пользователя на обновления афиши."""

    model_config = ConfigDict(frozen=True)

    subscription_identifier: UUID
    channel: DialogChannel
    subscriber_identifier: str
    city: str | None = None
    category: EventCategory | None = None
    category_label: str | None = None
    created_at: datetime

    @classmethod
    def from_domain(cls, subscription: SubscriptionRecord) -> SubscriptionSchema:
        """Преобразует доменную подписку в API-схему."""
        return cls(
            subscription_identifier=subscription.subscription_identifier,
            channel=subscription.channel,
            subscriber_identifier=subscription.subscriber_identifier,
            city=subscription.city,
            category=subscription.category,
            category_label=(
                CATEGORY_LABELS.get(subscription.category, subscription.category.value)
                if subscription.category is not None
                else None
            ),
            created_at=subscription.created_at,
        )


class SubscriptionDeletedSchema(BaseModel):
    """Подтверждение физического удаления одной или нескольких подписок."""

    model_config = ConfigDict(frozen=True)

    deleted_count: int


class FeedbackCreateRequest(BaseModel):
    """Заявка организатора или уточнение по карточке события с антиспам-полем."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={
            "example": {
                "channel": "web",
                "event_identifier": None,
                "message_text": "Просим добавить уточнение о входе со стороны главного фасада.",
                "contact_hint": "@organizer_rb",
                "honeypot_website": "",
            }
        },
    )

    channel: DialogChannel = DialogChannel.WEB
    event_identifier: UUID | None = None
    message_text: str = Field(min_length=1, max_length=2000)
    contact_hint: str | None = Field(default=None, max_length=255)
    honeypot_website: str = Field(default="", max_length=255)


class FeedbackSchema(BaseModel):
    """Зарегистрированное обращение организатора или пользователя."""

    model_config = ConfigDict(frozen=True)

    feedback_identifier: UUID
    channel: DialogChannel
    event_identifier: UUID | None = None
    message_text: str
    contact_hint: str | None = None
    created_at: datetime
    status: FeedbackStatus

    @classmethod
    def from_domain(cls, record: FeedbackRequestRecord) -> FeedbackSchema:
        """Преобразует доменную запись обращения в схему API."""
        return cls(
            feedback_identifier=record.feedback_identifier,
            channel=record.channel,
            event_identifier=record.event_identifier,
            message_text=record.message_text,
            contact_hint=record.contact_hint,
            created_at=record.created_at,
            status=record.status,
        )


class HealthComponentSchema(BaseModel):
    """Состояние отдельного компонента сервиса при проверке готовности."""

    model_config = ConfigDict(frozen=True)

    status: str
    detail: str | None = None


class HealthResponse(BaseModel):
    """Ответ конечной точки наблюдаемости `/health`."""

    model_config = ConfigDict(frozen=True)

    status: str
    service: str
    environment: str
    components: dict[str, HealthComponentSchema]


class MetricsResponse(BaseModel):
    """Базовые эксплуатационные метрики `/metrics` (запросы, ошибки, задержка)."""

    model_config = ConfigDict(frozen=True)

    requests_total: int
    errors_4xx: int
    errors_5xx: int
    average_latency_milliseconds: float
    p95_latency_milliseconds: float


class WeekendPlanApiRequest(BaseModel):
    """Запрос на подбор непересекающегося маршрута выходного дня."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={
            "example": {
                "city": "Уфа",
                "target_date": "2026-11-21",
                "window_start_time": "10:00:00",
                "window_end_time": "21:00:00",
                "budget_rubles": 2000,
                "companions_count": 2,
                "child_age": 6,
                "maximum_travel_minutes": 25,
            }
        },
    )

    city: str = Field(
        min_length=1,
        max_length=120,
        description="Город или населённый пункт Республики Башкортостан (допускает словоформы).",
    )
    target_date: date = Field(description="Целевая дата посещения мероприятий (YYYY-MM-DD).")
    window_start_time: time = Field(description="Время начала свободного окна (HH:MM или HH:MM:SS).")
    window_end_time: time = Field(description="Время окончания свободного окна (HH:MM или HH:MM:SS).")
    budget_rubles: int = Field(ge=0, description="Максимальный общий бюджет маршрута на всех участников в рублях.")
    companions_count: int = Field(default=1, ge=1, le=50, description="Количество участников маршрута.")
    child_age: int | None = Field(
        default=None,
        ge=0,
        le=18,
        description="Возраст ребёнка в годах для фильтрации по возрастному цензу (необязательно).",
    )
    maximum_travel_minutes: int = Field(
        default=20,
        ge=0,
        le=240,
        description="Максимально допустимое время перемещения между соседними площадками в минутах.",
    )


class WeekendPlanStepSchema(BaseModel):
    """Отдельное мероприятие в составе подобранного маршрута выходного дня."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "example": {
                "event_identifier": "66c51c98-97f9-473c-8b42-9382f84a83de",
                "title": "Щелкунчик",
                "category": "spectacle",
                "category_label": "Спектакли",
                "venue": "Башкирский государственный театр оперы и балета",
                "address": "ул. Ленина, 5/1",
                "starts_at": "2026-11-21T12:00:00+05:00",
                "ends_at": "2026-11-21T14:10:00+05:00",
                "price_minimum_rubles": 500,
                "price_maximum_rubles": 1800,
                "total_cost_rubles": 1000,
                "travel_from_previous_minutes": 0,
                "event_path": "/events/66c51c98-97f9-473c-8b42-9382f84a83de",
            }
        },
    )

    event_identifier: UUID = Field(description="Уникальный идентификатор мероприятия в каталоге.")
    title: str = Field(description="Название мероприятия.")
    category: EventCategory = Field(description="Код категории мероприятия.")
    category_label: str = Field(description="Русское наименование категории мероприятия.")
    venue: str = Field(description="Название площадки проведения.")
    address: str = Field(description="Адрес площадки.")
    starts_at: datetime = Field(description="Время начала мероприятия с учётом часового пояса.")
    ends_at: datetime = Field(description="Расчётное или точное время окончания мероприятия.")
    price_minimum_rubles: int = Field(ge=0, description="Минимальная стоимость одного билета в рублях.")
    price_maximum_rubles: int = Field(ge=0, description="Максимальная стоимость одного билета в рублях.")
    total_cost_rubles: int = Field(ge=0, description="Оценочная минимальная стоимость на всех участников в рублях.")
    travel_from_previous_minutes: int = Field(ge=0, description="Время в пути от предыдущей точки маршрута в минутах.")
    event_path: str = Field(description="Относительный путь к карточке мероприятия в веб-афише.")

    @classmethod
    def from_domain(cls, step: WeekendPlanStep) -> WeekendPlanStepSchema:
        """Преобразует доменный шаг маршрута в схему REST API."""
        return cls(
            event_identifier=step.event_identifier,
            title=step.title,
            category=step.category,
            category_label=CATEGORY_LABELS.get(step.category, step.category.value),
            venue=step.venue,
            address=step.address,
            starts_at=step.starts_at,
            ends_at=step.ends_at,
            price_minimum_rubles=step.price_minimum_rubles,
            price_maximum_rubles=step.price_maximum_rubles,
            total_cost_rubles=step.total_cost_rubles,
            travel_from_previous_minutes=step.travel_from_previous_minutes,
            event_path=f"/events/{step.event_identifier}",
        )


class ConstraintRelaxationSchema(BaseModel):
    """Предложение минимального ослабления ограничения при невозможности составить маршрут."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "example": {
                "constraint_name": "budget",
                "explanation": "Общего бюджета 500 ₽ на 2 чел. недостаточно для набора из 2–4 подходящих мероприятий.",
                "suggested_change": "Увеличить общий бюджет минимум до 1200 ₽.",
            }
        },
    )

    constraint_name: str = Field(
        description="Название конфликтующего ограничения (`budget`, `time_window`, `travel`, `age`, `count`)."
    )
    explanation: str = Field(description="Понятное жителю объяснение причины невозможности подбора.")
    suggested_change: str = Field(description="Минимальное изменение параметра, при котором маршрут собирается.")

    @classmethod
    def from_domain(cls, suggestion: ConstraintRelaxationSuggestion) -> ConstraintRelaxationSchema:
        """Преобразует доменное предложение релаксации в схему REST API."""
        return cls(
            constraint_name=suggestion.constraint_name,
            explanation=suggestion.explanation,
            suggested_change=suggestion.suggested_change,
        )


class WeekendPlanApiResponse(BaseModel):
    """Результат работы планировщика «Выходной день» (маршрут или диагностика конфликта)."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "example": {
                "is_feasible": True,
                "steps": [
                    {
                        "event_identifier": "66c51c98-97f9-473c-8b42-9382f84a83de",
                        "title": "Выставка современной графики",
                        "category": "exhibition",
                        "category_label": "Выставки",
                        "venue": "Национальный музей РБ",
                        "address": "ул. Советская, 14",
                        "starts_at": "2026-11-21T11:00:00+05:00",
                        "ends_at": "2026-11-21T12:30:00+05:00",
                        "price_minimum_rubles": 200,
                        "price_maximum_rubles": 400,
                        "total_cost_rubles": 400,
                        "travel_from_previous_minutes": 0,
                        "event_path": "/events/66c51c98-97f9-473c-8b42-9382f84a83de",
                    },
                    {
                        "event_identifier": "77d62d09-12a3-484d-9c53-0493a95b94ef",
                        "title": "Щелкунчик",
                        "category": "spectacle",
                        "category_label": "Спектакли",
                        "venue": "Башкирский государственный театр оперы и балета",
                        "address": "ул. Ленина, 5/1",
                        "starts_at": "2026-11-21T14:00:00+05:00",
                        "ends_at": "2026-11-21T16:10:00+05:00",
                        "price_minimum_rubles": 500,
                        "price_maximum_rubles": 1500,
                        "total_cost_rubles": 1000,
                        "travel_from_previous_minutes": 12,
                        "event_path": "/events/77d62d09-12a3-484d-9c53-0493a95b94ef",
                    },
                ],
                "total_minimum_cost_rubles": 1400,
                "total_maximum_cost_rubles": 3800,
                "total_travel_minutes": 12,
                "failure_reason": None,
                "relaxation_suggestions": [],
            }
        },
    )

    is_feasible: bool = Field(description="Признак успешного построения маршрута из 2–4 событий.")
    steps: list[WeekendPlanStepSchema] = Field(
        default_factory=list, description="Упорядоченная по времени последовательность шагов маршрута."
    )
    total_minimum_cost_rubles: int = Field(
        ge=0, description="Суммарная минимальная стоимость маршрута на всех участников."
    )
    total_maximum_cost_rubles: int = Field(
        ge=0, description="Суммарная максимальная стоимость маршрута на всех участников."
    )
    total_travel_minutes: int = Field(ge=0, description="Суммарное время на перемещения между площадками в минутах.")
    failure_reason: str | None = Field(
        default=None, description="Причина отсутствия маршрута, если `is_feasible` равно `False`."
    )
    relaxation_suggestions: list[ConstraintRelaxationSchema] = Field(
        default_factory=list, description="Список предложений минимального смягчения ограничений."
    )

    @classmethod
    def from_domain(cls, result: WeekendPlanResult) -> WeekendPlanApiResponse:
        """Преобразует доменный результат планирования в схему REST API."""
        return cls(
            is_feasible=result.is_feasible,
            steps=[WeekendPlanStepSchema.from_domain(step) for step in result.steps],
            total_minimum_cost_rubles=result.total_minimum_cost_rubles,
            total_maximum_cost_rubles=result.total_maximum_cost_rubles,
            total_travel_minutes=result.total_travel_minutes,
            failure_reason=result.failure_reason,
            relaxation_suggestions=[
                ConstraintRelaxationSchema.from_domain(item) for item in result.relaxation_suggestions
            ],
        )


def _get_container(request: Request) -> ApplicationContainer:
    container = getattr(request.app.state, "container", None)
    if not isinstance(container, ApplicationContainer):
        raise ServiceAvailabilityException("Контейнер зависимостей приложения не инициализирован.")
    return container


def _get_search_service(container: Annotated[ApplicationContainer, Depends(_get_container)]) -> EventSearchService:
    return container.search_service


def _get_subscription_service(
    container: Annotated[ApplicationContainer, Depends(_get_container)],
) -> SubscriptionService:
    return container.subscription_service


def _get_weekend_planner(container: Annotated[ApplicationContainer, Depends(_get_container)]) -> WeekendPlannerService:
    return container.weekend_planner


def _get_rate_limiter(request: Request) -> InMemoryRateLimiter:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if not isinstance(limiter, InMemoryRateLimiter):
        raise ServiceAvailabilityException("Ограничитель частоты запросов не инициализирован.")
    return limiter


def _build_not_modified_if_matching(request: Request, *, etag: str, cache_control: str) -> Response | None:
    if_none_match = request.headers.get("If-None-Match")
    if if_none_match is not None and if_none_match.strip() == etag:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag, "Cache-Control": cache_control}
        )
    return None


def create_api_router() -> APIRouter:
    """Создаёт маршрутизатор открытого версионированного REST API `/api/v1`."""
    router = APIRouter(prefix="/api/v1")

    @router.get(
        "/events",
        response_model=EventSearchPageSchema,
        tags=["События"],
        summary="Поиск и фильтрация опубликованных мероприятий",
    )
    async def list_events(
        search_service: Annotated[EventSearchService, Depends(_get_search_service)],
        query: Annotated[str | None, Query(description="Текстовый поиск по названию, описанию и тегам")] = None,
        city: Annotated[str | None, Query(description="Город или населённый пункт (допускает словоформы)")] = None,
        category: Annotated[EventCategory | None, Query(description="Категория мероприятия")] = None,
        period: Annotated[
            str | None, Query(description="Быстрый период: today, tomorrow, weekend, week, month")
        ] = None,
        date_from: Annotated[str | None, Query(description="Начало диапазона дат (YYYY-MM-DD или ISO-8601)")] = None,
        date_to: Annotated[str | None, Query(description="Конец диапазона дат (YYYY-MM-DD или ISO-8601)")] = None,
        time_from: Annotated[str | None, Query(description="Минимальное время начала (HH:MM)")] = None,
        time_to: Annotated[str | None, Query(description="Максимальное время начала (HH:MM)")] = None,
        maximum_price: Annotated[int | None, Query(ge=0, description="Максимальная цена билета в рублях")] = None,
        only_free: Annotated[bool, Query(description="Только бесплатные мероприятия")] = False,
        maximum_age_limit: Annotated[int | None, Query(ge=0, le=18, description="Максимальный возрастной ценз")] = None,
        maximum_duration_minutes: Annotated[
            int | None, Query(gt=0, description="Максимальная длительность в минутах")
        ] = None,
        language: Annotated[str | None, Query(description="Язык проведения")] = None,
        near_center: Annotated[bool, Query(description="Сортировать с приоритетом площадок у центра города")] = False,
        venue: Annotated[str | None, Query(description="Площадка проведения")] = None,
        sort_field: Annotated[EventSortField, Query(description="Поле сортировки")] = EventSortField.DATE_START,
        sort_direction: Annotated[SortDirection, Query(description="Направление сортировки")] = SortDirection.ASCENDING,
        page: Annotated[int, Query(ge=1, description="Номер страницы выборки")] = 1,
        page_size: Annotated[int, Query(ge=1, le=100, description="Размер страницы")] = 10,
    ) -> EventSearchPageSchema:
        """Возвращает страницу опубликованных карточек и пояснение пустой выдачи при нулевом результате."""
        period_start, period_end = compute_period_range(period)

        try:
            explicit_start = parse_date_boundary(date_from, end_of_day=False)
        except ValueError as error:
            raise InvalidSearchFilterError(
                "Некорректный формат параметра `date_from`.",
                parameter_name="date_from",
                reason="Ожидается YYYY-MM-DD или ISO-8601",
            ) from error

        try:
            explicit_end = parse_date_boundary(date_to, end_of_day=True)
        except ValueError as error:
            raise InvalidSearchFilterError(
                "Некорректный формат параметра `date_to`.",
                parameter_name="date_to",
                reason="Ожидается YYYY-MM-DD или ISO-8601",
            ) from error

        parsed_time_from: time | None
        try:
            parsed_time_from = parse_time_value(time_from)
        except ValueError as error:
            raise InvalidSearchFilterError(
                "Некорректный формат времени `time_from` (ожидается ЧЧ:ММ).",
                parameter_name="time_from",
                reason="Ошибка разбора времени",
            ) from error

        parsed_time_to: time | None
        try:
            parsed_time_to = parse_time_value(time_to)
        except ValueError as error:
            raise InvalidSearchFilterError(
                "Некорректный формат времени `time_to` (ожидается ЧЧ:ММ).",
                parameter_name="time_to",
                reason="Ошибка разбора времени",
            ) from error

        search_page = await search_service.search_events(
            search_filter=EventSearchFilter(
                query=query,
                city=city,
                category=category,
                date_from=explicit_start or period_start,
                date_to=explicit_end or period_end,
                time_from=parsed_time_from,
                time_to=parsed_time_to,
                maximum_price=maximum_price,
                only_free=only_free,
                maximum_age_limit=maximum_age_limit,
                maximum_duration_minutes=maximum_duration_minutes,
                language=language,
                near_center=near_center,
                venue=venue,
                sort_field=sort_field,
                sort_direction=sort_direction,
                page=page,
                page_size=page_size,
            )
        )
        return EventSearchPageSchema.from_domain(search_page)

    @router.get(
        "/events/{event_identifier}",
        response_model=EventDetailSchema,
        responses={
            304: {"description": "Содержимое карточки не изменилось (ETag совпал)"},
            404: {"model": ErrorResponse},
        },
        tags=["События"],
        summary="Получить полную карточку мероприятия с поддержкой ETag",
    )
    async def get_event_detail(
        event_identifier: UUID,
        request: Request,
        response: Response,
        search_service: Annotated[EventSearchService, Depends(_get_search_service)],
    ) -> EventDetailSchema | Response:
        """Возвращает подробную карточку события со всеми подтверждёнными источниками и ETag."""
        event = await search_service.get_event(event_identifier=event_identifier)
        etag = build_entity_etag(
            "event",
            {"identifier": str(event.identifier), "updated_at": event.updated_at.isoformat(), "status": event.status},
        )
        cache_control = "public, max-age=60, must-revalidate"
        not_modified = _build_not_modified_if_matching(request, etag=etag, cache_control=cache_control)
        if not_modified is not None:
            return not_modified

        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = cache_control
        return EventDetailSchema.from_domain_detail(event)

    @router.get(
        "/events/{event_identifier}/similar",
        response_model=list[EventSummarySchema],
        responses={404: {"model": ErrorResponse}},
        tags=["События"],
        summary="Подбор похожих опубликованных мероприятий",
    )
    async def get_similar_events(
        event_identifier: UUID,
        search_service: Annotated[EventSearchService, Depends(_get_search_service)],
        limit: Annotated[int, Query(ge=1, le=20)] = 5,
    ) -> list[EventSummarySchema]:
        """Возвращает список похожих мероприятий для выбранной карточки."""
        similar = await search_service.find_similar_events(event_identifier=event_identifier, limit=limit)
        return [EventSummarySchema.from_domain(item) for item in similar]

    @router.get(
        "/venues/schedule",
        response_model=EventSearchPageSchema,
        tags=["События"],
        summary="Афиша и расписание выбранной площадки",
    )
    async def get_venue_schedule(
        search_service: Annotated[EventSearchService, Depends(_get_search_service)],
        venue: Annotated[str, Query(min_length=1, description="Название площадки или его часть")],
        city: Annotated[str | None, Query(description="Город расположения площадки")] = None,
        date_from: Annotated[str | None, Query(description="Начальная дата")] = None,
        date_to: Annotated[str | None, Query(description="Конечная дата")] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> EventSearchPageSchema:
        """Возвращает расписание мероприятий на выбранной культурной площадке."""
        try:
            parsed_from = parse_date_boundary(date_from, end_of_day=False)
            parsed_to = parse_date_boundary(date_to, end_of_day=True)
        except ValueError as error:
            raise InvalidSearchFilterError(
                "Некорректный формат границ дат расписания площадки.",
                parameter_name="date_from",
                reason="Ошибка разбора даты",
            ) from error

        schedule_page = await search_service.get_venue_schedule(
            venue=venue, city=city, date_from=parsed_from, date_to=parsed_to, page=page, page_size=page_size
        )
        return EventSearchPageSchema.from_domain(schedule_page)

    @router.get(
        "/references/cities",
        response_model=list[CityReferenceSchema],
        responses={304: {"description": "Справочник городов не изменился"}},
        tags=["Справочники"],
        summary="Справочник городов и населённых пунктов с ETag",
    )
    async def list_cities_reference(
        request: Request,
        response: Response,
        search_service: Annotated[EventSearchService, Depends(_get_search_service)],
    ) -> list[CityReferenceSchema] | Response:
        """Возвращает список городов Республики Башкортостан и их синонимов."""
        cities = await search_service.list_cities()
        etag = build_entity_etag("cities", [city.normalized_name for city in cities])
        cache_control = "public, max-age=300, must-revalidate"
        not_modified = _build_not_modified_if_matching(request, etag=etag, cache_control=cache_control)
        if not_modified is not None:
            return not_modified

        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = cache_control
        return [CityReferenceSchema.from_domain(city) for city in cities]

    @router.get(
        "/references/venues",
        response_model=list[VenueReferenceSchema],
        responses={304: {"description": "Справочник площадок не изменился"}},
        tags=["Справочники"],
        summary="Справочник культурных площадок с ETag",
    )
    async def list_venues_reference(
        request: Request,
        response: Response,
        search_service: Annotated[EventSearchService, Depends(_get_search_service)],
        city: Annotated[str | None, Query(description="Фильтр площадок по городу")] = None,
    ) -> list[VenueReferenceSchema] | Response:
        """Возвращает перечень площадок с адресами и координатами."""
        venues = await search_service.list_venues(city=city)
        etag = build_entity_etag("venues", [(venue.city, venue.normalized_name) for venue in venues])
        cache_control = "public, max-age=300, must-revalidate"
        not_modified = _build_not_modified_if_matching(request, etag=etag, cache_control=cache_control)
        if not_modified is not None:
            return not_modified

        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = cache_control
        return [VenueReferenceSchema.from_domain(venue) for venue in venues]

    @router.get(
        "/references/categories",
        response_model=list[CategoryReferenceSchema],
        responses={304: {"description": "Справочник категорий не изменился"}},
        tags=["Справочники"],
        summary="Справочник категорий мероприятий с ETag",
    )
    async def list_categories_reference(
        request: Request, response: Response
    ) -> list[CategoryReferenceSchema] | Response:
        """Возвращает перечень кодов категорий API и их русских наименований."""
        items = [CategoryReferenceSchema(code=category, label=CATEGORY_LABELS[category]) for category in EventCategory]
        etag = build_entity_etag("categories", [item.code.value for item in items])
        cache_control = "public, max-age=3600, must-revalidate"
        not_modified = _build_not_modified_if_matching(request, etag=etag, cache_control=cache_control)
        if not_modified is not None:
            return not_modified

        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = cache_control
        return items

    @router.post(
        "/subscriptions",
        response_model=SubscriptionSchema,
        status_code=status.HTTP_201_CREATED,
        tags=["Подписки"],
        summary="Оформить подписку без сбора персональных контактов",
    )
    async def create_subscription(
        payload: SubscriptionCreateRequest,
        subscription_service: Annotated[SubscriptionService, Depends(_get_subscription_service)],
    ) -> SubscriptionSchema:
        """Создаёт или возвращает существующую подписку по техническому идентификатору."""
        record = await subscription_service.subscribe(
            channel=payload.channel,
            subscriber_identifier=payload.subscriber_identifier,
            city=payload.city,
            category=payload.category,
        )
        return SubscriptionSchema.from_domain(record)

    @router.get(
        "/subscriptions",
        response_model=list[SubscriptionSchema],
        tags=["Подписки"],
        summary="Список активных подписок пользователя",
    )
    async def list_subscriptions(
        subscription_service: Annotated[SubscriptionService, Depends(_get_subscription_service)],
        subscriber_identifier: Annotated[str, Query(min_length=1, max_length=128)],
        channel: Annotated[DialogChannel, Query()] = DialogChannel.WEB,
    ) -> list[SubscriptionSchema]:
        """Возвращает активные подписки пользователя в выбранном канале."""
        records = await subscription_service.list_subscriptions(
            channel=channel, subscriber_identifier=subscriber_identifier
        )
        return [SubscriptionSchema.from_domain(item) for item in records]

    @router.delete(
        "/subscriptions/{subscription_identifier}",
        response_model=SubscriptionDeletedSchema,
        responses={404: {"model": ErrorResponse}},
        tags=["Подписки"],
        summary="Физически удалить конкретную подписку",
    )
    async def delete_subscription(
        subscription_identifier: UUID,
        subscription_service: Annotated[SubscriptionService, Depends(_get_subscription_service)],
        subscriber_identifier: Annotated[str | None, Query(max_length=128)] = None,
    ) -> SubscriptionDeletedSchema:
        """Удаляет подписку из базы данных без мягких пометок."""
        await subscription_service.unsubscribe(
            subscription_identifier=subscription_identifier, subscriber_identifier=subscriber_identifier
        )
        return SubscriptionDeletedSchema(deleted_count=1)

    @router.delete(
        "/subscriptions",
        response_model=SubscriptionDeletedSchema,
        tags=["Подписки"],
        summary="Отписаться от всех подписок пользователя в канале",
    )
    async def delete_all_subscriptions(
        subscription_service: Annotated[SubscriptionService, Depends(_get_subscription_service)],
        subscriber_identifier: Annotated[str, Query(min_length=1, max_length=128)],
        channel: Annotated[DialogChannel, Query()] = DialogChannel.WEB,
    ) -> SubscriptionDeletedSchema:
        """Физически удаляет все подписки пользователя для указанного канала."""
        deleted_count = await subscription_service.unsubscribe_all(
            channel=channel, subscriber_identifier=subscriber_identifier
        )
        return SubscriptionDeletedSchema(deleted_count=deleted_count)

    @router.post(
        "/feedback",
        response_model=FeedbackSchema,
        status_code=status.HTTP_201_CREATED,
        responses={422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
        tags=["Обратная связь"],
        summary="Отправить заявку организатора или уточнение по событию",
    )
    async def submit_feedback(
        payload: FeedbackCreateRequest,
        request: Request,
        subscription_service: Annotated[SubscriptionService, Depends(_get_subscription_service)],
        rate_limiter: Annotated[InMemoryRateLimiter, Depends(_get_rate_limiter)],
    ) -> FeedbackSchema:
        """Регистрирует обращение с проверкой антиспам-поля, частоты запросов и защиты от инъекций."""
        client_address = request.client.host if request.client is not None else "unknown"
        allowed, retry_after = rate_limiter.check_and_consume(client_address=client_address, scope="feedback")
        if not allowed:
            raise _FeedbackRateLimitExceededError(retry_after_seconds=retry_after)

        if normalize_optional_text(payload.honeypot_website) is not None:
            raise ValidationException("Запрос отклонён системой защиты от автоматического спама.")

        record = await subscription_service.submit_feedback(
            channel=payload.channel,
            message_text=payload.message_text,
            event_identifier=payload.event_identifier,
            contact_hint=payload.contact_hint,
        )
        return FeedbackSchema.from_domain(record)

    @router.post(
        "/weekend-plan",
        response_model=WeekendPlanApiResponse,
        responses={422: {"model": ErrorResponse}},
        tags=["Планировщик выходного дня"],
        summary="Подобрать маршрут выходного дня из 2–4 мероприятий",
    )
    async def plan_weekend(
        payload: WeekendPlanApiRequest, weekend_planner: Annotated[WeekendPlannerService, Depends(_get_weekend_planner)]
    ) -> WeekendPlanApiResponse:
        """Строит непересекающийся маршрут с учётом бюджета, возраста и времени на дорогу."""
        result = await weekend_planner.plan_weekend(
            WeekendPlanRequest(
                city=payload.city,
                target_date=payload.target_date,
                window_start_time=payload.window_start_time,
                window_end_time=payload.window_end_time,
                budget_rubles=payload.budget_rubles,
                companions_count=payload.companions_count,
                child_age=payload.child_age,
                maximum_travel_minutes=payload.maximum_travel_minutes,
            )
        )
        return WeekendPlanApiResponse.from_domain(result)

    return router


class _FeedbackRateLimitExceededError(ApplicationException):
    """Внутреннее исключение превышения лимита отправки формы обратной связи."""

    def __init__(self, *, retry_after_seconds: int) -> None:
        super().__init__("Слишком частая отправка обращений. Повторите попытку чуть позже.")
        self.retry_after_seconds = retry_after_seconds


def register_exception_handlers(app: FastAPI) -> None:
    """Регистрирует централизованные обработчики исключений с единым форматом `ErrorResponse`."""

    @app.exception_handler(_FeedbackRateLimitExceededError)
    async def handle_feedback_rate_limit(request: Request, error: _FeedbackRateLimitExceededError) -> JSONResponse:
        return _build_error_json_response(
            request=request,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            code="feedback_rate_limit_exceeded",
            message=error.message,
            details={"retry_after_seconds": error.retry_after_seconds},
            headers={"Retry-After": str(error.retry_after_seconds)},
        )

    @app.exception_handler(ApplicationException)
    async def handle_application_exception(request: Request, error: ApplicationException) -> JSONResponse:
        status_code, error_code = _map_application_exception_to_http(error)
        extra_details = {
            key: value for key, value in error.__dict__.items() if key != "message" and not key.startswith("_")
        }
        return _build_error_json_response(
            request=request,
            status_code=status_code,
            code=error_code,
            message=error.message,
            details=extra_details or None,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        sanitized_errors = [
            {
                "location": [str(part) for part in item.get("loc", ())],
                "message": str(item.get("msg", "Некорректное значение")),
                "type": str(item.get("type", "value_error")),
            }
            for item in error.errors()
        ]
        return _build_error_json_response(
            request=request,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="request_validation_error",
            message="Некорректные параметры или тело запроса.",
            details=sanitized_errors,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(request: Request, error: Exception) -> JSONResponse:
        logger.exception("Необработанная ошибка веб-обработчика %s: %s", request.url.path, type(error).__name__)
        return _build_error_json_response(
            request=request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_server_error",
            message="Внутренняя ошибка сервиса. Пожалуйста, повторите попытку позже.",
            details=None,
        )


def _map_application_exception_to_http(error: ApplicationException) -> tuple[int, str]:
    if isinstance(error, (EventNotFoundError, ModerationItemNotFoundError, SubscriptionNotFoundError)):
        return status.HTTP_404_NOT_FOUND, _camel_to_snake_code(type(error).__name__)
    if isinstance(error, DuplicateEventConflictError):
        return status.HTTP_409_CONFLICT, "duplicate_event_conflict"
    if isinstance(
        error,
        (
            InvalidSearchFilterError,
            InvalidEventDataError,
            InvalidWeekendPlanRequestError,
            UnsupportedSourceFormatError,
            PromptInjectionDetectedError,
            ValidationException,
        ),
    ):
        return status.HTTP_422_UNPROCESSABLE_CONTENT, _camel_to_snake_code(type(error).__name__)
    if isinstance(error, (ServiceAvailabilityException, LifecycleException)):
        return status.HTTP_503_SERVICE_UNAVAILABLE, _camel_to_snake_code(type(error).__name__)
    if isinstance(error, InfrastructureException):
        return status.HTTP_500_INTERNAL_SERVER_ERROR, _camel_to_snake_code(type(error).__name__)
    return status.HTTP_400_BAD_REQUEST, _camel_to_snake_code(type(error).__name__)


def _camel_to_snake_code(class_name: str) -> str:
    characters: list[str] = []
    for index, character in enumerate(class_name):
        if character.isupper() and index > 0:
            characters.append("_")
        characters.append(character.lower())
    return "".join(characters)


def _build_error_json_response(
    *,
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | list[Any] | None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_identifier = getattr(request.state, "request_identifier", None)
    body = ErrorResponse(
        code=code,
        message=message,
        details=details,
        request_id=request_identifier if isinstance(request_identifier, str) else None,
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"), headers=headers)


def register_observability_routes(app: FastAPI) -> None:
    """Регистрирует конечные точки `/health` и `/metrics` для контроля работоспособности."""

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["Наблюдаемость"],
        summary="Проверка доступности веб-сервиса и базы данных PostgreSQL",
    )
    async def health_check(request: Request, response: Response) -> HealthResponse:
        container = _get_container(request)
        database_healthy = await container.event_repository.check_health()
        overall_status = "healthy" if database_healthy else "degraded"
        if not database_healthy:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        return HealthResponse(
            status=overall_status,
            service=container.application_settings.NAME,
            environment=container.application_settings.ENVIRONMENT,
            components={
                "database": HealthComponentSchema(
                    status="up" if database_healthy else "down",
                    detail="PostgreSQL 17" if database_healthy else "База данных недоступна",
                ),
                "web_api": HealthComponentSchema(status="up", detail="REST API v1 и SSR активны"),
            },
        )

    @app.get(
        "/metrics",
        response_model=MetricsResponse,
        tags=["Наблюдаемость"],
        summary="Базовые метрики HTTP-трафика (запросы, ошибки, задержка)",
    )
    async def metrics_endpoint(request: Request) -> MetricsResponse:
        collector = getattr(request.app.state, "metrics_collector", None)
        if not isinstance(collector, RequestMetricsCollector):
            return MetricsResponse(
                requests_total=0,
                errors_4xx=0,
                errors_5xx=0,
                average_latency_milliseconds=0.0,
                p95_latency_milliseconds=0.0,
            )
        snapshot = collector.snapshot()
        return MetricsResponse(
            requests_total=snapshot.requests_total,
            errors_4xx=snapshot.errors_4xx,
            errors_5xx=snapshot.errors_5xx,
            average_latency_milliseconds=snapshot.average_latency_milliseconds,
            p95_latency_milliseconds=snapshot.p95_latency_milliseconds,
        )
