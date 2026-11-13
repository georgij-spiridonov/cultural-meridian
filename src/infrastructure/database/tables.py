"""ORM-модели и нормализованная реляционная схема PostgreSQL 17."""

from datetime import date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as POSTGRESQL_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

__all__ = [
    "AgentTraceGroundedEventRow",
    "AgentTraceRow",
    "AgentTraceToolInvocationRow",
    "AuditLogRow",
    "CategoryRow",
    "CityAliasRow",
    "CityRow",
    "DatabaseBase",
    "DialogMessageRow",
    "DialogSessionRow",
    "EventRow",
    "EventSourceRow",
    "EventTagRow",
    "FeedbackRequestRow",
    "LanguageModelUsageDailyRow",
    "ModerationQueueRow",
    "ModeratorRow",
    "PendingActionRow",
    "RawEventRow",
    "SubscriptionRow",
    "TagRow",
    "VenueAliasRow",
    "VenueRow",
]

_CONSTRAINT_NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class DatabaseBase(DeclarativeBase):
    """Базовый декларативный класс для всех ORM-таблиц базы данных PostgreSQL 17."""

    metadata = MetaData(naming_convention=_CONSTRAINT_NAMING_CONVENTION)


class CategoryRow(DatabaseBase):
    """Справочник категорий культурных мероприятий (латинский код и русское название)."""

    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint("btrim(code) <> '' AND code = lower(btrim(code))", name="code_normalized"),
        CheckConstraint("btrim(title_russian) <> ''", name="title_russian_non_empty"),
        CheckConstraint("sort_order >= 0", name="sort_order_non_negative"),
    )

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    title_russian: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))


class CityRow(DatabaseBase):
    """Канонический справочник городов и населённых пунктов Республики Башкортостан."""

    __tablename__ = "cities"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="name_non_empty"),
        CheckConstraint(
            "btrim(normalized_name) <> '' AND normalized_name = lower(btrim(normalized_name))",
            name="normalized_name_clean",
        ),
        CheckConstraint("(center_latitude IS NULL) = (center_longitude IS NULL)", name="center_coordinates_paired"),
        CheckConstraint(
            "center_latitude IS NULL OR (center_latitude >= -90.0 AND center_latitude <= 90.0)",
            name="center_latitude_range",
        ),
        CheckConstraint(
            "center_longitude IS NULL OR (center_longitude >= -180.0 AND center_longitude <= 180.0)",
            name="center_longitude_range",
        ),
        Index(
            "ix_cities_normalized_name_trgm",
            "normalized_name",
            postgresql_using="gin",
            postgresql_ops={"normalized_name": "gin_trgm_ops"},
        ),
    )

    identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    normalized_name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    center_latitude: Mapped[float | None] = mapped_column(Float(precision=53), nullable=True)
    center_longitude: Mapped[float | None] = mapped_column(Float(precision=53), nullable=True)

    aliases: Mapped[list[CityAliasRow]] = relationship(
        back_populates="city", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )
    venues: Mapped[list[VenueRow]] = relationship(back_populates="city", lazy="raise")


class CityAliasRow(DatabaseBase):
    """Нормализованная таблица словоформ и распространённых опечаток названий городов."""

    __tablename__ = "city_aliases"
    __table_args__ = (
        CheckConstraint("btrim(alias) <> ''", name="alias_non_empty"),
        CheckConstraint(
            "btrim(normalized_alias) <> '' AND normalized_alias = lower(btrim(normalized_alias))",
            name="normalized_alias_clean",
        ),
        Index("ix_city_aliases_city_identifier", "city_identifier"),
        Index(
            "ix_city_aliases_normalized_alias_trgm",
            "normalized_alias",
            postgresql_using="gin",
            postgresql_ops={"normalized_alias": "gin_trgm_ops"},
        ),
    )

    normalized_alias: Mapped[str] = mapped_column(String(120), primary_key=True)
    city_identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("cities.identifier", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(120), nullable=False)

    city: Mapped[CityRow] = relationship(back_populates="aliases", lazy="raise")


