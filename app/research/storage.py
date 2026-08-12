"""Estimating what a historical expansion will actually cost on disk.

Every constant here was **measured against the live database**, not derived from
struct sizes or row widths. Theoretical estimates go wrong in both directions at
once: they ignore B-tree overhead, page slack and index duplication, and they
assume the provider delivers a bar for every calendar slot. The measurements
below already contain all of that.

The number that matters
-----------------------
A candle costs ~295 bytes. A :class:`SignalEvaluation` costs **~4,200** -- roughly
fourteen times as much -- because it stores four timeframes of assessment plus
five metric blobs as JSON. So the research dataset, not the market data, is what
fills a disk: a decade of 5-minute bars for 52 symbols is tens of gigabytes, but
scanning that decade every 15 minutes and storing the result is hundreds.

That asymmetry is why raw expansion and research materialisation are separate
stages (part P), and why the plan reports them separately.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from app.core.time import ensure_utc
from app.domain.enums import Timeframe
from app.market_data.calendars import TradingCalendar

MEASUREMENT_VERSION: Final = "storage-v1"
"""Bump when the constants are re-measured, so a plan can be traced to its basis."""

BYTES_PER_CANDLE: Final = 295.0
"""Measured: 162,378 candles occupying 47.9 MB including both indexes.

Heap is ~205 bytes and the indexes add ~90. The composite primary key
``(instrument_id, timeframe, timestamp)`` is itself a covering index, which is
why index overhead is a third of the total rather than a rounding error.
"""

BYTES_PER_EVALUATION: Final = 4218.0
"""Measured: 4,324 evaluations occupying 18.2 MB.

Dominated by five JSON columns, chiefly ``timeframe_states`` -- four timeframes,
each with trend, structure and a nested structure-metrics object. Storing the
full context was a deliberate phase-4 choice so a future model could inspect it;
this is the price, and it is the single largest driver of research storage.
"""

BYTES_PER_OUTCOME: Final = 263.0
"""Measured: 29,414 outcome rows occupying 7.7 MB including three indexes."""

BYTES_PER_TRADE_OUTCOME: Final = 533.0
"""Measured: 192 rows occupying 102 KB. Small sample, so treated as approximate."""

BARS_PER_SESSION: Final[dict[Timeframe, float]] = {
    Timeframe.M5: 73.89,
    Timeframe.M15: 26.90,
    Timeframe.H1: 7.29,
    Timeframe.D1: 1.00,
}
"""Measured bars per symbol per session, as **Alpaca actually delivers them**.

Not the theoretical slot count. A regular session holds 78 five-minute slots, but
the IEX feed prints no bar for a slot with no trades, so the realised yield is
lower; conversely the numbers include the extended-hours bars Alpaca returns.
Using 78 would overstate five-minute volume by ~6% and understate hourly volume,
because hourly bars span pre- and post-market too.
"""

EVALUATIONS_PER_SESSION_PER_SYMBOL: Final = 6.0
"""One evaluation per hourly bar close in the regular session.

Matches the phase-5 benchmark: 3,744 observations = 52 symbols x 12 sessions x 6
instants. A 15-minute scan cadence would be ~26; see
:func:`estimate_research` for how the cadence is varied.
"""

HORIZON_COUNT: Final = 7
"""15m, 1h, 4h, 1d, 3d, 5d, 20d -- one outcome row each per evaluation."""

LOW_FACTOR: Final = 0.85
HIGH_FACTOR: Final = 1.30
"""Bounds on the point estimate.

Asymmetric on purpose. Overshooting a disk budget is recoverable; running out of
space mid-backfill with a half-written database is not, so the upper bound is
further from the centre than the lower one.
"""

WORKING_HEADROOM_FACTOR: Final = 2.0
"""Transient space a write needs beyond the final size.

SQLite's WAL grows before checkpointing, ``VACUUM`` rewrites the whole file
alongside the original, and an export materialises a second copy. Budgeting only
the steady-state size is how a backfill dies at 95% complete.
"""

MINIMUM_FREE_BYTES: Final = 20 * 1024**3
"""20 GB must remain free afterwards, whatever the projection says.

