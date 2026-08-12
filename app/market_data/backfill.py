"""Multi-year historical expansion: chunked, resumable, restart-safe.

A six-year, four-timeframe, 52-symbol backfill is millions of rows and hours of
wall time. Three properties follow from that, and none of them is optional:

**Resumable.** A run that fails at 90% must continue from 90%, not from zero.
Progress is derived from the database itself -- the newest stored bar per
(symbol, timeframe) -- rather than from a checkpoint file, because a checkpoint
can disagree with reality after a crash and the database cannot.

**Chunked.** One request for six years of five-minute bars is millions of rows in
a single response. Chunks are bounded in *calendar span*, so each request stays
small, each commit is short, and a failure costs one chunk.

**Coexistent.** The production scheduler is syncing every five minutes while this
runs. Chunks commit independently and briefly, so the live sync waits
milliseconds for the write lock rather than hours. See ``docs/operations.md``.

Idempotency comes from the existing upsert: re-running a completed chunk updates
rows in place and reports them as ``existing`` rather than inserting duplicates.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.core.redaction import safe_message
from app.core.time import ensure_utc
from app.db.models import Candle
from app.db.session import session_scope
from app.domain.enums import Timeframe
from app.instruments.repository import InstrumentRepository
from app.market_data.calendars import TradingCalendar, get_trading_calendar
from app.market_data.import_service import MarketDataImportService
from app.market_data.provider import MarketDataProvider
from app.market_data.quality import expected_bar_count

logger = get_logger(__name__)

CHUNK_DAYS: dict[Timeframe, int] = {
    Timeframe.M5: 14,
    Timeframe.M15: 45,
    Timeframe.H1: 90,
    Timeframe.D1: 365,
}
"""Calendar days per request, per timeframe.

Sized so each chunk lands near the same number of *rows* -- roughly 40-50k for a
52-symbol universe -- rather than the same number of days. A uniform day count
would make the five-minute chunks enormous and the daily chunks pointlessly
small.

The ceiling is the provider timeout, not memory. Since bar requests stopped
sending a `limit` (see `providers/alpaca.py` -- it truncated by dropping symbols),
responses are complete and therefore much larger, and a 180-day hourly window for
52 symbols reliably exceeded the 30-second request timeout. These values were
reduced until every timeframe completed; if the timeout is raised, they can grow
again.
"""

COVERAGE_TOLERANCE = 0.95
"""Fraction of a window's sessions that must be present to call it covered."""

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0
"""A failed chunk is retried in place before the run moves on.

Transient provider errors are the common case; retrying the chunk is far cheaper
than failing a six-hour run. After ``MAX_RETRIES`` the chunk is recorded as
failed and the run continues -- one bad window must not cost the other 99%.
"""


@dataclass(slots=True)
class ChunkResult:
    """One (symbol, timeframe, window) request."""

    symbol: str
    timeframe: Timeframe
    start: datetime
    end: datetime
    received: int = 0
    inserted: int = 0
    existing: int = 0
    rejected: int = 0
    expected: int = 0
    retries: int = 0
    duration_seconds: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def gap(self) -> int:
        """Bars the calendar expected but the provider did not deliver."""
        return max(0, self.expected - self.received)


