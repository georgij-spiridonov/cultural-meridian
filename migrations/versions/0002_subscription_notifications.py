"""Таблица очереди и журнала уведомлений по подпискам на мероприятия.

Идентификатор ревизии: 0002
Предыдущая ревизия: 0001
Дата создания: 2026-11-14 17:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Создаёт таблицу `subscription_notifications` и её индексы."""
    op.create_table(
        "subscription_notifications",
        sa.Column("identifier", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("subscription_identifier", sa.UUID(), nullable=False),
        sa.Column("event_identifier", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("subscriber_identifier", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_subscription_notifications_attempts_non_negative")),
        sa.CheckConstraint(
            "btrim(subscriber_identifier) <> ''", name=op.f("ck_subscription_notifications_subscriber_non_empty")
        ),
        sa.CheckConstraint(
            "channel IN ('web', 'telegram', 'evaluation')", name=op.f("ck_subscription_notifications_channel_allowed")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'read')",
            name=op.f("ck_subscription_notifications_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["event_identifier"],
            ["events.identifier"],
            name=op.f("fk_subscription_notifications_event_identifier_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_identifier"],
            ["subscriptions.identifier"],
            name=op.f("fk_subscription_notifications_subscription_identifier_subscriptions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("identifier", name=op.f("pk_subscription_notifications")),
        sa.UniqueConstraint("channel", "subscriber_identifier", "event_identifier", name="target_event"),
    )
    op.create_index(
        "ix_subscription_notifications_pending_dispatch",
        "subscription_notifications",
        ["channel", "status", "next_retry_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_subscription_notifications_web_inbox",
        "subscription_notifications",
        ["channel", "subscriber_identifier", "status", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_subscription_notifications_subscription_identifier",
        "subscription_notifications",
        ["subscription_identifier"],
        unique=False,
    )
    op.create_index(
        "ix_subscription_notifications_event_identifier",
        "subscription_notifications",
        ["event_identifier"],
        unique=False,
    )


def downgrade() -> None:
    """Удаляет таблицу `subscription_notifications` и её индексы."""
    op.drop_index("ix_subscription_notifications_event_identifier", table_name="subscription_notifications")
    op.drop_index("ix_subscription_notifications_subscription_identifier", table_name="subscription_notifications")
    op.drop_index("ix_subscription_notifications_web_inbox", table_name="subscription_notifications")
    op.drop_index("ix_subscription_notifications_pending_dispatch", table_name="subscription_notifications")
    op.drop_table("subscription_notifications")
