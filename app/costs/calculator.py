"""Round-trip transaction-cost calculation.

The cost model is intentionally simple and fully explicit:

    buy fill  = mid * (1 + half_spread_rate + slippage_rate)
    sell fill = mid * (1 - half_spread_rate - slippage_rate)
    fees      = 2 * order_fee + variable_fee_rate * (entry_notional + exit_notional)

with ``slippage_rate = half_spread_rate * slippage_spread_multiple``.

Every parameter comes from :class:`~app.core.config.CostSettings`, i.e. from
configuration, never from a constant buried in this file (coding rule 14).

**Not modelled yet**, and each of these makes real costs worse, never better:
market impact for orders large relative to displayed size, partial fills, spread
widening at the open/close and around news, borrow costs for shorts, taxes
(withholding, stamp duty, financial-transaction taxes), and currency conversion
on cross-currency instruments. Phase 5 exists to calibrate these against
observed fills.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.config import CostSettings
from app.costs.models import BPS, BPS_QUANTUM, CostBreakdown, NetEdge, RoundTripCost
from app.domain.enums import Side
from app.domain.quotes import Quote


def half_spread_rate(spread_bps: float | Decimal) -> Decimal:
    """Fraction of mid price paid to cross one side of the book."""
    spread = Decimal(str(spread_bps))
    if spread < 0:
        msg = f"spread_bps must be non-negative, got {spread}"
        raise ValueError(msg)
    return spread / BPS / Decimal(2)


def estimate_round_trip_cost(
    *,
    entry_mid: Decimal,
    exit_mid: Decimal,
    quantity: Decimal,
    spread_bps: float | Decimal,
    settings: CostSettings,
    side: Side = Side.LONG,
) -> RoundTripCost:
    """Cost and net P&L of a hypothetical round trip.

    Args:
        entry_mid: mid price when the position is opened.
        exit_mid: mid price when it is closed.
        quantity: number of units. Must be positive for both sides -- direction is
            carried by ``side``, not by the sign of the quantity.
        spread_bps: quoted spread in bps of mid. Assumed equal at entry and exit;
            in reality it varies with time of day and volatility.
        settings: broker cost assumptions.
        side: ``LONG`` buys then sells; ``SHORT`` sells then buys.

    Returns:
        A fully itemised :class:`RoundTripCost`.

    Raises:
        ValueError: on non-positive prices or quantity.
    """
    _require_positive(entry_mid, "entry_mid")
    _require_positive(exit_mid, "exit_mid")
    _require_positive(quantity, "quantity")

    half_spread = half_spread_rate(spread_bps)
    slippage = half_spread * settings.slippage_spread_multiple
    adverse = half_spread + slippage

    # Costs are always adverse: you buy above mid and sell below it, whichever
    # leg comes first.
    if side is Side.LONG:
        entry_fill = entry_mid * (Decimal(1) + adverse)
        exit_fill = exit_mid * (Decimal(1) - adverse)
        gross_pnl = (exit_mid - entry_mid) * quantity
        net_pnl_before_fees = (exit_fill - entry_fill) * quantity
    else:
        entry_fill = entry_mid * (Decimal(1) - adverse)
        exit_fill = exit_mid * (Decimal(1) + adverse)
        gross_pnl = (entry_mid - exit_mid) * quantity
        net_pnl_before_fees = (entry_fill - exit_fill) * quantity

    # Split the total adverse move into its spread and slippage parts, in
    # proportion to their rates, so the breakdown sums exactly to the difference
    # between gross and pre-fee net P&L.
    total_adverse_cost = gross_pnl - net_pnl_before_fees
    spread_share = half_spread / adverse if adverse > 0 else Decimal(0)
    spread_cost = total_adverse_cost * spread_share
    slippage_cost = total_adverse_cost - spread_cost

    entry_notional = entry_mid * quantity
    exit_notional = exit_mid * quantity
    fee_cost = Decimal(2) * settings.order_fee + settings.variable_fee_rate * (
        entry_notional + exit_notional
    )

    breakdown = CostBreakdown(
        spread_cost=spread_cost,
        fee_cost=fee_cost,
        slippage_cost=slippage_cost,
    )

    return RoundTripCost(
        quantity=quantity,
        entry_mid=entry_mid,
        exit_mid=exit_mid,
        entry_fill=entry_fill,
        exit_fill=exit_fill,
        breakdown=breakdown,
        gross_pnl=gross_pnl,
        net_pnl=gross_pnl - breakdown.total,
    )


def round_trip_cost_bps(
    *,
    price: Decimal,
    quantity: Decimal,
    spread_bps: float | Decimal,
    settings: CostSettings,
) -> Decimal:
    """Round-trip cost in bps for a flat position of ``quantity`` at ``price``.

    Convenience wrapper that evaluates the cost of entering and immediately
    exiting at the same mid -- i.e. the pure friction, independent of any market
    move. This is the number to compare an expected move against.

    Note the fixed per-order fee makes this **size-dependent**: at a EUR 1.00 fee,
    a EUR 500 position pays 40 bps in fees alone, while a EUR 20,000 position pays
    1 bps. Small positions are frequently uneconomic for reasons that have nothing
    to do with the signal.
    """
    cost = estimate_round_trip_cost(
        entry_mid=price,
        exit_mid=price,
        quantity=quantity,
        spread_bps=spread_bps,
        settings=settings,
    )
    return cost.total_cost_bps


def spread_bps_for(quote: Quote | None, settings: CostSettings) -> Decimal:
    """Spread in bps from a quote, or the configured fallback.

    The fallback is used when no quote is available (e.g. scoring purely from
    historical candles). It is an assumption, and an optimistic one for illiquid
    names, so it is configurable rather than hardcoded.
    """
    if quote is None:
        return Decimal(str(settings.default_spread_bps))
    return Decimal(str(quote.spread_bps))


def net_expected_edge(
    *,
    expected_move_bps: float | Decimal,
    cost_bps: float | Decimal,
) -> NetEdge:
    """Combine a raw predicted move with its cost.

    This is the boundary the project requires between *prediction* and
    *opportunity*. A model that is right about direction 60% of the time is still
    a losing strategy if its average move is smaller than the round trip it takes
    to capture it.
    """
    expected = Decimal(str(expected_move_bps))
    cost = Decimal(str(cost_bps))
    if cost < 0:
        msg = f"cost_bps must be non-negative, got {cost}"
        raise ValueError(msg)
    # Quantise for presentation. Division by a notional produces a full-precision
    # Decimal (28 significant digits), and a cost quoted to 25 decimal places
    # implies an accuracy this estimate does not remotely have.
    return NetEdge(
        expected_move_bps=expected.quantize(BPS_QUANTUM),
        cost_bps=cost.quantize(BPS_QUANTUM),
        net_edge_bps=(expected - cost).quantize(BPS_QUANTUM),
    )


def _require_positive(value: Decimal, name: str) -> None:
    if value <= 0:
        msg = f"{name} must be positive, got {value}"
        raise ValueError(msg)
