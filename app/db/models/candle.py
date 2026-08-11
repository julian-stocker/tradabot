"""OHLCV candle ORM model."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, Enum, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import Money, UTCDateTime
from app.domain.enums import Timeframe

# Prices: 18 significant digits, 6 decimals -- covers sub-cent ticks and any
# realistic equity price. Volume gets a wider integer range and 4 decimals for
# fractional-share and crypto quantities.
PRICE_PRECISION = 18
PRICE_SCALE = 6
VOLUME_PRECISION = 24
VOLUME_SCALE = 4


class Candle(Base):
    """One OHLCV bar for an instrument at a given timeframe.

    Primary key is ``(instrument_id, timeframe, timestamp)``. That composite is
    simultaneously:

    * the natural identity of a bar, making ingestion idempotent via upsert;
    * the index that answers the core query -- "all 5-minute candles for NVDA
      between t0 and t1" -- as a single range scan with no extra index needed;
    * TimescaleDB-compatible, because the partitioning column (``timestamp``)
      is part of every unique index, which Timescale requires.

    ``timestamp`` is the bar's **open** time (left edge), in UTC. A 5-minute bar
    stamped 14:30 covers [14:30, 14:35). This convention matters enormously for
    look-ahead bias: a bar stamped 14:30 is only *complete* at 14:35, so anything
    consuming live data must not treat the current bar as final.
    """

    __tablename__ = "candles"

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    timeframe: Mapped[Timeframe] = mapped_column(
        Enum(Timeframe, native_enum=False, length=8, validate_strings=True),
        primary_key=True,
    )
    timestamp: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        primary_key=True,
        doc="Bar open time (left edge of the interval), UTC.",
    )

    open: Mapped[Decimal] = mapped_column(Money(PRICE_PRECISION, PRICE_SCALE), nullable=False)
    high: Mapped[Decimal] = mapped_column(Money(PRICE_PRECISION, PRICE_SCALE), nullable=False)
    low: Mapped[Decimal] = mapped_column(Money(PRICE_PRECISION, PRICE_SCALE), nullable=False)
    close: Mapped[Decimal] = mapped_column(Money(PRICE_PRECISION, PRICE_SCALE), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Money(VOLUME_PRECISION, VOLUME_SCALE), nullable=False)

    trade_count: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        doc="Number of trades in the bar, when the provider reports it.",
    )
    vwap: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE),
        nullable=True,
        doc="Volume-weighted average price, when the provider reports it.",
    )

    __table_args__ = (
        # Structural sanity. A provider that violates these is broken, and we want
        # to find out at ingest time rather than three features downstream.
        CheckConstraint("high >= low", name="high_ge_low"),
        CheckConstraint("high >= open AND high >= close", name="high_is_max"),
        CheckConstraint("low <= open AND low <= close", name="low_is_min"),
        CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0", name="prices_positive"),
        CheckConstraint("volume >= 0", name="volume_non_negative"),
        CheckConstraint("trade_count IS NULL OR trade_count >= 0", name="trade_count_non_negative"),
        # Supports "the newest bars across all instruments for a timeframe" --
        # the access pattern the future scanner needs, which the instrument-first
        # primary key cannot serve.
        Index("ix_candles_timeframe_timestamp", "timeframe", "timestamp"),
    )

    def __repr__(self) -> str:
        return (
            f"<Candle instrument={self.instrument_id} {self.timeframe} "
            f"{self.timestamp.isoformat()} c={self.close}>"
        )
