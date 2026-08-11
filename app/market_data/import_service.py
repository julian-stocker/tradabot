"""Historical import and incremental synchronisation.

Wraps the existing :class:`~app.market_data.ingest.IngestionService` with the
statistics an operator actually needs: what was requested, what arrived, what was
stored, what was refused and where the gaps are.

Built on top of the phase 1 ingestion path rather than replacing it. The upsert on
``(instrument, timeframe, timestamp)`` already makes re-import idempotent, so
nothing here has to reimplement that -- it reports on it.

This service is also the **synchronisation boundary** (Part W): ``sync_watchlist``
is a plain awaitable with no scheduler attached. A cron job, a systemd timer or a
future in-process scheduler all call the same method, and none of the business
logic knows which.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InstrumentNotFoundError, ProviderError
from app.core.events import Event, EventPublisher, NullEventPublisher
from app.core.logging import get_logger
from app.core.time import ensure_utc, utc_now
from app.corporate_actions.repository import CorporateActionRepository
from app.domain.enums import Timeframe
from app.instruments.repository import InstrumentRepository
from app.market_data.calendars import get_trading_calendar
from app.market_data.provider import MarketDataProvider
from app.market_data.quality import CandleGap, check_series, expected_bar_count
from app.market_data.repository import CandleRepository
from app.paper.corporate_actions import PositionCorporateActionService

logger = get_logger(__name__)

DEFAULT_BACKFILL_DAYS = 400
"""How far back an instrument with no stored bars is seeded. Comfortably past the
61-bar feature warm-up without pulling a decade on first run."""

BACKFILL_DAYS_BY_TIMEFRAME: dict[Timeframe, int] = {
    Timeframe.M1: 5,
    Timeframe.M5: 20,
    Timeframe.M15: 45,
    Timeframe.M30: 90,
    Timeframe.H1: 180,
    Timeframe.H4: 400,
    Timeframe.D1: DEFAULT_BACKFILL_DAYS,
    Timeframe.W1: 2_000,
}
"""Backfill window per timeframe, sized to the *bars* the features need rather
than to a calendar span.

