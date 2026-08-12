"""Support and resistance as **zones**, not lines.

A level is not a price. Price does not reverse at 174.32; it reverses somewhere
around 174, and pretending otherwise produces invalidations that trigger on noise
and targets that are never quite reached. Every level here is an interval with a
width derived from the instrument's own volatility.

Built entirely from :func:`~app.scanner.structure.swing_points`, which is already
causally safe: it only reports a swing once ``window`` bars have passed *after*
it, so a swing visible at bar ``i`` was confirmed by data bar ``i`` could see.
This module adds clustering, strength and role, and never looks at a bar the
caller did not pass in.

Causality
---------
The single rule: **a zone at time T may only use bars at or before T.** The
functions here take a bar window and nothing else -- no repository, no `as_of`,
no clock. A caller that slices the window correctly cannot produce a leaky level,
and a caller that slices it wrongly is doing so visibly.

What this is not
----------------
Not a prediction. A resistance zone says "price has repeatedly failed here",
which is a statement about the past. Whether it holds again is exactly what the
historical evaluation in docs/trade-plans.md measures, and the answer is closer
to a coin flip than the phrase "resistance" suggests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from statistics import fmean
from typing import Final, Protocol

from app.scanner.structure import swing_points

MIN_BARS_FOR_LEVELS: Final = 30
"""Below this there are too few confirmed swings to cluster anything."""

CLUSTER_ATR_MULTIPLE: Final = 0.6
"""Two swings within this many ATRs are the same level.

Volatility-scaled rather than a fixed percentage: 0.5% is a wide band on a utility
and a tight one on a semiconductor, and a fixed number would merge unrelated
levels on the first and split a single level on the second.
"""

ZONE_ATR_MULTIPLE: Final = 0.35
"""Half-width of a zone built from a single touch, in ATRs.

A multi-touch zone spans its own touches instead; this only sets the floor, so a
lone swing still gets a plausible band rather than an infinitely thin line.
"""

MAX_ZONES_PER_SIDE: Final = 4
"""More than four levels per side is a chart nobody can read."""

RECENCY_HALF_LIFE_BARS: Final = 60
"""Bars after which a touch counts half.

