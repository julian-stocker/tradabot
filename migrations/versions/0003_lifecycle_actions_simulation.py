"""Instrument lifecycle, corporate actions, simulation profiles, trade decisions

Revision ID: 0003
Revises: 0002
Phase 2 schema. Purely additive: no existing column is altered or dropped, so a
phase 1 database upgrades without touching stored candles.

`instruments.listed_at` / `delisted_at` are nullable with no backfill. NULL means
"unknown", which is the honest state for rows ingested before lifecycle tracking
existed -- inventing a listing date would be worse than admitting we lack one.

Create Date: 2026-08-11 12:15:17.886679

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import app.db.types

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "broker_cost_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("order_fee", app.db.types.Money(precision=18, scale=6), nullable=False),
        sa.Column("variable_fee_rate", app.db.types.Money(precision=9, scale=6), nullable=False),
        sa.Column(
            "slippage_spread_multiple", app.db.types.Money(precision=9, scale=6), nullable=False
        ),
        sa.Column("default_spread_bps", app.db.types.Money(precision=9, scale=6), nullable=False),
        sa.Column("min_order_notional", app.db.types.Money(precision=18, scale=6), nullable=False),
        sa.Column(
            "created_at",
            app.db.types.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.db.types.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "default_spread_bps >= 0",
            name=op.f("ck_broker_cost_profiles_default_spread_non_negative"),
        ),
        sa.CheckConstraint(
            "min_order_notional >= 0",
            name=op.f("ck_broker_cost_profiles_min_order_notional_non_negative"),
        ),
        sa.CheckConstraint(
            "order_fee >= 0", name=op.f("ck_broker_cost_profiles_order_fee_non_negative")
        ),
        sa.CheckConstraint(
            "slippage_spread_multiple >= 0",
            name=op.f("ck_broker_cost_profiles_slippage_non_negative"),
        ),
        sa.CheckConstraint(
            "variable_fee_rate >= 0 AND variable_fee_rate <= 1",
            name=op.f("ck_broker_cost_profiles_variable_fee_rate_fraction"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broker_cost_profiles")),
        sa.UniqueConstraint("name", name=op.f("uq_broker_cost_profiles_name")),
    )
    op.create_table(
        "risk_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("risk_per_trade", app.db.types.Money(precision=9, scale=6), nullable=False),
        sa.Column("max_position_percent", app.db.types.Money(precision=9, scale=6), nullable=False),
        sa.Column("max_total_exposure", app.db.types.Money(precision=9, scale=6), nullable=False),
        sa.Column("max_open_positions", sa.Integer(), nullable=False),
        sa.Column("max_daily_loss", app.db.types.Money(precision=9, scale=6), nullable=False),
        sa.Column("max_drawdown", app.db.types.Money(precision=9, scale=6), nullable=False),
        sa.Column("min_signal_score", app.db.types.Money(precision=9, scale=6), nullable=False),
        sa.Column("min_confidence", app.db.types.Money(precision=9, scale=6), nullable=False),
        sa.Column("require_positive_net_edge", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("allow_short", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            app.db.types.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.db.types.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "max_daily_loss > 0 AND max_daily_loss <= 1",
            name=op.f("ck_risk_profiles_max_daily_loss_fraction"),
        ),
        sa.CheckConstraint(
            "max_drawdown > 0 AND max_drawdown <= 1",
            name=op.f("ck_risk_profiles_max_drawdown_fraction"),
        ),
        sa.CheckConstraint(
            "max_open_positions >= 1", name=op.f("ck_risk_profiles_max_open_positions_positive")
        ),
        sa.CheckConstraint(
            "max_position_percent > 0 AND max_position_percent <= 1",
            name=op.f("ck_risk_profiles_max_position_percent_fraction"),
        ),
        sa.CheckConstraint(
            "max_total_exposure > 0", name=op.f("ck_risk_profiles_max_total_exposure_positive")
        ),
        sa.CheckConstraint(
            "min_confidence >= 0 AND min_confidence <= 1",
            name=op.f("ck_risk_profiles_min_confidence_range"),
        ),
        sa.CheckConstraint(
            "min_signal_score >= 0 AND min_signal_score <= 100",
            name=op.f("ck_risk_profiles_min_signal_score_range"),
        ),
        sa.CheckConstraint(
            "risk_per_trade <= max_position_percent",
            name=op.f("ck_risk_profiles_risk_within_position_cap"),
        ),
        sa.CheckConstraint(
            "risk_per_trade > 0 AND risk_per_trade <= 1",
            name=op.f("ck_risk_profiles_risk_per_trade_fraction"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_profiles")),
        sa.UniqueConstraint("name", name=op.f("uq_risk_profiles_name")),
    )
    op.create_table(
        "corporate_actions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column(
            "action_type",
            sa.Enum(
                "SPLIT",
                "CASH_DIVIDEND",
                "STOCK_DIVIDEND",
                "SPIN_OFF",
                "MERGER",
                "SYMBOL_CHANGE",
                name="corporateactiontype",
                native_enum=False,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column("effective_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("payment_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("from_shares", app.db.types.Money(precision=18, scale=6), nullable=True),
        sa.Column("to_shares", app.db.types.Money(precision=18, scale=6), nullable=True),
        sa.Column("cash_amount", app.db.types.Money(precision=18, scale=6), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("source", sa.String(length=32), server_default="unknown", nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            app.db.types.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.db.types.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action_type <> 'CASH_DIVIDEND' OR (cash_amount IS NOT NULL AND cash_amount > 0 AND currency IS NOT NULL)",
            name=op.f("ck_corporate_actions_dividend_requires_amount"),
        ),
        sa.CheckConstraint(
            "action_type <> 'SPLIT' OR (from_shares IS NOT NULL AND to_shares IS NOT NULL  AND from_shares > 0 AND to_shares > 0)",
            name=op.f("ck_corporate_actions_split_requires_ratio"),
        ),
        sa.CheckConstraint(
            "currency IS NULL OR length(currency) = 3",
            name=op.f("ck_corporate_actions_currency_iso4217"),
        ),
        sa.CheckConstraint(
            "payment_at IS NULL OR payment_at >= effective_at",
            name=op.f("ck_corporate_actions_payment_after_effective"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_corporate_actions_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corporate_actions")),
        sa.UniqueConstraint(
            "instrument_id",
            "action_type",
            "effective_at",
            name="instrument_id_action_type_effective_at",
        ),
    )
    with op.batch_alter_table("corporate_actions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_corporate_actions_instrument_id_effective_at",
            ["instrument_id", "effective_at"],
            unique=False,
        )

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("bar_timestamp", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("generated_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column(
            "timeframe",
            sa.Enum(
                "M1",
                "M5",
                "M15",
                "M30",
                "H1",
                "H4",
                "D1",
                "W1",
                name="timeframe",
                native_enum=False,
                length=8,
            ),
            nullable=False,
        ),
        sa.Column(
            "horizon",
            sa.Enum(
                "M30",
                "H2",
                "D1",
                "D3",
                "D5",
                "D20",
                "MO1",
                "MO3",
                "MO6",
                name="horizon",
                native_enum=False,
                length=8,
            ),
            nullable=False,
        ),
        sa.Column(
            "price_adjustment",
            sa.Enum(
                "RAW",
                "SPLIT_ADJUSTED",
                "TOTAL_RETURN",
                name="priceseriesadjustment",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column(
            "classification",
            sa.Enum(
                "STRONG_BEARISH",
                "BEARISH",
                "NEUTRAL",
                "BULLISH",
                "STRONG_BULLISH",
                name="classification",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reference_price", app.db.types.Money(precision=18, scale=6), nullable=False),
        sa.Column("spread_bps", app.db.types.Money(precision=18, scale=4), nullable=False),
        sa.Column("expected_move_bps", app.db.types.Money(precision=18, scale=4), nullable=False),
        sa.Column("cost_bps", app.db.types.Money(precision=18, scale=4), nullable=False),
        sa.Column("net_edge_bps", app.db.types.Money(precision=18, scale=4), nullable=False),
        sa.Column("bars_used", sa.Integer(), nullable=False),
        sa.Column("engine_version", sa.String(length=64), nullable=False),
        sa.Column(
            "feature_snapshot",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "components",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            app.db.types.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.db.types.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint("bars_used >= 0", name=op.f("ck_signals_bars_used_non_negative")),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_signals_confidence_range")
        ),
        sa.CheckConstraint("reference_price > 0", name=op.f("ck_signals_reference_price_positive")),
        sa.CheckConstraint("score >= -100 AND score <= 100", name=op.f("ck_signals_score_range")),
        sa.CheckConstraint("spread_bps >= 0", name=op.f("ck_signals_spread_bps_non_negative")),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_signals_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signals")),
        sa.UniqueConstraint(
            "instrument_id",
            "timeframe",
            "horizon",
            "bar_timestamp",
            "engine_version",
            "price_adjustment",
            name="instrument_id_timeframe_horizon_bar_timestamp_engine_version_price_adjustment",
        ),
    )
    with op.batch_alter_table("signals", schema=None) as batch_op:
        batch_op.create_index("ix_signals_generated_at", ["generated_at"], unique=False)
        batch_op.create_index(
            "ix_signals_instrument_id_bar_timestamp",
            ["instrument_id", "bar_timestamp"],
            unique=False,
        )

    op.create_table(
        "simulation_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("initial_capital", app.db.types.Money(precision=18, scale=6), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("risk_profile_id", sa.Integer(), nullable=False),
        sa.Column("broker_cost_profile_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            app.db.types.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.db.types.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "initial_capital > 0", name=op.f("ck_simulation_profiles_initial_capital_positive")
        ),
        sa.CheckConstraint(
            "length(currency) = 3", name=op.f("ck_simulation_profiles_currency_iso4217")
        ),
        sa.ForeignKeyConstraint(
            ["broker_cost_profile_id"],
            ["broker_cost_profiles.id"],
            name=op.f("fk_simulation_profiles_broker_cost_profile_id_broker_cost_profiles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["risk_profile_id"],
            ["risk_profiles.id"],
            name=op.f("fk_simulation_profiles_risk_profile_id_risk_profiles"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_simulation_profiles")),
        sa.UniqueConstraint("name", name=op.f("uq_simulation_profiles_name")),
    )
    op.create_table(
        "trade_decisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("signal_id", sa.Integer(), nullable=False),
        sa.Column("simulation_profile_id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("decided_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column(
            "decision",
            sa.Enum("TRADE", "SKIP", name="tradedecisiontype", native_enum=False, length=8),
            nullable=False,
        ),
        sa.Column(
            "reason",
            sa.Enum(
                "ACCEPTED",
                "SCORE_BELOW_THRESHOLD",
                "CONFIDENCE_BELOW_THRESHOLD",
                "CLASSIFICATION_NEUTRAL",
                "NEGATIVE_NET_EDGE",
                "POSITION_BELOW_MIN_NOTIONAL",
                "INSUFFICIENT_CAPITAL",
                "SHORT_NOT_PERMITTED",
                "PROFILE_DISABLED",
                name="decisionreason",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("reason_detail", sa.String(length=500), nullable=False),
        sa.Column(
            "side",
            sa.Enum("LONG", "SHORT", name="side", native_enum=False, length=8),
            nullable=True,
        ),
        sa.Column("signal_score", sa.Float(), nullable=False),
        sa.Column(
            "signal_classification",
            sa.Enum(
                "STRONG_BEARISH",
                "BEARISH",
                "NEUTRAL",
                "BULLISH",
                "STRONG_BULLISH",
                name="classification",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("signal_confidence", sa.Float(), nullable=False),
        sa.Column("expected_move_bps", app.db.types.Money(precision=18, scale=4), nullable=False),
        sa.Column("reference_price", app.db.types.Money(precision=18, scale=6), nullable=False),
        sa.Column("bid", app.db.types.Money(precision=18, scale=6), nullable=True),
        sa.Column("ask", app.db.types.Money(precision=18, scale=6), nullable=True),
        sa.Column("spread_bps", app.db.types.Money(precision=18, scale=4), nullable=False),
        sa.Column("available_capital", app.db.types.Money(precision=18, scale=6), nullable=False),
        sa.Column("position_quantity", app.db.types.Money(precision=24, scale=8), nullable=False),
        sa.Column("position_notional", app.db.types.Money(precision=18, scale=6), nullable=False),
        sa.Column("estimated_fees", app.db.types.Money(precision=18, scale=6), nullable=False),
        sa.Column(
            "estimated_spread_cost", app.db.types.Money(precision=18, scale=6), nullable=False
        ),
        sa.Column("estimated_slippage", app.db.types.Money(precision=18, scale=6), nullable=False),
        sa.Column(
            "estimated_total_cost", app.db.types.Money(precision=18, scale=6), nullable=False
        ),
        sa.Column("cost_bps_at_size", app.db.types.Money(precision=18, scale=4), nullable=False),
        sa.Column(
            "net_edge_bps_at_size", app.db.types.Money(precision=18, scale=4), nullable=False
        ),
        sa.Column(
            "created_at",
            app.db.types.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.db.types.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision <> 'SKIP' OR reason <> 'ACCEPTED'",
            name=op.f("ck_trade_decisions_skip_requires_skip_reason"),
        ),
        sa.CheckConstraint(
            "decision <> 'TRADE' OR (position_quantity > 0 AND side IS NOT NULL)",
            name=op.f("ck_trade_decisions_trade_requires_position"),
        ),
        sa.CheckConstraint(
            "available_capital >= 0", name=op.f("ck_trade_decisions_available_capital_non_negative")
        ),
        sa.CheckConstraint(
            "estimated_fees >= 0", name=op.f("ck_trade_decisions_estimated_fees_non_negative")
        ),
        sa.CheckConstraint(
            "position_notional >= 0", name=op.f("ck_trade_decisions_position_notional_non_negative")
        ),
        sa.CheckConstraint(
            "position_quantity >= 0", name=op.f("ck_trade_decisions_position_quantity_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_trade_decisions_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["signals.id"],
            name=op.f("fk_trade_decisions_signal_id_signals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["simulation_profile_id"],
            ["simulation_profiles.id"],
            name=op.f("fk_trade_decisions_simulation_profile_id_simulation_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trade_decisions")),
        sa.UniqueConstraint(
            "signal_id", "simulation_profile_id", name="signal_id_simulation_profile_id"
        ),
    )
    with op.batch_alter_table("trade_decisions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_trade_decisions_decision_reason", ["decision", "reason"], unique=False
        )
        batch_op.create_index(
            "ix_trade_decisions_instrument_id_decided_at",
            ["instrument_id", "decided_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_trade_decisions_simulation_profile_id_decided_at",
            ["simulation_profile_id", "decided_at"],
            unique=False,
        )

    with op.batch_alter_table("instruments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("listed_at", app.db.types.UTCDateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("delisted_at", app.db.types.UTCDateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            "ix_instruments_listed_at_delisted_at", ["listed_at", "delisted_at"], unique=False
        )
        batch_op.create_check_constraint(
            batch_op.f("ck_instruments_lifecycle_ordered"),
            "listed_at IS NULL OR delisted_at IS NULL OR delisted_at > listed_at",
        )


def downgrade() -> None:
    with op.batch_alter_table("instruments", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("ck_instruments_lifecycle_ordered"), type_="check")
        batch_op.drop_index("ix_instruments_listed_at_delisted_at")
        batch_op.drop_column("delisted_at")
        batch_op.drop_column("listed_at")

    with op.batch_alter_table("trade_decisions", schema=None) as batch_op:
        batch_op.drop_index("ix_trade_decisions_simulation_profile_id_decided_at")
        batch_op.drop_index("ix_trade_decisions_instrument_id_decided_at")
        batch_op.drop_index("ix_trade_decisions_decision_reason")

    op.drop_table("trade_decisions")
    op.drop_table("simulation_profiles")
    with op.batch_alter_table("signals", schema=None) as batch_op:
        batch_op.drop_index("ix_signals_instrument_id_bar_timestamp")
        batch_op.drop_index("ix_signals_generated_at")

    op.drop_table("signals")
    with op.batch_alter_table("corporate_actions", schema=None) as batch_op:
        batch_op.drop_index("ix_corporate_actions_instrument_id_effective_at")

    op.drop_table("corporate_actions")
    op.drop_table("risk_profiles")
    op.drop_table("broker_cost_profiles")
