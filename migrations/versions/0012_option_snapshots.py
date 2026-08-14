"""Point-in-time option surface snapshots.

Alpaca serves option chains as snapshots of *now*: there is no historical "as
of" parameter, and historical option bars need the OPRA agreement and reach back
only to February 2024. An option surface not captured today therefore cannot be
reconstructed tomorrow -- unlike candles, which can always be backfilled.

Two grains. ``option_surface_snapshots`` is one derived row per symbol per
capture and is small enough to keep indefinitely (~3 MB/year for 52 symbols).
``option_quote_snapshots`` holds the canonical near-the-money slice it was
derived from (~740 MB/year), so a later phase can recompute skew differently
instead of inheriting today's definition. Storing full chains would be ~6.8
GB/year and was rejected as operationally absurd.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db import types

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "option_quote_snapshots",
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("captured_at", types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("occ_symbol", sa.String(length=32), nullable=False),
        sa.Column("expiration", types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("strike", types.Money(precision=18, scale=6), nullable=False),
        sa.Column("option_type", sa.String(length=1), nullable=False),
        sa.Column("bid", types.Money(precision=18, scale=6), nullable=True),
        sa.Column("ask", types.Money(precision=18, scale=6), nullable=True),
        sa.Column("mid", types.Money(precision=18, scale=6), nullable=True),
        sa.Column("implied_volatility", types.Money(precision=12, scale=6), nullable=True),
        sa.Column("delta", types.Money(precision=12, scale=6), nullable=True),
        sa.Column("gamma", types.Money(precision=12, scale=6), nullable=True),
        sa.Column("vega", types.Money(precision=12, scale=6), nullable=True),
        sa.Column("theta", types.Money(precision=12, scale=6), nullable=True),
        sa.Column("open_interest", sa.Integer(), nullable=True),
        sa.Column("feed", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_option_quote_snapshots_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "instrument_id", "captured_at", "occ_symbol", name=op.f("pk_option_quote_snapshots")
        ),
    )
    with op.batch_alter_table("option_quote_snapshots", schema=None) as batch_op:
        batch_op.create_index("ix_option_quotes_captured", ["captured_at"], unique=False)
        batch_op.create_index(
            "ix_option_quotes_instrument_expiry", ["instrument_id", "expiration"], unique=False
        )

    op.create_table(
        "option_surface_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("captured_at", types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("underlying_price", types.Money(precision=18, scale=6), nullable=False),
        sa.Column("atm_iv", types.Money(precision=12, scale=6), nullable=True),
        sa.Column("iv_30d", types.Money(precision=12, scale=6), nullable=True),
        sa.Column("skew_25d", types.Money(precision=12, scale=6), nullable=True),
        sa.Column("term_slope", types.Money(precision=12, scale=6), nullable=True),
        sa.Column("expected_move_pct", types.Money(precision=12, scale=6), nullable=True),
        sa.Column("contracts_seen", sa.Integer(), nullable=False),
        sa.Column("contracts_with_iv", sa.Integer(), nullable=False),
        sa.Column("feed", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_option_surface_snapshots_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_option_surface_snapshots")),
        sa.UniqueConstraint("instrument_id", "captured_at", name="uq_option_surface_capture"),
    )
    with op.batch_alter_table("option_surface_snapshots", schema=None) as batch_op:
        batch_op.create_index("ix_option_surface_captured", ["captured_at"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_option_surface_snapshots_instrument_id"), ["instrument_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("option_surface_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_option_surface_snapshots_instrument_id"))
        batch_op.drop_index("ix_option_surface_captured")

    op.drop_table("option_surface_snapshots")
    with op.batch_alter_table("option_quote_snapshots", schema=None) as batch_op:
        batch_op.drop_index("ix_option_quotes_instrument_expiry")
        batch_op.drop_index("ix_option_quotes_captured")

    op.drop_table("option_quote_snapshots")