class VenueRow(DatabaseBase):
    """Справочник площадок проведения культурных мероприятий с привязкой к городу."""

    __tablename__ = "venues"
    __table_args__ = (
        UniqueConstraint("city_identifier", "normalized_name", name="city_normalized_name"),
        CheckConstraint("btrim(name) <> ''", name="name_non_empty"),
        CheckConstraint(
            "btrim(normalized_name) <> '' AND normalized_name = lower(btrim(normalized_name))",
            name="normalized_name_clean",
        ),
        CheckConstraint("(latitude IS NULL) = (longitude IS NULL)", name="coordinates_paired"),
        CheckConstraint("latitude IS NULL OR (latitude >= -90.0 AND latitude <= 90.0)", name="latitude_range"),
        CheckConstraint("longitude IS NULL OR (longitude >= -180.0 AND longitude <= 180.0)", name="longitude_range"),
        Index("ix_venues_city_identifier", "city_identifier"),
        Index(
            "ix_venues_normalized_name_trgm",
            "normalized_name",
            postgresql_using="gin",
            postgresql_ops={"normalized_name": "gin_trgm_ops"},
        ),
        Index(
            "ix_venues_coordinates_present",
            "city_identifier",
            postgresql_where=text("latitude IS NOT NULL AND longitude IS NOT NULL"),
        ),
    )

    identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    city_identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("cities.identifier", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str] = mapped_column(String(500), nullable=False, server_default=text("''"))
    latitude: Mapped[float | None] = mapped_column(Float(precision=53), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float(precision=53), nullable=True)

    city: Mapped[CityRow] = relationship(back_populates="venues", lazy="raise")
    aliases: Mapped[list[VenueAliasRow]] = relationship(
        back_populates="venue", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )
    events: Mapped[list[EventRow]] = relationship(back_populates="venue", lazy="raise")


class VenueAliasRow(DatabaseBase):
    """Нормализованная таблица альтернативных написаний и сокращений площадок."""

    __tablename__ = "venue_aliases"
    __table_args__ = (
        UniqueConstraint("venue_identifier", "normalized_alias", name="venue_normalized_alias"),
        CheckConstraint("btrim(alias) <> ''", name="alias_non_empty"),
        CheckConstraint(
            "btrim(normalized_alias) <> '' AND normalized_alias = lower(btrim(normalized_alias))",
            name="normalized_alias_clean",
        ),
        Index("ix_venue_aliases_venue_identifier", "venue_identifier"),
        Index(
            "ix_venue_aliases_normalized_alias_trgm",
            "normalized_alias",
            postgresql_using="gin",
            postgresql_ops={"normalized_alias": "gin_trgm_ops"},
        ),
    )

    identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    venue_identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("venues.identifier", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(255), nullable=False)

    venue: Mapped[VenueRow] = relationship(back_populates="aliases", lazy="raise")


class TagRow(DatabaseBase):
    """Справочник нормализованных тематических меток мероприятий."""

    __tablename__ = "tags"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="name_non_empty"),
        CheckConstraint(
            "btrim(normalized_name) <> '' AND normalized_name = lower(btrim(normalized_name))",
            name="normalized_name_clean",
        ),
    )

    identifier: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)


