"""Backtesting interfaces.

Not implemented in phase 1. Defined now so that the bias constraints in
docs/backtesting.md are expressed as *type signatures* rather than as advice in a
document nobody re-reads.

The critical design choice is :class:`DataFeed`. It exposes only
``history(as_of)`` -- there is no method that returns the full series, and no way
to ask for a future bar. A strategy written against this interface cannot commit
look-ahead bias, because the interface gives it nothing to look ahead into. That
is a stronger guarantee than a code-review checklist.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

import polars as pl

from app.backtesting.models import Fill, Order, Position
from app.domain.enums import Timeframe


@runtime_checkable
class DataFeed(Protocol):
    """Point-in-time access to market data.

    Every method is bounded by ``as_of``. Implementations **must** exclude any bar
    whose close is at or after that instant: a bar stamped 14:30 for a 5-minute
    timeframe does not finish until 14:35, so at 14:32 it does not yet exist.
    """

    def history(
        self, symbol: str, timeframe: Timeframe, as_of: datetime, bars: int
    ) -> pl.DataFrame:
        """The last ``bars`` **closed** candles strictly before ``as_of``."""
        ...

    def timestamps(self, timeframe: Timeframe) -> Iterator[datetime]:
        """Bar timestamps to iterate over, ascending."""
        ...

    def universe(self, as_of: datetime) -> Sequence[str]:
        """Symbols tradable at ``as_of``.

        Must include instruments that were later delisted, and exclude those not
        yet listed. Returning today's survivors for a 2015 date is the definition
        of survivorship bias.
        """
        ...


@runtime_checkable
class ExecutionModel(Protocol):
    """Turns orders into fills.

    The only place fill prices are decided, so the realism of a backtest is
    entirely a property of this object.
    """

    def execute(
        self,
        order: Order,
        *,
        bar: pl.DataFrame,
        spread_bps: Decimal,
    ) -> Fill | None:
        """Fill ``order`` against ``bar``, or return ``None`` if it cannot fill.

        Implementations must:

        * fill on the bar **after** the signal bar, never on the signal bar's own
          close -- that close was not observable when the decision was made;
        * apply spread and slippage adversely on both legs;
        * refuse to fill on zero volume, and cap size against available volume
          rather than assuming infinite liquidity;
        * return ``None`` rather than inventing a fill, so unfilled orders show up
          in results instead of silently becoming free.
        """
        ...


@runtime_checkable
class Strategy(Protocol):
    """Decides what to trade.

    Receives only a :class:`DataFeed` bounded by the current bar, so it is
    structurally unable to see the future.
    """

    @property
    def name(self) -> str: ...

    def on_bar(
        self,
        *,
        timestamp: datetime,
        feed: DataFeed,
        positions: Sequence[Position],
    ) -> Sequence[Order]:
        """Orders to submit for the *next* bar, given information up to now."""
        ...
