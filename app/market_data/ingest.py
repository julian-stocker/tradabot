"""Market-data ingestion: provider -> normalised database rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InstrumentNotFoundError, ProviderError
from app.core.logging import get_logger
from app.core.time import ensure_utc, utc_now
from app.corporate_actions.repository import CorporateActionRepository
from app.domain.enums import Timeframe
from app.instruments.repository import InstrumentRepository
from app.market_data.provider import MarketDataProvider
from app.market_data.repository import CandleRepository

logger = get_logger(__name__)

# On an incremental sync, re-fetch a little before the newest stored bar. Providers
# revise recently published bars (late prints, consolidated-tape corrections), and
# the upsert makes re-fetching free.
REFETCH_OVERLAP_BARS = 3


@dataclass(frozen=True, slots=True)
class IngestionReport:
    """Outcome of one ingestion run."""

    instruments_synced: int
    candles_written: int
    corporate_actions_written: int
    symbols_succeeded: tuple[str, ...]
    symbols_failed: tuple[tuple[str, str], ...]
    """``(symbol, error message)`` pairs -- failures are reported, never hidden."""

    @property
    def ok(self) -> bool:
        return not self.symbols_failed


class IngestionService:
    """Pulls data from a provider and stores it.

    The only component that talks to both a provider and the database. Keeping
    that intersection in one place is what lets the feature and signal layers stay
    ignorant of where candles came from.
    """

    def __init__(
        self,
        session: AsyncSession,
        provider: MarketDataProvider,
    ) -> None:
        self._session = session
        self._provider = provider
        self._instruments = InstrumentRepository(session)
        self._candles = CandleRepository(session)
        self._actions = CorporateActionRepository(session)

    async def sync_instruments(self) -> int:
        """Fetch and upsert the provider's instrument universe."""
        infos = await self._provider.get_instruments()
        count = await self._instruments.upsert_many(infos)
        logger.info("instrument sync complete", provider=self._provider.name, count=count)
        return count

    async def sync_corporate_actions(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        """Fetch and store corporate actions for one symbol.

        Runs before candles in :meth:`sync_all`, so an adjusted series is never
        computed from a partially ingested action set -- which would produce a
        chart that looks continuous and is wrong.

        ``start``/``end`` bound the provider query. Pass the span of the stored
        price series: providers default to a window of days, and an unbounded
        call returns almost nothing while looking like a successful sync.

        Raises:
            InstrumentNotFoundError: the symbol is not in the database.
            ProviderError: propagated from the provider.
        """
        instrument = await self._instruments.get_by_symbol(symbol)
        if instrument is None:
            raise InstrumentNotFoundError(symbol)

        actions = await self._provider.get_corporate_actions(
            instrument.symbol, start=start, end=end
        )
        return await self._actions.upsert_many(instrument_id=instrument.id, actions=actions)

    async def sync_candles(
        self,
        *,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        """Fetch and store candles for one symbol.

        When ``start`` is omitted, ingestion resumes a few bars before the newest
        stored candle. When there is nothing stored, it falls back to a default
        lookback sized to the timeframe.

        Returns:
            Number of candles written.

        Raises:
            InstrumentNotFoundError: the symbol is not in the database. Sync
                instruments first -- silently creating one from a candle request
                would let a typo invent an instrument.
            ProviderError: propagated from the provider.
        """
        instrument = await self._instruments.get_by_symbol(symbol)
        if instrument is None:
            raise InstrumentNotFoundError(symbol)

        end = ensure_utc(end) if end is not None else utc_now()
        if start is None:
            start = await self._resolve_start(instrument.id, timeframe, end)
        else:
            start = ensure_utc(start)

        if start >= end:
            logger.debug("nothing to ingest", symbol=symbol, start=start, end=end)
            return 0

        candles = await self._provider.get_historical_candles(
            symbol=instrument.symbol, timeframe=timeframe, start=start, end=end
        )
        written = await self._candles.upsert_many(
            instrument_id=instrument.id, timeframe=timeframe, candles=candles
        )
        logger.info(
            "candle sync complete",
            symbol=instrument.symbol,
            timeframe=timeframe.value,
            written=written,
        )
        return written

    async def sync_all(
        self,
        *,
        timeframe: Timeframe = Timeframe.D1,
        symbols: list[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> IngestionReport:
        """Sync instruments, then corporate actions and candles for each symbol.

        Corporate actions are ingested **before** candles for the same symbol.
        Ordering matters: an adjusted series computed from a partial action set
        is continuous-looking and wrong, and there is no later signal that
        anything was missed.

        A failure on one symbol is recorded and the run continues -- one delisted
        ticker should not abort a universe-wide sync. Failures are returned in the
        report and logged (coding rule 8); nothing is swallowed.

        ``symbols=None`` syncs the **full stored universe including inactive
        instruments**. Delisted names still need their history maintained, or
        every backtest that includes them inherits a survivorship gap.
        """
        instruments_synced = await self.sync_instruments()

        if symbols is None:
            stored = await self._instruments.list_all(active_only=False, limit=1000)
            symbols = [row.symbol for row in stored]

        total = 0
        actions_total = 0
        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []

        for symbol in symbols:
            try:
                actions_total += await self.sync_corporate_actions(symbol)
                total += await self.sync_candles(
                    symbol=symbol, timeframe=timeframe, start=start, end=end
                )
            except (ProviderError, InstrumentNotFoundError) as exc:
                logger.warning("symbol sync failed", symbol=symbol, error=str(exc))
                failed.append((symbol, str(exc)))
            else:
                succeeded.append(symbol)

        if succeeded and actions_total == 0:
            # Not an error, but worth saying out loud: a provider that never
            # reports actions is indistinguishable from a universe that had none,
            # and split-adjusted series would silently equal raw ones.
            logger.warning(
                "no corporate actions ingested; adjusted series will equal raw series",
                provider=self._provider.name,
                symbols=len(succeeded),
            )

        return IngestionReport(
            instruments_synced=instruments_synced,
            candles_written=total,
            corporate_actions_written=actions_total,
            symbols_succeeded=tuple(succeeded),
            symbols_failed=tuple(failed),
        )

    async def _resolve_start(
        self, instrument_id: int, timeframe: Timeframe, end: datetime
    ) -> datetime:
        """Where to resume ingestion from."""
        latest = await self._candles.latest_timestamp(
            instrument_id=instrument_id, timeframe=timeframe
        )
        if latest is not None:
            return latest - timeframe.duration * REFETCH_OVERLAP_BARS
        return end - _default_lookback(timeframe)


def _default_lookback(timeframe: Timeframe) -> timedelta:
    """Initial history to fetch for an instrument with no stored candles.

    Sized to comfortably exceed the feature warm-up requirement (~61 bars) while
    staying small enough that a first sync is quick.
    """
    return timeframe.duration * 400
