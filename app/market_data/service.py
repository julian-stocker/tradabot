"""Market-data read operations."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from app.core.time import utc_now
from app.db.models import Candle, Instrument
from app.domain.enums import Timeframe
from app.domain.quotes import Quote
from app.instruments.service import InstrumentService
from app.market_data.provider import MarketDataProvider
from app.market_data.repository import CandleRepository

DEFAULT_CANDLE_LOOKBACK_BARS = 200


class MarketDataService:
    """Serves stored candles and live quotes."""

    def __init__(
        self,
        instruments: InstrumentService,
        candles: CandleRepository,
        provider: MarketDataProvider,
    ) -> None:
        self._instruments = instruments
        self._candles = candles
        self._provider = provider

    async def get_candles(
        self,
        *,
        symbol: str,
        timeframe: Timeframe = Timeframe.D1,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> tuple[Instrument, Sequence[Candle]]:
        """Stored candles in ``[start, end)``.

        Defaults span the most recent :data:`DEFAULT_CANDLE_LOOKBACK_BARS` bars.

        Raises:
            InstrumentNotFoundError: unknown symbol.
            ValueError: ``start`` is not before ``end``.
        """
        instrument = await self._instruments.get_instrument(symbol)

        end = end or utc_now()
        if start is None:
            start = end - timeframe.duration * DEFAULT_CANDLE_LOOKBACK_BARS
        if start >= end:
            msg = f"start ({start.isoformat()}) must be before end ({end.isoformat()})"
            raise ValueError(msg)

        rows = await self._candles.get_range(
            instrument_id=instrument.id,
            timeframe=timeframe,
            start=start,
            end=end,
            limit=limit,
        )
        return instrument, rows

    async def get_latest_quote(self, symbol: str) -> tuple[Instrument, Quote]:
        """Live quote from the active provider.

        Raises:
            InstrumentNotFoundError: unknown symbol.
            ProviderError: the provider has no quote for it.
        """
        instrument = await self._instruments.get_instrument(symbol)
        quote = await self._provider.get_latest_quote(instrument.symbol)
        return instrument, quote


def default_start_for(timeframe: Timeframe, bars: int) -> timedelta:
    """Lookback covering ``bars`` candles of ``timeframe``."""
    return timeframe.duration * bars
