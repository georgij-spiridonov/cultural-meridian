"""PostgreSQL-репозиторий нормализованного каталога мероприятий и справочников."""

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, Numeric, Time, and_, case, cast, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.base import ExecutableOption
from sqlalchemy.sql.elements import UnaryExpression

from src.core.logger import get_logger
from src.core.types import EventStatus, RealCollection, SourceType
from src.domain.entities import (
    CityReference,
    Event,
    EventSearchFilter,
    EventSearchPage,
    EventSortField,
    EventSourceReference,
    SortDirection,
    VenueReference,
)
from src.domain.exceptions import (
    DuplicateEventConflictError,
    EventNotFoundError,
    InvalidEventDataError,
    InvalidSearchFilterError,
)
from src.domain.ports import EventRepositoryPort
from src.domain.services.normalization import normalize_optional_text, normalize_text_for_matching, normalize_title
from src.infrastructure.database._helpers import (
    build_city_match_condition,
    resolve_or_create_city,
    validate_positive_limit,
)
from src.infrastructure.database.engine import DatabaseEngineManager
from src.infrastructure.database.errors import handle_database_errors
from src.infrastructure.database.mappers import map_city_from_row, map_event_from_row, map_venue_from_row
from src.infrastructure.database.tables import (
    CityRow,
    EventRow,
    EventSourceRow,
    EventTagRow,
    TagRow,
    VenueAliasRow,
    VenueRow,
)

__all__ = ["PostgreSqlEventRepository"]

logger = get_logger(__name__)

_BASHKORTOSTAN_IANATZ = "Asia/Yekaterinburg"
_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)

type _OrderByClause = (
    UnaryExpression[datetime]
    | UnaryExpression[float]
    | UnaryExpression[float | None]
    | UnaryExpression[int]
    | UnaryExpression[int | None]
    | UnaryExpression[str]
    | UnaryExpression[UUID]
)


def _event_load_options() -> tuple[ExecutableOption, ...]:
    return (
        selectinload(EventRow.venue).selectinload(VenueRow.city),
        selectinload(EventRow.sources),
        selectinload(EventRow.tags),
    )


def _validate_search_filter(search_filter: EventSearchFilter) -> None:
    if search_filter.page < 1:
        raise InvalidSearchFilterError("Номер страницы должен быть не меньше 1.", parameter_name="page")
    if search_filter.page_size < 1 or search_filter.page_size > 1000:
        raise InvalidSearchFilterError(
            "Размер страницы должен находиться в диапазоне от 1 до 1000.", parameter_name="page_size"
        )
    if (
        search_filter.date_from is not None
        and search_filter.date_to is not None
        and search_filter.date_from > search_filter.date_to
    ):
        raise InvalidSearchFilterError(
            "Начальная дата фильтрации не может быть позже конечной даты.", parameter_name="date_from"
        )
    if search_filter.maximum_price is not None and search_filter.maximum_price < 0:
        raise InvalidSearchFilterError("Максимальная цена не может быть отрицательной.", parameter_name="maximum_price")
    if search_filter.maximum_age_limit is not None and search_filter.maximum_age_limit < 0:
        raise InvalidSearchFilterError(
            "Возрастной порог не может быть отрицательным.", parameter_name="maximum_age_limit"
        )
    if search_filter.maximum_duration_minutes is not None and search_filter.maximum_duration_minutes <= 0:
        raise InvalidSearchFilterError(
            "Максимальная длительность должна быть положительной.", parameter_name="maximum_duration_minutes"
        )


def _build_venue_match_condition(raw_venue_query: str) -> ColumnElement[bool] | None:
    cleaned_input = normalize_optional_text(raw_venue_query)
    if cleaned_input is None:
        return None
    normalized_venue = normalize_text_for_matching(cleaned_input)
    if not normalized_venue:
        return None

    exact_or_contains = or_(
        VenueRow.normalized_name == normalized_venue, VenueRow.normalized_name.contains(normalized_venue)
    )
    alias_match = exists(
        select(1).where(
            (VenueAliasRow.venue_identifier == VenueRow.identifier)
            & or_(
                VenueAliasRow.normalized_alias == normalized_venue,
                VenueAliasRow.normalized_alias.contains(normalized_venue),
            )
        )
    )
    trigram_match = func.word_similarity(normalized_venue, VenueRow.normalized_name) >= 0.45
    return or_(exact_or_contains, alias_match, trigram_match)