Not a technical limit -- it is the user's laptop. macOS needs room for swap,
Time Machine snapshots and updates, and a tool that fills a personal machine to
capacity has caused a much bigger problem than the one it solved.
"""


@dataclass(frozen=True, slots=True)
class Range:
    """LOW / EXPECTED / HIGH for one quantity."""

    expected: float

    @property
    def low(self) -> float:
        return self.expected * LOW_FACTOR

    @property
    def high(self) -> float:
        return self.expected * HIGH_FACTOR

    def as_dict(self) -> dict[str, float]:
        return {"low": self.low, "expected": self.expected, "high": self.high}


@dataclass(frozen=True, slots=True)
class DiskStatus:
    """Free space, and whether the plan fits in it."""

    total_bytes: int
    free_bytes: int
    required_bytes: float
    verdict: str
    detail: str

    @property
    def is_safe(self) -> bool:
        return self.verdict == "SAFE"


@dataclass(frozen=True, slots=True)
class StoragePlan:
    """A deterministic projection. Same inputs, same numbers, always."""

    symbols: int
    sessions: int
    start: datetime
    end: datetime
    timeframes: tuple[Timeframe, ...]

    candle_rows: int
    evaluation_rows: int
    outcome_rows: int
    trade_outcome_rows: int

    raw_bytes: Range
    research_bytes: Range
    export_bytes: Range
    disk: DiskStatus | None = None
    measurement_version: str = MEASUREMENT_VERSION
    notes: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> Range:
        return Range(self.raw_bytes.expected + self.research_bytes.expected)

    def as_dict(self) -> dict[str, Any]:
        return {
            "measurement_version": self.measurement_version,
            "symbols": self.symbols,
            "sessions": self.sessions,
            "range": [self.start.date().isoformat(), self.end.date().isoformat()],
            "timeframes": [t.value for t in self.timeframes],
            "rows": {
                "candles": self.candle_rows,
                "evaluations": self.evaluation_rows,
                "outcomes": self.outcome_rows,
                "trade_outcomes": self.trade_outcome_rows,
            },
            "bytes": {
                "raw": self.raw_bytes.as_dict(),
                "research": self.research_bytes.as_dict(),
                "export": self.export_bytes.as_dict(),
                "total": self.total_bytes.as_dict(),
            },
            "disk": None
            if self.disk is None
            else {
                "free": self.disk.free_bytes,
                "required": self.disk.required_bytes,
                "verdict": self.disk.verdict,
                "detail": self.disk.detail,
            },
            "notes": self.notes,
        }


def count_sessions(calendar: TradingCalendar, *, start: datetime, end: datetime) -> int:
    """Trading sessions in ``[start, end]``, from the exchange calendar.

    The calendar rather than ``(end - start) / 7 * 5``: holidays vary by year and
    the approximation drifts by roughly nine sessions annually, which compounds
    to a whole quarter over a decade.
    """
    return calendar.count_sessions(ensure_utc(start), ensure_utc(end))


def estimate_candles(
    *, symbols: int, sessions: int, timeframes: tuple[Timeframe, ...]
) -> dict[Timeframe, int]:
    """Candle rows per timeframe, from the measured per-session yields."""
    return {
        timeframe: round(symbols * sessions * BARS_PER_SESSION.get(timeframe, 1.0))
        for timeframe in timeframes
    }


def estimate_research(
    *, symbols: int, sessions: int, evaluations_per_session: float
) -> tuple[int, int, int]:
    """Evaluation, outcome and trade-outcome rows for a full materialisation.

    Trade outcomes are counted only for *qualified* signals -- the phase-5
    benchmark qualified 16 of 3,744 observations (0.43%), and assuming one trade
    per observation would overstate that table by two orders of magnitude.
    """
    evaluations = round(symbols * sessions * evaluations_per_session)
    outcomes = evaluations * HORIZON_COUNT
    qualified = round(evaluations * 0.0043)
    trade_outcomes = qualified * 3  # three personal portfolios
    return evaluations, outcomes, trade_outcomes


def check_disk(required_bytes: float, *, path: Path | None = None) -> DiskStatus:
    """Whether ``required_bytes`` can be written without endangering the machine.

    Three separate guards, and the plan must clear all of them:

    1. the projected data itself,
    2. :data:`WORKING_HEADROOM_FACTOR` for WAL growth, vacuum and export copies,
    3. :data:`MINIMUM_FREE_BYTES` still free afterwards.

    ``WARNING`` means it fits but leaves less than double the minimum reserve --
    proceed deliberately, not by default.
    """
    usage = shutil.disk_usage(path or Path.cwd())
    needed = required_bytes * WORKING_HEADROOM_FACTOR
    remaining = usage.free - needed

    if remaining < MINIMUM_FREE_BYTES:
        verdict = "UNSAFE"
        detail = (
            f"needs {_gb(needed)} incl. working headroom; only {_gb(usage.free)} free, "
            f"which would leave {_gb(max(remaining, 0))} against a {_gb(MINIMUM_FREE_BYTES)} floor"
        )
    elif remaining < MINIMUM_FREE_BYTES * 2:
        verdict = "WARNING"
        detail = (
            f"fits, but would leave only {_gb(remaining)} free; "
            f"consider a shorter range or fewer timeframes"
        )
    else:
        verdict = "SAFE"
        detail = f"needs {_gb(needed)} incl. headroom; {_gb(usage.free)} free"

    return DiskStatus(
        total_bytes=usage.total,
        free_bytes=usage.free,
        required_bytes=needed,
        verdict=verdict,
        detail=detail,
    )


def build_plan(
    *,
    calendar: TradingCalendar,
    symbols: int,
    start: datetime,
    end: datetime,
    timeframes: tuple[Timeframe, ...] = (
        Timeframe.M5,
        Timeframe.M15,
        Timeframe.H1,
        Timeframe.D1,
    ),
    evaluations_per_session: float = EVALUATIONS_PER_SESSION_PER_SYMBOL,
    include_research: bool = True,
    path: Path | None = None,
) -> StoragePlan:
    """Project storage for one expansion. Deterministic and side-effect free."""
    sessions = count_sessions(calendar, start=start, end=end)
    per_timeframe = estimate_candles(symbols=symbols, sessions=sessions, timeframes=timeframes)
    candle_rows = sum(per_timeframe.values())

    evaluations, outcomes, trade_outcomes = (
        estimate_research(
            symbols=symbols, sessions=sessions, evaluations_per_session=evaluations_per_session
        )
        if include_research
        else (0, 0, 0)
    )

    raw = Range(candle_rows * BYTES_PER_CANDLE)
    research = Range(
        evaluations * BYTES_PER_EVALUATION
        + outcomes * BYTES_PER_OUTCOME
        + trade_outcomes * BYTES_PER_TRADE_OUTCOME
    )
    # Parquet is columnar and compressed; the ratio is measured, not assumed --
    # see `research storage-plan --measure-parquet` and docs/storage-planning.md.
    export = Range(research.expected * PARQUET_RATIO)

    notes: list[str] = []
    if include_research and research.expected > raw.expected:
        notes.append(
            "research data exceeds raw market data: an evaluation costs ~14x a candle, "
            "so materialise research by date range rather than all at once"
        )
    if Timeframe.M5 in timeframes:
        notes.append("5-minute bars are ~74% of all candle rows")

    return StoragePlan(
        symbols=symbols,
        sessions=sessions,
        start=ensure_utc(start),
        end=ensure_utc(end),
        timeframes=timeframes,
        candle_rows=candle_rows,
        evaluation_rows=evaluations,
        outcome_rows=outcomes,
        trade_outcome_rows=trade_outcomes,
        raw_bytes=raw,
        research_bytes=research,
        export_bytes=export,
        disk=check_disk(raw.high + research.high, path=path),
        notes=notes,
    )


PARQUET_RATIO: Final = 0.02
"""Measured Parquet size as a fraction of the equivalent SQLite research rows.

From a real 3,120-row export: **88 bytes/row in Parquet against 4,481 in SQLite**
(one evaluation plus one outcome), a ~51x reduction. Columnar layout plus
dictionary encoding on the repeated strings -- symbol, sector, session, status
and the five version fields -- does most of the work.

**Not a like-for-like comparison.** The export carries a *subset* of the
evaluation's columns; SQLite additionally stores the five JSON metric blobs,
which is most of its 4.2 KB. The honest columnar-compression figure is
Parquet against CSV of the same columns: **0.197**, a 5x reduction.

Both numbers are useful and they answer different questions. This one sizes an
archive; the CSV ratio describes the format.
"""


def measure_parquet_ratio(*, parquet_bytes: int, sqlite_bytes: float) -> float:
    """The realised compression ratio for a representative export."""
    return parquet_bytes / sqlite_bytes if sqlite_bytes else 0.0


def _gb(value: float) -> str:
    return f"{value / 1024**3:.1f} GB"


def human_bytes(value: float) -> str:
    """Bytes as MB or GB, whichever reads better."""
    if value >= 1024**3:
        return f"{value / 1024**3:.2f} GB"
    return f"{value / 1024**2:.1f} MB"
