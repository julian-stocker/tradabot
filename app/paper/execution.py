"""Fill pricing for a single order leg.

Phase 1's :func:`~app.costs.calculator.estimate_round_trip_cost` prices *both*
legs at once, which is right for "what does a round trip cost?" and wrong for a
paper broker, which fills one leg at a time and must know the exact price it got.

This module answers the per-leg question. The two agree exactly: a buy leg plus a
sell leg at the same mid reconcile with the round-trip calculation, and
``test_per_leg_reconciles_with_round_trip`` pins that.

The rule
--------
A market order does not fill at the mid. It crosses the book::

    BUY  base = ask   then slippage moves it further UP
    SELL base = bid   then slippage moves it further DOWN

Slippage is always adverse. There is no branch in which it helps, because a
simulator that occasionally fills you *better* than the touch is not modelling a
market -- it is modelling a wish.

When no quote is available the touch is reconstructed from the mid and the
configured default spread. That is a fallback, marked as such on the result, not
a silent equivalence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.config import CostSettings
from app.costs.models import BPS
from app.domain.enums import Side
from app.domain.quotes import Quote

PRICE_EXPONENT = Decimal("0.000001")
MONEY_EXPONENT = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class FillPricing:
    """What one leg cost, itemised.

    Kept itemised because the three components have different natures: the fee is
    contractual, the spread is observed, and the slippage is an *assumption*.
    A single "cost" number hides which part of an estimate is actually measured.
    """

    side: Side
    quantity: Decimal
    mid_price: Decimal
    touch_price: Decimal
    """Ask for a buy, bid for a sell -- before slippage."""
    fill_price: Decimal
    """What the order actually filled at, after slippage."""

    spread_cost: Decimal
    slippage_cost: Decimal
    fee: Decimal
    used_quote: bool
    """False when the touch was reconstructed from the default spread."""

    @property
    def notional(self) -> Decimal:
        """Value of the leg at the fill price."""
        return self.fill_price * self.quantity

    @property
    def total_cost(self) -> Decimal:
        """Every cost of this leg, in account currency."""
        return self.spread_cost + self.slippage_cost + self.fee

    @property
    def cash_delta(self) -> Decimal:
        """Signed change in cash from this leg, fees included.

        A buy consumes ``notional + fee``; a sell returns ``notional - fee``. The
        spread and slippage are already inside ``fill_price`` and must not be
        subtracted again -- double-counting them is the easiest accounting error
        to make here, which is why cash movement is derived in exactly one place.
        """
        if self.side is Side.LONG:
            return -(self.notional + self.fee)
        return self.notional - self.fee


def half_spread_from_quote(quote: Quote) -> Decimal:
    """Half the quoted spread, in price units."""
    return quote.half_spread


def price_fill(
    *,
    side: Side,
    quantity: Decimal,
    settings: CostSettings,
    quote: Quote | None = None,
    reference_price: Decimal | None = None,
) -> FillPricing:
    """Price one market-order leg.

    Args:
        side: ``LONG`` buys, ``SHORT`` sells. (For an *exit* of a long position,
            pass ``SHORT`` -- this function prices a leg, not a position.)
        quantity: units. Must be positive.
        settings: the profile's broker cost assumptions.
        quote: live top-of-book. Preferred, because it is measured.
        reference_price: mid to fall back on when ``quote`` is None. Required in
            that case.

    Returns:
        A :class:`FillPricing` with the achieved price and itemised costs.

    Raises:
        ValueError: non-positive quantity, or neither a quote nor a reference
            price. Guessing a price here would be inventing market data.
    """
    if quantity <= 0:
        msg = f"quantity must be positive, got {quantity}"
        raise ValueError(msg)

    if quote is not None:
        mid = quote.mid_price
        half_spread = quote.half_spread
        used_quote = True
    else:
        if reference_price is None or reference_price <= 0:
            msg = (
                "price_fill needs either a quote or a positive reference_price; "
                "refusing to invent a price"
            )
            raise ValueError(msg)
        mid = reference_price
        # Reconstruct the touch from the configured default spread.
        half_spread = mid * Decimal(str(settings.default_spread_bps)) / BPS / Decimal(2)
        used_quote = False

    # Slippage is expressed as a multiple of the half-spread, matching the phase 1
    # cost model, so one configured number drives both.
    slippage_per_unit = half_spread * settings.slippage_spread_multiple

    if side is Side.LONG:
        touch = mid + half_spread
        fill = touch + slippage_per_unit
    else:
        touch = mid - half_spread
        fill = touch - slippage_per_unit

    if fill <= 0:
        msg = (
            f"computed a non-positive fill price ({fill}) for a {side.value} leg at "
            f"mid {mid}; the spread or slippage assumption is implausible"
        )
        raise ValueError(msg)

    notional = _quantize_price(fill) * quantity
    fee = settings.order_fee + settings.variable_fee_rate * notional

    return FillPricing(
        side=side,
        quantity=quantity,
        mid_price=_quantize_price(mid),
        touch_price=_quantize_price(touch),
        fill_price=_quantize_price(fill),
        spread_cost=_quantize_money(half_spread * quantity),
        slippage_cost=_quantize_money(slippage_per_unit * quantity),
        fee=_quantize_money(fee),
        used_quote=used_quote,
    )


def estimate_round_trip_cost(
    *,
    settings: CostSettings,
    notional: Decimal,
    quote: Quote | None = None,
) -> Decimal:
    """Modelled cost of entering and later exiting a position of ``notional``.

    **The single source of truth for "what will this trade cost".** Before this
    existed, the risk gate took a caller-supplied estimate, which meant the
    number constraining a position and the number actually charged came from two
    places and could silently disagree.

    Both legs are counted, because a risk budget has to survive the exit as well
    as the entry:

    * two order fees, and the variable fee on both notionals;
    * the half-spread crossed twice;
    * slippage twice, at the configured multiple of the half-spread.

    The exit notional is approximated by the entry notional. That is a
    simplification and a deliberate one: the true exit size depends on where the
    position is closed, which is unknowable at entry, and assuming it equals the
    entry is neutral rather than optimistic.
    """
    if notional <= 0:
        return Decimal(0)

    if quote is not None and quote.mid_price > 0:
        half_spread_rate = quote.half_spread / quote.mid_price
    else:
        half_spread_rate = Decimal(str(settings.default_spread_bps)) / BPS / Decimal(2)

    spread_cost = half_spread_rate * notional * Decimal(2)
    slippage_cost = half_spread_rate * settings.slippage_spread_multiple * notional * Decimal(2)
    fees = (settings.order_fee * Decimal(2)) + (settings.variable_fee_rate * notional * Decimal(2))
    return _quantize_money(spread_cost + slippage_cost + fees)


def liquidation_value(*, quantity: Decimal, quote: Quote | None, mark_price: Decimal) -> Decimal:
    """What a long position is worth if closed right now.

    Uses the **bid** when a quote exists, not the mid. A holder cannot realise the
    mid -- they must sell into the bid -- so marking at mid overstates equity by
    half a spread on every open position, and that overstatement flatters both the
    equity curve and the drawdown.

    Fees and slippage are deliberately *not* deducted: they belong to an exit that
    has not happened. Equity is therefore still slightly optimistic, by roughly
    one exit fee per position, and that is documented rather than silently fixed
    with an arbitrary reserve.
    """
    price = quote.bid if quote is not None else mark_price
    return _quantize_money(price * quantity)


def _quantize_price(value: Decimal) -> Decimal:
    return value.quantize(PRICE_EXPONENT)


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_EXPONENT)