class EventRow(DatabaseBase):
    """Нормализованная карточка культурного мероприятия без дублирования площадки и источников."""

    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("btrim(title) <> ''", name="title_non_empty"),
        CheckConstraint(
            "btrim(normalized_title) <> '' AND normalized_title = lower(btrim(normalized_title))",
            name="normalized_title_clean",
        ),
        CheckConstraint("date_end IS NULL OR date_end >= date_start", name="date_range_valid"),
        CheckConstraint("duration_minutes IS NULL OR duration_minutes > 0", name="duration_positive"),
        CheckConstraint("price_minimum IS NULL OR price_minimum >= 0", name="price_minimum_non_negative"),
        CheckConstraint("price_maximum IS NULL OR price_maximum >= 0", name="price_maximum_non_negative"),
        CheckConstraint(
            "price_minimum IS NULL OR price_maximum IS NULL OR price_maximum >= price_minimum", name="price_range_valid"
        ),
        CheckConstraint(
            "NOT is_free OR ((price_minimum IS NULL OR price_minimum = 0) AND "
            "(price_maximum IS NULL OR price_maximum = 0))",
            name="free_price_consistency",
        ),
        CheckConstraint("age_limit IN (0, 6, 12, 16, 18)", name="age_limit_allowed"),
        CheckConstraint(
            "language IS NULL OR (btrim(language) <> '' AND language = lower(btrim(language)))", name="lang"
        ),
        CheckConstraint(
            "organization_link IS NULL OR btrim(organization_link) <> ''", name="organization_link_non_empty"
        ),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="confidence_range"),
        CheckConstraint("status IN ('published', 'draft', 'canceled')", name="status_allowed"),
        CheckConstraint(
            "btrim(deduplication_key) <> '' AND deduplication_key = lower(btrim(deduplication_key))",
            name="deduplication_key_clean",
        ),
        Index(
            "ix_events_normalized_title_trgm",
            "normalized_title",
            postgresql_using="gin",
            postgresql_ops={"normalized_title": "gin_trgm_ops"},
        ),
        Index("ix_events_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_events_status_date_start", "status", "date_start"),
        Index(
            "ix_events_published_filters",
            "venue_identifier",
            "category_code",
            "date_start",
            "price_minimum",
            "age_limit",
            postgresql_where=text("status = 'published'"),
        ),
        Index("ix_events_deduplication_key", "deduplication_key"),
        Index("ix_events_created_at_desc", text("created_at DESC")),
        Index("ix_events_updated_at_desc", text("updated_at DESC")),
    )

    identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(500), nullable=False)
    category_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("categories.code", ondelete="RESTRICT"), nullable=False
    )
    venue_identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("venues.identifier", ondelete="RESTRICT"), nullable=False
    )
    date_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    date_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    price_minimum: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_maximum: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_free: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    age_limit: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    organization_link: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    confidence: Mapped[float] = mapped_column(Float(precision=53), nullable=False, server_default=text("1.0"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'published'"))
    deduplication_key: Mapped[str] = mapped_column(String(128), nullable=False)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('russian', title || ' ' || description)", persisted=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    category: Mapped[CategoryRow] = relationship(lazy="raise")
    venue: Mapped[VenueRow] = relationship(back_populates="events", lazy="raise")
    sources: Mapped[list[EventSourceRow]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="EventSourceRow.is_primary.desc(), EventSourceRow.created_at.asc()",
        lazy="raise",
    )
    tags: Mapped[list[TagRow]] = relationship(
        secondary="event_tags", order_by="TagRow.normalized_name.asc()", lazy="raise"
    )


class EventTagRow(DatabaseBase):
    """Связь «многие-ко-многим» между карточками мероприятий и справочником меток."""

    __tablename__ = "event_tags"
    __table_args__ = (Index("ix_event_tags_tag_event", "tag_identifier", "event_identifier"),)

    event_identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("events.identifier", ondelete="CASCADE"), primary_key=True
    )
    tag_identifier: Mapped[int] = mapped_column(
        Integer, ForeignKey("tags.identifier", ondelete="RESTRICT"), primary_key=True
    )


class EventSourceRow(DatabaseBase):
    """Подтверждённые источники мероприятия (включая единый основной источник)."""

    __tablename__ = "event_sources"
    __table_args__ = (
        UniqueConstraint("source_type", "source_hash", name="source_type_hash"),
        CheckConstraint("btrim(source_name) <> ''", name="source_name_non_empty"),
        CheckConstraint(
            "source_type IN ('manual', 'import_file', 'rss', 'api', 'webpage')", name="source_type_allowed"
        ),
        CheckConstraint("btrim(source_url) <> ''", name="source_url_non_empty"),
        CheckConstraint("btrim(source_hash) <> ''", name="source_hash_non_empty"),
        Index(
            "uq_event_sources_primary_per_event",
            "event_identifier",
            unique=True,
            postgresql_where=text("is_primary = true"),
        ),
        Index("ix_event_sources_event_identifier", "event_identifier"),
    )

    identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    event_identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("events.identifier", ondelete="CASCADE"), nullable=False
    )
    source_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    event: Mapped[EventRow] = relationship(back_populates="sources", lazy="raise")


