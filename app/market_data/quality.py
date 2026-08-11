"""Market-data quality: normalisation reporting and gap detection.

Two distinct jobs, deliberately separated:

**Normalisation reporting** — what a provider sent that we refused, and why.
The validation itself already lives in :class:`~app.market_data.provider.CandleData`
and :class:`~app.domain.quotes.Quote`, which reject impossible OHLC, negative
volume, naive timestamps and crossed books at construction. This module does not
duplicate that; it *records* the rejections so a bad feed is visible rather than
silently thinned.

**Gap detection** — bars that should exist and do not.

The rule that makes gap detection worth anything:

> **A gap in the data is not the same as a gap in the market.**

Nights, weekends and holidays are not missing data. Counting them as such
produces a "quality report" that screams about a perfectly healthy feed and is
therefore ignored — at which point real gaps go unnoticed too. Every expectation
here is derived from an exchange calendar.

Nothing is silently corrected. The one exception is stated at its call site: a
duplicate bar with identical values is deduplicated rather than reported, because
that correction is mathematically unambiguous.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import pairwise

from app.core.logging import get_logger
from app.core.time import ensure_utc
from app.domain.enums import Timeframe
from app.domain.quotes import Quote
from app.market_data.calendars import TradingCalendar

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RejectedRecord:
    """One record the provider sent that tradabot refused."""

    timestamp: datetime | None
    reason: str

    def describe(self) -> str:
        when = self.timestamp.isoformat() if self.timestamp else "unknown time"
        return f"{when}: {self.reason}"


@dataclass
class NormalisationReport:
    """What happened while converting one provider response.

    Mutable during a single normalisation pass, then read. Kept as a plain
    dataclass rather than a log line so callers can decide what to do with a
    partially bad response instead of discovering it in a log file afterwards.
    """

    symbol: str
    rejected: list[RejectedRecord] = field(default_factory=list)
    duplicates: list[datetime] = field(default_factory=list)
    out_of_window: int = 0

    def reject(self, timestamp: datetime | None, reason: str) -> None:
        self.rejected.append(RejectedRecord(timestamp=timestamp, reason=reason))

    def duplicate(self, timestamp: datetime) -> None:
        self.duplicates.append(timestamp)

    def skip_out_of_window(self) -> None:
        """A bar outside the requested half-open window.

        Not an error: Alpaca treats ``end`` as inclusive while tradabot's windows
        are ``[start, end)``, so exactly one boundary bar is routinely dropped.
        Counted separately from rejections so it never looks like a data fault.
        """
        self.out_of_window += 1

    @property
    def is_clean(self) -> bool:
        return not self.rejected and not self.duplicates

    def summary(self) -> str:
        return (
            f"{self.symbol}: {len(self.rejected)} rejected, "
            f"{len(self.duplicates)} duplicates, {self.out_of_window} outside window"
        )


@dataclass(frozen=True, slots=True)
class CandleGap:
    """A stretch of expected-but-absent bars."""

    start: datetime
    end: datetime
    missing_bars: int
    sessions: tuple[date, ...]
    """The trading sessions the gap covers. Empty means the market was shut, in
    which case this is not a gap at all and would not be reported."""

    def describe(self) -> str:
        return (
            f"{self.missing_bars} bar(s) missing between {self.start.isoformat()} "
            f"and {self.end.isoformat()} across {len(self.sessions)} session(s)"
        )


@dataclass(frozen=True, slots=True)
class QualityReport:
    """The outcome of checking one stored series."""

    symbol: str
    timeframe: Timeframe
    bars_checked: int
    gaps: tuple[CandleGap, ...]
    duplicate_timestamps: tuple[datetime, ...]
    non_monotonic: int
    invalid_ohlc: tuple[datetime, ...]
    non_positive_prices: tuple[datetime, ...]
    negative_volume: tuple[datetime, ...]

    @property
    def is_clean(self) -> bool:
        return not (
            self.gaps
            or self.duplicate_timestamps
            or self.non_monotonic
            or self.invalid_ohlc
            or self.non_positive_prices
            or self.negative_volume
        )

    @property
    def missing_bars(self) -> int:
        return sum(gap.missing_bars for gap in self.gaps)

    def summary(self) -> str:
        if self.is_clean:
            return f"{self.symbol} {self.timeframe.value}: {self.bars_checked} bars, clean"
        parts = [f"{self.symbol} {self.timeframe.value}: {self.bars_checked} bars"]
        if self.gaps:
            parts.append(f"{self.missing_bars} missing in {len(self.gaps)} gap(s)")
        if self.duplicate_timestamps:
            parts.append(f"{len(self.duplicate_timestamps)} duplicate(s)")
        if self.non_monotonic:
            parts.append(f"{self.non_monotonic} out of order")
        if self.invalid_ohlc:
            parts.append(f"{len(self.invalid_ohlc)} invalid OHLC")
        if self.non_positive_prices:
            parts.append(f"{len(self.non_positive_prices)} non-positive price(s)")
        if self.negative_volume:
            parts.append(f"{len(self.negative_volume)} negative volume(s)")
        return ", ".join(parts)


class CandleLike:
    """Structural expectation for :func:`check_series` inputs.

    Documented rather than enforced: both the ORM row and the provider DTO
    satisfy it, and a Protocol here would add an import cycle for no gain.
    """


def check_series(
    candles: Sequence[object],
    *,
    symbol: str,
    timeframe: Timeframe,
    calendar: TradingCalendar,
) -> QualityReport:
    """Inspect a stored series for the defects that matter.

    Detects duplicate timestamps, non-monotonic ordering, impossible OHLC,
    non-positive prices, negative volume, and **calendar-aware** gaps.

    The OHLC rule checked here is the full one::

        low <= open  <= high
        low <= close <= high

    ``CandleData`` enforces this at ingestion, so a violation found here means
    data reached the database by another route -- a direct insert, a migration, a
    bug. Worth knowing about.
    """
    timestamps: list[datetime] = []
    duplicates: list[datetime] = []
    non_monotonic = 0
    invalid_ohlc: list[datetime] = []
    non_positive: list[datetime] = []
    negative_volume: list[datetime] = []

    seen: set[datetime] = set()
    previous: datetime | None = None

    for candle in candles:
        stamp = ensure_utc(candle.timestamp)  # type: ignore[attr-defined]
        timestamps.append(stamp)

        if stamp in seen:
            duplicates.append(stamp)
        seen.add(stamp)

        if previous is not None and stamp < previous:
            non_monotonic += 1
        previous = stamp

        open_ = candle.open  # type: ignore[attr-defined]
        high = candle.high  # type: ignore[attr-defined]
        low = candle.low  # type: ignore[attr-defined]
        close = candle.close  # type: ignore[attr-defined]
        volume = candle.volume  # type: ignore[attr-defined]

        if min(open_, high, low, close) <= 0:
            non_positive.append(stamp)
        elif not (low <= open_ <= high and low <= close <= high):
            invalid_ohlc.append(stamp)

        if volume < 0:
            negative_volume.append(stamp)

    gaps = detect_gaps(timestamps=sorted(seen), timeframe=timeframe, calendar=calendar)

    return QualityReport(
        symbol=symbol,
        timeframe=timeframe,
        bars_checked=len(timestamps),
        gaps=tuple(gaps),
        duplicate_timestamps=tuple(duplicates),
        non_monotonic=non_monotonic,
        invalid_ohlc=tuple(invalid_ohlc),
        non_positive_prices=tuple(non_positive),
        negative_volume=tuple(negative_volume),
    )


def detect_gaps(
    *,
    timestamps: Sequence[datetime],
    timeframe: Timeframe,
    calendar: TradingCalendar,
) -> list[CandleGap]:
    """Find stretches of missing bars, ignoring legitimate market closures.

    Daily and weekly bars are checked by counting **sessions** between
    consecutive timestamps: two bars a weekend apart are adjacent, and one
    spanning a holiday still is.

    Intraday bars are checked only *within* a single session. Across a session
    boundary the expected bar count depends on half-days, early closes and
    pre/post-market inclusion, and guessing it would generate false positives
    that train the reader to ignore the report.
    """
    if len(timestamps) < 2:  # noqa: PLR2004 -- a single bar cannot have an internal gap
        return []

    if timeframe in (Timeframe.D1, Timeframe.W1):
        return _daily_gaps(timestamps, timeframe, calendar)
    return _intraday_gaps(timestamps, timeframe, calendar)


def _daily_gaps(
    timestamps: Sequence[datetime], timeframe: Timeframe, calendar: TradingCalendar
) -> list[CandleGap]:
    """Gaps in a daily/weekly series, measured in trading sessions."""
    gaps: list[CandleGap] = []
    step = 1 if timeframe is Timeframe.D1 else 5  # a week is ~5 sessions

    for previous, current in pairwise(timestamps):
        sessions = calendar.sessions_between(previous, current)
        # sessions includes both endpoints; anything beyond the two endpoints and
        # the expected step is genuinely absent.
        interior = [s for s in sessions if previous.date() < s < current.date()]
        if timeframe is Timeframe.D1 and interior:
            gaps.append(
                CandleGap(
                    start=previous,
                    end=current,
                    missing_bars=len(interior),
                    sessions=tuple(interior),
                )
            )
        elif timeframe is Timeframe.W1 and len(interior) > step:
            gaps.append(
                CandleGap(
                    start=previous,
                    end=current,
                    missing_bars=len(interior) // step,
                    sessions=tuple(interior),
                )
            )
    return gaps


def _intraday_gaps(
    timestamps: Sequence[datetime], timeframe: Timeframe, calendar: TradingCalendar
) -> list[CandleGap]:
    """Gaps within a single session, for intraday timeframes."""
    gaps: list[CandleGap] = []
    step = timeframe.duration

    for previous, current in pairwise(timestamps):
        if previous.date() != current.date():
            # Overnight. Expected bar counts across a session boundary depend on
            # half-days and pre/post-market policy; not guessed at.
            continue
        if not calendar.is_trading_day(previous):
            continue

        expected = (current - previous) / step
        missing = int(expected) - 1
        if missing > 0:
            gaps.append(
                CandleGap(
                    start=previous,
                    end=current,
                    missing_bars=missing,
                    sessions=(previous.date(),),
                )
            )
    return gaps


def expected_bar_count(
    *,
    start: datetime,
    end: datetime,
    timeframe: Timeframe,
    calendar: TradingCalendar,
) -> int | None:
    """How many bars a window *should* contain, or None when unknowable.

    Returns ``None`` for intraday timeframes rather than a plausible guess:
    without modelling half-days and pre/post-market inclusion the number would be
    wrong often enough to be misleading, and a wrong expectation is worse than an
    admitted unknown.
    """
    if timeframe is Timeframe.D1:
        return calendar.count_sessions(start, end)
    if timeframe is Timeframe.W1:
        sessions = calendar.count_sessions(start, end)
        return max(0, sessions // 5)
    return None


def quote_is_stale(quote: Quote, *, now: datetime, max_age_seconds: int) -> bool:
    """Whether a quote is too old to price against.

    A quote from the *future* is also stale: it means a clock or wiring problem,
    and pricing against it would be look-ahead.
    """
    timestamp = ensure_utc(quote.timestamp)
    now = ensure_utc(now)
    if timestamp > now:
        return True
    return timestamp < now - timedelta(seconds=max_age_seconds)


def quote_age_seconds(quote: Quote, *, now: datetime) -> float:
    """Age of a quote in seconds. Negative if it is timestamped in the future."""
    return (ensure_utc(now) - ensure_utc(quote.timestamp)).total_seconds()
