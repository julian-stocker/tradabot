"""Expected movement: how much a symbol typically moves, not which way.

This is the one result from phases 6-8 that earned production. Across 550,000
observations volatility persisted strongly (Spearman 0.95 over one session) while
every directional feature tested flat. So tradabot can honestly say "NVDA is in
its 92nd volatility percentile and a typical session moves it about 2.3%" and
cannot honestly say anything about the direction of that move.

**Placed in `market_data` on purpose.** Expected movement is a property of the
price series, derived from candles, with no signal semantics anywhere in it.
Putting it beside the scanner would invite a future reader to treat it as one
more input to a buy decision, and the whole point of phases 6-8 is that it is
not that. The package boundary is the cheapest available reminder.

Calibration
-----------
The regime-to-range mapping is **measured, not assumed**. Each band's typical and
stress ranges are the median and 90th percentile of the *realised* next-session
range for observations in that band, taken from the phase-8 study over
2020-2026. They are frozen constants here rather than recomputed at runtime,
because a calibration that drifts with recent data would quietly become a
different model under the same name.

What this module must never emit
--------------------------------
No expected closing price, no direction, no probability of an increase, no
target. Those require directional evidence that does not exist. The types make
this structural: there is nowhere in :class:`ExpectedMovement` to put one.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from app.core.time import utc_now
from app.domain.enums import Timeframe

MODEL_VERSION: Final = "volatility-v1"
"""Bump on any change to the bands, the calibration or the ATR definition.

Stored alongside every estimate so a rendered message can always be traced to
the arithmetic that produced it.
"""

ATR_PERIOD: Final = 14
PERCENTILE_WINDOW: Final = 252
"""Trailing hourly bars used to rank today's ATR against the symbol's own past.

About thirty-six sessions. Long enough that a single volatile week does not
redefine "normal", short enough to notice a genuine regime change within a month.
"""

MIN_BARS: Final = 60
"""Below this there is not enough history to rank anything, and the estimate is
refused rather than issued with a wide disclaimer nobody reads."""

BARS_PER_SESSION: Final = 7


class VolatilityRegime(StrEnum):
    """How active a symbol is **relative to its own history**.

    Relative rather than absolute: a 2% session is ordinary for a semiconductor
    and remarkable for a utility, so one universal percentage would classify the
    universe by sector instead of by state.
    """

    LOW = "LOW_VOL"
    NORMAL = "NORMAL_VOL"
    HIGH = "HIGH_VOL"
    EXTREME = "EXTREME_VOL"

    @property
    def is_elevated(self) -> bool:
        return self in {VolatilityRegime.HIGH, VolatilityRegime.EXTREME}


REGIME_BANDS: Final[tuple[tuple[VolatilityRegime, float, float], ...]] = (
    (VolatilityRegime.LOW, 0.00, 0.25),
    (VolatilityRegime.NORMAL, 0.25, 0.70),
    (VolatilityRegime.HIGH, 0.70, 0.90),
    (VolatilityRegime.EXTREME, 0.90, 1.01),
)


@dataclass(frozen=True, slots=True)
class RangeCalibration:
    """Measured next-session range for one regime. **Percentages, not prices.**"""

    typical_pct: float
    """Median realised next-session high-low range, as a percent of price."""
    stress_pct: float
    """90th percentile of the same. The number that belongs in a risk sentence."""
    sample: int


CALIBRATION: Final[dict[VolatilityRegime, RangeCalibration]] = {
    VolatilityRegime.LOW: RangeCalibration(typical_pct=1.82, stress_pct=3.85, sample=156_923),
    VolatilityRegime.NORMAL: RangeCalibration(typical_pct=2.05, stress_pct=4.31, sample=224_021),
    VolatilityRegime.HIGH: RangeCalibration(typical_pct=2.30, stress_pct=4.82, sample=105_123),
    VolatilityRegime.EXTREME: RangeCalibration(typical_pct=2.71, stress_pct=5.83, sample=68_693),
}
"""Phase-8 measurements, frozen. Monotone across all four bands, as measured.

EXTREME delivers about 1.5x LOW's typical range -- a real and useful separation,
and deliberately quoted as such rather than inflated. Anyone tempted to promise
more should read the sample sizes beside it.
"""

VALIDITY = timedelta(hours=7)
"""How long an estimate may be presented as current: one trading session.

Phase 8 measured the decay directly. After one session 96% of EXTREME states are
still elevated; after five, 55%. So a one-session claim is well supported and a
one-week claim is not, and this constant is where that finding is enforced rather
than remembered.
"""

MAX_BAR_AGE = timedelta(minutes=95)
"""Newest bar older than this and the inputs are stale, not the conclusion.

