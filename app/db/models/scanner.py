"""Scanner persistence: watchlist, signal lifecycle, evaluations and scan runs.

Four tables, and the relationship between two of them is the important part.

``tracked_signals`` is a **stable identity** for a continuing setup.
``signal_evaluations`` is **what tradabot knew at time T**. One signal has many
evaluations. A setup that persists across scans keeps one signal row and gains
one evaluation row per cycle -- so "how long has this been true?" and "what did
it look like an hour ago?" are both answerable, and neither is if every scan
invents a new signal.

The existing ``signals`` table is unchanged and is **not** duplicated:
``signal_evaluations.primary_signal_id`` points at it. That table already stores
a full single-timeframe scored snapshot; what phase 4 adds is the multi-timeframe
context around it.

The ML boundary
---------------
A ``signal_evaluations`` row is **X**: inputs known at time T, and nothing else.
Future outcome labels (returns after 1h/1d/5d, excursions, stop/target hits) are
phase 5's job and will live in a **separate table** joined on evaluation id.
Putting a label in this table would guarantee look-ahead leakage the first time
someone selects ``*``.
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
from app.db.types import UTCDateTime

# Mirrors app/db/models/signal.py: JSON on SQLite, JSONB on PostgreSQL. Declared
# per-module rather than shared because it is a two-line expression and the
# import graph is clearer without a types module that only holds aliases.
JSONColumn = JSON().with_variant(JSONB(), "postgresql")


class WatchlistEntry(Base):
    """An instrument the scanner should evaluate.

    Configuration, not logic: the scanner reads this table, so changing the
    universe is a database operation. Disabling is preferred over deleting --
    a deleted row loses the history of why it was ever there.
    """

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        doc="Higher is scanned first when a cycle is time-bounded. Ordering only.",
    )
    tags: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list, doc="Free-form labels, e.g. sector."
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("instrument_id", name="uq_watchlist_instrument"),
        Index("ix_watchlist_enabled", "enabled"),
    )

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid
        return f"<WatchlistEntry instrument={self.instrument_id} enabled={self.enabled}>"


class TrackedSignal(Base):
    """A continuing market setup, with one identity across many evaluations.

    Named ``TrackedSignal`` rather than ``Signal`` because the ``signals`` table
    already exists and means something narrower -- one scored snapshot at one
    timeframe. Reusing the name would make every future conversation ambiguous.
    """

    __tablename__ = "tracked_signals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )

    # -- Identity ---------------------------------------------------------
    # These four fields *are* the identity. Two evaluations belong to the same
    # signal when they match and the signal is still active -- see
    # app/scanner/lifecycle.py for the full rule.
    direction: Mapped[str] = mapped_column(
        String(8), nullable=False, doc="'LONG' or 'SHORT'. A reversal is a new signal."
    )
    primary_timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    horizon: Mapped[str] = mapped_column(String(8), nullable=False)
    setup: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc="Structural premise, e.g. 'BREAKOUT'. A breakout becoming a breakdown "
        "is a different setup, not the same one continuing.",
    )

    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False)

    current_score: Mapped[float] = mapped_column(Float, nullable=False)
    peak_score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        doc="Highest score reached. Preserved after a decline so 'how good did "
        "this get?' survives the setup weakening.",
    )
    current_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evaluation_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    # -- Lifecycle timestamps. Null means "has not happened". -------------
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    qualified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    strong_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    weakened_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        Index("ix_tracked_signals_lifecycle", "lifecycle"),
        Index("ix_tracked_signals_instrument", "instrument_id", "lifecycle"),
        Index("ix_tracked_signals_last_evaluated", "last_evaluated_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid
        return f"<TrackedSignal {self.instrument_id} {self.direction} {self.lifecycle}>"


class SignalEvaluation(Base):
    """What tradabot knew about one instrument at one instant. **This is X.**

    Written for *every* evaluated candidate, including the ones that scored badly
    and the ones nothing was notified about. A rejected candidate is training
    data: without negatives, a future model has nothing to learn the boundary
    from, and a dataset of winners only teaches survivorship.

    **No future-derived value belongs in this row.** Outcome labels arrive in
    phase 5, in their own table.
    """

    __tablename__ = "signal_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    tracked_signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracked_signals.id", ondelete="SET NULL"),
        nullable=True,
        doc="The continuing setup this observation belongs to, if any.",
    )
    primary_signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"),
        nullable=True,
        doc="The primary timeframe's full scored snapshot, in the existing "
        "`signals` table. Referenced rather than copied.",
    )
    scan_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True
    )
    backtest_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"),
        nullable=True,
        doc="Set only on observations manufactured by a historical replay. "
        "NULL means the live scanner produced it. This column is the isolation "
        "boundary of phase 5: research and production share one schema -- so a "
        "dataset is one shape rather than two -- but every production read "
        "filters on NULL, so a backtest can never inflate the live candidate "
        "list, the daily summary or the operations counts while the scheduler "
        "is running.",
    )

    evaluated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, doc="When the scanner ran."
    )
    market_data_timestamp: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
        doc="Newest bar the evaluation saw. The gap to `evaluated_at` is the "
        "data's age, which is what makes staleness auditable after the fact.",
    )

    # -- Verdict ----------------------------------------------------------
    score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", doc="-1, 0 or +1."
    )
    qualified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="0",
        doc="Whether it cleared the configured threshold. A *notification* "
        "decision, not a claim about the future.",
    )

    agreement: Mapped[float | None] = mapped_column(
        Float, nullable=True, doc="Multi-timeframe agreement, 0..1."
    )
    aligned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")

    # -- Cost and horizon heuristics --------------------------------------
    expected_move_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_edge_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_horizon: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # -- Liquidity --------------------------------------------------------
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    quote_age_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    # -- Context, preserved whole -----------------------------------------
    timeframe_states: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn,
        nullable=False,
        default=dict,
        doc="Every timeframe's assessment. Persisted individually, not collapsed "
        "into the score, so a future model can inspect the underlying context.",
    )
    trend_metrics: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    momentum_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    volume_metrics: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    volatility_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    structure_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    liquidity_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    reason_codes: Mapped[list[str]] = mapped_column(JSONColumn, nullable=False, default=list)
    risk_codes: Mapped[list[str]] = mapped_column(JSONColumn, nullable=False, default=list)

    # -- Provenance -------------------------------------------------------
    data_quality: Mapped[str] = mapped_column(String(16), nullable=False)
    session_phase: Mapped[str] = mapped_column(String(16), nullable=False)

    feature_set_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc="Which features were computed. Without it, rows from different "
        "versions are silently incomparable and a model trains on a moving target.",
    )
    signal_model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    scanner_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("ix_signal_evaluations_instrument_time", "instrument_id", "evaluated_at"),
        Index("ix_signal_evaluations_evaluated_at", "evaluated_at"),
        Index("ix_signal_evaluations_qualified", "qualified"),
        Index("ix_signal_evaluations_tracked", "tracked_signal_id"),
        Index("ix_signal_evaluations_backtest_run", "backtest_run_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid
        return f"<SignalEvaluation {self.instrument_id} score={self.score:.1f}>"


class ScanRun(Base):
    """One scan cycle: its metrics, and the lease that prevents overlap.

    The lease lives in the database rather than in process memory so that a
    second scheduled invocation -- from cron, from a restarted process, from a
    second machine -- cannot run concurrently with the first. ``lease_expires_at``
    is what stops a killed process from locking the scanner forever: a stale
    lease can be taken over, an unexpiring one would need manual intervention at
    exactly the wrong moment.
    """

    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    scope: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc="Which activity is locked, e.g. 'scan' or 'sync'. Separate scopes let "
        "a data sync and a signal scan hold independent leases.",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, doc="'running', 'completed' or 'failed'."
    )

    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    lease_owner: Mapped[str] = mapped_column(
        String(128), nullable=False, doc="host:pid. Identifies who holds it, for diagnosis."
    )
    lease_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    # -- Metrics ----------------------------------------------------------
    symbols_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    symbols_synced: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    symbols_evaluated: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    symbols_skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    symbols_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    candidates_discovered: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    signals_qualified: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    signals_strong: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    paper_decisions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    positions_opened: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    positions_closed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    error: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="Redacted failure text, if the cycle failed."
    )

    __table_args__ = (
        Index("ix_scan_runs_scope_started", "scope", "started_at"),
        Index("ix_scan_runs_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid
        return f"<ScanRun {self.scope} {self.status} evaluated={self.symbols_evaluated}>"
