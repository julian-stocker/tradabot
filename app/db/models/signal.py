"""Persisted signal ORM model.

Phase 1 computed signals on demand and never stored them. Phase 2 needs them
stored for two reasons:

1. **Fan-out.** One signal is evaluated independently by many simulation
   profiles. Without a signal row there is nothing for those decisions to share,
   and each would have to duplicate the full evaluation context.
2. **Reproducibility.** Months later, "why did tradabot do that?" must be
   answerable. That requires the exact feature values and component scores as
   they were, not a recomputation against today's code.

Storage is a faithful record, not a summary: :attr:`feature_snapshot` and
:attr:`components` keep the full detail as JSON, and :attr:`engine_version` marks
which scoring code produced it. Comparing scores across engine versions is
meaningless without that column.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import Money, UTCDateTime
from app.domain.enums import Classification, Horizon, PriceSeriesAdjustment, Timeframe

PRICE_PRECISION = 18
PRICE_SCALE = 6
BPS_PRECISION = 18
BPS_SCALE = 4

# JSONB on PostgreSQL (indexable, binary); plain JSON on SQLite for tests.
JSONColumn = JSON().with_variant(JSONB(), "postgresql")


class SignalRow(Base, TimestampMixin):
    """A generated signal, stored for audit and multi-profile evaluation."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )

    bar_timestamp: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, doc="Timestamp of the bar the signal was computed on."
    )
    generated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, doc="When the computation ran."
    )
    timeframe: Mapped[Timeframe] = mapped_column(
        Enum(Timeframe, native_enum=False, length=8, validate_strings=True), nullable=False
    )
    horizon: Mapped[Horizon] = mapped_column(
        Enum(Horizon, native_enum=False, length=8, validate_strings=True), nullable=False
    )
    price_adjustment: Mapped[PriceSeriesAdjustment] = mapped_column(
        Enum(PriceSeriesAdjustment, native_enum=False, length=16, validate_strings=True),
        nullable=False,
        doc=(
            "Which price series the features were computed from. Without this a "
            "stored signal cannot be reproduced -- the same bar yields different "
            "features on raw versus split-adjusted prices."
        ),
    )

    score: Mapped[float] = mapped_column(Float, nullable=False)
    classification: Mapped[Classification] = mapped_column(
        Enum(Classification, native_enum=False, length=16, validate_strings=True), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    reference_price: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    spread_bps: Mapped[Decimal] = mapped_column(Money(BPS_PRECISION, BPS_SCALE), nullable=False)
    expected_move_bps: Mapped[Decimal] = mapped_column(
        Money(BPS_PRECISION, BPS_SCALE), nullable=False
    )
    cost_bps: Mapped[Decimal] = mapped_column(Money(BPS_PRECISION, BPS_SCALE), nullable=False)
    net_edge_bps: Mapped[Decimal] = mapped_column(Money(BPS_PRECISION, BPS_SCALE), nullable=False)

    bars_used: Mapped[int] = mapped_column(Integer, nullable=False)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False)

    feature_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, doc="Every feature value the signal was computed from."
    )
    components: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=False, doc="Component scores, weights and reasons."
    )

    __table_args__ = (
        # Recomputing the same signal upserts rather than duplicating. The engine
        # version is part of the key: a scoring change legitimately produces a
        # different signal for the same bar, and both should be retained.
        UniqueConstraint(
            "instrument_id",
            "timeframe",
            "horizon",
            "bar_timestamp",
            "engine_version",
            "price_adjustment",
            name="instrument_id_timeframe_horizon_bar_timestamp_engine_version_price_adjustment",
        ),
        CheckConstraint("score >= -100 AND score <= 100", name="score_range"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("reference_price > 0", name="reference_price_positive"),
        CheckConstraint("spread_bps >= 0", name="spread_bps_non_negative"),
        CheckConstraint("bars_used >= 0", name="bars_used_non_negative"),
        Index("ix_signals_instrument_id_bar_timestamp", "instrument_id", "bar_timestamp"),
        Index("ix_signals_generated_at", "generated_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Signal instrument={self.instrument_id} {self.classification} "
            f"score={self.score:.1f} @ {self.bar_timestamp.isoformat()}>"
        )
