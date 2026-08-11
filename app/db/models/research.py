"""Research persistence: backtest runs and outcome labels. **This is Y.**

Three tables, and the split between two of them is the point.

``signal_outcomes`` records what the *market* did after an evaluation: the
return over a horizon, the excursions, which barrier was touched first. It knows
nothing about capital or costs, so it is the same fact for every portfolio.

``trade_outcomes`` records what *we* would have made: sized against a specific
portfolio, charged a specific cost model, possibly rejected outright. One
evaluation therefore has one market outcome per horizon but up to one trade
outcome per portfolio, and the two disagree constantly -- a signal the market
proved right by 40 bps is a loss after a round trip costing 60.

Collapsing them into one table would force a choice between storing the market
fact three times or letting portfolio-specific costs contaminate it. Both are
worse.

Why labels are not columns on ``signal_evaluations``
----------------------------------------------------
Because ``SELECT *`` is the most common query anyone writes. A future-derived
column sitting next to the features is leakage waiting for the first careless
join, and no amount of naming discipline survives contact with a notebook. The
join is deliberate friction: it forces the feature/label boundary to be stated
every single time it is crossed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import Money, UTCDateTime

JSONColumn = JSON().with_variant(JSONB(), "postgresql")


class BacktestRun(Base):
    """One historical replay, with everything needed to reproduce it.

    The configuration is stored rather than merely logged because a result whose
    inputs are unknown cannot be checked, only believed. Every strategy-affecting
    version is a column, so two runs that disagree can be told apart without
    guessing which code produced which number.

    Never stores credentials: the provider is named, its keys are not.
    """

    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        doc="Deterministic digest of the configuration and every strategy version. "
        "Indexed but **not unique**: it identifies the configuration, not the row. "
        "Running the same configuration twice is legitimate and is precisely how "
        "reproducibility is checked -- two runs sharing a key must produce the "
        "same numbers, which cannot be verified if the second insert is refused.",
    )

    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    from_timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    to_timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    universe_definition: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn,
        nullable=False,
        default=dict,
        doc="Scope mode and the resolved symbol list. Resolved, not just the "
        "mode: 'active universe' means something different next month.",
    )
    primary_timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    regular_session_only: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")

    feature_set_version: Mapped[str] = mapped_column(String(32), nullable=False)
    signal_model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    scanner_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    cost_model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    label_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="RUNNING")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    symbols_processed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    metrics: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_backtest_runs_started_at", "started_at"),
        Index("ix_backtest_runs_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid
        return f"<BacktestRun {self.id} {self.status} obs={self.observation_count}>"


class SignalOutcome(Base):
    """What the market did after one evaluation, over one horizon.

    Nullable outcome columns with a ``status`` beside them, never zeros. A
    horizon that has not elapsed is ``PENDING``; one whose bars are missing is
    ``INSUFFICIENT_FUTURE_DATA``. Writing 0.0 for either would be indistinguishable
    from a flat market and would drag every mean toward zero, worst of all for the
    most recent observations.
    """

    __tablename__ = "signal_outcomes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    evaluation_id: Mapped[int] = mapped_column(
        ForeignKey("signal_evaluations.id", ondelete="CASCADE"), nullable=False
    )
    horizon: Mapped[str] = mapped_column(String(8), nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="1",
        doc="Direction the label is computed for. +1 today; the column exists so "
        "SHORT research needs no migration, not because shorts are supported.",
    )

    reference_timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    reference_price: Mapped[Any] = mapped_column(Money(), nullable=False)
    future_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    future_price: Mapped[Any] = mapped_column(Money(), nullable=True)

    raw_return: Mapped[float | None] = mapped_column(
        Float, nullable=True, doc="Simple return over the horizon, as a ratio."
    )
    mfe: Mapped[float | None] = mapped_column(
        Float, nullable=True, doc="Maximum favourable excursion, ratio. Not realised P/L."
    )
    mae: Mapped[float | None] = mapped_column(
        Float, nullable=True, doc="Maximum adverse excursion, ratio. Not realised P/L."
    )

    target_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    stop_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    barrier_outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    time_to_target_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_to_stop_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    ambiguous_bar_timestamp: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
        doc="Set when one candle spanned both barriers, so such rows can be "
        "excluded from any statistic that depends on the ordering.",
    )

    label_timeframe: Mapped[str | None] = mapped_column(
        String(8),
        nullable=True,
        doc="Series the excursions were measured on. An MFE from daily bars and "
        "one from 5-minute bars are different measurements.",
    )
    bars_observed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rolled_to_next_session: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0"
    )

    label_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "evaluation_id",
            "horizon",
            "direction",
            "label_policy_version",
            name="uq_signal_outcome_evaluation_horizon",
        ),
        Index("ix_signal_outcomes_evaluation", "evaluation_id"),
        Index("ix_signal_outcomes_status", "status"),
        Index("ix_signal_outcomes_horizon", "horizon"),
    )

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid
        return f"<SignalOutcome eval={self.evaluation_id} {self.horizon} {self.status}>"


class TradeOutcome(Base):
    """What one portfolio's execution model would have produced.

    Separate per portfolio because that is the question phase 5 exists to answer:
    the same signal sized into 100 EUR and into 10,000 EUR are not the same trade.
    A flat fee that is 1% of the small account's capital and 0.01% of the large
    one's turns identical market outcomes into opposite results, and only a
    per-portfolio row can show it.

    ``cost_basis`` is mandatory. Every historical cost here is ``MODELLED`` --
    tradabot stores no historical quotes -- and a report that presented these as
    observed spreads would be making a claim the data cannot support.
    """

    __tablename__ = "trade_outcomes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    evaluation_id: Mapped[int] = mapped_column(
        ForeignKey("signal_evaluations.id", ondelete="CASCADE"), nullable=False
    )
    backtest_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=True
    )
    simulation_profile_id: Mapped[int] = mapped_column(
        ForeignKey("simulation_profiles.id", ondelete="CASCADE"), nullable=False
    )
    profile_key: Mapped[str] = mapped_column(String(32), nullable=False)

    executed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    rejection_reason: Mapped[str | None] = mapped_column(
        String(48),
        nullable=True,
        doc="Why no trade happened. A rejected signal is data: 'insufficient "
        "capital' at 100 EUR is the finding, not a missing row.",
    )

    entry_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    entry_price: Mapped[Any] = mapped_column(Money(), nullable=True)
    exit_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    exit_price: Mapped[Any] = mapped_column(Money(), nullable=True)
    quantity: Mapped[Any] = mapped_column(Money(), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    gross_pnl: Mapped[Any] = mapped_column(Money(), nullable=True)
    fees: Mapped[Any] = mapped_column(Money(), nullable=True)
    spread_cost: Mapped[Any] = mapped_column(Money(), nullable=True)
    slippage_cost: Mapped[Any] = mapped_column(Money(), nullable=True)
    net_pnl: Mapped[Any] = mapped_column(Money(), nullable=True)
    net_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    holding_period_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    modelled_spread_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_basis: Mapped[str] = mapped_column(String(16), nullable=False)
    spread_quality: Mapped[str | None] = mapped_column(String(24), nullable=True)
    session_phase: Mapped[str | None] = mapped_column(String(16), nullable=True)

    cost_model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    label_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "evaluation_id",
            "simulation_profile_id",
            "backtest_run_id",
            "cost_model_version",
            name="uq_trade_outcome_evaluation_profile",
        ),
        Index("ix_trade_outcomes_evaluation", "evaluation_id"),
        Index("ix_trade_outcomes_run", "backtest_run_id"),
        Index("ix_trade_outcomes_profile", "profile_key"),
    )

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid
        return (
            f"<TradeOutcome eval={self.evaluation_id} {self.profile_key} executed={self.executed}>"
        )