def _build_query_condition_and_relevance(
    raw_query: str | None,
) -> tuple[ColumnElement[bool] | None, ColumnElement[float] | None]:
    cleaned_query = normalize_optional_text(raw_query)
    if cleaned_query is None:
        return None, None
    normalized_query = normalize_text_for_matching(cleaned_query)
    if not normalized_query:
        return None, None

    tsquery = func.websearch_to_tsquery("russian", cleaned_query)
    tag_match = exists(
        select(1)
        .select_from(EventTagRow)
        .join(TagRow, TagRow.identifier == EventTagRow.tag_identifier)
        .where(
            (EventTagRow.event_identifier == EventRow.identifier)
            & or_(TagRow.normalized_name == normalized_query, TagRow.normalized_name.contains(normalized_query))
        )
    )

    conditions: list[ColumnElement[bool]] = [
        EventRow.search_vector.op("@@")(tsquery),
        EventRow.normalized_title.contains(normalized_query),
        and_(
            EventRow.normalized_title.op("%")(normalized_query),
            func.similarity(EventRow.normalized_title, normalized_query) >= 0.25,
        ),
        func.word_similarity(normalized_query, EventRow.normalized_title) >= 0.4,
        VenueRow.normalized_name.contains(normalized_query),
        CityRow.normalized_name.contains(normalized_query),
        tag_match,
    ]
    if " " in normalized_query:
        combined_text = EventRow.title + " " + EventRow.description + " " + CityRow.name + " " + VenueRow.name
        conditions.append(func.to_tsvector("russian", combined_text).op("@@")(tsquery))

    relevance_expression = cast(
        func.ts_rank_cd(EventRow.search_vector, tsquery) * 2.0
        + func.greatest(
            func.word_similarity(normalized_query, EventRow.normalized_title),
            func.similarity(EventRow.normalized_title, normalized_query),
        )
        * 3.0
        + case((EventRow.normalized_title.contains(normalized_query), 1.0), else_=0.0),
        Numeric(10, 4),
    )
    return or_(*conditions), cast(relevance_expression, Numeric)


def _build_distance_to_center_expression() -> ColumnElement[float | None]:
    coordinates_present = and_(
        CityRow.center_latitude.is_not(None),
        CityRow.center_longitude.is_not(None),
        VenueRow.latitude.is_not(None),
        VenueRow.longitude.is_not(None),
    )
    delta_lat_km = (VenueRow.latitude - CityRow.center_latitude) * 111.32
    delta_lon_km = (
        (VenueRow.longitude - CityRow.center_longitude) * 111.32 * func.cos(func.radians(CityRow.center_latitude))
    )
    euclidean_km = func.sqrt(func.power(delta_lat_km, 2.0) + func.power(delta_lon_km, 2.0))
    return case((coordinates_present, euclidean_km), else_=None)


def _build_search_conditions(
    search_filter: EventSearchFilter, *, statuses: RealCollection[EventStatus]
) -> tuple[list[ColumnElement[bool]], ColumnElement[float] | None]:
    conditions: list[ColumnElement[bool]] = [EventRow.status.in_([status.value for status in statuses])]

    if search_filter.city is not None:
        city_condition = build_city_match_condition(search_filter.city)
        if city_condition is not None:
            conditions.append(city_condition)

    if search_filter.category is not None:
        conditions.append(EventRow.category_code == search_filter.category.value)

    if search_filter.date_from is not None:
        conditions.append(EventRow.date_start >= search_filter.date_from)
    if search_filter.date_to is not None:
        conditions.append(EventRow.date_start <= search_filter.date_to)

    if search_filter.time_from is not None or search_filter.time_to is not None:
        local_start_time = cast(func.timezone(_BASHKORTOSTAN_IANATZ, EventRow.date_start), Time)
        if search_filter.time_from is not None and search_filter.time_to is not None:
            if search_filter.time_from <= search_filter.time_to:
                conditions.append(
                    and_(local_start_time >= search_filter.time_from, local_start_time <= search_filter.time_to)
                )
            else:
                conditions.append(
                    or_(local_start_time >= search_filter.time_from, local_start_time <= search_filter.time_to)
                )
        elif search_filter.time_from is not None:
            conditions.append(local_start_time >= search_filter.time_from)
        elif search_filter.time_to is not None:
            conditions.append(local_start_time <= search_filter.time_to)

    if search_filter.only_free:
        conditions.append(EventRow.is_free.is_(True))
    elif search_filter.maximum_price is not None:
        conditions.append(
            or_(
                EventRow.is_free.is_(True),
                and_(EventRow.price_minimum.is_not(None), EventRow.price_minimum <= search_filter.maximum_price),
            )
        )

    if search_filter.maximum_age_limit is not None:
        conditions.append(EventRow.age_limit <= search_filter.maximum_age_limit)

    if search_filter.maximum_duration_minutes is not None:
        conditions.append(
            and_(
                EventRow.duration_minutes.is_not(None),
                EventRow.duration_minutes <= search_filter.maximum_duration_minutes,
            )
        )

    if search_filter.language is not None:
        cleaned_language = normalize_optional_text(search_filter.language)
        if cleaned_language is not None:
            conditions.append(EventRow.language == cleaned_language.lower())

    if search_filter.venue is not None:
        venue_condition = _build_venue_match_condition(search_filter.venue)
        if venue_condition is not None:
            conditions.append(venue_condition)

    query_condition, relevance_expression = _build_query_condition_and_relevance(search_filter.query)
    if query_condition is not None:
        conditions.append(query_condition)

    return conditions, relevance_expression


