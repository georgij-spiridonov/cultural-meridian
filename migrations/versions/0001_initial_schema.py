"""Начальная нормализованная схема PostgreSQL 17.

Идентификатор ревизии: 0001
Предыдущая ревизия:
Дата создания: 2026-11-13 21:06:59.450163
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Применяет изменения схемы базы данных."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.create_table(
        "agent_traces",
        sa.Column("trace_identifier", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("request_identifier", sa.String(length=128), nullable=False),
        sa.Column("session_identifier", sa.String(length=128), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("user_query", sa.Text(), nullable=False),
        sa.Column("plan_summary", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("latency_milliseconds", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("tokens_input", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("tokens_output", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_rubles", sa.Numeric(precision=12, scale=6), server_default=sa.text("0"), nullable=False),
        sa.Column("model_name", sa.String(length=128), server_default=sa.text("''"), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "btrim(request_identifier) <> ''", name=op.f("ck_agent_traces_request_identifier_non_empty")
        ),
        sa.CheckConstraint(
            "btrim(session_identifier) <> ''", name=op.f("ck_agent_traces_session_identifier_non_empty")
        ),
        sa.CheckConstraint(
            "channel IN ('web', 'telegram', 'evaluation')", name=op.f("ck_agent_traces_channel_allowed")
        ),
        sa.CheckConstraint("cost_rubles >= 0", name=op.f("ck_agent_traces_cost_rubles_non_negative")),
        sa.CheckConstraint("latency_milliseconds >= 0", name=op.f("ck_agent_traces_latency_non_negative")),
        sa.CheckConstraint("tokens_input >= 0", name=op.f("ck_agent_traces_tokens_input_non_negative")),
        sa.CheckConstraint("tokens_output >= 0", name=op.f("ck_agent_traces_tokens_output_non_negative")),
        sa.PrimaryKeyConstraint("trace_identifier", name=op.f("pk_agent_traces")),
    )
    op.create_index(
        "ix_agent_traces_created_at_desc", "agent_traces", [sa.literal_column("created_at DESC")], unique=False
    )
    op.create_index("ix_agent_traces_request_identifier", "agent_traces", ["request_identifier"], unique=False)
    op.create_index(
        "ix_agent_traces_session_created_desc",
        "agent_traces",
        ["session_identifier", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "categories",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("title_russian", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "btrim(code) <> '' AND code = lower(btrim(code))", name=op.f("ck_categories_code_normalized")
        ),
        sa.CheckConstraint("btrim(title_russian) <> ''", name=op.f("ck_categories_title_russian_non_empty")),
        sa.CheckConstraint("sort_order >= 0", name=op.f("ck_categories_sort_order_non_negative")),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_categories")),
        sa.UniqueConstraint("title_russian", name=op.f("uq_categories_title_russian")),
    )
    op.create_table(
        "cities",
        sa.Column("identifier", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("normalized_name", sa.String(length=120), nullable=False),
        sa.Column("center_latitude", sa.Float(precision=53), nullable=True),
        sa.Column("center_longitude", sa.Float(precision=53), nullable=True),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f("ck_cities_name_non_empty")),
        sa.CheckConstraint(
            "btrim(normalized_name) <> '' AND normalized_name = lower(btrim(normalized_name))",
            name=op.f("ck_cities_normalized_name_clean"),
        ),
        sa.CheckConstraint(
            "(center_latitude IS NULL) = (center_longitude IS NULL)", name=op.f("ck_cities_center_coordinates_paired")
        ),
        sa.CheckConstraint(
            "center_latitude IS NULL OR (center_latitude >= -90.0 AND center_latitude <= 90.0)",
            name=op.f("ck_cities_center_latitude_range"),
        ),
        sa.CheckConstraint(
            "center_longitude IS NULL OR (center_longitude >= -180.0 AND center_longitude <= 180.0)",
            name=op.f("ck_cities_center_longitude_range"),
        ),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_cities")),
        sa.UniqueConstraint("name", name=op.f("uq_cities_name")),
        sa.UniqueConstraint("normalized_name", name=op.f("uq_cities_normalized_name")),
    )
    op.create_index(
        "ix_cities_normalized_name_trgm",
        "cities",
        ["normalized_name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"normalized_name": "gin_trgm_ops"},
    )
    op.create_table(
        "dialog_sessions",
        sa.Column("session_identifier", sa.String(length=128), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("subscriber_identifier", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "btrim(session_identifier) <> ''", name=op.f("ck_dialog_sessions_session_identifier_non_empty")
        ),
        sa.CheckConstraint(
            "btrim(subscriber_identifier) <> ''", name=op.f("ck_dialog_sessions_subscriber_identifier_non_empty")
        ),
        sa.CheckConstraint(
            "channel IN ('web', 'telegram', 'evaluation')", name=op.f("ck_dialog_sessions_channel_allowed")
        ),
        sa.PrimaryKeyConstraint("session_identifier", name=op.f("pk_dialog_sessions")),
    )
    op.create_table(
        "llm_usage_daily",
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("subscriber_identifier", sa.String(length=128), nullable=False),
        sa.Column("tokens_input", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("tokens_output", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("spent_rubles", sa.Numeric(precision=12, scale=6), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("btrim(subscriber_identifier) <> ''", name=op.f("ck_llm_usage_daily_subscriber_non_empty")),
        sa.CheckConstraint("spent_rubles >= 0", name=op.f("ck_llm_usage_daily_spent_rubles_non_negative")),
        sa.CheckConstraint("tokens_input >= 0", name=op.f("ck_llm_usage_daily_tokens_input_non_negative")),
        sa.CheckConstraint("tokens_output >= 0", name=op.f("ck_llm_usage_daily_tokens_output_non_negative")),
        sa.PrimaryKeyConstraint("usage_date", "subscriber_identifier", name=op.f("pk_llm_usage_daily")),
    )
    op.create_index("ix_llm_usage_daily_usage_date", "llm_usage_daily", ["usage_date"], unique=False)
    op.create_table(
        "moderators",
        sa.Column("identifier", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("btrim(display_name) <> ''", name=op.f("ck_moderators_display_name_non_empty")),
        sa.CheckConstraint("btrim(password_hash) <> ''", name=op.f("ck_moderators_password_hash_non_empty")),
        sa.CheckConstraint(
            "username = lower(btrim(username)) AND username ~ '^[a-z0-9_.-]{3,64}$'",
            name=op.f("ck_moderators_username_valid"),
        ),
        sa.CheckConstraint(
            "last_login_at IS NULL OR last_login_at >= created_at", name=op.f("ck_moderators_last_login_after_creation")
        ),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_moderators")),
        sa.UniqueConstraint("username", name=op.f("uq_moderators_username")),
    )
    op.create_table(
        "raw_events",
        sa.Column("identifier", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_name", sa.String(length=120), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("external_identifier", sa.String(length=255), nullable=False),
        sa.Column("source_url", sa.String(length=1024), nullable=False),
        sa.Column("source_hash", sa.String(length=128), nullable=False),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("normalization_status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "btrim(external_identifier) <> ''", name=op.f("ck_raw_events_external_identifier_non_empty")
        ),
        sa.CheckConstraint("btrim(source_hash) <> ''", name=op.f("ck_raw_events_source_hash_non_empty")),
        sa.CheckConstraint("btrim(source_name) <> ''", name=op.f("ck_raw_events_source_name_non_empty")),
        sa.CheckConstraint("btrim(source_url) <> ''", name=op.f("ck_raw_events_source_url_non_empty")),
        sa.CheckConstraint(
            "error_message IS NULL OR btrim(error_message) <> ''", name=op.f("ck_raw_events_error_message_non_empty")
        ),
        sa.CheckConstraint("jsonb_typeof(raw_payload) = 'object'", name=op.f("ck_raw_events_raw_payload_json_object")),
        sa.CheckConstraint(
            "normalization_status IN ('pending', 'normalized', 'failed')", name=op.f("ck_raw_events_status_allowed")
        ),
        sa.CheckConstraint(
            "source_type IN ('manual', 'import_file', 'rss', 'api', 'webpage')",
            name=op.f("ck_raw_events_source_type_allowed"),
        ),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_raw_events")),
        sa.UniqueConstraint("source_name", "source_hash", name="source_name_hash"),
    )
    op.create_index(
        "ix_raw_events_source_status_fetched_at",
        "raw_events",
        ["source_name", "normalization_status", "fetched_at"],
        unique=False,
    )
    op.create_index(
        "ix_raw_events_status_fetched_at", "raw_events", ["normalization_status", "fetched_at"], unique=False
    )
    op.create_table(
        "tags",
        sa.Column("identifier", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("normalized_name", sa.String(length=64), nullable=False),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f("ck_tags_name_non_empty")),
        sa.CheckConstraint(
            "btrim(normalized_name) <> '' AND normalized_name = lower(btrim(normalized_name))",
            name=op.f("ck_tags_normalized_name_clean"),
        ),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_tags")),
        sa.UniqueConstraint("normalized_name", name=op.f("uq_tags_normalized_name")),
    )
    op.create_table(
        "agent_trace_tool_invocations",
        sa.Column("trace_identifier", sa.UUID(), nullable=False),
        sa.Column("sequence_number", sa.SmallInteger(), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column(
            "arguments", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "returned_event_identifiers",
            postgresql.ARRAY(sa.UUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("succeeded", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint("btrim(tool_name) <> ''", name=op.f("ck_agent_trace_tool_invocations_tool_name_non_empty")),
        sa.CheckConstraint(
            "error_message IS NULL OR btrim(error_message) <> ''",
            name=op.f("ck_agent_trace_tool_invocations_error_message_non_empty"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(arguments) = 'object'", name=op.f("ck_agent_trace_tool_invocations_arguments_json_object")
        ),
        sa.CheckConstraint(
            "sequence_number >= 0", name=op.f("ck_agent_trace_tool_invocations_sequence_number_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["trace_identifier"],
            ["agent_traces.trace_identifier"],
            name=op.f("fk_agent_trace_tool_invocations_trace_identifier_agent_traces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("trace_identifier", "sequence_number", name=op.f("pk_agent_trace_tool_invocations")),
    )
    op.create_table(
        "audit_logs",
        sa.Column("identifier", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("moderator_identifier", sa.UUID(), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_identifier", sa.String(length=128), nullable=False),
        sa.Column(
            "changes", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("btrim(action) <> ''", name=op.f("ck_audit_logs_action_non_empty")),
        sa.CheckConstraint("btrim(actor) <> ''", name=op.f("ck_audit_logs_actor_non_empty")),
        sa.CheckConstraint("btrim(entity_identifier) <> ''", name=op.f("ck_audit_logs_entity_identifier_non_empty")),
        sa.CheckConstraint("btrim(entity_type) <> ''", name=op.f("ck_audit_logs_entity_type_non_empty")),
        sa.CheckConstraint("jsonb_typeof(changes) = 'object'", name=op.f("ck_audit_logs_changes_json_object")),
        sa.ForeignKeyConstraint(
            ["moderator_identifier"],
            ["moderators.identifier"],
            name=op.f("fk_audit_logs_moderator_identifier_moderators"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_audit_logs")),
    )
    op.create_index("ix_audit_logs_created_at_desc", "audit_logs", [sa.literal_column("created_at DESC")], unique=False)
    op.create_index(
        "ix_audit_logs_entity_created_desc",
        "audit_logs",
        ["entity_type", "entity_identifier", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_moderator_identifier",
        "audit_logs",
        ["moderator_identifier"],
        unique=False,
        postgresql_where=sa.text("moderator_identifier IS NOT NULL"),
    )
    op.create_table(
        "city_aliases",
        sa.Column("normalized_alias", sa.String(length=120), nullable=False),
        sa.Column("city_identifier", sa.UUID(), nullable=False),
        sa.Column("alias", sa.String(length=120), nullable=False),
        sa.CheckConstraint("btrim(alias) <> ''", name=op.f("ck_city_aliases_alias_non_empty")),
        sa.CheckConstraint(
            "btrim(normalized_alias) <> '' AND normalized_alias = lower(btrim(normalized_alias))",
            name=op.f("ck_city_aliases_normalized_alias_clean"),
        ),
        sa.ForeignKeyConstraint(
            ["city_identifier"],
            ["cities.identifier"],
            name=op.f("fk_city_aliases_city_identifier_cities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("normalized_alias", name=op.f("pk_city_aliases")),
    )
    op.create_index("ix_city_aliases_city_identifier", "city_aliases", ["city_identifier"], unique=False)
    op.create_index(
        "ix_city_aliases_normalized_alias_trgm",
        "city_aliases",
        ["normalized_alias"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"normalized_alias": "gin_trgm_ops"},
    )
    op.create_table(
        "dialog_messages",
        sa.Column("identifier", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_identifier", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("btrim(content) <> ''", name=op.f("ck_dialog_messages_content_non_empty")),
        sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f("ck_dialog_messages_role_allowed")),
        sa.ForeignKeyConstraint(
            ["session_identifier"],
            ["dialog_sessions.session_identifier"],
            name=op.f("fk_dialog_messages_session_identifier_dialog_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_dialog_messages")),
    )
    op.create_index(
        "ix_dialog_messages_session_order",
        "dialog_messages",
        ["session_identifier", sa.literal_column("created_at DESC"), sa.literal_column("identifier DESC")],
        unique=False,
    )
    op.create_table(
        "pending_actions",
        sa.Column("action_identifier", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("session_identifier", sa.String(length=128), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("confirmation_prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action_type IN ('subscribe', 'save_feedback')", name=op.f("ck_pending_actions_action_type_allowed")
        ),
        sa.CheckConstraint(
            "btrim(confirmation_prompt) <> ''", name=op.f("ck_pending_actions_confirmation_prompt_non_empty")
        ),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name=op.f("ck_pending_actions_payload_json_object")),
        sa.CheckConstraint(
            "status IN ('pending', 'consumed', 'canceled', 'expired')", name=op.f("ck_pending_actions_status_allowed")
        ),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_pending_actions_expires_after_creation")),
        sa.ForeignKeyConstraint(
            ["session_identifier"],
            ["dialog_sessions.session_identifier"],
            name=op.f("fk_pending_actions_session_identifier_dialog_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("action_identifier", name=op.f("pk_pending_actions")),
    )
    op.create_index(
        "ix_pending_actions_expires_active",
        "pending_actions",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_pending_actions_active_session",
        "pending_actions",
        ["session_identifier"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "subscriptions",
        sa.Column("identifier", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("subscriber_identifier", sa.String(length=128), nullable=False),
        sa.Column("city_identifier", sa.UUID(), nullable=True),
        sa.Column("category_code", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("btrim(subscriber_identifier) <> ''", name=op.f("ck_subscriptions_subscriber_non_empty")),
        sa.CheckConstraint(
            "channel IN ('web', 'telegram', 'evaluation')", name=op.f("ck_subscriptions_channel_allowed")
        ),
        sa.ForeignKeyConstraint(
            ["category_code"],
            ["categories.code"],
            name=op.f("fk_subscriptions_category_code_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["city_identifier"],
            ["cities.identifier"],
            name=op.f("fk_subscriptions_city_identifier_cities"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_subscriptions")),
        sa.UniqueConstraint(
            "channel",
            "subscriber_identifier",
            "city_identifier",
            "category_code",
            name="unique_filter",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index("ix_subscriptions_matching", "subscriptions", ["city_identifier", "category_code"], unique=False)
    op.create_index(
        "ix_subscriptions_subscriber",
        "subscriptions",
        ["channel", "subscriber_identifier", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "venues",
        sa.Column("identifier", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("city_identifier", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("address", sa.String(length=500), server_default=sa.text("''"), nullable=False),
        sa.Column("latitude", sa.Float(precision=53), nullable=True),
        sa.Column("longitude", sa.Float(precision=53), nullable=True),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f("ck_venues_name_non_empty")),
        sa.CheckConstraint(
            "btrim(normalized_name) <> '' AND normalized_name = lower(btrim(normalized_name))",
            name=op.f("ck_venues_normalized_name_clean"),
        ),
        sa.CheckConstraint("(latitude IS NULL) = (longitude IS NULL)", name=op.f("ck_venues_coordinates_paired")),
        sa.CheckConstraint(
            "latitude IS NULL OR (latitude >= -90.0 AND latitude <= 90.0)", name=op.f("ck_venues_latitude_range")
        ),
        sa.CheckConstraint(
            "longitude IS NULL OR (longitude >= -180.0 AND longitude <= 180.0)", name=op.f("ck_venues_longitude_range")
        ),
        sa.ForeignKeyConstraint(
            ["city_identifier"],
            ["cities.identifier"],
            name=op.f("fk_venues_city_identifier_cities"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_venues")),
        sa.UniqueConstraint("city_identifier", "normalized_name", name="city_normalized_name"),
    )
    op.create_index("ix_venues_city_identifier", "venues", ["city_identifier"], unique=False)
    op.create_index(
        "ix_venues_coordinates_present",
        "venues",
        ["city_identifier"],
        unique=False,
        postgresql_where=sa.text("latitude IS NOT NULL AND longitude IS NOT NULL"),
    )
    op.create_index(
        "ix_venues_normalized_name_trgm",
        "venues",
        ["normalized_name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"normalized_name": "gin_trgm_ops"},
    )
    op.create_table(
        "events",
        sa.Column("identifier", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("normalized_title", sa.String(length=500), nullable=False),
        sa.Column("category_code", sa.String(length=32), nullable=False),
        sa.Column("venue_identifier", sa.UUID(), nullable=False),
        sa.Column("date_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("date_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("price_minimum", sa.Integer(), nullable=True),
        sa.Column("price_maximum", sa.Integer(), nullable=True),
        sa.Column("is_free", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("age_limit", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("language", sa.String(length=64), nullable=True),
        sa.Column("organization_link", sa.String(length=1024), nullable=True),
        sa.Column("confidence", sa.Float(precision=53), server_default=sa.text("1.0"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'published'"), nullable=False),
        sa.Column("deduplication_key", sa.String(length=128), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('russian', title || ' ' || description)", persisted=True),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "btrim(deduplication_key) <> '' AND deduplication_key = lower(btrim(deduplication_key))",
            name=op.f("ck_events_deduplication_key_clean"),
        ),
        sa.CheckConstraint(
            "btrim(normalized_title) <> '' AND normalized_title = lower(btrim(normalized_title))",
            name=op.f("ck_events_normalized_title_clean"),
        ),
        sa.CheckConstraint("btrim(title) <> ''", name=op.f("ck_events_title_non_empty")),
        sa.CheckConstraint(
            "language IS NULL OR (btrim(language) <> '' AND language = lower(btrim(language)))",
            name=op.f("ck_events_lang"),
        ),
        sa.CheckConstraint(
            "organization_link IS NULL OR btrim(organization_link) <> ''",
            name=op.f("ck_events_organization_link_non_empty"),
        ),
        sa.CheckConstraint("status IN ('published', 'draft', 'canceled')", name=op.f("ck_events_status_allowed")),
        sa.CheckConstraint(
            "NOT is_free OR "
            "((price_minimum IS NULL OR price_minimum = 0) AND (price_maximum IS NULL OR price_maximum = 0))",
            name=op.f("ck_events_free_price_consistency"),
        ),
        sa.CheckConstraint("age_limit IN (0, 6, 12, 16, 18)", name=op.f("ck_events_age_limit_allowed")),
        sa.CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name=op.f("ck_events_confidence_range")),
        sa.CheckConstraint("date_end IS NULL OR date_end >= date_start", name=op.f("ck_events_date_range_valid")),
        sa.CheckConstraint(
            "duration_minutes IS NULL OR duration_minutes > 0", name=op.f("ck_events_duration_positive")
        ),
        sa.CheckConstraint(
            "price_maximum IS NULL OR price_maximum >= 0", name=op.f("ck_events_price_maximum_non_negative")
        ),
        sa.CheckConstraint(
            "price_minimum IS NULL OR price_maximum IS NULL OR price_maximum >= price_minimum",
            name=op.f("ck_events_price_range_valid"),
        ),
        sa.CheckConstraint(
            "price_minimum IS NULL OR price_minimum >= 0", name=op.f("ck_events_price_minimum_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["category_code"], ["categories.code"], name=op.f("fk_events_category_code_categories"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["venue_identifier"],
            ["venues.identifier"],
            name=op.f("fk_events_venue_identifier_venues"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_events")),
    )
    op.create_index("ix_events_created_at_desc", "events", [sa.literal_column("created_at DESC")], unique=False)
    op.create_index("ix_events_deduplication_key", "events", ["deduplication_key"], unique=False)
    op.create_index(
        "ix_events_normalized_title_trgm",
        "events",
        ["normalized_title"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"normalized_title": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_events_published_filters",
        "events",
        ["venue_identifier", "category_code", "date_start", "price_minimum", "age_limit"],
        unique=False,
        postgresql_where=sa.text("status = 'published'"),
    )
    op.create_index("ix_events_search_vector", "events", ["search_vector"], unique=False, postgresql_using="gin")
    op.create_index("ix_events_status_date_start", "events", ["status", "date_start"], unique=False)
    op.create_index("ix_events_updated_at_desc", "events", [sa.literal_column("updated_at DESC")], unique=False)
    op.create_table(
        "venue_aliases",
        sa.Column("identifier", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("venue_identifier", sa.UUID(), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("normalized_alias", sa.String(length=255), nullable=False),
        sa.CheckConstraint("btrim(alias) <> ''", name=op.f("ck_venue_aliases_alias_non_empty")),
        sa.CheckConstraint(
            "btrim(normalized_alias) <> '' AND normalized_alias = lower(btrim(normalized_alias))",
            name=op.f("ck_venue_aliases_normalized_alias_clean"),
        ),
        sa.ForeignKeyConstraint(
            ["venue_identifier"],
            ["venues.identifier"],
            name=op.f("fk_venue_aliases_venue_identifier_venues"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_venue_aliases")),
        sa.UniqueConstraint("venue_identifier", "normalized_alias", name="venue_normalized_alias"),
    )
    op.create_index(
        "ix_venue_aliases_normalized_alias_trgm",
        "venue_aliases",
        ["normalized_alias"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"normalized_alias": "gin_trgm_ops"},
    )
    op.create_index("ix_venue_aliases_venue_identifier", "venue_aliases", ["venue_identifier"], unique=False)
    op.create_table(
        "agent_trace_grounded_events",
        sa.Column("trace_identifier", sa.UUID(), nullable=False),
        sa.Column("event_identifier", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_identifier"],
            ["events.identifier"],
            name=op.f("fk_agent_trace_grounded_events_event_identifier_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trace_identifier"],
            ["agent_traces.trace_identifier"],
            name=op.f("fk_agent_trace_grounded_events_trace_identifier_agent_traces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("trace_identifier", "event_identifier", name=op.f("pk_agent_trace_grounded_events")),
    )
    op.create_index(
        "ix_agent_trace_grounded_events_event_identifier",
        "agent_trace_grounded_events",
        ["event_identifier"],
        unique=False,
    )
    op.create_table(
        "event_sources",
        sa.Column("identifier", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_identifier", sa.UUID(), nullable=False),
        sa.Column("source_name", sa.String(length=120), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=1024), nullable=False),
        sa.Column("source_hash", sa.String(length=128), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("btrim(source_hash) <> ''", name=op.f("ck_event_sources_source_hash_non_empty")),
        sa.CheckConstraint("btrim(source_name) <> ''", name=op.f("ck_event_sources_source_name_non_empty")),
        sa.CheckConstraint("btrim(source_url) <> ''", name=op.f("ck_event_sources_source_url_non_empty")),
        sa.CheckConstraint(
            "source_type IN ('manual', 'import_file', 'rss', 'api', 'webpage')",
            name=op.f("ck_event_sources_source_type_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["event_identifier"],
            ["events.identifier"],
            name=op.f("fk_event_sources_event_identifier_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_event_sources")),
        sa.UniqueConstraint("source_type", "source_hash", name="source_type_hash"),
    )
    op.create_index("ix_event_sources_event_identifier", "event_sources", ["event_identifier"], unique=False)
    op.create_index(
        "uq_event_sources_primary_per_event",
        "event_sources",
        ["event_identifier"],
        unique=True,
        postgresql_where=sa.text("is_primary = true"),
    )
    op.create_table(
        "event_tags",
        sa.Column("event_identifier", sa.UUID(), nullable=False),
        sa.Column("tag_identifier", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_identifier"],
            ["events.identifier"],
            name=op.f("fk_event_tags_event_identifier_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tag_identifier"], ["tags.identifier"], name=op.f("fk_event_tags_tag_identifier_tags"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("event_identifier", "tag_identifier", name=op.f("pk_event_tags")),
    )
    op.create_index("ix_event_tags_tag_event", "event_tags", ["tag_identifier", "event_identifier"], unique=False)
    op.create_table(
        "feedback_requests",
        sa.Column("identifier", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("event_identifier", sa.UUID(), nullable=True),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("contact_hint", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'new'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("btrim(message_text) <> ''", name=op.f("ck_feedback_requests_message_text_non_empty")),
        sa.CheckConstraint(
            "channel IN ('web', 'telegram', 'evaluation')", name=op.f("ck_feedback_requests_channel_allowed")
        ),
        sa.CheckConstraint(
            "contact_hint IS NULL OR btrim(contact_hint) <> ''",
            name=op.f("ck_feedback_requests_contact_hint_non_empty"),
        ),
        sa.CheckConstraint("status IN ('new', 'reviewed')", name=op.f("ck_feedback_requests_status_allowed")),
        sa.ForeignKeyConstraint(
            ["event_identifier"],
            ["events.identifier"],
            name=op.f("fk_feedback_requests_event_identifier_events"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_feedback_requests")),
    )
    op.create_index(
        "ix_feedback_requests_event_identifier",
        "feedback_requests",
        ["event_identifier"],
        unique=False,
        postgresql_where=sa.text("event_identifier IS NOT NULL"),
    )
    op.create_index(
        "ix_feedback_requests_status_created_desc",
        "feedback_requests",
        ["status", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "moderation_queue",
        sa.Column("identifier", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_identifier", sa.UUID(), nullable=False),
        sa.Column("raw_event_identifier", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("has_suspicious_instructions", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "matched_injection_patterns",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=64), nullable=True),
        sa.Column("resolved_by_moderator_identifier", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "(status = 'pending' AND resolved_at IS NULL AND resolved_by IS NULL "
            "AND resolved_by_moderator_identifier IS NULL) OR "
            "(status IN ('approved', 'rejected') AND resolved_at IS NOT NULL "
            "AND resolved_by IS NOT NULL AND btrim(resolved_by) <> '')",
            name=op.f("ck_moderation_queue_resolution_consistency"),
        ),
        sa.CheckConstraint("btrim(reason) <> ''", name=op.f("ck_moderation_queue_reason_non_empty")),
        sa.CheckConstraint(
            "jsonb_typeof(matched_injection_patterns) = 'array'",
            name=op.f("ck_moderation_queue_matched_injection_patterns_json_array"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name=op.f("ck_moderation_queue_status_allowed")
        ),
        sa.ForeignKeyConstraint(
            ["event_identifier"],
            ["events.identifier"],
            name=op.f("fk_moderation_queue_event_identifier_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["raw_event_identifier"],
            ["raw_events.identifier"],
            name=op.f("fk_moderation_queue_raw_event_identifier_raw_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_moderator_identifier"],
            ["moderators.identifier"],
            name=op.f("fk_moderation_queue_resolved_by_moderator_identifier_moderators"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_moderation_queue")),
    )
    op.create_index("ix_moderation_queue_event_identifier", "moderation_queue", ["event_identifier"], unique=False)
    op.create_index("ix_moderation_queue_status_created_at", "moderation_queue", ["status", "created_at"], unique=False)

    op.execute(
        """
        CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
        BEGIN
            IF NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at THEN
                NEW.updated_at = now();
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table_name in ("events", "dialog_sessions", "llm_usage_daily"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_touch_updated_at
            BEFORE UPDATE ON {table_name}
            FOR EACH ROW
            EXECUTE FUNCTION touch_updated_at();
            """
        )

    categories_table = sa.table(
        "categories",
        sa.column("code", sa.String()),
        sa.column("title_russian", sa.String()),
        sa.column("sort_order", sa.SmallInteger()),
    )
    op.bulk_insert(
        categories_table,
        [
            {"code": "concert", "title_russian": "Концерт", "sort_order": 1},
            {"code": "spectacle", "title_russian": "Спектакль", "sort_order": 2},
            {"code": "exhibition", "title_russian": "Выставка", "sort_order": 3},
            {"code": "festival", "title_russian": "Фестиваль", "sort_order": 4},
            {"code": "cinema", "title_russian": "Кино", "sort_order": 5},
            {"code": "excursion", "title_russian": "Экскурсия", "sort_order": 6},
            {"code": "other", "title_russian": "Другое", "sort_order": 7},
        ],
    )


def downgrade() -> None:
    """Откатывает изменения схемы базы данных."""
    for table_name in ("llm_usage_daily", "dialog_sessions", "events"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_touch_updated_at ON {table_name}")
    op.execute("DROP FUNCTION IF EXISTS touch_updated_at()")
    op.drop_index("ix_moderation_queue_status_created_at", table_name="moderation_queue")
    op.drop_index("ix_moderation_queue_event_identifier", table_name="moderation_queue")
    op.drop_table("moderation_queue")
    op.drop_index("ix_feedback_requests_status_created_desc", table_name="feedback_requests")
    op.drop_index(
        "ix_feedback_requests_event_identifier",
        table_name="feedback_requests",
        postgresql_where=sa.text("event_identifier IS NOT NULL"),
    )
    op.drop_table("feedback_requests")
    op.drop_index("ix_event_tags_tag_event", table_name="event_tags")
    op.drop_table("event_tags")
    op.drop_index(
        "uq_event_sources_primary_per_event", table_name="event_sources", postgresql_where=sa.text("is_primary = true")
    )
    op.drop_index("ix_event_sources_event_identifier", table_name="event_sources")
    op.drop_table("event_sources")
    op.drop_index("ix_agent_trace_grounded_events_event_identifier", table_name="agent_trace_grounded_events")
    op.drop_table("agent_trace_grounded_events")
    op.drop_index("ix_venue_aliases_venue_identifier", table_name="venue_aliases")
    op.drop_index(
        "ix_venue_aliases_normalized_alias_trgm",
        table_name="venue_aliases",
        postgresql_using="gin",
        postgresql_ops={"normalized_alias": "gin_trgm_ops"},
    )
    op.drop_table("venue_aliases")
    op.drop_index("ix_events_updated_at_desc", table_name="events")
    op.drop_index("ix_events_status_date_start", table_name="events")
    op.drop_index("ix_events_search_vector", table_name="events", postgresql_using="gin")
    op.drop_index("ix_events_published_filters", table_name="events", postgresql_where=sa.text("status = 'published'"))
    op.drop_index(
        "ix_events_normalized_title_trgm",
        table_name="events",
        postgresql_using="gin",
        postgresql_ops={"normalized_title": "gin_trgm_ops"},
    )
    op.drop_index("ix_events_deduplication_key", table_name="events")
    op.drop_index("ix_events_created_at_desc", table_name="events")
    op.drop_table("events")
    op.drop_index(
        "ix_venues_normalized_name_trgm",
        table_name="venues",
        postgresql_using="gin",
        postgresql_ops={"normalized_name": "gin_trgm_ops"},
    )
    op.drop_index(
        "ix_venues_coordinates_present",
        table_name="venues",
        postgresql_where=sa.text("latitude IS NOT NULL AND longitude IS NOT NULL"),
    )
    op.drop_index("ix_venues_city_identifier", table_name="venues")
    op.drop_table("venues")
    op.drop_index("ix_subscriptions_subscriber", table_name="subscriptions")
    op.drop_index("ix_subscriptions_matching", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_index(
        "uq_pending_actions_active_session",
        table_name="pending_actions",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index(
        "ix_pending_actions_expires_active",
        table_name="pending_actions",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("pending_actions")
    op.drop_index("ix_dialog_messages_session_order", table_name="dialog_messages")
    op.drop_table("dialog_messages")
    op.drop_index(
        "ix_city_aliases_normalized_alias_trgm",
        table_name="city_aliases",
        postgresql_using="gin",
        postgresql_ops={"normalized_alias": "gin_trgm_ops"},
    )
    op.drop_index("ix_city_aliases_city_identifier", table_name="city_aliases")
    op.drop_table("city_aliases")
    op.drop_index(
        "ix_audit_logs_moderator_identifier",
        table_name="audit_logs",
        postgresql_where=sa.text("moderator_identifier IS NOT NULL"),
    )
    op.drop_index("ix_audit_logs_entity_created_desc", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at_desc", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_table("agent_trace_tool_invocations")
    op.drop_table("tags")
    op.drop_index("ix_raw_events_status_fetched_at", table_name="raw_events")
    op.drop_index("ix_raw_events_source_status_fetched_at", table_name="raw_events")
    op.drop_table("raw_events")
    op.drop_table("moderators")
    op.drop_index("ix_llm_usage_daily_usage_date", table_name="llm_usage_daily")
    op.drop_table("llm_usage_daily")
    op.drop_table("dialog_sessions")
    op.drop_index(
        "ix_cities_normalized_name_trgm",
        table_name="cities",
        postgresql_using="gin",
        postgresql_ops={"normalized_name": "gin_trgm_ops"},
    )
    op.drop_table("cities")
    op.drop_table("categories")
    op.drop_index("ix_agent_traces_session_created_desc", table_name="agent_traces")
    op.drop_index("ix_agent_traces_request_identifier", table_name="agent_traces")
    op.drop_index("ix_agent_traces_created_at_desc", table_name="agent_traces")
    op.drop_table("agent_traces")
