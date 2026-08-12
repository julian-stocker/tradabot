"""Classifying missing bars, and resampling as a cross-check.

A missing bar is not automatically missing *data*. Most absences in an equity
series are the market being shut, and a naive "expected minus received" gap
report on US equities is roughly 70% weekends. Reporting those as data problems
trains the reader to ignore the report, which is worse than not having one.

So gaps are classified against the exchange calendar first, and only what
survives that is a question for the provider. **Nothing is ever interpolated** --
a fabricated bar is indistinguishable from a real one once stored, and every
statistic downstream inherits it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.core.time import ensure_utc
from app.domain.enums import Timeframe
from app.market_data.calendars import TradingCalendar


class GapKind(StrEnum):
    """Why a bar is absent."""

    EXPECTED_MARKET_CLOSURE = "EXPECTED_MARKET_CLOSURE"
    """Weekend, holiday or outside session hours. Not a defect."""
    PROVIDER_MISSING = "PROVIDER_MISSING"
    """The market was open and the provider returned nothing."""
    SYMBOL_NOT_TRADING = "SYMBOL_NOT_TRADING"
    """Before the instrument's first bar -- not yet listed, or not yet covered."""
    UNKNOWN = "UNKNOWN"
    """Survives every rule above. Rare, and worth looking at individually."""

    @property
    def is_actionable(self) -> bool:
        """Whether this gap represents data that ought to exist."""
        return self in {GapKind.PROVIDER_MISSING, GapKind.UNKNOWN}


@dataclass(frozen=True, slots=True)
class Gap:
    """One classified absence."""

    symbol: str
    timeframe: Timeframe
    session: date
    kind: GapKind
    expected: int
    received: int

    @property
    def missing(self) -> int:
        return max(0, self.expected - self.received)


class _Bar(Protocol):
    timestamp: datetime


def classify_session(
    *,
    symbol: str,
    timeframe: Timeframe,
    session: date,
    received: int,
    expected: int,
    calendar: TradingCalendar,
    first_known: datetime | None,
) -> Gap:
    """Classify one session's shortfall.

    Order matters. A closed market explains an absence completely, so it is
    checked before anything else; an unlisted instrument explains it next. Only a
    session that was genuinely open, for an instrument that genuinely traded, and
    still has no data, is the provider's.
    """
    if not calendar.is_trading_day(session):
        kind = GapKind.EXPECTED_MARKET_CLOSURE
    elif first_known is not None and session < ensure_utc(first_known).date():
        kind = GapKind.SYMBOL_NOT_TRADING
    elif received == 0 and expected > 0:
        kind = GapKind.PROVIDER_MISSING
    elif received < expected:
        kind = GapKind.UNKNOWN
    else:
        kind = GapKind.EXPECTED_MARKET_CLOSURE

    return Gap(
        symbol=symbol,
        timeframe=timeframe,
        session=session,
        kind=kind,
        expected=expected,
        received=received,
    )


def summarise(gaps: Sequence[Gap]) -> dict[str, int]:
    """Counts by kind, so a report leads with the number that matters."""
    counts: dict[str, int] = {}
    for gap in gaps:
        counts[gap.kind.value] = counts.get(gap.kind.value, 0) + 1
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ResampledBar:
    """A higher-timeframe bar derived from finer ones."""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def resample(bars: Sequence[_Bar], *, target: Timeframe) -> list[ResampledBar]:
    """Aggregate finer bars into ``target`` buckets.

    Provided as a **verification tool**, not as a storage strategy. Measured
    against real data it reproduces Alpaca's own 15-minute and hourly bars
    exactly -- 416/416 and 115/115, OHLC and volume -- which is what makes it
    usable to cross-check a backfill. Whether to *stop storing* the higher
    timeframes is a separate question, answered in docs/storage-planning.md
    (the recommendation is no: query cost and provider fidelity are worth more
    than the 31% of rows it would save).

    Buckets align to the epoch-anchored grid, matching how the provider stamps
    them. Partial buckets are emitted as-is; the caller decides whether an
    incomplete final bucket is usable.
    """
    minutes = int(target.duration.total_seconds() // 60)
    buckets: dict[datetime, list[_Bar]] = {}

    for bar in bars:
        stamp = ensure_utc(bar.timestamp)
        if minutes >= 1440:  # noqa: PLR2004 -- daily and longer bucket by date
            key = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            floored = (stamp.hour * 60 + stamp.minute) // minutes * minutes
            key = stamp.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)
        buckets.setdefault(key, []).append(bar)

    out: list[ResampledBar] = []
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda b: ensure_utc(b.timestamp))
        out.append(
            ResampledBar(
                timestamp=key,
                open=group[0].open,  # type: ignore[attr-defined]
                high=max(b.high for b in group),  # type: ignore[attr-defined]
                low=min(b.low for b in group),  # type: ignore[attr-defined]
                close=group[-1].close,  # type: ignore[attr-defined]
                volume=sum((b.volume for b in group), Decimal(0)),  # type: ignore[attr-defined]
            )
        )
    return out