def _build_order_by_clauses(
    search_filter: EventSearchFilter, *, relevance_expression: ColumnElement[float] | None
) -> list[_OrderByClause]:
    is_ascending = search_filter.sort_direction is SortDirection.ASCENDING
    clauses: list[_OrderByClause] = []

    if search_filter.near_center:
        distance_expr = _build_distance_to_center_expression()
        outside_center_bucket = case((distance_expr.is_(None), 2), (distance_expr <= 4.0, 0), else_=1)
        clauses.append(outside_center_bucket.asc())
        clauses.append(distance_expr.asc().nullslast())

    if search_filter.sort_field is EventSortField.DATE_START:
        clauses.append(EventRow.date_start.asc() if is_ascending else EventRow.date_start.desc())
    elif search_filter.sort_field is EventSortField.PRICE:
        effective_price = case((EventRow.is_free.is_(True), 0), else_=EventRow.price_minimum)
        clauses.append(effective_price.asc().nullslast() if is_ascending else effective_price.desc().nullslast())
    elif search_filter.sort_field is EventSortField.RELEVANCE:
        if relevance_expression is not None:
            clauses.append(relevance_expression.asc() if is_ascending else relevance_expression.desc())
        clauses.append(EventRow.date_start.asc())
    elif search_filter.sort_field is EventSortField.TITLE:
        clauses.append(EventRow.normalized_title.asc() if is_ascending else EventRow.normalized_title.desc())
    elif search_filter.sort_field is EventSortField.DURATION:
        clauses.append(
            EventRow.duration_minutes.asc().nullslast()
            if is_ascending
            else EventRow.duration_minutes.desc().nullslast()
        )
    elif search_filter.sort_field is EventSortField.AGE_LIMIT:
        clauses.append(EventRow.age_limit.asc() if is_ascending else EventRow.age_limit.desc())
    elif search_filter.sort_field is EventSortField.CREATED_AT:
        clauses.append(EventRow.created_at.asc() if is_ascending else EventRow.created_at.desc())

    clauses.append(EventRow.date_start.asc())
    clauses.append(EventRow.identifier.asc())
    return clauses


async def _resolve_or_create_venue(
    session: AsyncSession,
    *,
    city_row: CityRow,
    venue_name: str,
    address: str,
    latitude: float | None,
    longitude: float | None,
    allow_overwrite_metadata: bool,
) -> VenueRow:
    cleaned_name = normalize_optional_text(venue_name)
    if cleaned_name is None:
        raise InvalidEventDataError("Название площадки проведения не может быть пустым.", field_name="venue")

    normalized_name = normalize_text_for_matching(cleaned_name)
    if not normalized_name:
        raise InvalidEventDataError("Не удалось нормализовать название площадки.", field_name="venue")

    cleaned_address = normalize_optional_text(address) or ""

    existing_venue = (
        await session.execute(
            select(VenueRow).where(
                (VenueRow.city_identifier == city_row.identifier) & (VenueRow.normalized_name == normalized_name)
            )
        )
    ).scalar_one_or_none()

    if existing_venue is None:
        alias_row = (
            await session.execute(
                select(VenueAliasRow)
                .join(VenueRow, VenueRow.identifier == VenueAliasRow.venue_identifier)
                .where(
                    (VenueRow.city_identifier == city_row.identifier)
                    & (VenueAliasRow.normalized_alias == normalized_name)
                )
            )
        ).scalar_one_or_none()
        if alias_row is not None:
            existing_venue = (
                await session.execute(select(VenueRow).where(VenueRow.identifier == alias_row.venue_identifier))
            ).scalar_one_or_none()

    if existing_venue is None:
        await session.execute(
            postgresql_insert(VenueRow)
            .values(
                city_identifier=city_row.identifier,
                name=cleaned_name,
                normalized_name=normalized_name,
                address=cleaned_address,
                latitude=latitude,
                longitude=longitude,
            )
            .on_conflict_do_nothing(constraint="city_normalized_name")
        )
        resolved = (
            await session.execute(
                select(VenueRow).where(
                    (VenueRow.city_identifier == city_row.identifier) & (VenueRow.normalized_name == normalized_name)
                )
            )
        ).scalar_one()
        return resolved

    if cleaned_address and (not existing_venue.address or allow_overwrite_metadata):
        existing_venue.address = cleaned_address
    if latitude is not None and longitude is not None and (existing_venue.latitude is None or allow_overwrite_metadata):
        existing_venue.latitude = latitude
        existing_venue.longitude = longitude

    return existing_venue