class RawEventRow(DatabaseBase):
    """Необработанные записи внешних источников для повторного прогона нормализации."""

    __tablename__ = "raw_events"
    __table_args__ = (
        UniqueConstraint("source_name", "source_hash", name="source_name_hash"),
        CheckConstraint("btrim(source_name) <> ''", name="source_name_non_empty"),
        CheckConstraint(
            "source_type IN ('manual', 'import_file', 'rss', 'api', 'webpage')", name="source_type_allowed"
        ),
        CheckConstraint("btrim(external_identifier) <> ''", name="external_identifier_non_empty"),
        CheckConstraint("btrim(source_url) <> ''", name="source_url_non_empty"),
        CheckConstraint("btrim(source_hash) <> ''", name="source_hash_non_empty"),
        CheckConstraint("jsonb_typeof(raw_payload) = 'object'", name="raw_payload_json_object"),
        CheckConstraint("normalization_status IN ('pending', 'normalized', 'failed')", name="status_allowed"),
        CheckConstraint("error_message IS NULL OR btrim(error_message) <> ''", name="error_message_non_empty"),
        Index("ix_raw_events_status_fetched_at", "normalization_status", "fetched_at"),
        Index("ix_raw_events_source_status_fetched_at", "source_name", "normalization_status", "fetched_at"),
    )

    identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    external_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    normalization_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ModeratorRow(DatabaseBase):
    """Учётная запись модератора административного контура `/admin`."""

    __tablename__ = "moderators"
    __table_args__ = (
        CheckConstraint(
            "username = lower(btrim(username)) AND username ~ '^[a-z0-9_.-]{3,64}$'", name="username_valid"
        ),
        CheckConstraint("btrim(display_name) <> ''", name="display_name_non_empty"),
        CheckConstraint("btrim(password_hash) <> ''", name="password_hash_non_empty"),
        CheckConstraint("last_login_at IS NULL OR last_login_at >= created_at", name="last_login_after_creation"),
    )

    identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModerationQueueRow(DatabaseBase):
    """Очередь модерации спорных и потенциально вредоносных карточек."""

    __tablename__ = "moderation_queue"
    __table_args__ = (
        CheckConstraint("btrim(reason) <> ''", name="reason_non_empty"),
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="status_allowed"),
        CheckConstraint(
            "jsonb_typeof(matched_injection_patterns) = 'array'", name="matched_injection_patterns_json_array"
        ),
        CheckConstraint(
            "(status = 'pending' AND resolved_at IS NULL AND resolved_by IS NULL AND "
            "resolved_by_moderator_identifier IS NULL) OR "
            "(status IN ('approved', 'rejected') AND resolved_at IS NOT NULL AND "
            "resolved_by IS NOT NULL AND btrim(resolved_by) <> '')",
            name="resolution_consistency",
        ),
        Index("ix_moderation_queue_status_created_at", "status", "created_at"),
        Index("ix_moderation_queue_event_identifier", "event_identifier"),
    )

    identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    event_identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("events.identifier", ondelete="CASCADE"), nullable=False
    )
    raw_event_identifier: Mapped[UUID | None] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("raw_events.identifier", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    has_suspicious_instructions: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    matched_injection_patterns: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_by_moderator_identifier: Mapped[UUID | None] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("moderators.identifier", ondelete="SET NULL"), nullable=True
    )