@dataclass(slots=True)
class BackfillReport:
    """Totals for a whole expansion."""

    chunks: list[ChunkResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_seconds: float = 0.0
    skipped_chunks: int = 0
    """Windows already fully covered; the resume path."""

    @property
    def ok(self) -> bool:
        return all(chunk.ok for chunk in self.chunks)

    @property
    def inserted(self) -> int:
        return sum(chunk.inserted for chunk in self.chunks)

    @property
    def received(self) -> int:
        return sum(chunk.received for chunk in self.chunks)

    @property
    def failed(self) -> list[ChunkResult]:
        return [chunk for chunk in self.chunks if not chunk.ok]

    def summary(self) -> str:
        return (
            f"{len(self.chunks)} chunks ({self.skipped_chunks} already complete), "
            f"{self.received:,} bars received, {self.inserted:,} inserted, "
            f"{len(self.failed)} failed, {self.duration_seconds:.0f}s"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunks": len(self.chunks),
            "skipped": self.skipped_chunks,
            "received": self.received,
            "inserted": self.inserted,
            "failed": len(self.failed),
            "duration_seconds": self.duration_seconds,
        }


def chunk_windows(
    *, start: datetime, end: datetime, timeframe: Timeframe
) -> Iterator[tuple[datetime, datetime]]:
    """Split ``[start, end)`` into provider-sized windows, oldest first.

    Oldest first so a partial run leaves a *contiguous* history with a known
    frontier. Filling newest-first would leave holes that the resume logic --
    which reads the newest stored bar -- would never notice.
    """
    span = timedelta(days=CHUNK_DAYS.get(timeframe, 90))
    cursor = ensure_utc(start)
    finish = ensure_utc(end)
    while cursor < finish:
        stop = min(cursor + span, finish)
        yield cursor, stop
        cursor = stop


class HistoricalBackfill:
    """Expands stored history for a symbol universe."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        provider: MarketDataProvider,
        *,
        exchange: str = "XNYS",
    ) -> None:
        self._factory = factory
        self._provider = provider
        self._calendar = get_trading_calendar(exchange)

    async def run(
        self,
        *,
        symbols: Sequence[str],
        timeframes: Sequence[Timeframe],
        start: datetime,
        end: datetime,
        resume: bool = True,
        progress: Any = None,
    ) -> BackfillReport:
        """Backfill every (symbol, timeframe) over ``[start, end)``.

        Args:
            resume: skip windows already covered by stored data. This is what
                makes a re-run cheap instead of a full re-download.
            progress: optional callable invoked with each :class:`ChunkResult`.
        """
        began = time.perf_counter()
        report = BackfillReport()

        for timeframe in timeframes:
            # Which sessions each symbol already holds. Read once per symbol,
            # then every window is checked against it in memory.
            stored: dict[str, frozenset[date]] = (
                {symbol: await self._stored_sessions(symbol, timeframe) for symbol in symbols}
                if resume
                else {symbol: frozenset() for symbol in symbols}
            )
            for window_start, window_end in chunk_windows(
                start=start, end=end, timeframe=timeframe
            ):
                pending = [
                    symbol
                    for symbol in symbols
                    if not _covered(
                        stored.get(symbol, frozenset()),
                        window_start,
                        window_end,
                        self._calendar,
                    )
                ]
                if not pending:
                    report.skipped_chunks += 1
                    continue

                results = await self._chunk(
                    symbols=pending,
                    timeframe=timeframe,
                    start=window_start,
                    end=window_end,
                )
                report.chunks.extend(results)
                if progress is not None:
                    progress(_merge(results, timeframe, window_start, window_end))

        report.duration_seconds = time.perf_counter() - began
        return report

    async def _chunk(
        self,
        *,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[ChunkResult]:
        """One batched request for many symbols, retried, in a short transaction.

        Batched because the alternative does not finish: fifty-two symbols over
        six years is roughly six thousand per-symbol round trips, which is hours
        of pure latency. One request per (timeframe, window) is about a hundred.
        """
        began = time.perf_counter()
        reports: list[Any] = []
        error: str | None = None
        retries = 0

        for attempt in range(MAX_RETRIES):
            retries = attempt
            error = None
            try:
                async with session_scope(self._factory) as session:
                    service = MarketDataImportService(session, self._provider)
                    reports = await service.import_batch(
                        symbols=symbols, timeframe=timeframe, start=start, end=end
                    )
                failures = [item for item in reports if item.error]
                if not failures:
                    break
                error = failures[0].error
            except Exception as exc:
                error = safe_message(exc)

            if attempt < MAX_RETRIES - 1:
                await _sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))

        elapsed = time.perf_counter() - began
        expected = expected_bar_count(
            start=start, end=end, timeframe=timeframe, calendar=self._calendar
        )
        if not reports:
            return [
                ChunkResult(
                    symbol=symbol,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                    retries=retries,
                    duration_seconds=elapsed,
                    expected=expected or 0,
                    error=error or "no response",
                )
                for symbol in symbols
            ]

        return [
            ChunkResult(
                symbol=item.symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                received=item.received_bars,
                inserted=item.inserted_bars,
                existing=item.existing_bars,
                rejected=item.rejected_bars,
                expected=expected or 0,
                retries=retries,
                duration_seconds=elapsed / max(len(reports), 1),
                error=item.error,
            )
            for item in reports
        ]

    async def _stored_sessions(self, symbol: str, timeframe: Timeframe) -> frozenset[date]:
        """Session dates that already hold at least one bar.

        Coverage has to be measured per session, not from the oldest stored bar.
        An "oldest bar" frontier silently assumes history was filled
        contiguously, and it is not: one exploratory chunk from 2020 plus the
        live sync's recent data left a five-year hole that the frontier check
        cheerfully reported as complete. Asking which sessions actually have data
        cannot be fooled that way.

        One query per (symbol, timeframe); the windows are then checked in
        memory.
        """
        async with session_scope(self._factory) as session:
            instrument = await InstrumentRepository(session).get_by_symbol(symbol)
            if instrument is None:
                return frozenset()
            rows = await session.execute(
                select(func.date(Candle.timestamp))
                .where(
                    Candle.instrument_id == instrument.id,
                    Candle.timeframe == timeframe,
                )
                .distinct()
            )
            return frozenset(
                date.fromisoformat(value) for (value,) in rows.all() if value is not None
            )


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _covered(
    stored: frozenset[date],
    start: datetime,
    end: datetime,
    calendar: TradingCalendar,
) -> bool:
    """Whether every trading session in ``[start, end)`` already has data.

    Session-level, deliberately. A window is only skipped when the sessions it
    contains are actually present -- an overlap at one end proves nothing about
    the middle.

    ``COVERAGE_TOLERANCE`` allows a small shortfall: on a thin feed an individual
    symbol occasionally has no prints for a session, and demanding 100% would
    re-download that window on every run forever.
    """
    sessions = calendar.sessions_between(start, end)
    if not sessions:
        return True  # nothing to fetch
    present = sum(1 for session in sessions if session in stored)
    return present >= len(sessions) * COVERAGE_TOLERANCE


def _merge(
    results: list[ChunkResult], timeframe: Timeframe, start: datetime, end: datetime
) -> ChunkResult:
    """Collapse a batch into one progress line, so 52 symbols are not 52 lines."""
    return ChunkResult(
        symbol=f"{len(results)} symbols",
        timeframe=timeframe,
        start=start,
        end=end,
        received=sum(r.received for r in results),
        inserted=sum(r.inserted for r in results),
        existing=sum(r.existing for r in results),
        rejected=sum(r.rejected for r in results),
        expected=max((r.expected for r in results), default=0),
        retries=max((r.retries for r in results), default=0),
        duration_seconds=sum(r.duration_seconds for r in results),
        error=next((r.error for r in results if r.error), None),
    )
