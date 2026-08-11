"""Notification attempts and policy state

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-11 15:31:53.611114

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types


revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("event_key", sa.String(length=128), nullable=True),
        sa.Column("backend", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("attempted_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_attempts")),
    )
    with op.batch_alter_table("notification_attempts", schema=None) as batch_op:
        batch_op.create_index("ix_notification_attempts_created_at", ["created_at"], unique=False)
        batch_op.create_index("ix_notification_attempts_event_key", ["event_key"], unique=False)
        batch_op.create_index("ix_notification_attempts_status", ["status"], unique=False)

    op.create_table(
        "notification_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("changed_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("notified_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("updated_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_state")),
        sa.UniqueConstraint("scope", "key", name="uq_notification_state_scope_key"),
    )


def downgrade() -> None:
    op.drop_table("notification_state")
    with op.batch_alter_table("notification_attempts", schema=None) as batch_op:
        batch_op.drop_index("ix_notification_attempts_status")
        batch_op.drop_index("ix_notification_attempts_event_key")
        batch_op.drop_index("ix_notification_attempts_created_at")

    op.drop_table("notification_attempts")
