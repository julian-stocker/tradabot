"""Transaction-cost data structures.

Everything here is :class:`~decimal.Decimal`. Cost accounting is exactly the place
where binary floating point is unacceptable: costs are small numbers subtracted
from other small numbers, which is the worst case for relative error.

Basis points (bps, 1/100th of a percent) are the internal unit for *rates*. They
compose additively -- a 6 bps spread plus 3 bps slippage plus 2 bps commission is
11 bps -- whereas percentages invite percent-of-percent mistakes.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

BPS = Decimal(10_000)

BPS_QUANTUM = Decimal("0.0001")
"""Presentation precision for bps rates. Sub-basis-point resolution already
exceeds what any of these estimates can justify."""


class CostBreakdown(BaseModel):
    """Itemised cost of one round trip (open + close), in account currency.

    Kept itemised rather than reduced to a single number because the items have
    very different natures: fees are contractual and known, spread is observable,
    and slippage is an *assumption*. Collapsing them hides which part of a cost
    estimate is actually measured.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    spread_cost: Decimal = Field(
        ge=0, description="Cost of crossing the bid/ask spread on both legs."
    )
    fee_cost: Decimal = Field(ge=0, description="Fixed plus variable broker fees across both legs.")
    slippage_cost: Decimal = Field(
        ge=0, description="Assumed adverse fill beyond the quoted touch, both legs."
    )

    @property
    def total(self) -> Decimal:
        return self.spread_cost + self.fee_cost + self.slippage_cost


class RoundTripCost(BaseModel):
    """Full cost accounting for a hypothetical round trip.

    Prices supplied by the caller are **mid prices**. Fill prices are derived by
    walking out from the mid by the half-spread and the slippage assumption, which
    is the only way to avoid double-counting the spread (a common error: taking an
    ask-price entry *and* then subtracting a full spread).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    quantity: Decimal = Field(gt=0)
    entry_mid: Decimal = Field(gt=0)
    exit_mid: Decimal = Field(gt=0)
    entry_fill: Decimal = Field(gt=0, description="Modelled effective entry price.")
    exit_fill: Decimal = Field(gt=0, description="Modelled effective exit price.")

    breakdown: CostBreakdown
    gross_pnl: Decimal = Field(description="P&L on mid prices, before any cost.")
    net_pnl: Decimal = Field(description="P&L after spread, fees and slippage.")

    @property
    def entry_notional(self) -> Decimal:
        return self.entry_mid * self.quantity

    @property
    def total_cost(self) -> Decimal:
        return self.breakdown.total

    @property
    def total_cost_bps(self) -> Decimal:
        """Round-trip cost as basis points of the entry notional."""
        notional = self.entry_notional
        if notional == 0:
            return Decimal(0)
        return self.total_cost / notional * BPS

    @property
    def breakeven_move_bps(self) -> Decimal:
        """Favourable mid-price move required just to break even.

        The single most useful number in this module. If a strategy's expected
        move is below its break-even move, it loses money *on average even when
        it is right about direction*.
        """
        return self.total_cost_bps


class NetEdge(BaseModel):
    """Expected edge before and after costs.

    Implements the distinction the project requires between *raw predicted
    movement* and *net expected edge*: a bullish view is only actionable when the
    move it predicts survives the cost of expressing it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_move_bps: Decimal = Field(
        description="Predicted favourable mid-price move, in bps. Raw, pre-cost."
    )
    cost_bps: Decimal = Field(ge=0, description="Modelled round-trip cost, in bps.")
    net_edge_bps: Decimal = Field(description="expected_move_bps - cost_bps.")

    @property
    def is_actionable(self) -> bool:
        """True when the expected move exceeds the cost of capturing it.

        Note the deliberate absence of a safety margin. A real trading rule should
        demand net edge well above zero to compensate for estimation error; that
        threshold is a policy decision for the caller, not a hidden constant here.
        """
        return self.net_edge_bps > 0

    @property
    def cost_coverage_ratio(self) -> float | None:
        """How many times the expected move covers the cost.

        ``None`` when cost is zero. 1.0 is exact break-even; below 1.0 the trade
        is expected to lose money even if the direction is right.
        """
        if self.cost_bps == 0:
            return None
        return float(self.expected_move_bps / self.cost_bps)
