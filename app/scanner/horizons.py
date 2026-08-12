"""Trading horizons: what tradabot can and cannot say about a stock.

One score is not one recommendation. A name can be breaking out on the hour and
going nowhere on the day, and collapsing that into a single BUY throws away the
only part a human actually needs -- *over what period*.

So a horizon is answered separately, from the timeframes that bear on it, and a
horizon with no evidence returns :attr:`HorizonState.NOT_AVAILABLE` rather than
a neutral-looking guess. "No opinion" and "no movement expected" are different
statements, and conflating them is how a tool starts implying forecasts it
cannot make.

Why LONG_TERM is not available
------------------------------
It would be easy to claim it: daily candles go back six years, so a six-month
view *looks* supportable. It is not, for three independent reasons, and any one
of them would be enough:

* **No label reaches that far.** Outcomes are computed to 20 trading days
  (docs/outcome-labels.md). Nothing in the research dataset measures what
  happens over three months, so no claim about it has ever been checked.
* **The features do not look that far.** The whole set -- EMA 20/50, RSI 14,
  ATR 14, volatility 20, 60-bar structure -- has a lookback measured in weeks.
  A six-month view built from a 50-day average is a short-term view with a long
  label on it.
* **There is no fundamental input at all.** Earnings, guidance, valuation and
  balance sheet are what actually drive a multi-month thesis, and tradabot has
  none of them.

Adding daily candles cannot fix this. See :data:`LONG_TERM_REQUIREMENTS`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.domain.enums import Horizon, Timeframe
from app.scanner.enums import DataQuality, TrendState
from app.scanner.timeframes import MultiTimeframeContext, TimeframeAssessment


class TradingHorizon(StrEnum):
    """The periods a human might act over."""

    INTRADAY = "INTRADAY"
    """Minutes to the same session's close."""
    SHORT_TERM = "SHORT_TERM"
    """1-5 trading days."""
    MEDIUM_TERM = "MEDIUM_TERM"
    """1-4 weeks (roughly 5-20 trading days)."""
    LONG_TERM = "LONG_TERM"
    """1-6 months. Not supported by the current data -- see the module docstring."""


class HorizonState(StrEnum):
    """A directional read, or the honest absence of one."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    """Evidence exists and points nowhere in particular."""
    NOT_AVAILABLE = "NOT_AVAILABLE"
    """No usable evidence. **Not** the same as NEUTRAL."""

    @property
    def is_directional(self) -> bool:
        return self in {HorizonState.BULLISH, HorizonState.BEARISH}


HORIZON_EVIDENCE: Final[dict[TradingHorizon, tuple[Timeframe, ...]]] = {
    TradingHorizon.INTRADAY: (Timeframe.M5, Timeframe.M15),
    TradingHorizon.SHORT_TERM: (Timeframe.H1, Timeframe.M15),
    TradingHorizon.MEDIUM_TERM: (Timeframe.D1, Timeframe.H1),
    TradingHorizon.LONG_TERM: (),
}
"""Which stored timeframes inform each horizon.

Ordered by weight: the first is primary, the second corroborates. The mapping is
deliberately conservative -- a 5-minute bar says nothing about next month, and a
daily bar says little about the next twenty minutes.

``LONG_TERM`` maps to nothing, which is the point: it is unsupported by
construction, not by accident of configuration.
"""

HORIZON_LABELS: Final[dict[TradingHorizon, tuple[Horizon, ...]]] = {
    TradingHorizon.INTRADAY: (Horizon.M15, Horizon.H1, Horizon.H4),
    TradingHorizon.SHORT_TERM: (Horizon.D1, Horizon.D3, Horizon.D5),
    TradingHorizon.MEDIUM_TERM: (Horizon.D20,),
    TradingHorizon.LONG_TERM: (),
}
"""Which outcome labels measure each horizon, so a claim can be checked.

A horizon with no label has never been validated against anything. That is the
formal reason ``LONG_TERM`` cannot be reported.
"""

LONG_TERM_REQUIREMENTS: Final[tuple[str, ...]] = (
    "outcome labels beyond 20 trading days (60d / 120d)",
    "fundamental data: earnings, guidance, valuation, balance sheet",
    "features with a multi-month lookback (200-day trend, relative strength)",
    "sector and index context to separate a stock's move from its market's",
)
"""What phase 5.7+ would need before LONG_TERM could be answered honestly."""


@dataclass(frozen=True, slots=True)
class HorizonAssessment:
    """One horizon's read, with the evidence that produced it."""

    horizon: TradingHorizon
    state: HorizonState
    evidence: tuple[Timeframe, ...] = ()
    quality: DataQuality | None = None
    detail: str = ""

    @property
    def is_available(self) -> bool:
        return self.state is not HorizonState.NOT_AVAILABLE


def classify_horizons(
    context: MultiTimeframeContext,
) -> dict[TradingHorizon, HorizonAssessment]:
    """Read each horizon independently from the timeframes that bear on it.

    Independently is the whole point. A stock may legitimately be

        INTRADAY BULLISH · SHORT_TERM BULLISH · MEDIUM_TERM NEUTRAL · LONG_TERM NOT_AVAILABLE

    and reporting a single verdict would have to discard three of those four.
    """
    return {horizon: _assess(horizon, context) for horizon in TradingHorizon}


def _assess(horizon: TradingHorizon, context: MultiTimeframeContext) -> HorizonAssessment:
    sources = HORIZON_EVIDENCE[horizon]
    if not sources:
        return HorizonAssessment(
            horizon=horizon,
            state=HorizonState.NOT_AVAILABLE,
            detail="no supporting timeframe, label or fundamental input",
        )

    usable = [
        assessment
        for timeframe in sources
        if (assessment := context.get(timeframe)) is not None
        and assessment.quality in (DataQuality.OK, DataQuality.STALE)
        and assessment.trend is not TrendState.UNKNOWN
    ]
    if not usable:
        return HorizonAssessment(
            horizon=horizon,
            state=HorizonState.NOT_AVAILABLE,
            evidence=sources,
            detail="supporting timeframes are missing, too short or unreadable",
        )

    return HorizonAssessment(
        horizon=horizon,
        state=_direction_of(usable),
        evidence=tuple(assessment.timeframe for assessment in usable),
        quality=min((a.quality for a in usable), key=_quality_rank),
        detail=f"from {', '.join(a.timeframe.value for a in usable)}",
    )


def _direction_of(assessments: list[TimeframeAssessment]) -> HorizonState:
    """Agreement across the horizon's own timeframes.

    Unanimity is required for a directional call. Two timeframes disagreeing is
    precisely the situation where a confident answer is least warranted, and
    NEUTRAL says that honestly.
    """
    bullish = sum(1 for a in assessments if a.trend in _BULLISH)
    bearish = sum(1 for a in assessments if a.trend in _BEARISH)

    if bullish and not bearish:
        return HorizonState.BULLISH
    if bearish and not bullish:
        return HorizonState.BEARISH
    return HorizonState.NEUTRAL


_BULLISH: Final[frozenset[TrendState]] = frozenset({TrendState.STRONG_UP, TrendState.UP})
_BEARISH: Final[frozenset[TrendState]] = frozenset({TrendState.STRONG_DOWN, TrendState.DOWN})

_QUALITY_ORDER: Final[dict[DataQuality, int]] = {
    DataQuality.OK: 0,
    DataQuality.STALE: 1,
    DataQuality.INSUFFICIENT: 2,
    DataQuality.MISSING: 3,
}


def _quality_rank(quality: DataQuality) -> int:
    return _QUALITY_ORDER.get(quality, 99)
