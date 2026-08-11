"""Trade-decision ORM model.

Records what each simulation profile decided about each signal -- **including the
signals it declined**. A system that stores only what it did cannot measure what
it missed, and "would that skipped trade have worked?" is one of the more
valuable questions the feedback system will ask (docs/simulation-design.md).

Denormalisation is deliberate here. The economics that drove the decision (spread,
fees, slippage, capital, position size) are copied onto the row rather than
recomputed from the signal and the profile. Profiles are mutable: raising
``risk_per_trade`` next month must not silently rewrite the reason a decision was
made in March. The row is an immutable record of a moment.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import Money, UTCDateTime
from app.domain.enums import Classification, DecisionReason, Side, TradeDecisionType

PRICE_PRECISION = 18
PRICE_SCALE = 6
BPS_PRECISION = 18
BPS_SCALE = 4
QTY_PRECISION = 24
QTY_SCALE = 8


class TradeDecisionRow(Base, TimestampMixin):
    """One simulation profile's verdict on one signal."""

    __tablename__ = "trade_decisions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    signal_id: Mapped[int] = mapped_column(
        ForeignKey("signals.id", ondelete="CASCADE"), nullable=False
    )
    simulation_profile_id: Mapped[int] = mapped_column(
        ForeignKey("simulation_profiles.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"),
        nullable=False,
        doc="Denormalised from the signal so per-instrument queries need no join.",
    )

    decided_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    decision: Mapped[TradeDecisionType] = mapped_column(
        Enum(TradeDecisionType, native_enum=False, length=8, validate_strings=True), nullable=False
    )
    reason: Mapped[DecisionReason] = mapped_column(
        Enum(DecisionReason, native_enum=False, length=32, validate_strings=True),
        nullable=False,
        doc="Machine-readable reason code, so decisions are aggregatable.",
    )
    reason_detail: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        default="",
        doc="Human-readable explanation with the actual numbers.",
    )
    side: Mapped[Side | None] = mapped_column(
        Enum(Side, native_enum=False, length=8, validate_strings=True),
        nullable=True,
        doc="Intended direction; NULL when the decision was SKIP.",
    )

    # --- Signal snapshot -----------------------------------------------
    signal_score: Mapped[float] = mapped_column(Float, nullable=False)
    signal_classification: Mapped[Classification] = mapped_column(
        Enum(Classification, native_enum=False, length=16, validate_strings=True), nullable=False
    )
    signal_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    expected_move_bps: Mapped[Decimal] = mapped_column(
        Money(BPS_PRECISION, BPS_SCALE), nullable=False
    )

    # --- Market snapshot -------------------------------------------------
    reference_price: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    bid: Mapped[Decimal | None] = mapped_column(Money(PRICE_PRECISION, PRICE_SCALE), nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(Money(PRICE_PRECISION, PRICE_SCALE), nullable=True)
    spread_bps: Mapped[Decimal] = mapped_column(Money(BPS_PRECISION, BPS_SCALE), nullable=False)

    # --- Position economics at this profile's size ------------------------
    available_capital: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    position_quantity: Mapped[Decimal] = mapped_column(
        Money(QTY_PRECISION, QTY_SCALE),
        nullable=False,
        doc="Units sized for this profile. Zero when the decision was SKIP.",
    )
    position_notional: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    estimated_fees: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    estimated_spread_cost: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    estimated_slippage: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    estimated_total_cost: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    cost_bps_at_size: Mapped[Decimal] = mapped_column(
        Money(BPS_PRECISION, BPS_SCALE),
        nullable=False,
        doc=(
            "Round-trip cost in bps at THIS profile's position size. The number "
            "that differs most between a 50 EUR and a 5000 EUR portfolio."
        ),
    )
    net_edge_bps_at_size: Mapped[Decimal] = mapped_column(
        Money(BPS_PRECISION, BPS_SCALE),
        nullable=False,
        doc="expected_move_bps - cost_bps_at_size. The number the decision turns on.",
    )

    __table_args__ = (
        # One verdict per signal per profile. Re-evaluating updates in place.
        UniqueConstraint(
            "signal_id", "simulation_profile_id", name="signal_id_simulation_profile_id"
        ),
        CheckConstraint("position_quantity >= 0", name="position_quantity_non_negative"),
        CheckConstraint("position_notional >= 0", name="position_notional_non_negative"),
        CheckConstraint("estimated_fees >= 0", name="estimated_fees_non_negative"),
        CheckConstraint("available_capital >= 0", name="available_capital_non_negative"),
        CheckConstraint(
            "decision <> 'TRADE' OR (position_quantity > 0 AND side IS NOT NULL)",
            name="trade_requires_position",
        ),
        CheckConstraint(
            "decision <> 'SKIP' OR reason <> 'ACCEPTED'", name="skip_requires_skip_reason"
        ),
        Index(
            "ix_trade_decisions_simulation_profile_id_decided_at",
            "simulation_profile_id",
            "decided_at",
        ),
        Index("ix_trade_decisions_instrument_id_decided_at", "instrument_id", "decided_at"),
        Index("ix_trade_decisions_decision_reason", "decision", "reason"),
    )

    def __repr__(self) -> str:
        return (
            f"<TradeDecision {self.decision}/{self.reason} signal={self.signal_id} "
            f"profile={self.simulation_profile_id}>"
        )