Old levels genuinely matter less -- the participants who defended them have moved
on -- but they do not vanish, so this decays rather than truncates.
"""


class LevelType(StrEnum):
    SUPPORT = "SUPPORT"
    RESISTANCE = "RESISTANCE"


class BreakState(StrEnum):
    """Where price stands relative to a zone it is interacting with.

    Deliberately more granular than "broke out". A close beyond a level is the
    *beginning* of a breakout, not the event; distinguishing the attempt from the
    confirmation is what stops a wick through resistance being reported as a
    breakout.
    """

    NONE = "NONE"
    BREAKOUT_ATTEMPT = "BREAKOUT_ATTEMPT"
    """Closed beyond resistance, not yet confirmed by a further bar."""
    BREAKOUT_CONFIRMED = "BREAKOUT_CONFIRMED"
    """Held beyond the zone on a subsequent bar."""
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    """Closed beyond, then closed back inside. The trap."""
    RETEST_IN_PROGRESS = "RETEST_IN_PROGRESS"
    """After a confirmed break, price has returned to the zone."""
    RETEST_CONFIRMED = "RETEST_CONFIRMED"
    """Returned to the zone and held: broken resistance acting as support."""
    BREAKDOWN_ATTEMPT = "BREAKDOWN_ATTEMPT"
    BREAKDOWN_CONFIRMED = "BREAKDOWN_CONFIRMED"
    FAILED_BREAKDOWN = "FAILED_BREAKDOWN"

    @property
    def is_confirmed_break(self) -> bool:
        return self in {BreakState.BREAKOUT_CONFIRMED, BreakState.BREAKDOWN_CONFIRMED}


class _Bar(Protocol):
    timestamp: datetime
    open: object
    high: object
    low: object
    close: object


@dataclass(frozen=True, slots=True)
class Zone:
    """One support or resistance area."""

    type: LevelType
    lower_bound: float
    upper_bound: float
    timeframe: str
    touch_count: int
    first_seen: datetime
    last_seen: datetime
    strength: float
    """0-1. An engineering assumption, not a fitted weight -- see :func:`_strength`."""
    confidence: float
    """0-1. How much evidence the zone rests on, separate from how strong it is."""
    reason_codes: tuple[str, ...] = ()

    @property
    def midpoint(self) -> float:
        return (self.lower_bound + self.upper_bound) / 2

    @property
    def width(self) -> float:
        return self.upper_bound - self.lower_bound

    def contains(self, price: float) -> bool:
        return self.lower_bound <= price <= self.upper_bound

    def distance_bps(self, price: float) -> float:
        """Signed distance from ``price`` to the zone edge, in basis points.

        Zero inside the zone. Measured to the **near edge**, because that is
        where an interaction begins -- measuring to the midpoint would
        systematically understate how close price already is.
        """
        if price <= 0:
            return 0.0
        if self.contains(price):
            return 0.0
        edge = self.lower_bound if price < self.lower_bound else self.upper_bound
        return (edge - price) / price * 10_000

    def as_dict(self) -> dict[str, object]:
        return {
            "type": self.type.value,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "midpoint": self.midpoint,
            "timeframe": self.timeframe,
            "touch_count": self.touch_count,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "strength": self.strength,
            "confidence": self.confidence,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class LevelMap:
    """Every zone found for one instrument on one timeframe."""

    symbol: str
    timeframe: str
    support: tuple[Zone, ...]
    resistance: tuple[Zone, ...]
    atr: float | None = None

    def nearest_support(self, price: float) -> Zone | None:
        """The closest support at or below ``price``."""
        below = [z for z in self.support if z.upper_bound <= price]
        return max(below, key=lambda z: z.upper_bound) if below else None

    def nearest_resistance(self, price: float) -> Zone | None:
        above = [z for z in self.resistance if z.lower_bound >= price]
        return min(above, key=lambda z: z.lower_bound) if above else None


def build_levels(
    bars: list[_Bar],
    *,
    symbol: str,
    timeframe: str,
    atr: float | None = None,
    window: int = 2,
) -> LevelMap:
    """Cluster confirmed swings into support and resistance zones.

    ``bars`` must end at the evaluation instant. Nothing here reaches beyond the
    list, so the caller's slice *is* the causality boundary.
    """
    if len(bars) < MIN_BARS_FOR_LEVELS:
        return LevelMap(symbol=symbol, timeframe=timeframe, support=(), resistance=(), atr=atr)

    highs = [float(bar.high) for bar in bars]  # type: ignore[arg-type]
    lows = [float(bar.low) for bar in bars]  # type: ignore[arg-type]
    closes = [float(bar.close) for bar in bars]  # type: ignore[arg-type]
    swings = swing_points(highs, lows, window=window)

    scale = atr if atr and atr > 0 else _fallback_atr(highs, lows)
    last_index = len(bars) - 1

    resistance = _cluster(
        points=swings.highs,
        bars=bars,
        level_type=LevelType.RESISTANCE,
        timeframe=timeframe,
        scale=scale,
        last_index=last_index,
        closes=closes,
    )
    support = _cluster(
        points=swings.lows,
        bars=bars,
        level_type=LevelType.SUPPORT,
        timeframe=timeframe,
        scale=scale,
        last_index=last_index,
        closes=closes,
    )

    return LevelMap(
        symbol=symbol,
        timeframe=timeframe,
        support=support,
        resistance=resistance,
        atr=scale,
    )


def _cluster(
    *,
    points: tuple[tuple[int, float], ...],
    bars: list[_Bar],
    level_type: LevelType,
    timeframe: str,
    scale: float,
    last_index: int,
    closes: list[float],
) -> tuple[Zone, ...]:
    """Group nearby swings into zones, strongest first.

    Clustering is single-pass over price-sorted swings: a swing joins the current
    cluster if it sits within ``CLUSTER_ATR_MULTIPLE`` ATRs of it. Simple and
    deterministic on purpose -- a k-means over swing prices would be less
    predictable and no more meaningful.
    """
    if not points:
        return ()

    tolerance = scale * CLUSTER_ATR_MULTIPLE
    ordered = sorted(points, key=lambda pair: pair[1])

    clusters: list[list[tuple[int, float]]] = [[ordered[0]]]
    for index, price in ordered[1:]:
        if price - clusters[-1][-1][1] <= tolerance:
            clusters[-1].append((index, price))
        else:
            clusters.append([(index, price)])

    zones: list[Zone] = []
    for cluster in clusters:
        prices = [price for _, price in cluster]
        indices = [index for index, _ in cluster]
        half = max(scale * ZONE_ATR_MULTIPLE, (max(prices) - min(prices)) / 2)
        centre = fmean(prices)

        strength, confidence, reasons = _strength(
            touches=len(cluster),
            newest_index=max(indices),
            last_index=last_index,
            level_type=level_type,
            centre=centre,
            closes=closes,
            scale=scale,
        )
        zones.append(
            Zone(
                type=level_type,
                lower_bound=centre - half,
                upper_bound=centre + half,
                timeframe=timeframe,
                touch_count=len(cluster),
                first_seen=bars[min(indices)].timestamp,
                last_seen=bars[max(indices)].timestamp,
                strength=strength,
                confidence=confidence,
                reason_codes=reasons,
            )
        )

    zones.sort(key=lambda zone: zone.strength, reverse=True)
    return tuple(zones[:MAX_ZONES_PER_SIDE])


def _strength(
    *,
    touches: int,
    newest_index: int,
    last_index: int,
    level_type: LevelType,
    centre: float,
    closes: list[float],
    scale: float,
) -> tuple[float, float, tuple[str, ...]]:
    """How much a zone is worth paying attention to.

    **Engineering assumptions, not fitted weights.** Nothing here was optimised
    against outcome labels, and doing so on this dataset would be fitting three
    parameters to 228 episodes. The components are:

    * **touches** -- a level defended three times is more real than one touched
      once. Saturating, because the tenth touch adds little;
    * **recency** -- exponential decay over ``RECENCY_HALF_LIFE_BARS``; old
      levels matter less but do not vanish;
    * **role reversal** -- price having traded through the level and returned to
      respect it from the other side is the strongest single piece of evidence a
      level is real.

    Returned separately from ``confidence``, which measures how much *evidence*
    exists rather than how strong the level is: one recent touch can be a strong
    level observed weakly.
    """
    reasons: list[str] = []

    touch_score = min(touches / 3.0, 1.0)
    if touches >= 3:  # noqa: PLR2004
        reasons.append("multiple_touches")
    elif touches == 2:  # noqa: PLR2004
        reasons.append("two_touches")
    else:
        reasons.append("single_touch")

    age = max(0, last_index - newest_index)
    recency = 0.5 ** (age / RECENCY_HALF_LIFE_BARS)
    if age <= RECENCY_HALF_LIFE_BARS / 2:
        reasons.append("recent")

    reversal = _role_reversal(centre=centre, closes=closes, level_type=level_type, scale=scale)
    if reversal:
        reasons.append("role_reversal")

    strength = min(1.0, 0.5 * touch_score + 0.3 * recency + (0.2 if reversal else 0.0))
    # Confidence rises with evidence, not with strength: it is how much we know.
    confidence = min(1.0, 0.4 + 0.2 * min(touches, 3) + (0.1 if reversal else 0.0))

    return round(strength, 4), round(confidence, 4), tuple(reasons)


def _role_reversal(
    *, centre: float, closes: list[float], level_type: LevelType, scale: float
) -> bool:
    """Has price traded on both sides of this level and come back to respect it?

    Support that used to be resistance (or vice versa) is the classic evidence
    that a level is a real decision point rather than an accident of one swing.
    """
    if not closes or scale <= 0:
        return False
    band = scale * CLUSTER_ATR_MULTIPLE
    above = any(close > centre + band for close in closes)
    below = any(close < centre - band for close in closes)
    if not (above and below):
        return False
    # And price is currently on the side the level's role implies.
    latest = closes[-1]
    return latest > centre if level_type is LevelType.SUPPORT else latest < centre


def classify_break(  # noqa: PLR0911 -- one return per break state; merging them would hide the states
    *,
    zone: Zone,
    bars: list[_Bar],
    volume_confirmed: bool | None = None,  # noqa: ARG001 -- see the docstring
) -> BreakState:
    """Where price stands relative to a zone, using only the bars given.

    **Confirmation requires a subsequent bar, and that is the whole point.** A
    close beyond resistance is an *attempt*; it becomes confirmed only once a
    later bar holds beyond it. Treating the breakout bar itself as confirmation
    would be reading the next bar's behaviour out of a bar that has not happened,
    which is the exact look-ahead this module exists to avoid.

    So a breakout is never CONFIRMED on the final bar of the window.
    """
    if len(bars) < 2:  # noqa: PLR2004
        return BreakState.NONE

    closes = [float(bar.close) for bar in bars]  # type: ignore[arg-type]
    latest = closes[-1]
    previous = closes[-2]

    if zone.type is LevelType.RESISTANCE:
        if previous > zone.upper_bound and latest > zone.upper_bound:
            return BreakState.BREAKOUT_CONFIRMED
        if previous > zone.upper_bound and zone.contains(latest):
            return BreakState.RETEST_IN_PROGRESS
        if previous > zone.upper_bound and latest < zone.lower_bound:
            return BreakState.FAILED_BREAKOUT
        if latest > zone.upper_bound:
            # An attempt either way. Volume is recorded in the reason codes by
            # the caller rather than changing the state: a high-volume attempt is
            # still an attempt until a later bar holds, and letting volume alone
            # promote it to CONFIRMED would reintroduce the look-ahead this
            # function exists to prevent.
            return BreakState.BREAKOUT_ATTEMPT
        if zone.contains(latest) and _broke_earlier(closes, zone, above=True):
            return BreakState.RETEST_CONFIRMED
        return BreakState.NONE

    if previous < zone.lower_bound and latest < zone.lower_bound:
        return BreakState.BREAKDOWN_CONFIRMED
    if previous < zone.lower_bound and latest > zone.upper_bound:
        return BreakState.FAILED_BREAKDOWN
    if latest < zone.lower_bound:
        return BreakState.BREAKDOWN_ATTEMPT
    return BreakState.NONE


def _broke_earlier(closes: list[float], zone: Zone, *, above: bool) -> bool:
    """Did price close beyond the zone at any point before the last two bars?"""
    history = closes[:-2]
    if not history:
        return False
    return (
        any(close > zone.upper_bound for close in history)
        if above
        else any(close < zone.lower_bound for close in history)
    )


def _fallback_atr(highs: list[float], lows: list[float]) -> float:
    """A crude range proxy when no ATR was supplied.

    Mean bar range over the window. Not a true ATR -- it ignores gaps -- but it is
    the right order of magnitude, and the alternative is a fixed percentage that
    is wrong for most instruments.
    """
    ranges = [high - low for high, low in zip(highs, lows, strict=True) if high >= low]
    return fmean(ranges) if ranges else 0.0
