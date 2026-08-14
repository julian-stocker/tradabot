"""Position sizing.

Pure arithmetic: given equity, a price, a stop and a risk configuration, how many
units?

The primary rule is **risk-based**::

    risk_budget    = equity * risk_per_trade
    risk_per_share = entry_price - stop_loss        (long)
    quantity       = risk_budget / risk_per_share

This sizes by *what a loss would cost*, not by notional, which is the only way a
"1% risk" setting means the same thing on a volatile instrument as on a quiet one:
a wider stop buys fewer shares.

The result is then capped -- never raised -- by every other constraint: the
position cap, available cash including the entry fee, and the portfolio exposure
limit. Caps only ever reduce, so the binding constraint is whichever is tightest,
and which one bound is recorded.

No stop, no risk-based size
---------------------------
``risk_per_share`` is the denominator. Without a stop there is no denominator, and
**tradabot will not invent one** -- a fabricated stop distance silently produces an
arbitrary position size that looks principled. The behaviour is configurable:

* ``require_stop_loss=True`` (default) -- refuse the trade, reason ``INVALID_STOP``.
* ``require_stop_loss=False`` -- fall back to notional sizing at
  ``max_position_percent``, which is an explicit, documented rule rather than a
  guess dressed up as risk management.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from app.domain.enums import OrderRejectionReason
from app.paper.execution import estimate_round_trip_cost
from app.simulation.models import SimulationProfileConfig

QUANTITY_EXPONENT = Decimal("0.00000001")
WHOLE_SHARE_EXPONENT = Decimal(1)
ZERO = Decimal(0)


class ExecutionFractionality(StrEnum):
    """Whether the venue will fill part of a share.

    Not cosmetic. Every cap in this module is an *upper* bound derived from risk,
    cash or exposure, so the only safe way to land on a tradable quantity is to
    round **down**. Rounding up by even one share breaches whichever constraint
    was binding -- and on an expensive instrument in a small account, one share
    is the entire position.

    ``WHOLE_SHARES_ONLY`` therefore makes a real constraint visible rather than
    approximating it away: a €100 account that cannot afford one share of a €250
    instrument gets ``QUANTITY_TOO_SMALL``, not 0.4 shares it cannot buy.
    """

    FRACTIONAL_ALLOWED = "FRACTIONAL_ALLOWED"
    WHOLE_SHARES_ONLY = "WHOLE_SHARES_ONLY"


def _round_to_tradable(quantity: Decimal, fractionality: ExecutionFractionality) -> Decimal:
    """Reduce a computed quantity to one the venue can fill. **Never rounds up.**"""
    exponent = (
        WHOLE_SHARE_EXPONENT
        if fractionality is ExecutionFractionality.WHOLE_SHARES_ONLY
        else QUANTITY_EXPONENT
    )
    return quantity.quantize(exponent, rounding=ROUND_DOWN)


class SizingConstraint(StrEnum):
    """Which rule determined the final quantity.

    Recorded on every sized order. Knowing that the €50 portfolio is permanently
    bound by ``AVAILABLE_CASH`` while the €5000 one is bound by ``RISK_BUDGET``
    tells you they are running materially different strategies, which no
    aggregate P&L number would reveal.
    """

    RISK_BUDGET = "RISK_BUDGET"
    MAX_POSITION_PERCENT = "MAX_POSITION_PERCENT"
    AVAILABLE_CASH = "AVAILABLE_CASH"
    MAX_TOTAL_EXPOSURE = "MAX_TOTAL_EXPOSURE"
    NOTIONAL_FALLBACK = "NOTIONAL_FALLBACK"


@dataclass(frozen=True, slots=True)
class SizingResult:
    """A sizing outcome: a quantity, or a reason there is none."""

    quantity: Decimal
    constraint: SizingConstraint | None
    rejection: OrderRejectionReason | None = None
    detail: str = ""

    risk_budget: Decimal = ZERO
    risk_per_share: Decimal = ZERO

    @property
    def is_tradable(self) -> bool:
        return self.quantity > 0 and self.rejection is None


def size_position(  # noqa: PLR0911, PLR0912 -- one branch and one return per rejection
    # cause; merging any two would report the wrong reason for refusing a trade
    *,
    profile: SimulationProfileConfig,
    equity: Decimal,
    available_cash: Decimal,
    current_exposure: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal | None,
    fractionality: ExecutionFractionality = ExecutionFractionality.FRACTIONAL_ALLOWED,
) -> SizingResult:
    """Size a long entry for one profile.

    Args:
        profile: capital, risk appetite and cost assumptions.
        equity: current portfolio equity -- risk fractions apply to this, not to
            initial capital, so a drawdown automatically shrinks position sizes.
        available_cash: free cash, which must also cover the entry fee.
        current_exposure: market value of existing positions.
        entry_price: expected fill price. The *fill*, not the mid -- sizing
            against the mid would systematically overshoot.
        stop_loss: the protective level. ``None`` triggers the policy above.
        fractionality: whether partial shares are fillable. Either way the final
            quantity is rounded **down**, because every cap here is a maximum.

    Returns:
        A :class:`SizingResult`. A rejection is an outcome, not an exception.
    """
    risk = profile.risk

    if entry_price <= 0:
        return SizingResult(
            quantity=ZERO,
            constraint=None,
            rejection=OrderRejectionReason.QUANTITY_TOO_SMALL,
            detail=f"entry price must be positive, got {entry_price}",
        )
    if equity <= 0:
        return SizingResult(
            quantity=ZERO,
            constraint=None,
            rejection=OrderRejectionReason.INSUFFICIENT_CASH,
            detail=f"equity is {equity}; nothing left to risk",
        )

    # --- Primary rule: risk-based ----------------------------------------
    risk_budget = equity * risk.risk_per_trade
    constraint = SizingConstraint.RISK_BUDGET
    risk_per_share = ZERO

    if stop_loss is not None and stop_loss > 0 and stop_loss < entry_price:
        risk_per_share = entry_price - stop_loss
        quantity = risk_budget / risk_per_share
    elif risk.require_stop_loss:
        return SizingResult(
            quantity=ZERO,
            constraint=None,
            rejection=OrderRejectionReason.INVALID_STOP,
            detail=(
                f"no usable stop for a long entry at {entry_price} "
                f"(stop={stop_loss}); {risk.name!r} requires one and tradabot will "
                f"not invent a stop distance"
            ),
            risk_budget=risk_budget,
        )
    else:
        # Explicit, configured fallback -- notional sizing at the position cap.
        quantity = (equity * risk.max_position_percent) / entry_price
        constraint = SizingConstraint.NOTIONAL_FALLBACK

    # --- Caps. Each may only reduce the quantity. -------------------------
    position_cap = (equity * risk.max_position_percent) / entry_price
    if position_cap < quantity:
        quantity, constraint = position_cap, SizingConstraint.MAX_POSITION_PERCENT

    # Cash must cover the notional *and* the entry fee. Ignoring the fee is how a
    # portfolio ends up with negative cash on its last trade.
    fee_adjusted_cash = available_cash - profile.costs.order_fee
    if fee_adjusted_cash <= 0:
        return SizingResult(
            quantity=ZERO,
            constraint=None,
            rejection=OrderRejectionReason.INSUFFICIENT_CASH,
            detail=(
                f"cash {available_cash} does not cover the {profile.costs.order_fee} order fee"
            ),
            risk_budget=risk_budget,
            risk_per_share=risk_per_share,
        )
    cash_cap = fee_adjusted_cash / (entry_price * (Decimal(1) + profile.costs.variable_fee_rate))
    if cash_cap < quantity:
        quantity, constraint = cash_cap, SizingConstraint.AVAILABLE_CASH

    # Portfolio-level exposure headroom.
    exposure_limit = equity * risk.max_total_exposure
    headroom = exposure_limit - current_exposure
    if headroom <= 0:
        return SizingResult(
            quantity=ZERO,
            constraint=None,
            rejection=OrderRejectionReason.MAX_EXPOSURE,
            detail=(
                f"exposure {current_exposure} already at or above the "
                f"{risk.max_total_exposure} x equity limit ({exposure_limit})"
            ),
            risk_budget=risk_budget,
            risk_per_share=risk_per_share,
        )
    exposure_cap = headroom / entry_price
    if exposure_cap < quantity:
        quantity, constraint = exposure_cap, SizingConstraint.MAX_TOTAL_EXPOSURE

    unrounded = quantity
    quantity = _round_to_tradable(quantity, fractionality)

    if quantity <= 0:
        return SizingResult(
            quantity=ZERO,
            constraint=constraint,
            rejection=OrderRejectionReason.QUANTITY_TOO_SMALL,
            detail=(
                f"sized quantity {unrounded:.8f} rounds down to zero at "
                f"{entry_price} per unit under {fractionality.value} "
                f"(bound by {constraint.value if constraint else 'unknown'})"
            ),
            risk_budget=risk_budget,
            risk_per_share=risk_per_share,
        )

    notional = quantity * entry_price

    # A position must be able to pay for its own exit.
    #
    # Nothing above guarantees this. The cash cap reserves the *entry* fee, but a
    # flat exit fee is charged against whatever the position is worth when it
    # closes -- so a position worth less than that fee returns **negative** cash
    # on exit. Phase 11.4 hit it: a EUR 100 portfolio ran 52 trades, paid EUR 104
    # in fees, and drove cash to -0.24, violating the ``cash >= 0`` check
    # constraint the schema declares. The engine reached a state its own database
    # forbids, and crashed rather than refusing.
    #
    # This is a practicality test, not reserved capital: no balance is held back
    # and no new capital concept exists. It only refuses to open a position whose
    # entire value is smaller than the cost of getting out of it, which is never
    # a trade worth making at any account size.
    round_trip = estimate_round_trip_cost(
        settings=profile.costs.to_cost_settings(), notional=notional
    )
    if notional <= round_trip:
        return SizingResult(
            quantity=ZERO,
            constraint=constraint,
            rejection=OrderRejectionReason.BELOW_MIN_NOTIONAL,
            detail=(
                f"position notional {notional:.2f} does not cover its own "
                f"{round_trip:.2f} round-trip cost"
            ),
            risk_budget=risk_budget,
            risk_per_share=risk_per_share,
        )

    if notional < profile.costs.min_order_notional:
        return SizingResult(
            quantity=ZERO,
            constraint=constraint,
            rejection=OrderRejectionReason.BELOW_MIN_NOTIONAL,
            detail=(
                f"notional {notional:.2f} is below the broker minimum "
                f"{profile.costs.min_order_notional}"
            ),
            risk_budget=risk_budget,
            risk_per_share=risk_per_share,
        )

    return SizingResult(
        quantity=quantity,
        constraint=constraint,
        risk_budget=risk_budget,
        risk_per_share=risk_per_share,
        detail=(
            f"{quantity} units at ~{entry_price} "
            f"(bound by {constraint.value if constraint else 'unknown'})"
        ),
    )
