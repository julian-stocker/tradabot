"""Price-structure metrics from OHLCV.

The one indicator family phase 4 adds. Everything else the scanner needs -- EMAs,
RSI, ATR, realised volatility, relative volume -- already exists in
:mod:`app.features.registry` and is reused rather than reimplemented.

**Every definition here is arithmetic on OHLCV.** No subjective chart patterns,
no "head and shoulders", no image recognition. If a name appears in this module,
the rule that produces it is stated in the function that computes it and can be
disagreed with by reading it. A pattern name whose definition lives only in
someone's head is untestable and, worse, unfalsifiable.

Swing points
------------
A swing high is a bar whose high exceeds the ``k`` bars either side of it. That
is the whole definition. It needs ``k`` bars of *future* data to confirm, which
is why :func:`swing_points` only ever reports swings that are already confirmed
at the evaluation bar -- the alternative would be look-ahead bias wearing the
costume of a chart pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.scanner.enums import StructureState

SWING_WINDOW: Final = 2
"""Bars either side of a candidate swing point. Two is the smallest window that
excludes single-bar noise; larger windows find fewer, slower structures."""

MIN_BARS_FOR_STRUCTURE: Final = 20
BREAKOUT_TOLERANCE: Final = 0.001
"""A close within 0.1% of the range high counts as a breakout. Exact equality
would almost never fire; a wide tolerance would call every strong day a breakout."""

CONSOLIDATION_RANGE_FRACTION: Final = 0.5
"""Recent range must be at most half the earlier range to count as consolidating.
An engineering threshold, not a discovered constant."""


@dataclass(frozen=True, slots=True)
class SwingPoints:
    """Confirmed swing highs and lows, oldest first."""

    highs: tuple[tuple[int, float], ...]
    lows: tuple[tuple[int, float], ...]

    @property
    def has_two_highs(self) -> bool:
        return len(self.highs) >= 2  # noqa: PLR2004 -- two points make a comparison

    @property
    def has_two_lows(self) -> bool:
        return len(self.lows) >= 2  # noqa: PLR2004


@dataclass(frozen=True, slots=True)
class StructureMetrics:
    """What price is doing relative to its own recent history.

    Every field is ``None`` when it cannot be computed, never a default that
    reads as a real observation. ``higher_highs=None`` means "not enough
    confirmed swings"; ``higher_highs=False`` means "the swings say no".
    """

    state: StructureState

    higher_highs: bool | None = None
    higher_lows: bool | None = None
    lower_highs: bool | None = None
    lower_lows: bool | None = None

    distance_to_high_pct: float | None = None
    """Percent below the lookback high. 0 means at the high."""
    distance_to_low_pct: float | None = None
    """Percent above the lookback low."""

    range_position: float | None = None
    """Where the close sits in the lookback range: 0.0 at the low, 1.0 at the high."""

    range_width_pct: float | None = None
    """Lookback range as a percent of its midpoint. A width near zero is a tight
    range, which is what makes a breakout from it meaningful."""

    support: float | None = None
    resistance: float | None = None

    @property
    def is_uptrend_structure(self) -> bool:
        """Higher highs *and* higher lows. Both, because either alone is
        compatible with a widening range going nowhere."""
        return bool(self.higher_highs and self.higher_lows)

    @property
    def is_downtrend_structure(self) -> bool:
        return bool(self.lower_highs and self.lower_lows)

    def as_dict(self) -> dict[str, object]:
        """For persistence. Keys are stable; values may be null."""
        return {
            "state": self.state.value,
            "higher_highs": self.higher_highs,
            "higher_lows": self.higher_lows,
            "lower_highs": self.lower_highs,
            "lower_lows": self.lower_lows,
            "distance_to_high_pct": self.distance_to_high_pct,
            "distance_to_low_pct": self.distance_to_low_pct,
            "range_position": self.range_position,
            "range_width_pct": self.range_width_pct,
            "support": self.support,
            "resistance": self.resistance,
        }


def swing_points(
    highs: list[float], lows: list[float], *, window: int = SWING_WINDOW
) -> SwingPoints:
    """Confirmed swing highs and lows.

    A swing high at index ``i`` satisfies ``high[i] > high[j]`` for every ``j``
    within ``window`` bars either side. Only indices with a full ``window`` of
    bars **after** them are considered, so every reported swing was already
    confirmed by data the evaluation bar could see. Reporting an unconfirmed
    swing at the last bar would be look-ahead: it might still be exceeded.
    """
    if len(highs) != len(lows):
        msg = f"highs and lows must be the same length, got {len(highs)} and {len(lows)}"
        raise ValueError(msg)

    confirmed_highs: list[tuple[int, float]] = []
    confirmed_lows: list[tuple[int, float]] = []

    for index in range(window, len(highs) - window):
        left = range(index - window, index)
        right = range(index + 1, index + window + 1)

        if all(highs[index] > highs[j] for j in left) and all(
            highs[index] > highs[j] for j in right
        ):
            confirmed_highs.append((index, highs[index]))

        if all(lows[index] < lows[j] for j in left) and all(lows[index] < lows[j] for j in right):
            confirmed_lows.append((index, lows[index]))

    return SwingPoints(highs=tuple(confirmed_highs), lows=tuple(confirmed_lows))


def analyse_structure(
    *,
    highs: list[float],
    lows: list[float],
    closes: list[float],
    lookback: int = 20,
    window: int = SWING_WINDOW,
) -> StructureMetrics:
    """Compute structure metrics over the most recent ``lookback`` bars.

    Args:
        highs, lows, closes: equal-length series, oldest first.
        lookback: bars used for range, support and resistance.
        window: swing confirmation window.

    Returns a metrics object with ``UNKNOWN`` state and null fields when there is
    not enough history -- never a fabricated neutral reading.
    """
    if not closes or len(closes) < MIN_BARS_FOR_STRUCTURE:
        return StructureMetrics(state=StructureState.UNKNOWN)
    if not (len(highs) == len(lows) == len(closes)):
        msg = "highs, lows and closes must be the same length"
        raise ValueError(msg)

    recent_highs = highs[-lookback:]
    recent_lows = lows[-lookback:]
    close = closes[-1]

    range_high = max(recent_highs)
    range_low = min(recent_lows)
    span = range_high - range_low
    midpoint = (range_high + range_low) / 2

    swings = swing_points(highs, lows, window=window)
    higher_highs = _rising(swings.highs) if swings.has_two_highs else None
    lower_highs = _falling(swings.highs) if swings.has_two_highs else None
    higher_lows = _rising(swings.lows) if swings.has_two_lows else None
    lower_lows = _falling(swings.lows) if swings.has_two_lows else None

    return StructureMetrics(
        state=_classify_state(
            closes=closes, range_high=range_high, range_low=range_low, lookback=lookback
        ),
        higher_highs=higher_highs,
        higher_lows=higher_lows,
        lower_highs=lower_highs,
        lower_lows=lower_lows,
        distance_to_high_pct=_pct(range_high - close, range_high),
        distance_to_low_pct=_pct(close - range_low, range_low),
        range_position=(close - range_low) / span if span > 0 else None,
        range_width_pct=_pct(span, midpoint),
        support=range_low,
        resistance=range_high,
    )


def _classify_state(
    *, closes: list[float], range_high: float, range_low: float, lookback: int
) -> StructureState:
    """Which of the four structural states the latest close is in.

    Ordered deliberately: a breakout is checked before consolidation, because a
    close breaking out *of* a tight range is a breakout, and reporting it as
    consolidation would describe the bar before rather than the bar in hand.
    """
    close = closes[-1]

    if close >= range_high * (1 - BREAKOUT_TOLERANCE):
        return StructureState.BREAKOUT
    if close <= range_low * (1 + BREAKOUT_TOLERANCE):
        return StructureState.BREAKDOWN

    # Consolidation: the recent half of the window is materially tighter than the
    # earlier half. A range that is simply narrow is not consolidating -- it is
    # narrow. The contraction is the observation.
    half = max(2, lookback // 2)
    if len(closes) >= 2 * half:
        recent = closes[-half:]
        earlier = closes[-2 * half : -half]
        recent_span = max(recent) - min(recent)
        earlier_span = max(earlier) - min(earlier)
        if earlier_span > 0 and recent_span <= earlier_span * CONSOLIDATION_RANGE_FRACTION:
            return StructureState.CONSOLIDATION

    return StructureState.RANGING


def _rising(points: tuple[tuple[int, float], ...]) -> bool:
    """Whether the last two swing points ascend."""
    return points[-1][1] > points[-2][1]


def _falling(points: tuple[tuple[int, float], ...]) -> bool:
    return points[-1][1] < points[-2][1]


def _pct(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return (numerator / denominator) * 100
