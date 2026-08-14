"""Ownership portfolio routing and account connections

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-11 19:41:18.321237

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tradabot_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("external_identity_type", sa.String(length=16), nullable=False),
        sa.Column("external_identity_id", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("created_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tradabot_users")),
        sa.UniqueConstraint(
            "external_identity_type", "external_identity_id", name="uq_tradabot_users_identity"
        ),
    )
    op.create_table(
        "external_account_connections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("connection_status", sa.String(length=32), nullable=False),
        sa.Column("credential_reference", sa.String(length=255), nullable=True),
        sa.Column("created_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["tradabot_users.id"],
            name=op.f("fk_external_account_connections_owner_id_tradabot_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_account_connections")),
        sa.UniqueConstraint(
            "owner_id", "provider", "purpose", "environment", name="uq_connection_scope"
        ),
    )
    with op.batch_alter_table("external_account_connections", schema=None) as batch_op:
        batch_op.create_index("ix_external_connections_owner", ["owner_id"], unique=False)

    with op.batch_alter_table("simulation_profiles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("notification_channel", sa.String(length=64), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_simulation_profiles_owner_id_tradabot_users"),
            "tradabot_users",
            ["owner_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("simulation_profiles", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_simulation_profiles_owner_id_tradabot_users"), type_="foreignkey"
        )
        batch_op.drop_column("notification_channel")
        batch_op.drop_column("owner_id")

    with op.batch_alter_table("external_account_connections", schema=None) as batch_op:
        batch_op.drop_index("ix_external_connections_owner")

    op.drop_table("external_account_connections")
    op.drop_table("tradabot_users")
