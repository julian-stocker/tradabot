"""Multi-timeframe context.

Four timeframes with **explicit, different roles**. Averaging four scores would
throw away the only thing that makes multiple timeframes worth computing: whether
they agree, and which one disagrees.

======  ==========================================================
1d      Macro direction. Is the instrument in an uptrend at all?
1h      The primary setup. Where a signal is actually identified.
15m     Confirmation. Does the structure support the 1h read?
5m      Entry timing. Immediate momentum and volume.
======  ==========================================================

Why the roles matter
--------------------
These two situations produce a similar average and mean opposite things::

    1d UP    1h UP    15m BREAKOUT   5m volume confirmed   -> aligned
    1d DOWN  1h SIDEWAYS  15m UP     5m UP                 -> a bounce in a downtrend

The first is a setup. The second is the most common way a short-timeframe signal
loses money: real momentum, pointed against the larger trend. A single blended
number cannot distinguish them, so this module keeps the states separate, scores
*agreement* explicitly, and persists all four so a future model can inspect what
the scanner actually saw.

**Unknown is not neutral.** A timeframe with insufficient history is ``UNKNOWN``
and is excluded from the agreement denominator rather than counted as a
non-vote. Treating missing data as a neutral opinion silently rewards
instruments with less history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from app.domain.enums import Timeframe
from app.scanner.enums import DataQuality, StructureState, TrendState
from app.scanner.structure import StructureMetrics

SCANNER_TIMEFRAMES: Final[tuple[Timeframe, ...]] = (
    Timeframe.M5,
    Timeframe.M15,
    Timeframe.H1,
    Timeframe.D1,
)

MACRO_TIMEFRAME: Final = Timeframe.D1
PRIMARY_TIMEFRAME: Final = Timeframe.H1
CONFIRMATION_TIMEFRAME: Final = Timeframe.M15
ENTRY_TIMEFRAME: Final = Timeframe.M5

TIMEFRAME_ROLES: Final[dict[Timeframe, str]] = {
    Timeframe.D1: "macro",
    Timeframe.H1: "primary",
    Timeframe.M15: "confirmation",
    Timeframe.M5: "entry",
}

# EMA spread beyond which a trend is "strong" rather than merely present, as a
# percent of price. An engineering threshold for describing a state, not a claim
# that 2% predicts anything.
STRONG_TREND_EMA_SPREAD_PCT: Final = 2.0
TREND_EMA_SPREAD_PCT: Final = 0.2

RSI_OVERBOUGHT: Final = 70.0
RSI_OVERSOLD: Final = 30.0
HIGH_RELATIVE_VOLUME: Final = 1.5
"""Volume at 1.5x its 20-bar average counts as elevated."""


@dataclass(frozen=True, slots=True)
class TimeframeAssessment:
    """One timeframe's read, with its inputs preserved.

    Persisted whole. A future model must be able to inspect what the scanner saw
    on each timeframe, not just the number it collapsed them into.
    """

    timeframe: Timeframe
    role: str
    quality: DataQuality

    trend: TrendState = TrendState.UNKNOWN
    structure: StructureState = StructureState.UNKNOWN

    bar_timestamp: datetime | None = None
    bars_used: int = 0

    close: float | None = None
    ema_spread_pct: float | None = None
    rsi: float | None = None
    atr_pct: float | None = None
    relative_volume: float | None = None
    volatility: float | None = None

    structure_metrics: StructureMetrics | None = None

    @property
    def is_usable(self) -> bool:
        return self.quality.is_actionable and self.trend.is_known

    @property
    def volume_confirms(self) -> bool | None:
        """Whether volume is elevated. ``None`` when unmeasured.

        Volume confirmation is only meaningful alongside a directional move, so
        callers combine it with :attr:`trend` rather than reading it alone.
        """
        if self.relative_volume is None:
            return None
        return self.relative_volume >= HIGH_RELATIVE_VOLUME

    def as_dict(self) -> dict[str, Any]:
        """Persistable form. Stable keys, nullable values."""
        return {
            "timeframe": self.timeframe.value,
            "role": self.role,
            "quality": self.quality.value,
            "trend": self.trend.value,
            "structure": self.structure.value,
            "bar_timestamp": self.bar_timestamp.isoformat() if self.bar_timestamp else None,
            "bars_used": self.bars_used,
            "close": self.close,
            "ema_spread_pct": self.ema_spread_pct,
            "rsi": self.rsi,
            "atr_pct": self.atr_pct,
            "relative_volume": self.relative_volume,
            "volatility": self.volatility,
            "structure_metrics": (
                self.structure_metrics.as_dict() if self.structure_metrics else None
            ),
        }


@dataclass(frozen=True, slots=True)
class MultiTimeframeContext:
    """Four timeframes, their agreement, and what disagreed.

    Deliberately not a single blended score. The score is produced downstream by
    the existing :class:`~app.signals.engine.SignalEngine`; this object is the
    *context* that score is interpreted in.
    """

    symbol: str
    assessments: dict[Timeframe, TimeframeAssessment] = field(default_factory=dict)

    def get(self, timeframe: Timeframe) -> TimeframeAssessment | None:
        return self.assessments.get(timeframe)

    @property
    def macro(self) -> TimeframeAssessment | None:
        return self.assessments.get(MACRO_TIMEFRAME)

    @property
    def primary(self) -> TimeframeAssessment | None:
        return self.assessments.get(PRIMARY_TIMEFRAME)

    @property
    def confirmation(self) -> TimeframeAssessment | None:
        return self.assessments.get(CONFIRMATION_TIMEFRAME)

    @property
    def entry(self) -> TimeframeAssessment | None:
        return self.assessments.get(ENTRY_TIMEFRAME)

    @property
    def usable_assessments(self) -> tuple[TimeframeAssessment, ...]:
        return tuple(a for a in self.assessments.values() if a.is_usable)

    @property
    def direction(self) -> int:
        """Net direction across usable timeframes: -1, 0 or +1.

        The **macro** timeframe is not allowed to be outvoted by three shorter
        ones. A 1d downtrend with bullish intraday timeframes is a bounce, and
        reporting it as bullish is precisely the mistake this class exists to
        prevent -- so a directional read requires the macro timeframe not to
        oppose it.
        """
        usable = self.usable_assessments
        if not usable:
            return 0

        net = sum(a.trend.direction for a in usable)
        if net == 0:
            return 0
        proposed = 1 if net > 0 else -1

        macro = self.macro
        if macro is not None and macro.is_usable and macro.trend.direction == -proposed:
            return 0
        return proposed

    @property
    def agreement(self) -> float:
        """How aligned the usable timeframes are, from 0.0 to 1.0.

        The fraction of usable timeframes pointing the same way as the net
        direction. Unknown timeframes are excluded from the denominator entirely:
        counting them would let an instrument with two months of history look
        more "agreed" than one with two years.

        Returns 0.0 when nothing is usable, or when the net direction is zero --
        a genuine three-way split is not agreement about being sideways.
        """
        usable = self.usable_assessments
        if not usable:
            return 0.0

        net = sum(a.trend.direction for a in usable)
        if net == 0:
            return 0.0
        target = 1 if net > 0 else -1
        aligned = sum(1 for a in usable if a.trend.direction == target)
        return aligned / len(usable)

    @property
    def aligned(self) -> bool:
        """Whether macro, primary and confirmation all point the same way.

        The strict reading. Entry timing is excluded on purpose: 5-minute
        momentum flickers, and requiring it to agree would reject setups for a
        reason that will be different in five minutes.
        """
        wanted = self.direction
        if wanted == 0:
            return False
        return all(
            a is not None and a.is_usable and a.trend.direction == wanted
            for a in (self.macro, self.primary, self.confirmation)
        )

    @property
    def quality(self) -> DataQuality:
        """The worst quality across timeframes.

        The worst rather than the best or an average: a setup is only as
        trustworthy as its least trustworthy input, and a stale 1h read is not
        rescued by a fresh 5m one.
        """
        if not self.assessments:
            return DataQuality.MISSING
        order = [DataQuality.MISSING, DataQuality.INSUFFICIENT, DataQuality.STALE, DataQuality.OK]
        return min((a.quality for a in self.assessments.values()), key=order.index)

    @property
    def volume_confirmed(self) -> bool:
        """Whether the entry or confirmation timeframe shows elevated volume."""
        return any(
            a is not None and a.volume_confirms is True for a in (self.entry, self.confirmation)
        )

    def describe(self) -> str:
        """One line per timeframe, for logs and the CLI."""
        return " | ".join(
            f"{a.timeframe.value}:{a.trend.value}"
            for a in sorted(self.assessments.values(), key=lambda a: a.timeframe.value)
        )

    def as_dict(self) -> dict[str, Any]:
        """Persistable form: every timeframe state, not just the summary."""
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "agreement": self.agreement,
            "aligned": self.aligned,
            "quality": self.quality.value,
            "volume_confirmed": self.volume_confirmed,
            "timeframes": {
                timeframe.value: assessment.as_dict()
                for timeframe, assessment in self.assessments.items()
            },
        }


def classify_trend(
    *,
    ema_spread_pct: float | None,
    structure: StructureMetrics | None,
    quality: DataQuality,
) -> TrendState:
    """Turn EMA separation and swing structure into a coarse trend state.

    Two inputs, deliberately:

    * **EMA spread** (20 vs 50, as a percent of price) is fast and always
      available once warmed up, but says nothing about whether price is actually
      making progress.
    * **Swing structure** (higher highs and higher lows) says exactly that, but
      needs confirmed swings and is often unavailable.

    Structure *upgrades* a trend rather than creating one: an instrument with
    higher highs but no EMA separation is drifting, not trending, and calling
    that STRONG_UP would put a confident label on a chart going nowhere.
    """
    if not quality.is_actionable or ema_spread_pct is None:
        return TrendState.UNKNOWN

    if ema_spread_pct >= TREND_EMA_SPREAD_PCT:
        strong = ema_spread_pct >= STRONG_TREND_EMA_SPREAD_PCT and (
            structure.is_uptrend_structure if structure else False
        )
        return TrendState.STRONG_UP if strong else TrendState.UP

    if ema_spread_pct <= -TREND_EMA_SPREAD_PCT:
        strong = ema_spread_pct <= -STRONG_TREND_EMA_SPREAD_PCT and (
            structure.is_downtrend_structure if structure else False
        )
        return TrendState.STRONG_DOWN if strong else TrendState.DOWN

    return TrendState.SIDEWAYS
