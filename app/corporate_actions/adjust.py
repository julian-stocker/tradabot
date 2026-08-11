"""Price-series adjustment.

Pure arithmetic over candles and corporate actions. No database, no I/O, no
configuration -- which is what makes the correctness properties in
``tests/unit/test_adjustments.py`` testable directly.

The rule
--------
For a bar at time *t*, the cumulative price factor is the product of ``1/ratio``
over every split whose ``effective_at`` is **strictly after** *t*::

    price_factor(t) = Π  1 / ratio(s)     for all splits s with s.effective_at > t
    volume_factor(t) = Π  ratio(s)        (shares multiply as prices divide)

Bars at or after the last split are untouched (factor 1). Working backwards from
the present is what makes the *current* price the anchor -- an adjusted series
always ends at today's real traded price, which is the only value a reader can
sanity-check against a broker screen.

Why adjust on read rather than storing adjusted candles
------------------------------------------------------
A new split retroactively changes every earlier adjusted price. Storing them
would mean rewriting the entire history of an instrument on every new action --
a large, error-prone write that also destroys the raw record if it goes wrong.
Recomputation is a cumulative product over a handful of actions, and it keeps the
database a factual record of what the provider actually reported.

Look-ahead
----------
Retrospective adjustment uses knowledge of a *future* split to rescale *past*
prices, so it is not point-in-time honest in the strict sense. It is nonetheless
safe for feature calculation, and the reason is worth stating precisely:

    Multiplying an entire prefix of the series by a positive constant leaves
    every return, ratio and moving-average *relationship* within that prefix
    unchanged.

So the adjustment cannot manufacture a tradable edge -- it removes an artificial
discontinuity without altering any relative quantity. Callers that need strict
point-in-time behaviour (a backtest reconstructing what was knowable on a given
date) pass ``known_as_of`` when loading actions, which excludes actions not yet
effective. See docs/data-adjustments.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.errors import ValidationError
from app.corporate_actions.models import CorporateAction
from app.domain.enums import CorporateActionType, PriceSeriesAdjustment
from app.features.frame import CandleLike

# Adjusted prices are rounded to this many decimals. Deep enough that repeated
# splits do not accumulate visible error, and it matches the storage scale.
PRICE_EXPONENT = Decimal("0.000001")
VOLUME_EXPONENT = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class AdjustedCandle:
    """A candle after price adjustment.

    Structurally satisfies :class:`~app.features.frame.CandleLike`, so it flows
    into ``candles_to_frame`` with no special casing. Prices stay ``Decimal``:
    adjustment happens *before* the float boundary, not after it.
    """

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int | None = None
    vwap: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AdjustmentFactor:
    """The multipliers applied to one bar."""

    price: Decimal
    volume: Decimal

    @property
    def is_identity(self) -> bool:
        return self.price == 1 and self.volume == 1


IDENTITY = AdjustmentFactor(price=Decimal(1), volume=Decimal(1))


def cumulative_split_factors(
    timestamps: Sequence[datetime],
    actions: Sequence[CorporateAction],
) -> list[AdjustmentFactor]:
    """Per-bar adjustment factors for a chronologically sorted timestamp series.

    Computed in one reverse pass: walking backwards, the running factor picks up
    each split as its effective time is crossed. O(bars + actions) rather than
    O(bars x actions).

    Args:
        timestamps: bar timestamps, ascending.
        actions: corporate actions for the instrument, any order. Non-split
            actions are ignored -- dividends do not affect a split-adjusted series.
    """
    splits = sorted(
        (a for a in actions if a.action_type is CorporateActionType.SPLIT),
        key=lambda a: a.effective_at,
    )
    if not splits or not timestamps:
        return [IDENTITY] * len(timestamps)

    factors: list[AdjustmentFactor] = [IDENTITY] * len(timestamps)
    price_factor = Decimal(1)
    volume_factor = Decimal(1)
    split_index = len(splits) - 1

    for bar_index in range(len(timestamps) - 1, -1, -1):
        stamp = timestamps[bar_index]
        # Absorb every split effective strictly after this bar.
        while split_index >= 0 and splits[split_index].effective_at > stamp:
            ratio = splits[split_index].split_ratio
            price_factor /= ratio
            volume_factor *= ratio
            split_index -= 1
        factors[bar_index] = AdjustmentFactor(price=price_factor, volume=volume_factor)

    return factors


def adjust_candles(
    candles: Sequence[CandleLike],
    actions: Sequence[CorporateAction],
    adjustment: PriceSeriesAdjustment,
) -> list[AdjustedCandle]:
    """Apply ``adjustment`` to a chronologically sorted candle series.

    Args:
        candles: raw bars, ascending by timestamp.
        actions: corporate actions for the same instrument.
        adjustment: which series to produce.

    Returns:
        Adjusted candles. For :attr:`PriceSeriesAdjustment.RAW` this is a
        faithful copy -- the caller still gets ``AdjustedCandle`` objects so the
        downstream type does not depend on the mode.

    Raises:
        NotImplementedError: for ``TOTAL_RETURN``.
        ValidationError: if the series is not sorted ascending.
    """
    if adjustment is PriceSeriesAdjustment.TOTAL_RETURN:
        msg = (
            "TOTAL_RETURN series are not implemented. Dividend reinvestment requires "
            "deciding the reinvestment price and timing (ex-date close? payment-date "
            "open?), and a wrong choice silently biases every return. See "
            "docs/data-adjustments.md."
        )
        raise NotImplementedError(msg)

    if not candles:
        return []

    timestamps = [c.timestamp for c in candles]
    _assert_ascending(timestamps)

    if adjustment is PriceSeriesAdjustment.RAW:
        return [_copy(c) for c in candles]

    factors = cumulative_split_factors(timestamps, actions)
    return [
        _copy(candle) if factor.is_identity else _apply(candle, factor)
        for candle, factor in zip(candles, factors, strict=True)
    ]


def has_price_affecting_actions(actions: Sequence[CorporateAction]) -> bool:
    """True if any action would change a split-adjusted series."""
    return any(a.action_type.affects_price_series for a in actions)


def _apply(candle: CandleLike, factor: AdjustmentFactor) -> AdjustedCandle:
    """Scale one bar's prices and volume."""
    return AdjustedCandle(
        timestamp=candle.timestamp,
        open=_price(candle.open * factor.price),
        high=_price(candle.high * factor.price),
        low=_price(candle.low * factor.price),
        close=_price(candle.close * factor.price),
        volume=_volume(candle.volume * factor.volume),
        trade_count=getattr(candle, "trade_count", None),
        vwap=_optional_price(getattr(candle, "vwap", None), factor.price),
    )


def _copy(candle: CandleLike) -> AdjustedCandle:
    return AdjustedCandle(
        timestamp=candle.timestamp,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
        trade_count=getattr(candle, "trade_count", None),
        vwap=getattr(candle, "vwap", None),
    )


def _price(value: Decimal) -> Decimal:
    return value.quantize(PRICE_EXPONENT)


def _volume(value: Decimal) -> Decimal:
    return value.quantize(VOLUME_EXPONENT)


def _optional_price(value: Decimal | None, factor: Decimal) -> Decimal | None:
    return _price(value * factor) if value is not None else None


def _assert_ascending(timestamps: Sequence[datetime]) -> None:
    """Adjustment relies on ordering; an unsorted series would mis-assign factors."""
    for index in range(1, len(timestamps)):
        if timestamps[index] <= timestamps[index - 1]:
            msg = (
                f"candles must be sorted ascending by timestamp before adjustment "
                f"(bar {index} at {timestamps[index].isoformat()} does not follow "
                f"{timestamps[index - 1].isoformat()})"
            )
            raise ValidationError(msg)
