"""Quote value object and spread arithmetic.

Spread maths lives here rather than in ``app.costs`` because a quote's spread is
a property of the quote itself, while ``app.costs`` is about what a *round trip*
through a specific broker costs.

All monetary values are :class:`~decimal.Decimal`. Spread *ratios* are returned as
``float`` because they are unitless statistics, not money.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.time import ensure_utc

BPS_PER_UNIT = Decimal(10_000)


class Quote(BaseModel):
    """A single top-of-book bid/ask observation.

    Invariants enforced at construction time:
      * ``bid`` and ``ask`` are strictly positive
      * ``ask >= bid`` (a crossed book is a data error, not something to average)
      * ``timestamp`` is timezone-aware and normalised to UTC
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    timestamp: datetime
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    bid_size: Decimal | None = Field(default=None, ge=0)
    ask_size: Decimal | None = Field(default=None, ge=0)

    @field_validator("timestamp")
    @classmethod
    def _normalise_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _check_not_crossed(self) -> Quote:
        if self.ask < self.bid:
            msg = f"crossed quote for {self.symbol}: bid={self.bid} > ask={self.ask}"
            raise ValueError(msg)
        return self

    @property
    def mid_price(self) -> Decimal:
        """(bid + ask) / 2 -- the reference price for cost accounting."""
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread_absolute(self) -> Decimal:
        """ask - bid, in account currency."""
        return self.ask - self.bid

    @property
    def half_spread(self) -> Decimal:
        """Cost of crossing the book once, relative to the mid price."""
        return self.spread_absolute / Decimal(2)

    @property
    def spread_percent(self) -> float:
        """Spread as a percentage of the mid price."""
        return float(self.spread_absolute / self.mid_price * Decimal(100))

    @property
    def spread_bps(self) -> float:
        """Spread in basis points of the mid price.

        Basis points are the preferred unit internally: they compose additively
        with fees and slippage and avoid percent-of-percent confusion.
        """
        return float(self.spread_absolute / self.mid_price * BPS_PER_UNIT)


def spread_bps_from_prices(bid: Decimal, ask: Decimal) -> float:
    """Spread in basis points for a raw bid/ask pair.

    Convenience for callers that do not have a full :class:`Quote`.
    """
    if bid <= 0 or ask <= 0:
        msg = f"bid and ask must be positive, got bid={bid} ask={ask}"
        raise ValueError(msg)
    if ask < bid:
        msg = f"crossed quote: bid={bid} > ask={ask}"
        raise ValueError(msg)
    mid = (bid + ask) / Decimal(2)
    return float((ask - bid) / mid * BPS_PER_UNIT)
