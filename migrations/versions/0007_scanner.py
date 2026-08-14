"""Scanner watchlist signals evaluations

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-11 16:28:12.372758

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import app.db.types

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scan_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("completed_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=False),
        sa.Column("lease_expires_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("symbols_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("symbols_synced", sa.Integer(), server_default="0", nullable=False),
        sa.Column("symbols_evaluated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("symbols_skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("symbols_failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("candidates_discovered", sa.Integer(), server_default="0", nullable=False),
        sa.Column("signals_qualified", sa.Integer(), server_default="0", nullable=False),
        sa.Column("signals_strong", sa.Integer(), server_default="0", nullable=False),
        sa.Column("paper_decisions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("positions_opened", sa.Integer(), server_default="0", nullable=False),
        sa.Column("positions_closed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scan_runs")),
    )
    with op.batch_alter_table("scan_runs", schema=None) as batch_op:
        batch_op.create_index("ix_scan_runs_scope_started", ["scope", "started_at"], unique=False)
        batch_op.create_index("ix_scan_runs_status", ["status"], unique=False)

    op.create_table(
        "tracked_signals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("primary_timeframe", sa.String(length=8), nullable=False),
        sa.Column("horizon", sa.String(length=8), nullable=False),
        sa.Column("setup", sa.String(length=32), nullable=False),
        sa.Column("lifecycle", sa.String(length=16), nullable=False),
        sa.Column("current_score", sa.Float(), nullable=False),
        sa.Column("peak_score", sa.Float(), nullable=False),
        sa.Column("current_confidence", sa.Float(), nullable=True),
        sa.Column("evaluation_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("discovered_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("last_evaluated_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("qualified_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("strong_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("weakened_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("expired_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_tracked_signals_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tracked_signals")),
    )
    with op.batch_alter_table("tracked_signals", schema=None) as batch_op:
        batch_op.create_index(
            "ix_tracked_signals_instrument", ["instrument_id", "lifecycle"], unique=False
        )
        batch_op.create_index(
            "ix_tracked_signals_last_evaluated", ["last_evaluated_at"], unique=False
        )
        batch_op.create_index("ix_tracked_signals_lifecycle", ["lifecycle"], unique=False)

    op.create_table(
        "watchlist",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "tags",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_watchlist_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watchlist")),
        sa.UniqueConstraint("instrument_id", name="uq_watchlist_instrument"),
    )
    with op.batch_alter_table("watchlist", schema=None) as batch_op:
        batch_op.create_index("ix_watchlist_enabled", ["enabled"], unique=False)

    op.create_table(
        "signal_evaluations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("tracked_signal_id", sa.Integer(), nullable=True),
        sa.Column("primary_signal_id", sa.Integer(), nullable=True),
        sa.Column("scan_run_id", sa.Integer(), nullable=True),
        sa.Column("evaluated_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("market_data_timestamp", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("classification", sa.String(length=16), nullable=False),
        sa.Column("direction", sa.Integer(), server_default="0", nullable=False),
        sa.Column("qualified", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("agreement", sa.Float(), nullable=True),
        sa.Column("aligned", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("expected_move_bps", sa.Float(), nullable=True),
        sa.Column("cost_bps", sa.Float(), nullable=True),
        sa.Column("net_edge_bps", sa.Float(), nullable=True),
        sa.Column("expected_horizon", sa.String(length=8), nullable=True),
        sa.Column("bid", sa.Float(), nullable=True),
        sa.Column("ask", sa.Float(), nullable=True),
        sa.Column("spread_bps", sa.Float(), nullable=True),
        sa.Column("quote_age_seconds", sa.Float(), nullable=True),
        sa.Column(
            "timeframe_states",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "trend_metrics",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "momentum_metrics",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "volume_metrics",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "volatility_metrics",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "structure_metrics",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "liquidity_metrics",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "reason_codes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "risk_codes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("data_quality", sa.String(length=16), nullable=False),
        sa.Column("session_phase", sa.String(length=16), nullable=False),
        sa.Column("feature_set_version", sa.String(length=32), nullable=False),
        sa.Column("signal_model_version", sa.String(length=32), nullable=False),
        sa.Column("scanner_policy_version", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_signal_evaluations_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["primary_signal_id"],
            ["signals.id"],
            name=op.f("fk_signal_evaluations_primary_signal_id_signals"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["scan_run_id"],
            ["scan_runs.id"],
            name=op.f("fk_signal_evaluations_scan_run_id_scan_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tracked_signal_id"],
            ["tracked_signals.id"],
            name=op.f("fk_signal_evaluations_tracked_signal_id_tracked_signals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signal_evaluations")),
    )
    with op.batch_alter_table("signal_evaluations", schema=None) as batch_op:
        batch_op.create_index("ix_signal_evaluations_evaluated_at", ["evaluated_at"], unique=False)
        batch_op.create_index(
            "ix_signal_evaluations_instrument_time", ["instrument_id", "evaluated_at"], unique=False
        )
        batch_op.create_index("ix_signal_evaluations_qualified", ["qualified"], unique=False)
        batch_op.create_index("ix_signal_evaluations_tracked", ["tracked_signal_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("signal_evaluations", schema=None) as batch_op:
        batch_op.drop_index("ix_signal_evaluations_tracked")
        batch_op.drop_index("ix_signal_evaluations_qualified")
        batch_op.drop_index("ix_signal_evaluations_instrument_time")
        batch_op.drop_index("ix_signal_evaluations_evaluated_at")

    op.drop_table("signal_evaluations")
    with op.batch_alter_table("watchlist", schema=None) as batch_op:
        batch_op.drop_index("ix_watchlist_enabled")

    op.drop_table("watchlist")
    with op.batch_alter_table("tracked_signals", schema=None) as batch_op:
        batch_op.drop_index("ix_tracked_signals_lifecycle")
        batch_op.drop_index("ix_tracked_signals_last_evaluated")
        batch_op.drop_index("ix_tracked_signals_instrument")

    op.drop_table("tracked_signals")
    with op.batch_alter_table("scan_runs", schema=None) as batch_op:
        batch_op.drop_index("ix_scan_runs_status")
        batch_op.drop_index("ix_scan_runs_scope_started")

    op.drop_table("scan_runs")
