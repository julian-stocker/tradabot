"""Computing what happened after a signal. **This is Y.**

Pure functions over a future bar series, deliberately: nothing here touches the
database, the feature pipeline or the scoring engine, so there is no path by
which a label can influence the features it is supposed to be predicted from.
The separation is structural, not a rule people remember.

Market outcome, not trade outcome
---------------------------------
Everything in this module answers "what did the price do?". It knows nothing
about capital, sizing, fees or rejection. That question -- "what would *we* have
made?" -- is :mod:`app.research.execution`, and the two are kept apart because a
signal can easily be right about the market and lose money after costs. Merging
them produces a number that cannot answer either question.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from app.db.models import Candle
from app.domain.enums import BarrierOutcome, Horizon, LabelStatus, Side, Timeframe

BPS: Final = Decimal(10_000)


@dataclass(frozen=True, slots=True)
class BarrierResult:
    """Which barrier was reached, and when."""

    outcome: BarrierOutcome
    target_hit: bool
    stop_hit: bool
    time_to_target_seconds: float | None
    time_to_stop_seconds: float | None
    ambiguous_bar_timestamp: datetime | None
    """Set when one candle spanned both levels, so the row can be excluded."""


@dataclass(frozen=True, slots=True)
class MarketOutcome:
    """What the market did after a reference instant, over one horizon."""

    horizon: Horizon
    status: LabelStatus
    reference_timestamp: datetime
    reference_price: Decimal
    label_timeframe: Timeframe | None = None
    future_timestamp: datetime | None = None
    future_price: Decimal | None = None
    raw_return: float | None = None
    """Simple return over the horizon, as a ratio (0.012 == +1.2%)."""
    mfe: float | None = None
    """Maximum favourable excursion as a ratio, signed for the direction."""
    mae: float | None = None
    """Maximum adverse excursion as a ratio. Never positive for a live window."""
    bars_observed: int = 0
    barriers: BarrierResult | None = None

    @property
    def raw_return_bps(self) -> float | None:
        return None if self.raw_return is None else self.raw_return * 10_000.0

    @property
    def is_positive(self) -> bool | None:
        """Whether the horizon return was positive. ``None`` when unlabelled."""
        return None if self.raw_return is None else self.raw_return > 0


def compute_market_outcome(
    *,
    horizon: Horizon,
    reference_timestamp: datetime,
    reference_price: Decimal,
    future_bars: list[Candle],
    label_timeframe: Timeframe,
    target_price: Decimal | None = None,
    stop_price: Decimal | None = None,
    side: Side = Side.LONG,
    horizon_elapsed: bool = True,
) -> MarketOutcome:
    """Label one (evaluation, horizon) pair from the bars that followed it.

    Args:
        future_bars: bars strictly after ``reference_timestamp`` and no later
            than the resolved horizon target, ascending. The caller does the
            windowing; this function trusts it, because the point-in-time
            reasoning belongs in one place (:mod:`app.research.service`).
        horizon_elapsed: whether the horizon is in the past. Distinguishes "no
            bars because it has not happened yet" (``PENDING``) from "no bars
            because they are missing" (``INSUFFICIENT_FUTURE_DATA``) -- see
            :class:`~app.domain.enums.LabelStatus` on why neither may become 0.0.

    A window with no bars returns an unlabelled outcome rather than a zero.
    """
    if not future_bars:
        return MarketOutcome(
            horizon=horizon,
            status=LabelStatus.PENDING
            if not horizon_elapsed
            else (LabelStatus.INSUFFICIENT_FUTURE_DATA),
            reference_timestamp=reference_timestamp,
            reference_price=reference_price,
            label_timeframe=label_timeframe,
        )

    if reference_price <= 0:
        return MarketOutcome(
            horizon=horizon,
            status=LabelStatus.INSUFFICIENT_FUTURE_DATA,
            reference_timestamp=reference_timestamp,
            reference_price=reference_price,
            label_timeframe=label_timeframe,
        )

    final = future_bars[-1]
    reference = float(reference_price)
    direction = 1.0 if side is Side.LONG else -1.0

    raw_return = direction * (float(final.close) / reference - 1.0)

    highest = max(float(bar.high) for bar in future_bars)
    lowest = min(float(bar.low) for bar in future_bars)

    if side is Side.LONG:
        mfe = highest / reference - 1.0
        mae = lowest / reference - 1.0
    else:
        # For a short, a falling price is favourable; the excursions swap and the
        # sign flips so MFE stays "good" and MAE stays "bad" in both directions.
        mfe = 1.0 - lowest / reference
        mae = 1.0 - highest / reference

    barriers = (
        _walk_barriers(
            bars=future_bars,
            reference_timestamp=reference_timestamp,
            target_price=target_price,
            stop_price=stop_price,
            side=side,
        )
        if target_price is not None or stop_price is not None
        else None
    )

    return MarketOutcome(
        horizon=horizon,
        status=LabelStatus.COMPLETE,
        reference_timestamp=reference_timestamp,
        reference_price=reference_price,
        label_timeframe=label_timeframe,
        future_timestamp=final.timestamp,
        future_price=final.close,
        raw_return=raw_return,
        mfe=mfe,
        mae=mae,
        bars_observed=len(future_bars),
        barriers=barriers,
    )


def _walk_barriers(
    *,
    bars: list[Candle],
    reference_timestamp: datetime,
    target_price: Decimal | None,
    stop_price: Decimal | None,
    side: Side,
) -> BarrierResult:
    """Find which level was touched first, bar by bar.

    Same-bar ambiguity
    ------------------
    When a single candle's range covers both levels, OHLC cannot say which came
    first -- only that both happened within those minutes. The tempting move is
    to pick whichever suits the narrative, and picking the target is how a
    backtest quietly converts its worst trades into its best ones.

    This records :attr:`BarrierOutcome.AMBIGUOUS_SAME_BAR` and stops. It does not
    guess, because the label's job is to describe the data faithfully; when
    *execution* is forced to resolve it, that happens in the paper engine under
    :class:`~app.domain.enums.CandleAmbiguityPolicy`, which assumes the stop.
    Keeping the ambiguity visible here is what makes it possible to measure how
    many results depend on it.
    """
    for bar in bars:
        high = bar.high
        low = bar.low
        if side is Side.LONG:
            hit_target = target_price is not None and high >= target_price
            hit_stop = stop_price is not None and low <= stop_price
        else:
            hit_target = target_price is not None and low <= target_price
            hit_stop = stop_price is not None and high >= stop_price

        elapsed = (bar.timestamp - reference_timestamp).total_seconds()

        if hit_target and hit_stop:
            return BarrierResult(
                outcome=BarrierOutcome.AMBIGUOUS_SAME_BAR,
                target_hit=True,
                stop_hit=True,
                time_to_target_seconds=elapsed,
                time_to_stop_seconds=elapsed,
                ambiguous_bar_timestamp=bar.timestamp,
            )
        if hit_target:
            return BarrierResult(
                outcome=BarrierOutcome.TARGET_FIRST,
                target_hit=True,
                stop_hit=False,
                time_to_target_seconds=elapsed,
                time_to_stop_seconds=None,
                ambiguous_bar_timestamp=None,
            )
        if hit_stop:
            return BarrierResult(
                outcome=BarrierOutcome.STOP_FIRST,
                target_hit=False,
                stop_hit=True,
                time_to_target_seconds=None,
                time_to_stop_seconds=elapsed,
                ambiguous_bar_timestamp=None,
            )

    return BarrierResult(
        outcome=BarrierOutcome.NEITHER,
        target_hit=False,
        stop_hit=False,
        time_to_target_seconds=None,
        time_to_stop_seconds=None,
        ambiguous_bar_timestamp=None,
    )
