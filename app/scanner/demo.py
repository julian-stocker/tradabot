"""Deterministic scanner demonstration.

Walks one instrument through the whole lifecycle -- discovered, qualified,
strong, weakened, invalidated -- with paper-trading decisions along the way. No
Discord, no Alpaca, no network: a console notifier and a hand-built price series.

**The prices are constructed, not simulated.** They are chosen to cross the
score thresholds in a fixed order so the machinery is visible; they are not a
market, not a backtest, and not evidence about anything. The same warning applies
here as to the phase 3 paper-trading demo: reading this output as a result would
be reading a unit test as evidence about the world.

What it demonstrates: that the scan cycle persists every evaluation, that a
continuing setup keeps one identity across cycles, that lifecycle transitions
fire once each, and that paper decisions fan out across profiles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.time import utc_now
from app.db.models import Instrument
from app.domain.enums import AssetType, Timeframe
from app.instruments.repository import InstrumentRepository
from app.market_data.provider import CandleData, InstrumentInfo
from app.market_data.repository import CandleRepository
from app.scanner.repository import WatchlistRepository
from app.scanner.timeframes import SCANNER_TIMEFRAMES

logger = get_logger(__name__)

DEMO_SYMBOL = "DEMOSCAN"
DEMO_START = datetime(2024, 6, 3, 13, 30, tzinfo=UTC)

# Bars per timeframe. Enough to warm up a 50-period EMA (the deepest feature)
# with margin for the structure lookback.
DEMO_BARS = 235

SPIKE_BARS = 6
"""Bars at the end of a phase that carry its volume spike. Fewer than the 20-bar
averaging window, so the ratio actually moves."""

BASE_VOLUME = 1_000_000


@dataclass
class DemoPhase:
    """One stage of the demonstration."""

    name: str
    drift: float
    """Per-bar drift as a fraction. Positive builds a trend, negative breaks it."""
    bars: int
    note: str
    volume_spike: float = 1.0
    """Volume multiple on this phase's bars, relative to the flat baseline.

    Necessary, and instructive about the scoring model. The volume component
    carries a quarter of the signal weight and reads ``rel_volume_20`` -- volume
    against its own 20-bar average. *Smoothly* rising volume therefore scores
    zero, because the average rises with it. Only a step change registers. A pure
    trend with flat volume saturates momentum and trend at ~97 and still tops out
    near 63, below the 75 qualification threshold: half the weight sits in
    components a smooth synthetic series leaves neutral."""


DEMO_PHASES: tuple[DemoPhase, ...] = (
    DemoPhase("base", 0.0000, 140, "flat -- nothing to see, and nothing announced"),
    DemoPhase("advance", 0.0040, 55, "advance on expanding volume -- crosses the threshold", 2.6),
    DemoPhase("acceleration", 0.0060, 25, "acceleration -- the strongest reading", 3.2),
    DemoPhase("breakdown", -0.0120, 15, "the premise breaks", 1.2),
)


@dataclass
class DemoResult:
    """What the demonstration observed."""

    cycles: int = 0
    evaluations: int = 0
    transitions: list[tuple[str, str, float]] = field(default_factory=list)
    """(cycle label, lifecycle, score) each time the state changed."""
    paper_decisions: int = 0
    positions_opened: int = 0

    def describe(self) -> str:
        return (
            f"{self.cycles} cycles, {self.evaluations} evaluations stored, "
            f"{len(self.transitions)} lifecycle transitions, "
            f"{self.positions_opened} positions opened"
        )


def build_price_path() -> list[tuple[float, float]]:
    """The constructed ``(close, volume multiple)`` series, oldest first.

    Deterministic: no randomness anywhere, so the demo prints the same thing on
    every machine and every run. A demo that varied would be untestable and would
    invite reading its output as a result.
    """
    path: list[tuple[float, float]] = []
    price = 100.0
    for phase in DEMO_PHASES:
        for index in range(phase.bars):
            price *= 1.0 + phase.drift
            # The spike lands on the phase's *last* bars only. Applying it to the
            # whole phase would raise the 20-bar average with it and leave
            # `rel_volume_20` at 1.0 -- the ratio is against its own history, so
            # a sustained level is not elevated, only a step change is.
            spiking = index >= phase.bars - SPIKE_BARS
            path.append((round(price, 4), phase.volume_spike if spiking else 1.0))
    return path


def _candles(
    path: list[tuple[float, float]], timeframe: Timeframe, start: datetime
) -> list[CandleData]:
    """OHLCV around a close path.

    Highs and lows sit a fixed fraction either side of the bar's range, so every
    bar satisfies the OHLC invariants and the structure metrics have something to
    measure.
    """
    candles: list[CandleData] = []
    previous = path[0][0]
    for index, (close, volume_multiple) in enumerate(path):
        high = max(close, previous) * 1.004
        low = min(close, previous) * 0.996
        candles.append(
            CandleData(
                timestamp=start + timeframe.duration * index,
                open=Decimal(str(round(previous, 4))),
                high=Decimal(str(round(high, 4))),
                low=Decimal(str(round(low, 4))),
                close=Decimal(str(close)),
                volume=Decimal(str(round(BASE_VOLUME * volume_multiple, 0))),
            )
        )
        previous = close
    return candles


async def seed_demo_instrument(
    session: AsyncSession, *, now: datetime | None = None, bars: int | None = None
) -> Instrument:
    """Create (or extend) the demo instrument's candles on every timeframe.

    ``bars`` truncates the price path, so successive calls reveal the series
    progressively. That is how the demo advances: each cycle **re-seeds more
    data ending at the same instant**, rather than moving an ``as_of`` cursor
    backwards.

    The distinction matters and cost me a wrong demo. Each timeframe's 220 bars
    span a different wall-clock window -- 220 five-minute bars are 18 hours, 220
    daily bars are ten months -- so no single ``as_of`` corresponds to the same
    point in all four series. Stepping the clock showed the 1-hour series almost
    the same data every cycle, including the final breakdown, which is why a
    steady advance scored negative. Growing the data instead keeps every
    timeframe at the same phase.
    """
    now = now or utc_now()
    instruments = InstrumentRepository(session)
    await instruments.upsert_many(
        [
            InstrumentInfo(
                symbol=DEMO_SYMBOL,
                name="Scanner Demo Instrument",
                exchange="XNYS",
                currency="USD",
                asset_type=AssetType.STOCK,
                listed_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        ],
        provider="demo",
    )
    await session.flush()

    instrument = await instruments.get_by_symbol(DEMO_SYMBOL)
    if instrument is None:  # pragma: no cover -- just upserted
        msg = "demo instrument disappeared after upsert"
        raise RuntimeError(msg)

    path = build_price_path()
    if bars is not None:
        path = path[:bars]
    candles = CandleRepository(session)
    for timeframe in SCANNER_TIMEFRAMES:
        # Each timeframe ends at `now`, so every series is fresh at scan time and
        # the demo does not accidentally demonstrate staleness handling instead.
        start = now - timeframe.duration * len(path)
        await candles.upsert_many(
            instrument_id=instrument.id,
            timeframe=timeframe,
            candles=_candles(path, timeframe, start),
            provider="demo",
        )

    await WatchlistRepository(session).add(instrument.id, tags=["demo"])
    await session.flush()
    return instrument


def phase_boundaries() -> list[tuple[str, int, str]]:
    """(name, cumulative bar index, note) at the end of each phase.

    The demo scans at these points, so each cycle sees the price path as it stood
    at the end of a phase -- which is what makes the lifecycle progression
    reproducible rather than dependent on where the scan happens to land.
    """
    boundaries: list[tuple[str, int, str]] = []
    total = 0
    for phase in DEMO_PHASES:
        total += phase.bars
        boundaries.append((phase.name, total, phase.note))
    return boundaries


__all__ = [
    "DEMO_PHASES",
    "DEMO_SYMBOL",
    "DemoResult",
    "build_price_path",
    "phase_boundaries",
    "seed_demo_instrument",
]