async def _sync_tags(session: AsyncSession, *, raw_tags: Sequence[str]) -> list[TagRow]:
    normalized_to_display: dict[str, str] = {}
    for raw_tag in raw_tags:
        cleaned_tag = normalize_optional_text(raw_tag)
        if cleaned_tag is None:
            continue
        normalized_tag = normalize_text_for_matching(cleaned_tag)
        if normalized_tag and normalized_tag not in normalized_to_display:
            normalized_to_display[normalized_tag] = cleaned_tag[:64]

    if not normalized_to_display:
        return []

    insert_values = [
        {"name": name, "normalized_name": normalized} for normalized, name in normalized_to_display.items()
    ]
    await session.execute(postgresql_insert(TagRow).values(insert_values).on_conflict_do_nothing())
    tag_rows = (
        (
            await session.execute(
                select(TagRow)
                .where(TagRow.normalized_name.in_(list(normalized_to_display.keys())))
                .order_by(TagRow.normalized_name.asc())
            )
        )
        .scalars()
        .all()
    )
    return list(tag_rows)


def _prepare_normalized_sources(event: Event) -> list[tuple[EventSourceReference, bool]]:
    primary_key = (event.source_type, event.source_hash.strip())
    deduplicated: dict[tuple[SourceType, str], tuple[EventSourceReference, bool]] = {}

    for source in event.sources:
        cleaned_hash = source.source_hash.strip()
        cleaned_url = source.source_url.strip()
        cleaned_name = source.source_name.strip()
        if not cleaned_hash or not cleaned_url or not cleaned_name:
            continue
        key = (source.source_type, cleaned_hash)
        is_primary = key == primary_key
        normalized_source = EventSourceReference(
            source_name=cleaned_name,
            source_type=source.source_type,
            source_url=cleaned_url,
            source_hash=cleaned_hash,
            verified_at=source.verified_at if not is_primary else (event.verified_at or source.verified_at),
        )
        deduplicated[key] = (normalized_source, is_primary)

    if primary_key not in deduplicated:
        primary_ref = EventSourceReference(
            source_name=event.source_type.value,
            source_type=event.source_type,
            source_url=event.source_url.strip(),
            source_hash=event.source_hash.strip(),
            verified_at=event.verified_at,
        )
        deduplicated[primary_key] = (primary_ref, True)

    ordered = list(deduplicated.values())
    ordered.sort(key=lambda item: not item[1])
    return ordered


