"""Market data provenance and session state

Revision ID: 0005
Revises: 0004
Phase 3b schema. Purely additive; every new column is nullable.

Provenance (`provider`, `ingested_at`, `provider_symbol`) is nullable rather than
backfilled: rows written before provenance existed genuinely have unknown origin,
and stamping them with a guess would be worse than admitting it.

`virtual_portfolios.session_date` / `session_start_equity` carry the daily-loss
limit, which is measured against a *trading session* rather than a UTC day.

Create Date: 2026-08-11 14:07:14.737117

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types


revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("candles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("provider", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("ingested_at", app.db.types.UTCDateTime(timezone=True), nullable=True)
        )

    with op.batch_alter_table("instruments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("provider", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("provider_symbol", sa.String(length=32), nullable=True))

    with op.batch_alter_table("virtual_portfolios", schema=None) as batch_op:
        batch_op.add_column(sa.Column("session_date", sa.Date(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "session_start_equity", app.db.types.Money(precision=18, scale=6), nullable=True
            )
        )

    with op.batch_alter_table("virtual_positions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "corporate_actions_applied_through",
                app.db.types.UTCDateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("virtual_positions", schema=None) as batch_op:
        batch_op.drop_column("corporate_actions_applied_through")

    with op.batch_alter_table("virtual_portfolios", schema=None) as batch_op:
        batch_op.drop_column("session_start_equity")
        batch_op.drop_column("session_date")

    with op.batch_alter_table("instruments", schema=None) as batch_op:
        batch_op.drop_column("provider_symbol")
        batch_op.drop_column("provider")

    with op.batch_alter_table("candles", schema=None) as batch_op:
        batch_op.drop_column("ingested_at")
        batch_op.drop_column("provider")
