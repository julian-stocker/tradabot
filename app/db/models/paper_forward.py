"""Persistence for the three-account forward paper experiment.

Four tables, one per question the experiment has to answer later:

``strategy_evaluations``
    Did we look at this session at all, and what did we find? Keeps
    ``NO_OPPORTUNITY`` distinguishable from ``FAILED`` and from never having run.
``strategy_candidates``
    What match-b-v1 selected, with enough provenance to reconstruct the decision.
``paper_account_decisions``
    What each account did with that candidate, and — critically — *why not*.
``paper_broker_orders``
    What the broker actually did, which is the only truth about a position.

Why account decisions get their own table
-----------------------------------------
The central result of this experiment is not P&L, it is **which opportunities
each capital tier could reach**. Phase 12.7 measured PAPER_1K reaching 27.4% of
candidates against PAPER_10K's 97.2%, with semiconductors — where the effect
concentrates — cut to 21%. A schema that recorded only executed trades would
make that invisible: the 836 refusals *are* the finding, so every one is stored
with its binding constraint.

No secret is persisted anywhere in this module.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.paper import MONEY_PRECISION, MONEY_SCALE, PRICE_PRECISION, PRICE_SCALE
from app.db.types import Money, UTCDateTime


class StrategyEvaluation(Base):
    """One session, evaluated once. The idempotency boundary for candidates."""

    __tablename__ = "strategy_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    universe_version: Mapped[str] = mapped_column(String(32), nullable=False)
    universe_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    session: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    data_cutoff: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    outcome: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        doc=(
            "CANDIDATES, NO_OPPORTUNITY or FAILED. Kept distinct because a quiet "
            "day and an outage must never render identically -- a dashboard that "
            "showed both as '0 candidates' would hide a broken system."
        ),
    )
    universe_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    eligible_symbols: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "strategy_version",
            "universe_version",
            "session",
            name="uq_strategy_evaluation_session",
        ),
        Index("ix_strategy_evaluations_session", "session"),
    )


class StrategyCandidate(Base):
    """One opportunity. Carries no direction, target or conviction."""

    __tablename__ = "strategy_candidates"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    evaluation_id: Mapped[int] = mapped_column(
        ForeignKey("strategy_evaluations.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL"), nullable=True
    )

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    session: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    rank: Mapped[Decimal] = mapped_column(Money(PRICE_PRECISION, PRICE_SCALE), nullable=False)
    sector: Mapped[str] = mapped_column(String(32), nullable=False)
    sector_etf_return_20d: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    movement_to_cost: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    atr_pct: Mapped[Decimal] = mapped_column(Money(PRICE_PRECISION, PRICE_SCALE), nullable=False)
    reference_price: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )

    __table_args__ = (Index("ix_strategy_candidates_session", "session", "symbol"),)


class PaperAccountDecision(Base):
    """What one account did with one candidate. **Refusals are the finding.**"""

    __tablename__ = "paper_account_decisions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(String(32), nullable=False)
    slot: Mapped[str] = mapped_column(String(16), nullable=False)
    account_role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc=(
            "CAPITAL_CONSTRAINT_ARM, TRANSITION_ARM or FULL_STRATEGY_REFERENCE_ARM. "
            "Reporting metadata only -- the three arms run identical logic, and a "
            "test asserts no alpha rule reads this column."
        ),
    )
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    equity: Mapped[Decimal] = mapped_column(Money(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    cash: Mapped[Decimal] = mapped_column(Money(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    effective_capital: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        doc="min(broker buying power, equity) -- the no-leverage cap, not margin.",
    )
    current_exposure: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False, server_default="0"
    )
    risk_budget: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False
    )

    risk_regime: Mapped[str | None] = mapped_column(String(16), nullable=True)
    risk_available: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    stop_distance: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=True
    )

    risk_sized_quantity: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE),
        nullable=True,
        doc="Before the whole-share floor. Stored so the size lost to rounding "
        "is measurable rather than inferred.",
    )
    proposed_quantity: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=True
    )
    proposed_notional: Mapped[Decimal | None] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=True
    )
    whole_share_feasible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")

    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    binding_constraint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        UniqueConstraint("candidate_id", "slot", name="uq_paper_decision_candidate_slot"),
        Index("ix_paper_decisions_slot_outcome", "slot", "outcome"),
    )


class PaperBrokerOrder(Base):
    """The broker's own record. **The only truth about a position.**"""

    __tablename__ = "paper_broker_orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    slot: Mapped[str] = mapped_column(String(16), nullable=False)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_class: Mapped[str] = mapped_column(String(16), nullable=False, server_default="simple")
    requested_quantity: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="NEW")
    filled_quantity: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False, server_default="0"
    )
    filled_avg_price: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=True
    )
    filled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)

    stop_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=True
    )
    target_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_price: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=True
    )
    protected_quantity: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE),
        nullable=False,
        server_default="0",
        doc=(
            "Quantity the broker actually holds protection for. When this is "
            "below ``filled_quantity`` the position is partly naked, which must "
            "surface as an error state rather than sit silently healthy."
        ),
    )

    entry_session: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    expiry_session: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        Index("ix_paper_broker_orders_slot_status", "slot", "status"),
        Index("ix_paper_broker_orders_candidate", "candidate_id"),
    )
