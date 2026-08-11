"""Candle and quote wire schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import Timeframe


class CandleResponse(BaseModel):
    """One OHLCV bar.

    Prices are serialised as JSON *strings* rather than numbers. JSON has no
    decimal type, so a float round-trip would reintroduce exactly the binary
    rounding the storage layer works to avoid. Clients should parse these with a
    decimal type, not ``parseFloat``.
    """

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    timestamp: datetime = Field(description="Bar open time (left edge), UTC.")
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int | None = None
    vwap: Decimal | None = None


class CandleSeriesResponse(BaseModel):
    """A series of candles with its context."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: Timeframe
    start: datetime
    end: datetime
    count: int
    candles: list[CandleResponse]


class QuoteResponse(BaseModel):
    """Top-of-book quote with derived spread metrics."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    timestamp: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    mid_price: Decimal
    spread_absolute: Decimal
    spread_percent: float
    spread_bps: float = Field(description="Spread in basis points of mid price.")
