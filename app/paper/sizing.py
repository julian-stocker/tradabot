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
from decimal import Decimal
from enum import StrEnum

from app.domain.enums import OrderRejectionReason
from app.simulation.models import SimulationProfileConfig

QUANTITY_EXPONENT = Decimal("0.00000001")
ZERO = Decimal(0)


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


def size_position(  # noqa: PLR0911 -- one return per rejection cause; each is a distinct outcome
    *,
    profile: SimulationProfileConfig,
    equity: Decimal,
    available_cash: Decimal,
    current_exposure: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal | None,
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

    quantity = quantity.quantize(QUANTITY_EXPONENT)

    if quantity <= 0:
        return SizingResult(
            quantity=ZERO,
            constraint=constraint,
            rejection=OrderRejectionReason.QUANTITY_TOO_SMALL,
            detail=(
                f"sized quantity rounds to zero at {entry_price} per unit "
                f"(bound by {constraint.value if constraint else 'unknown'})"
            ),
            risk_budget=risk_budget,
            risk_per_share=risk_per_share,
        )

    notional = quantity * entry_price
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