400 days of daily bars is 400 rows; 400 days of 5-minute bars is over 30,000, and
warming up a 50-period EMA needs about sixty of them either way. Pulling the same
calendar window for every timeframe wastes provider quota, database space and
scan time to compute an identical answer."""


REFETCH_OVERLAP_BARS = 3
"""Incremental syncs re-request a few bars before the newest stored one. Providers
revise recently published bars (late prints, consolidated-tape corrections), and
the upsert makes re-fetching free."""


def backfill_days_for(timeframe: Timeframe) -> int:
    """How far back to seed a timeframe with no stored history."""
    return BACKFILL_DAYS_BY_TIMEFRAME.get(timeframe, DEFAULT_BACKFILL_DAYS)


@dataclass
class ImportReport:
    """What one symbol/timeframe import did.

    ``received`` and ``inserted`` differ whenever a re-import overlaps existing
    data, which is the normal case for an incremental sync -- so the two are
    reported separately rather than collapsed into a misleading "imported" count.
    """

    symbol: str
    timeframe: Timeframe
    start: datetime
    end: datetime
    provider: str

    expected_bars: int | None = None
    """From the exchange calendar. ``None`` for intraday, where the expected count
    depends on half-days and pre/post-market policy and would be a guess."""

    received_bars: int = 0
    inserted_bars: int = 0
    existing_bars: int = 0
    rejected_bars: int = 0
    gaps: tuple[CandleGap, ...] = ()
    corporate_actions: int = 0
    positions_adjusted: int = 0
    """Open paper positions rescaled for a split discovered by this import."""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def missing_bars(self) -> int:
        return sum(gap.missing_bars for gap in self.gaps)

    def summary(self) -> str:
        if self.error is not None:
            return f"{self.symbol} {self.timeframe.value}: FAILED -- {self.error}"
        expected = self.expected_bars if self.expected_bars is not None else "n/a"
        return (
            f"{self.symbol} {self.timeframe.value}: "
            f"expected={expected} received={self.received_bars} "
            f"inserted={self.inserted_bars} existing={self.existing_bars} "
            f"rejected={self.rejected_bars} gaps={len(self.gaps)} "
            f"({self.missing_bars} bars)"
        )


@dataclass
class SyncReport:
    """Outcome of synchronising a set of symbols."""

    provider: str
    started_at: datetime
    finished_at: datetime
    reports: list[ImportReport] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.reports)

    @property
    def symbols_succeeded(self) -> tuple[str, ...]:
        return tuple(r.symbol for r in self.reports if r.ok)

    @property
    def symbols_failed(self) -> tuple[str, ...]:
        return tuple(r.symbol for r in self.reports if not r.ok)

    @property
    def total_inserted(self) -> int:
        return sum(r.inserted_bars for r in self.reports)

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


class MarketDataImportService:
    """Imports and synchronises historical market data."""

    def __init__(
        self,
        session: AsyncSession,
        provider: MarketDataProvider,
        *,
        events: EventPublisher | None = None,
    ) -> None:
        self._session = session
        self._provider = provider
        self._instruments = InstrumentRepository(session)
        self._candles = CandleRepository(session)
        self._actions = CorporateActionRepository(session)
        self._events = events or NullEventPublisher()

    async def import_symbol(
        self,
        *,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: Timeframe = Timeframe.D1,
        with_corporate_actions: bool = True,
    ) -> ImportReport:
        """Import one symbol over an explicit window.

        Request, normalise, validate, persist, report -- in that order, and the
        report is produced whether or not the import succeeded.

        Idempotent: re-importing the same range updates rather than duplicating,
        and ``existing_bars`` shows how much of the range was already held.

        Raises nothing for ordinary failures -- a provider error becomes
        ``report.error`` so a multi-symbol run continues.
        """
        symbol = symbol.upper()
        start, end = ensure_utc(start), ensure_utc(end)
        report = ImportReport(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            provider=self._provider.name,
        )

        if start >= end:
            report.error = f"start ({start.isoformat()}) must be before end ({end.isoformat()})"
            return report

        instrument = await self._instruments.get_by_symbol(symbol)
        if instrument is None:
            report.error = (
                f"{symbol} is not in the instrument table; sync instruments first "
                f"(a candle request must not invent an instrument)"
            )
            return report

        calendar = get_trading_calendar(instrument.exchange)
        report.expected_bars = expected_bar_count(
            start=start, end=end, timeframe=timeframe, calendar=calendar
        )
        report.existing_bars = len(
            await self._candles.get_range(
                instrument_id=instrument.id, timeframe=timeframe, start=start, end=end
            )
        )

        try:
            candles = await self._provider.get_historical_candles(
                symbol=symbol, timeframe=timeframe, start=start, end=end
            )
        except ProviderError as exc:
            report.error = str(exc)
            logger.warning(
                "import failed", symbol=symbol, provider=self._provider.name, error=str(exc)
            )
            await self._events.publish(
                Event.market_data_sync_failed(
                    provider=self._provider.name, symbol=symbol, error=str(exc)
                )
            )
            return report

        report.received_bars = len(candles)
        if candles:
            report.inserted_bars = await self._candles.upsert_many(
                instrument_id=instrument.id,
                timeframe=timeframe,
                candles=candles,
                provider=self._provider.name,
            )

        if with_corporate_actions:
            report.corporate_actions = await self._import_corporate_actions(
                instrument_id=instrument.id, symbol=symbol
            )
            report.positions_adjusted = await self._adjust_open_positions(
                instrument_id=instrument.id, symbol=symbol, as_of=end
            )

        stored = await self._candles.get_range(
            instrument_id=instrument.id, timeframe=timeframe, start=start, end=end
        )
        quality = check_series(stored, symbol=symbol, timeframe=timeframe, calendar=calendar)
        report.gaps = quality.gaps

        logger.info("import complete", **_log_fields(report))
        return report

    async def sync_symbol(
        self,
        *,
        symbol: str,
        timeframe: Timeframe = Timeframe.D1,
        now: datetime | None = None,
        backfill_days: int | None = None,
    ) -> ImportReport:
        """Bring one symbol up to date from its newest stored bar.

        Requests only what is missing. A full re-download on every run would be
        wasteful and, on a rate-limited API, self-defeating.

        The window starts a few bars *before* the newest stored timestamp so that
        recently revised bars are corrected rather than frozen at their first
        published values.
        """
        symbol = symbol.upper()
        end = ensure_utc(now) if now is not None else utc_now()

        instrument = await self._instruments.get_by_symbol(symbol)
        if instrument is None:
            return ImportReport(
                symbol=symbol,
                timeframe=timeframe,
                start=end,
                end=end,
                provider=self._provider.name,
                error=f"{symbol} is not in the instrument table",
            )

        latest = await self._candles.latest_timestamp(
            instrument_id=instrument.id, timeframe=timeframe
        )
        # A caller may override the window; otherwise it is sized to the
        # timeframe, so a 5-minute series is not backfilled a year deep.
        days = backfill_days if backfill_days is not None else backfill_days_for(timeframe)
        start = (
            latest - timeframe.duration * REFETCH_OVERLAP_BARS
            if latest is not None
            else end - timedelta(days=days)
        )
        if start >= end:
            return ImportReport(
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                provider=self._provider.name,
            )

        return await self.import_symbol(symbol=symbol, start=start, end=end, timeframe=timeframe)

    async def sync_watchlist(
        self,
        symbols: Sequence[str],
        *,
        timeframe: Timeframe = Timeframe.D1,
        now: datetime | None = None,
    ) -> SyncReport:
        """Synchronise a set of symbols.

        **The scheduling boundary.** Nothing here knows about cron, systemd or an
        in-process timer -- a deployment calls this method on whatever cadence it
        likes, and the business logic is unaffected.

        One symbol's failure is recorded and the run continues: a single delisted
        or mistyped ticker must not abort a watchlist sync.
        """
        started = ensure_utc(now) if now is not None else utc_now()
        report = SyncReport(provider=self._provider.name, started_at=started, finished_at=started)

        for symbol in symbols:
            try:
                report.reports.append(
                    await self.sync_symbol(symbol=symbol, timeframe=timeframe, now=started)
                )
            except (ProviderError, InstrumentNotFoundError) as exc:
                logger.warning("sync failed", symbol=symbol, error=str(exc))
                report.reports.append(
                    ImportReport(
                        symbol=symbol.upper(),
                        timeframe=timeframe,
                        start=started,
                        end=started,
                        provider=self._provider.name,
                        error=str(exc),
                    )
                )

        report.finished_at = utc_now() if now is None else started
        await self._events.publish(
            Event.market_data_sync_completed(
                provider=report.provider,
                symbols=len(report.reports),
                inserted=report.total_inserted,
                failed=len(report.symbols_failed),
            )
        )
        logger.info(
            "watchlist sync complete",
            provider=report.provider,
            symbols=len(report.reports),
            inserted=report.total_inserted,
            failed=len(report.symbols_failed),
        )
        return report

    async def ensure_instruments(self, symbols: Sequence[str] | None = None) -> int:
        """Upsert the provider's instrument universe, recording provenance.

        ``symbols`` narrows the result to the tickers actually wanted. The filter
        is applied *after* fetching because a provider's asset endpoint returns
        its whole universe in one call -- Alpaca's is roughly ten thousand rows --
        and writing all of it to import three symbols is a lot of database churn
        for no benefit.

        A requested symbol the provider does not list is simply absent from the
        result; the import that follows reports it as an unknown instrument rather
        than failing here, where there is no report to put it in.
        """
        infos = await self._provider.get_instruments()
        if symbols is not None:
            wanted = {symbol.upper() for symbol in symbols}
            infos = [info for info in infos if info.symbol.upper() in wanted]
            missing = sorted(wanted - {info.symbol.upper() for info in infos})
            if missing:
                # Named, not invented. Alpaca's universe *is* the configured
                # watchlist (see its `get_instruments`), so the operator's fix is
                # to add the symbol there -- and an import that quietly conjured
                # an instrument row would break the rule that a candle request
                # never creates one.
                logger.warning(
                    "requested symbols are not in the provider's universe",
                    provider=self._provider.name,
                    symbols=",".join(missing),
                    hint="add them to TRADABOT_MARKET_DATA__WATCHLIST",
                )
        return await self._instruments.upsert_many(infos, provider=self._provider.name)

    async def _import_corporate_actions(self, *, instrument_id: int, symbol: str) -> int:
        """Fetch and store corporate actions, tolerating a provider that has none.

        A provider without corporate-action support must not fail an otherwise
        good candle import -- but the absence is logged, because "no actions" and
        "no action data" are indistinguishable downstream and the second one
        silently turns split-adjusted series into raw ones.
        """
        try:
            actions = await self._provider.get_corporate_actions(symbol)
        except ProviderError as exc:
            logger.warning(
                "corporate action fetch failed; candles kept",
                symbol=symbol,
                provider=self._provider.name,
                error=str(exc),
            )
            return 0

        if not actions:
            logger.debug("provider reported no corporate actions", symbol=symbol)
            return 0
        return await self._actions.upsert_many(instrument_id=instrument_id, actions=actions)

    async def _adjust_open_positions(
        self, *, instrument_id: int, symbol: str, as_of: datetime
    ) -> int:
        """Apply newly-known splits to open paper positions.

        Ingestion is where this belongs. A split becomes knowable the moment the
        provider reports it, and a position left unadjusted between that moment and
        the next simulation run would be marked against post-split prices at a
        pre-split quantity -- a fabricated 50% loss on a 2-for-1.

        The service is idempotent (see
        :mod:`app.paper.corporate_actions`), so running it on every import is safe
        and is the point: there is no separate "have I already done this" bookkeeping
        for a caller to get wrong.
        """
        stored = await self._actions.list_for_instrument(instrument_id=instrument_id, symbol=symbol)
        if not stored:
            return 0

        adjustments = await PositionCorporateActionService(self._session).apply_actions(
            instrument_id=instrument_id, actions=stored, as_of=as_of
        )
        if adjustments:
            logger.info(
                "adjusted open positions for corporate actions",
                symbol=symbol,
                positions=len(adjustments),
            )
        return len(adjustments)


def _log_fields(report: ImportReport) -> dict[str, object]:
    return {
        "symbol": report.symbol,
        "timeframe": report.timeframe.value,
        "provider": report.provider,
        "received": report.received_bars,
        "inserted": report.inserted_bars,
        "existing": report.existing_bars,
        "gaps": len(report.gaps),
    }