Just over one hourly bar, so a single late sync does not invalidate an estimate
but a stalled feed does.
"""


@dataclass(frozen=True, slots=True)
class ExpectedMovement:
    """A magnitude estimate for one symbol. **Contains no direction, by design.**"""

    symbol: str
    calculated_at: datetime
    bar_timestamp: datetime
    regime: VolatilityRegime
    percentile: float
    atr_pct: float
    recent_range_pct: float
    model_version: str = MODEL_VERSION

    @property
    def calibration(self) -> RangeCalibration:
        return CALIBRATION[self.regime]

    @property
    def typical_range_pct(self) -> float:
        return self.calibration.typical_pct

    @property
    def stress_range_pct(self) -> float:
        return self.calibration.stress_pct

    def data_age(self, *, now: datetime | None = None) -> timedelta:
        return (now or utc_now()) - self.bar_timestamp

    def is_stale(self, *, now: datetime | None = None) -> bool:
        """Whether the inputs are too old to present as current.

        Two separate clocks, and both matter: the newest *bar* may be old because
        the feed stalled, and the *estimate* may be old because nothing has
        recalculated it. Either one makes the number misleading.
        """
        moment = now or utc_now()
        return self.data_age(now=moment) > MAX_BAR_AGE or (moment - self.calculated_at) > VALIDITY

    def summary(self) -> str:
        """One line, magnitude only."""
        return (
            f"{self.symbol}: {self.regime.value} ({self.percentile * 100:.0f}th pct), "
            f"typical next session ~{self.typical_range_pct:.1f}%, "
            f"stress ~{self.stress_range_pct:.1f}%"
        )


def regime_for(percentile: float) -> VolatilityRegime:
    """Band lookup. Half-open, so a value lands in exactly one band."""
    for regime, low, high in REGIME_BANDS:
        if low <= percentile < high:
            return regime
    return VolatilityRegime.EXTREME


def true_range(high: float, low: float, previous_close: float | None) -> float:
    """Classic true range. Uses the *previous* close, never a later one."""
    if previous_close is None:
        return high - low
    return max(high - low, abs(high - previous_close), abs(low - previous_close))


def average_true_range(
    highs: list[float], lows: list[float], closes: list[float], *, period: int = ATR_PERIOD
) -> float | None:
    """Wilder-smoothed ATR over the supplied bars, oldest first.

    Returns ``None`` rather than a partial value when there is too little
    history: an ATR computed from four bars is a number, not an estimate.
    """
    if len(closes) <= period or not (len(highs) == len(lows) == len(closes)):
        return None

    ranges = [
        true_range(highs[i], lows[i], closes[i - 1] if i > 0 else None) for i in range(len(closes))
    ]
    atr = statistics.fmean(ranges[1 : period + 1])
    for value in ranges[period + 1 :]:
        atr = (atr * (period - 1) + value) / period
    return atr


def estimate(
    *,
    symbol: str,
    highs: list[float],
    lows: list[float],
    closes: list[float],
    bar_timestamp: datetime,
    now: datetime | None = None,
) -> ExpectedMovement | None:
    """Expected movement from a symbol's recent hourly bars, oldest first.

    Causal by construction: every input is a bar at or before ``bar_timestamp``,
    and the percentile ranks the current ATR against this symbol's own trailing
    window. Nothing later than the newest supplied bar can influence the result.

    Returns ``None`` when history is too short to rank against -- refusing is the
    correct output, and better than an estimate carrying an asterisk.
    """
    if len(closes) < MIN_BARS or not (len(highs) == len(lows) == len(closes)):
        return None

    current_atr = average_true_range(highs, lows, closes)
    if current_atr is None or closes[-1] <= 0:
        return None
    current_atr_pct = current_atr / closes[-1] * 100

    history = _trailing_atr_percentages(highs, lows, closes)
    if len(history) < MIN_BARS:
        return None

    percentile = sum(1 for value in history if value <= current_atr_pct) / len(history)
    window = min(BARS_PER_SESSION, len(closes))
    recent_range = (max(highs[-window:]) - min(lows[-window:])) / closes[-1] * 100

    return ExpectedMovement(
        symbol=symbol,
        calculated_at=now or utc_now(),
        bar_timestamp=bar_timestamp,
        regime=regime_for(percentile),
        percentile=percentile,
        atr_pct=current_atr_pct,
        recent_range_pct=recent_range,
    )


def _trailing_atr_percentages(
    highs: list[float], lows: list[float], closes: list[float]
) -> list[float]:
    """Rolling ATR% over the trailing window, one value per bar.

    Recomputed incrementally rather than by re-running the full smoothing at
    every offset: the naive form is O(n*period) and this runs for 52 symbols on
    every scheduled cycle.
    """
    ranges = [
        true_range(highs[i], lows[i], closes[i - 1] if i > 0 else None) for i in range(len(closes))
    ]
    if len(ranges) <= ATR_PERIOD:
        return []

    values: list[float] = []
    atr = statistics.fmean(ranges[1 : ATR_PERIOD + 1])
    for index in range(ATR_PERIOD + 1, len(ranges)):
        atr = (atr * (ATR_PERIOD - 1) + ranges[index]) / ATR_PERIOD
        if closes[index] > 0:
            values.append(atr / closes[index] * 100)
    return values[-PERCENTILE_WINDOW:]


PRIMARY_TIMEFRAME: Final = Timeframe.H1
REQUIRED_BARS: Final = PERCENTILE_WINDOW + ATR_PERIOD + 1
"""Bars to load per symbol. Enough for a full trailing window plus ATR warm-up."""