class PostgreSqlEventRepository(EventRepositoryPort):
    """Реализация `EventRepositoryPort` на базе PostgreSQL 17."""

    def __init__(self, engine_manager: DatabaseEngineManager) -> None:
        """Инициализирует репозиторий каталога мероприятий и справочников.

        Arguments:
            engine_manager: Менеджер асинхронного движка и пула сессий PostgreSQL.
        """
        self._engine_manager = engine_manager

    async def check_health(self) -> bool:
        """Проверяет доступность хранилища каталога мероприятий.

        Returns:
            `True`, если соединение с базой данных активно и готово к работе.
        """
        return await self._engine_manager.check_health()

    async def search_events(
        self, *, search_filter: EventSearchFilter, statuses: RealCollection[EventStatus] = (EventStatus.PUBLISHED,)
    ) -> EventSearchPage:
        """Выполняет поиск мероприятий по фильтрам, пагинации и допустимым статусам.

        Arguments:
            search_filter: Неизменяемый набор критериев поиска, сортировки и пагинации.
            statuses: Допустимые статусы публикаций в выборке (по умолчанию только `PUBLISHED`).

        Returns:
            Страница результатов поиска с общим количеством совпадений.

        Raises:
            InvalidSearchFilterError: При некорректных параметрах пагинации или диапазонов.
        """
        _validate_search_filter(search_filter)
        if not statuses:
            return EventSearchPage(
                items=(), total_count=0, page=search_filter.page, page_size=search_filter.page_size, has_more=False
            )

        conditions, relevance_expression = _build_search_conditions(search_filter, statuses=statuses)
        order_clauses = _build_order_by_clauses(search_filter, relevance_expression=relevance_expression)
        offset_value = (search_filter.page - 1) * search_filter.page_size

        count_statement = (
            select(func.count(EventRow.identifier))
            .select_from(EventRow)
            .join(EventRow.venue)
            .join(VenueRow.city)
            .where(*conditions)
        )
        items_statement = (
            select(EventRow)
            .join(EventRow.venue)
            .join(VenueRow.city)
            .where(*conditions)
            .options(*_event_load_options())
            .order_by(*order_clauses)
            .offset(offset_value)
            .limit(search_filter.page_size)
        )

        async with (
            handle_database_errors(operation="поиск мероприятий в каталоге"),
            self._engine_manager.session() as session,
        ):
            total_count = int((await session.execute(count_statement)).scalar_one())
            if total_count == 0 or offset_value >= total_count:
                return EventSearchPage(
                    items=(),
                    total_count=total_count,
                    page=search_filter.page,
                    page_size=search_filter.page_size,
                    has_more=False,
                )

            rows = (await session.execute(items_statement)).scalars().unique().all()
            items = tuple(map_event_from_row(row) for row in rows)
            has_more = (offset_value + len(items)) < total_count
            return EventSearchPage(
                items=items,
                total_count=total_count,
                page=search_filter.page,
                page_size=search_filter.page_size,
                has_more=has_more,
            )

    async def count_events(self, *, search_filter: EventSearchFilter) -> int:
        """Подсчитывает количество опубликованных мероприятий без загрузки карточек.

        Arguments:
            search_filter: Набор критериев фильтрации.

        Returns:
            Количество подходящих опубликованных мероприятий.

        Raises:
            InvalidSearchFilterError: При некорректных параметрах фильтрации.
        """
        _validate_search_filter(search_filter)
        conditions, _ = _build_search_conditions(search_filter, statuses=(EventStatus.PUBLISHED,))
        count_statement = (
            select(func.count(EventRow.identifier))
            .select_from(EventRow)
            .join(EventRow.venue)
            .join(VenueRow.city)
            .where(*conditions)
        )

        async with (
            handle_database_errors(operation="подсчёт мероприятий по фильтру"),
            self._engine_manager.session() as session,
        ):
            return int((await session.execute(count_statement)).scalar_one())

    async def get_event_by_identifier(self, *, event_identifier: UUID, include_non_published: bool = True) -> Event:
        """Возвращает полную карточку мероприятия по уникальному идентификатору.

        Arguments:
            event_identifier: Уникальный идентификатор мероприятия.
            include_non_published: Разрешать ли возврат черновиков и отменённых карточек.

        Returns:
            Полная доменная сущность мероприятия с перечнем подтверждённых источников.

        Raises:
            EventNotFoundError: Если мероприятие с указанным идентификатором не найдено.
        """
        statement = select(EventRow).where(EventRow.identifier == event_identifier).options(*_event_load_options())
        if not include_non_published:
            statement = statement.where(EventRow.status == EventStatus.PUBLISHED.value)

        async with (
            handle_database_errors(operation="чтение карточки мероприятия по идентификатору"),
            self._engine_manager.session() as session,
        ):
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                raise EventNotFoundError(
                    f"Мероприятие с идентификатором {event_identifier} не найдено.", event_identifier=event_identifier
                )
            return map_event_from_row(row)

    async def find_similar_events(
        self,
        *,
        event_identifier: UUID,
        city: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        near_center: bool = False,
        limit: int = 5,
    ) -> tuple[Event, ...]:
        """Подбирает похожие опубликованные мероприятия по категории, тегам и близости.

        Arguments:
            event_identifier: Идентификатор исходного мероприятия.
            city: Необязательное ограничение по городу.
            date_from: Нижняя граница времени начала похожих мероприятий.
            date_to: Верхняя граница времени начала похожих мероприятий.
            near_center: Приоритет мероприятий ближе к центру города.
            limit: Максимальное число возвращаемых карточек.

        Returns:
            Кортеж похожих опубликованных мероприятий.

        Raises:
            EventNotFoundError: Если исходное мероприятие не найдено.
        """
        validated_limit = validate_positive_limit(limit, parameter_name="limit", maximum=50)

        async with (
            handle_database_errors(operation="подбор похожих мероприятий"),
            self._engine_manager.session() as session,
        ):
            source_row = (
                await session.execute(
                    select(EventRow).where(EventRow.identifier == event_identifier).options(*_event_load_options())
                )
            ).scalar_one_or_none()
            if source_row is None:
                raise EventNotFoundError(
                    f"Исходное мероприятие {event_identifier} не найдено при подборе похожих.",
                    event_identifier=event_identifier,
                )

            source_tag_ids = [tag.identifier for tag in source_row.tags]
            conditions: list[ColumnElement[bool]] = [
                EventRow.identifier != event_identifier,
                EventRow.status == EventStatus.PUBLISHED.value,
            ]
            if city is not None:
                city_condition = build_city_match_condition(city)
                if city_condition is not None:
                    conditions.append(city_condition)
            if date_from is not None:
                conditions.append(EventRow.date_start >= date_from)
            if date_to is not None:
                conditions.append(EventRow.date_start <= date_to)

            shared_tag_score = (
                select(func.count(EventTagRow.tag_identifier))
                .where(
                    (EventTagRow.event_identifier == EventRow.identifier)
                    & (EventTagRow.tag_identifier.in_(source_tag_ids))
                )
                .scalar_subquery()
                if source_tag_ids
                else 0
            )
            similarity_score = (
                case((EventRow.category_code == source_row.category_code, 10.0), else_=0.0)
                + shared_tag_score * 3.0
                + case((VenueRow.city_identifier == source_row.venue.city_identifier, 2.0), else_=0.0)
                + func.similarity(EventRow.normalized_title, source_row.normalized_title) * 2.0
            )

            order_clauses: list[_OrderByClause] = [similarity_score.desc()]
            if near_center:
                distance_expr = _build_distance_to_center_expression()
                order_clauses.append(distance_expr.asc().nullslast())
            order_clauses.extend([EventRow.date_start.asc(), EventRow.identifier.asc()])

            statement = (
                select(EventRow)
                .join(EventRow.venue)
                .join(VenueRow.city)
                .where(*conditions)
                .options(*_event_load_options())
                .order_by(*order_clauses)
                .limit(validated_limit)
            )
            rows = (await session.execute(statement)).scalars().unique().all()
            return tuple(map_event_from_row(row) for row in rows)

    async def find_by_source_hash(self, *, source_type: SourceType, source_hash: str) -> Event | None:
        """Ищет существующую карточку по типу источника и контрольной сумме записи.

        Arguments:
            source_type: Тип источника поступления.
            source_hash: Хэш исходной записи для идемпотентности.

        Returns:
            Найденная карточка мероприятия или `None`.
        """
        cleaned_hash = source_hash.strip()
        if not cleaned_hash:
            return None

        statement = (
            select(EventRow)
            .join(EventSourceRow, EventSourceRow.event_identifier == EventRow.identifier)
            .where((EventSourceRow.source_type == source_type.value) & (EventSourceRow.source_hash == cleaned_hash))
            .options(*_event_load_options())
        )
        async with (
            handle_database_errors(operation="поиск мероприятия по хэшу источника"),
            self._engine_manager.session() as session,
        ):
            row = (await session.execute(statement)).scalar_one_or_none()
            return map_event_from_row(row) if row is not None else None

    async def save_event(self, *, event: Event) -> Event:
        """Идемпотентно создаёт или обновляет карточку мероприятия, её статус и список источников.

        Arguments:
            event: Сохраняемая доменная карточка мероприятия.

        Returns:
            Актуальная сохранённая карточка мероприятия.

        Raises:
            InvalidEventDataError: При пустых обязательных полях или непарных координатах.
            DuplicateEventConflictError: При конфликте уникальности `(source_type, source_hash)`.
        """
        cleaned_title = normalize_optional_text(event.title)
        if cleaned_title is None:
            raise InvalidEventDataError("Название мероприятия не может быть пустым.", field_name="title")
        if (event.latitude is None) != (event.longitude is None):
            raise InvalidEventDataError(
                "Широта и долгота площадки должны задаваться одновременно.", field_name="coordinates"
            )
        cleaned_source_url = normalize_optional_text(event.source_url)
        if cleaned_source_url is None:
            raise InvalidEventDataError("Ссылка на первоисточник не может быть пустой.", field_name="source_url")
        cleaned_source_hash = normalize_optional_text(event.source_hash)
        if cleaned_source_hash is None:
            raise InvalidEventDataError("Хэш первоисточника не может быть пустым.", field_name="source_hash")
        cleaned_dedup_key = (normalize_optional_text(event.deduplication_key) or "").lower()
        if not cleaned_dedup_key:
            raise InvalidEventDataError("Ключ дедупликации не может быть пустым.", field_name="deduplication_key")

        effective_normalized_title = normalize_title(event.normalized_title or cleaned_title)
        cleaned_language = normalize_optional_text(event.language)
        normalized_language = cleaned_language.lower() if cleaned_language is not None else None
        cleaned_org_link = normalize_optional_text(event.organization_link)
        cleaned_description = event.description.strip()
        target_sources = _prepare_normalized_sources(event)

        async with (
            handle_database_errors(
                operation="сохранение карточки мероприятия",
                duplicate_conflict_message="Конфликт уникальности источника при сохранении мероприятия.",
            ),
            self._engine_manager.session() as session,
            session.begin(),
        ):
            # Предварительная проверка кросс-карточного конфликта источников
            for source_ref, _ in target_sources:
                conflicting_owner = (
                    await session.execute(
                        select(EventSourceRow.event_identifier).where(
                            (EventSourceRow.source_type == source_ref.source_type.value)
                            & (EventSourceRow.source_hash == source_ref.source_hash)
                            & (EventSourceRow.event_identifier != event.identifier)
                        )
                    )
                ).scalar_one_or_none()
                if conflicting_owner is not None:
                    raise DuplicateEventConflictError(
                        f"Источник ({source_ref.source_type.value}, {source_ref.source_hash}) "
                        "уже привязан к другому мероприятию.",
                        deduplication_key=cleaned_dedup_key,
                        existing_event_identifier=conflicting_owner,
                    )

            existing_row = (
                await session.execute(
                    select(EventRow).where(EventRow.identifier == event.identifier).options(*_event_load_options())
                )
            ).scalar_one_or_none()

            city_row = await resolve_or_create_city(session, city_name=event.city)
            same_venue_on_update = (
                existing_row is not None
                and existing_row.venue.city_identifier == city_row.identifier
                and existing_row.venue.normalized_name == normalize_text_for_matching(event.venue)
            )
            venue_row = await _resolve_or_create_venue(
                session,
                city_row=city_row,
                venue_name=event.venue,
                address=event.address,
                latitude=event.latitude,
                longitude=event.longitude,
                allow_overwrite_metadata=same_venue_on_update,
            )
            tag_rows = await _sync_tags(session, raw_tags=event.tags)

            if existing_row is None:
                event_row = EventRow(
                    identifier=event.identifier,
                    title=cleaned_title,
                    normalized_title=effective_normalized_title,
                    category_code=event.category.value,
                    venue_identifier=venue_row.identifier,
                    date_start=event.date_start,
                    date_end=event.date_end,
                    duration_minutes=event.duration_minutes,
                    description=cleaned_description,
                    price_minimum=event.price_minimum,
                    price_maximum=event.price_maximum,
                    is_free=event.is_free,
                    age_limit=event.age_limit,
                    language=normalized_language,
                    organization_link=cleaned_org_link,
                    confidence=event.confidence,
                    status=event.status.value,
                    deduplication_key=cleaned_dedup_key,
                    created_at=event.created_at,
                    updated_at=event.updated_at,
                )
                event_row.sources = [
                    EventSourceRow(
                        event_identifier=event.identifier,
                        source_name=source_ref.source_name,
                        source_type=source_ref.source_type.value,
                        source_url=source_ref.source_url,
                        source_hash=source_ref.source_hash,
                        verified_at=source_ref.verified_at,
                        is_primary=is_primary,
                    )
                    for source_ref, is_primary in target_sources
                ]
                event_row.tags = tag_rows
                session.add(event_row)
            else:
                existing_row.title = cleaned_title
                existing_row.normalized_title = effective_normalized_title
                existing_row.category_code = event.category.value
                existing_row.venue_identifier = venue_row.identifier
                existing_row.date_start = event.date_start
                existing_row.date_end = event.date_end
                existing_row.duration_minutes = event.duration_minutes
                existing_row.description = cleaned_description
                existing_row.price_minimum = event.price_minimum
                existing_row.price_maximum = event.price_maximum
                existing_row.is_free = event.is_free
                existing_row.age_limit = event.age_limit
                existing_row.language = normalized_language
                existing_row.organization_link = cleaned_org_link
                existing_row.confidence = event.confidence
                existing_row.status = event.status.value
                existing_row.deduplication_key = cleaned_dedup_key
                existing_row.updated_at = event.updated_at
                existing_row.tags = tag_rows

                # Сброс флага основного источника предотвращает конфликт частичного уникального индекса при перестановке
                await session.execute(
                    update(EventSourceRow)
                    .where(EventSourceRow.event_identifier == event.identifier)
                    .values(is_primary=False)
                )
                await session.flush()

                existing_sources_by_key = {
                    (SourceType(source.source_type), source.source_hash): source for source in existing_row.sources
                }
                desired_keys = {(source_ref.source_type, source_ref.source_hash) for source_ref, _ in target_sources}

                for stale_source in list(existing_row.sources):
                    if (SourceType(stale_source.source_type), stale_source.source_hash) not in desired_keys:
                        existing_row.sources.remove(stale_source)
                await session.flush()

                for source_ref, is_primary in target_sources:
                    key = (source_ref.source_type, source_ref.source_hash)
                    matched_source = existing_sources_by_key.get(key)
                    if matched_source is not None:
                        matched_source.source_name = source_ref.source_name
                        matched_source.source_url = source_ref.source_url
                        matched_source.verified_at = source_ref.verified_at
                        matched_source.is_primary = is_primary
                    else:
                        existing_row.sources.append(
                            EventSourceRow(
                                event_identifier=event.identifier,
                                source_name=source_ref.source_name,
                                source_type=source_ref.source_type.value,
                                source_url=source_ref.source_url,
                                source_hash=source_ref.source_hash,
                                verified_at=source_ref.verified_at,
                                is_primary=is_primary,
                            )
                        )

            await session.flush()
            saved_row = (
                await session.execute(
                    select(EventRow).where(EventRow.identifier == event.identifier).options(*_event_load_options())
                )
            ).scalar_one()
            return map_event_from_row(saved_row)

    async def list_cities(self) -> tuple[CityReference, ...]:
        """Возвращает нормализованный справочник городов Республики Башкортостан.

        Returns:
            Кортеж записей справочника городов.
        """
        statement = select(CityRow).options(selectinload(CityRow.aliases)).order_by(CityRow.name.asc())
        async with (
            handle_database_errors(operation="чтение справочника городов"),
            self._engine_manager.session() as session,
        ):
            rows = (await session.execute(statement)).scalars().unique().all()
            return tuple(map_city_from_row(row) for row in rows)

    async def list_venues(self, *, city: str | None = None) -> tuple[VenueReference, ...]:
        """Возвращает справочник культурных площадок с фильтрацией по городу.

        Arguments:
            city: Необязательное название города для отбора площадок.

        Returns:
            Кортеж записей справочника площадок.
        """
        statement = (
            select(VenueRow).join(VenueRow.city).options(selectinload(VenueRow.city), selectinload(VenueRow.aliases))
        )
        if city is not None:
            city_condition = build_city_match_condition(city)
            if city_condition is not None:
                statement = statement.where(city_condition)
        statement = statement.order_by(VenueRow.name.asc())

        async with (
            handle_database_errors(operation="чтение справочника площадок"),
            self._engine_manager.session() as session,
        ):
            rows = (await session.execute(statement)).scalars().unique().all()
            return tuple(map_venue_from_row(row) for row in rows)

    async def get_catalog_fingerprint(self) -> str:
        """Возвращает стабильный отпечаток актуального состояния каталога и справочников.

        Returns:
            Строковый токен версии каталога и справочников для заголовка `ETag`.
        """
        statement = select(
            func.count(EventRow.identifier).label("events_count"),
            func.coalesce(func.max(EventRow.updated_at), _EPOCH_UTC).label("events_updated_at"),
            select(func.count(EventSourceRow.identifier)).scalar_subquery().label("sources_count"),
            select(func.count(CityRow.identifier)).scalar_subquery().label("cities_count"),
            select(func.count(VenueRow.identifier)).scalar_subquery().label("venues_count"),
        )
        async with (
            handle_database_errors(operation="вычисление отпечатка состояния каталога"),
            self._engine_manager.session() as session,
        ):
            row = (await session.execute(statement)).one()
            raw_state = (
                f"{row.events_count}:{row.events_updated_at.isoformat()}:"
                f"{row.sources_count}:{row.cities_count}:{row.venues_count}"
            )
            digest = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()[:16]
            return f"catalog-{digest}"
