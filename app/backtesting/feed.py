"""A point-in-time :class:`~app.backtesting.protocols.DataFeed` over stored candles.

Implements the phase-1 protocol that was written before any engine existed. Its
one design rule -- expose ``history(as_of)`` and nothing that returns a full
series -- is what makes look-ahead a *type error* rather than a review comment,
so the implementation must not quietly add an escape hatch.

Every read goes through :meth:`CandleRepository.get_latest` with an ``as_of``,
which since phase 5 excludes bars that had not finished at that instant. The feed
therefore inherits the bar-close rule instead of reimplementing it; there is one
definition of "knowable", and both the live scanner and this share it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime

import polars as pl
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc
from app.db.models import Candle
from app.domain.enums import Timeframe
from app.features.frame import candles_to_frame
from app.market_data.repository import CandleRepository


class HistoricalDataFeed:
    """Point-in-time market data for one instrument universe.

    Async, unlike the synchronous protocol: the repository is async all the way
    down, and a thread-blocking bridge inside a replay loop would be far worse
    than a slightly different signature. :meth:`history_sync` is provided for the
    protocol-shaped call sites that genuinely need it.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        instrument_ids: dict[str, int],
    ) -> None:
        self._candles = CandleRepository(session)
        self._instrument_ids = {symbol.upper(): ident for symbol, ident in instrument_ids.items()}
        self._cache: dict[tuple[str, Timeframe, datetime, int], list[Candle]] = {}

    async def bars(
        self, symbol: str, timeframe: Timeframe, as_of: datetime, bars: int
    ) -> list[Candle]:
        """The last ``bars`` **closed** candles at or before ``as_of``."""
        instrument_id = self._instrument_ids.get(symbol.upper())
        if instrument_id is None:
            return []
        rows = await self._candles.get_latest(
            instrument_id=instrument_id,
            timeframe=timeframe,
            limit=bars,
            as_of=ensure_utc(as_of),
        )
        return list(rows)

    async def history(
        self, symbol: str, timeframe: Timeframe, as_of: datetime, bars: int
    ) -> pl.DataFrame:
        """The protocol's frame form."""
        return candles_to_frame(await self.bars(symbol, timeframe, as_of, bars))

    async def execution_window(
        self,
        *,
        instrument_id: int,
        timeframe: Timeframe,
        after: datetime,
        bars: int,
    ) -> tuple[Candle | None, list[Candle]]:
        """The entry bar and the bars available to exit into.

        This is the execution primitive, and it is kept apart from
        :meth:`history` on purpose: it deliberately reaches *forward*, which is
        exactly what a strategy must never do. Separating them means no code path
        that computes features can reach a future bar, even by accident.

        ``after`` is a signal instant, which is a bar **close**. The entry bar is
        the first bar opening at or after it -- never the bar that just closed,
        whose open is already in the past.
        """
        rows = await self._candles.get_range(
            instrument_id=instrument_id,
            timeframe=timeframe,
            start=ensure_utc(after),
            end=ensure_utc(after) + timeframe.duration * bars,
            limit=bars,
        )
        if not rows:
            return None, []
        return rows[0], list(rows[1:])

    async def next_bar(self, symbol: str, timeframe: Timeframe, after: datetime) -> Candle | None:
        """The first bar that **opens** at or after ``after``."""
        instrument_id = self._instrument_ids.get(symbol.upper())
        if instrument_id is None:
            return None
        entry, _ = await self.execution_window(
            instrument_id=instrument_id, timeframe=timeframe, after=after, bars=40
        )
        return entry

    def universe(self, as_of: datetime) -> Sequence[str]:
        """Symbols tradable at ``as_of``.

        Returns the configured set unfiltered, because ``instruments`` carries no
        ``listed_at``/``delisted_at`` today. That is a **survivorship limitation**,
        recorded here and in the run's metadata rather than papered over -- see
        docs/backtesting.md. Pretending otherwise would be the more dangerous of
        the two options.
        """
        del as_of
        return tuple(self._instrument_ids)

    def timestamps(self, timeframe: Timeframe) -> Iterator[datetime]:  # pragma: no cover
        """Not used: the engine builds its grid from the calendar, not the data."""
        raise NotImplementedError