class AuditLogRow(DatabaseBase):
    """Неизменяемый журнал изменений каталога мероприятий и решений модерации."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint("btrim(actor) <> ''", name="actor_non_empty"),
        CheckConstraint("btrim(action) <> ''", name="action_non_empty"),
        CheckConstraint("btrim(entity_type) <> ''", name="entity_type_non_empty"),
        CheckConstraint("btrim(entity_identifier) <> ''", name="entity_identifier_non_empty"),
        CheckConstraint("jsonb_typeof(changes) = 'object'", name="changes_json_object"),
        Index("ix_audit_logs_entity_created_desc", "entity_type", "entity_identifier", text("created_at DESC")),
        Index("ix_audit_logs_created_at_desc", text("created_at DESC")),
        Index(
            "ix_audit_logs_moderator_identifier",
            "moderator_identifier",
            postgresql_where=text("moderator_identifier IS NOT NULL"),
        ),
    )

    identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    moderator_identifier: Mapped[UUID | None] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("moderators.identifier", ondelete="SET NULL"), nullable=True
    )
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    changes: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class SubscriptionRow(DatabaseBase):
    """Подписка пользователя на уведомления по городу и категории с минимумом ПДн."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "subscriber_identifier",
            "city_identifier",
            "category_code",
            name="unique_filter",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("channel IN ('web', 'telegram', 'evaluation')", name="channel_allowed"),
        CheckConstraint("btrim(subscriber_identifier) <> ''", name="subscriber_non_empty"),
        Index("ix_subscriptions_subscriber", "channel", "subscriber_identifier", text("created_at DESC")),
        Index("ix_subscriptions_matching", "city_identifier", "category_code"),
    )

    identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    subscriber_identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    city_identifier: Mapped[UUID | None] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("cities.identifier", ondelete="RESTRICT"), nullable=True
    )
    category_code: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("categories.code", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    city: Mapped[CityRow | None] = relationship(lazy="raise")


class FeedbackRequestRow(DatabaseBase):
    """Обращения пользователей и заявки организаторов на добавление или уточнение афиши."""

    __tablename__ = "feedback_requests"
    __table_args__ = (
        CheckConstraint("channel IN ('web', 'telegram', 'evaluation')", name="channel_allowed"),
        CheckConstraint("btrim(message_text) <> ''", name="message_text_non_empty"),
        CheckConstraint("contact_hint IS NULL OR btrim(contact_hint) <> ''", name="contact_hint_non_empty"),
        CheckConstraint("status IN ('new', 'reviewed')", name="status_allowed"),
        Index("ix_feedback_requests_status_created_desc", "status", text("created_at DESC")),
        Index(
            "ix_feedback_requests_event_identifier",
            "event_identifier",
            postgresql_where=text("event_identifier IS NOT NULL"),
        ),
    )

    identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    event_identifier: Mapped[UUID | None] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("events.identifier", ondelete="SET NULL"), nullable=True
    )
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    contact_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'new'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class DialogSessionRow(DatabaseBase):
    """Персистентная диалоговая сессия в веб-чате, Telegram или тестовом прогоне."""

    __tablename__ = "dialog_sessions"
    __table_args__ = (
        CheckConstraint("btrim(session_identifier) <> ''", name="session_identifier_non_empty"),
        CheckConstraint("channel IN ('web', 'telegram', 'evaluation')", name="channel_allowed"),
        CheckConstraint("btrim(subscriber_identifier) <> ''", name="subscriber_identifier_non_empty"),
    )

    session_identifier: Mapped[str] = mapped_column(String(128), primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    subscriber_identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class DialogMessageRow(DatabaseBase):
    """Реплики пользователя и ассистента в хронологическом порядке диалоговой сессии."""

    __tablename__ = "dialog_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="role_allowed"),
        CheckConstraint("btrim(content) <> ''", name="content_non_empty"),
        Index(
            "ix_dialog_messages_session_order", "session_identifier", text("created_at DESC"), text("identifier DESC")
        ),
    )

    identifier: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_identifier: Mapped[str] = mapped_column(
        String(128), ForeignKey("dialog_sessions.session_identifier", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class PendingActionRow(DatabaseBase):
    """Ожидающее подтверждения изменяющее действие диалогового контура."""

    __tablename__ = "pending_actions"
    __table_args__ = (
        CheckConstraint("action_type IN ('subscribe', 'save_feedback')", name="action_type_allowed"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_json_object"),
        CheckConstraint("btrim(confirmation_prompt) <> ''", name="confirmation_prompt_non_empty"),
        CheckConstraint("status IN ('pending', 'consumed', 'canceled', 'expired')", name="status_allowed"),
        CheckConstraint("expires_at > created_at", name="expires_after_creation"),
        Index(
            "uq_pending_actions_active_session",
            "session_identifier",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_pending_actions_expires_active", "expires_at", postgresql_where=text("status = 'pending'")),
    )

    action_identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    session_identifier: Mapped[str] = mapped_column(
        String(128), ForeignKey("dialog_sessions.session_identifier", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    confirmation_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    session: Mapped[DialogSessionRow] = relationship(lazy="raise")


class AgentTraceRow(DatabaseBase):
    """Полная трасса ответа ИИ-агента или детерминированного резервного поиска."""

    __tablename__ = "agent_traces"
    __table_args__ = (
        CheckConstraint("btrim(request_identifier) <> ''", name="request_identifier_non_empty"),
        CheckConstraint("btrim(session_identifier) <> ''", name="session_identifier_non_empty"),
        CheckConstraint("channel IN ('web', 'telegram', 'evaluation')", name="channel_allowed"),
        CheckConstraint("latency_milliseconds >= 0", name="latency_non_negative"),
        CheckConstraint("tokens_input >= 0", name="tokens_input_non_negative"),
        CheckConstraint("tokens_output >= 0", name="tokens_output_non_negative"),
        CheckConstraint("cost_rubles >= 0", name="cost_rubles_non_negative"),
        Index("ix_agent_traces_session_created_desc", "session_identifier", text("created_at DESC")),
        Index("ix_agent_traces_created_at_desc", text("created_at DESC")),
        Index("ix_agent_traces_request_identifier", "request_identifier"),
    )

    trace_identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    request_identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    session_identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    user_query: Mapped[str] = mapped_column(Text, nullable=False)
    plan_summary: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    latency_milliseconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tokens_input: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tokens_output: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost_rubles: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, server_default=text("0"))
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, server_default=text("''"))
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    tool_invocations: Mapped[list[AgentTraceToolInvocationRow]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AgentTraceToolInvocationRow.sequence_number.asc()",
        lazy="raise",
    )
    grounded_events: Mapped[list[AgentTraceGroundedEventRow]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )


class AgentTraceToolInvocationRow(DatabaseBase):
    """Нормализованная запись вызова инструмента в рамках трассы ответа ИИ-агента."""

    __tablename__ = "agent_trace_tool_invocations"
    __table_args__ = (
        CheckConstraint("sequence_number >= 0", name="sequence_number_non_negative"),
        CheckConstraint("btrim(tool_name) <> ''", name="tool_name_non_empty"),
        CheckConstraint("jsonb_typeof(arguments) = 'object'", name="arguments_json_object"),
        CheckConstraint("error_message IS NULL OR btrim(error_message) <> ''", name="error_message_non_empty"),
    )

    trace_identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("agent_traces.trace_identifier", ondelete="CASCADE"), primary_key=True
    )
    sequence_number: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    returned_event_identifiers: Mapped[list[UUID]] = mapped_column(
        ARRAY(POSTGRESQL_UUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentTraceGroundedEventRow(DatabaseBase):
    """Связь трассы ответа с подтверждёнными карточками каталога (0 галлюцинаций)."""

    __tablename__ = "agent_trace_grounded_events"
    __table_args__ = (Index("ix_agent_trace_grounded_events_event_identifier", "event_identifier"),)

    trace_identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("agent_traces.trace_identifier", ondelete="CASCADE"), primary_key=True
    )
    event_identifier: Mapped[UUID] = mapped_column(
        POSTGRESQL_UUID(as_uuid=True), ForeignKey("events.identifier", ondelete="CASCADE"), primary_key=True
    )


class LanguageModelUsageDailyRow(DatabaseBase):
    """Суточный агрегат токенов и расходов на языковую модель на одного пользователя."""

    __tablename__ = "llm_usage_daily"
    __table_args__ = (
        CheckConstraint("btrim(subscriber_identifier) <> ''", name="subscriber_non_empty"),
        CheckConstraint("tokens_input >= 0", name="tokens_input_non_negative"),
        CheckConstraint("tokens_output >= 0", name="tokens_output_non_negative"),
        CheckConstraint("spent_rubles >= 0", name="spent_rubles_non_negative"),
        Index("ix_llm_usage_daily_usage_date", "usage_date"),
    )

    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    subscriber_identifier: Mapped[str] = mapped_column(String(128), primary_key=True)
    tokens_input: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    tokens_output: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    spent_rubles: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
