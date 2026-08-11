"""Position sizing: risk-based sizing and the caps that reduce it."""

from __future__ import annotations

import math
from decimal import Decimal

from app.domain.enums import OrderRejectionReason
from app.paper.sizing import SizingConstraint, size_position
from app.simulation.defaults import BALANCED, FLAT_FEE_BROKER, PERCENTAGE_BROKER
from app.simulation.models import BrokerCostConfig, RiskConfig, SimulationProfileConfig


def profile(
    capital: str = "5000",
    risk: RiskConfig = BALANCED,
    costs: BrokerCostConfig = FLAT_FEE_BROKER,
) -> SimulationProfileConfig:
    return SimulationProfileConfig(
        id=1,
        name=f"{capital}-{risk.name}",
        initial_capital=Decimal(capital),
        currency="EUR",
        risk=risk,
        costs=costs,
    )


def size(
    *,
    capital: str = "5000",
    equity: str | None = None,
    cash: str | None = None,
    exposure: str = "0",
    price: str = "100",
    stop: str | None = "96",
    risk: RiskConfig = BALANCED,
    costs: BrokerCostConfig = FLAT_FEE_BROKER,
):
    equity_value = Decimal(equity or capital)
    return size_position(
        profile=profile(capital, risk, costs),
        equity=equity_value,
        available_cash=Decimal(cash or capital),
        current_exposure=Decimal(exposure),
        entry_price=Decimal(price),
        stop_loss=Decimal(stop) if stop is not None else None,
    )


class TestRiskBasedSizing:
    def test_quantity_follows_the_risk_formula(self):
        """risk_budget / risk_per_share, exactly."""
        result = size(capital="5000", price="100", stop="96")
        # 5000 x 1% = 50 risk budget; 4 per share -> 12.5 units
        assert result.quantity == Decimal("12.50000000")
        assert result.constraint is SizingConstraint.RISK_BUDGET
        assert result.risk_budget == Decimal("50.00")
        assert result.risk_per_share == Decimal("4")

    def test_a_wider_stop_buys_fewer_shares(self):
        """The whole point of risk-based sizing.

        Both stops are wide enough that the risk budget binds rather than the
        position cap, so the currency at risk is identical in each case.
        """
        tight = size(price="100", stop="90")
        wide = size(price="100", stop="80")
        assert tight.constraint is SizingConstraint.RISK_BUDGET
        assert wide.constraint is SizingConstraint.RISK_BUDGET
        assert tight.quantity > wide.quantity
        assert math.isclose(
            float(tight.quantity * Decimal(10)), float(wide.quantity * Decimal(20)), rel_tol=1e-6
        )

    def test_risk_scales_with_equity_not_initial_capital(self):
        """A drawdown automatically shrinks position sizes."""
        full = size(capital="5000", equity="5000")
        drawn_down = size(capital="5000", equity="2500")
        assert drawn_down.quantity < full.quantity

    def test_risk_per_trade_setting_changes_the_size(self):
        conservative = RiskConfig(
            **{
                **BALANCED.model_dump(exclude={"id"}),
                "name": "c",
                "risk_per_trade": Decimal("0.005"),
            }
        )
        assert size(risk=conservative).quantity < size(risk=BALANCED).quantity


class TestCaps:
    def test_position_cap_binds_before_risk_budget(self):
        """A very tight stop would otherwise buy an enormous position."""
        result = size(capital="5000", price="100", stop="99.99")
        assert result.constraint is SizingConstraint.MAX_POSITION_PERCENT
        # balanced max_position_percent is 30% -> 1500 / 100 = 15 units
        assert result.quantity == Decimal("15.00000000")

    def test_available_cash_caps_the_position(self):
        result = size(capital="5000", equity="5000", cash="200", price="100", stop="99.9")
        assert result.constraint is SizingConstraint.AVAILABLE_CASH
        assert result.quantity * Decimal(100) <= Decimal(200)

    def test_cash_cap_leaves_room_for_the_fee(self):
        """Ignoring the fee is how a portfolio ends up with negative cash."""
        result = size(capital="5000", equity="5000", cash="101", price="100", stop="99.9")
        assert result.is_tradable
        cost = result.quantity * Decimal(100) + FLAT_FEE_BROKER.order_fee
        assert cost <= Decimal(101)

    def test_exposure_cap_binds(self):
        result = size(capital="5000", exposure="4900", price="100", stop="99.9")
        assert result.constraint is SizingConstraint.MAX_TOTAL_EXPOSURE

    def test_exposure_already_at_the_limit_is_rejected(self):
        result = size(capital="5000", exposure="5000", price="100", stop="96")
        assert result.rejection is OrderRejectionReason.MAX_EXPOSURE

    def test_caps_only_ever_reduce(self):
        unconstrained = size(capital="5000", cash="5000", exposure="0")
        constrained = size(capital="5000", cash="300", exposure="4000")
        assert constrained.quantity <= unconstrained.quantity


class TestRejections:
    def test_no_stop_is_refused_by_default(self):
        """tradabot will not invent a stop distance."""
        result = size(stop=None)
        assert result.rejection is OrderRejectionReason.INVALID_STOP
        assert "not invent" in result.detail
        assert result.quantity == 0

    def test_stop_above_entry_is_refused(self):
        """A long stop above the entry is not a stop."""
        assert size(price="100", stop="105").rejection is OrderRejectionReason.INVALID_STOP

    def test_explicit_fallback_sizes_by_notional(self):
        """Configurable, documented -- not a guess dressed up as risk management."""
        permissive = RiskConfig(
            **{**BALANCED.model_dump(exclude={"id"}), "name": "p", "require_stop_loss": False}
        )
        result = size(stop=None, risk=permissive)
        assert result.is_tradable
        assert result.constraint is SizingConstraint.NOTIONAL_FALLBACK
        assert result.quantity == Decimal("15.00000000")

    def test_cash_below_the_fee_is_rejected(self):
        result = size(capital="5000", equity="5000", cash="0.50", price="100", stop="96")
        assert result.rejection is OrderRejectionReason.INSUFFICIENT_CASH

    def test_zero_equity_is_rejected(self):
        assert size(equity="0").rejection is OrderRejectionReason.INSUFFICIENT_CASH

    def test_below_broker_minimum_is_rejected(self):
        """A 50 EUR portfolio against a 25 EUR minimum order."""
        result = size(capital="50", price="100", stop="96", costs=PERCENTAGE_BROKER)
        assert result.rejection is OrderRejectionReason.BELOW_MIN_NOTIONAL

    def test_quantity_rounding_to_zero_is_rejected(self):
        """A price so large that the sized quantity underflows 8 decimal places."""
        result = size(
            capital="50", equity="50", cash="50", price="1000000000000", stop="999999999999"
        )
        assert result.rejection is OrderRejectionReason.QUANTITY_TOO_SMALL

    def test_non_positive_price_is_rejected(self):
        """A rejection, not an exception -- sizing failures are outcomes."""
        result = size(price="0")
        assert result.rejection is OrderRejectionReason.QUANTITY_TOO_SMALL
        assert result.quantity == 0


class TestCapitalSizeEffects:
    def test_larger_capital_buys_proportionally_more(self):
        small = size(capital="500", price="100", stop="96")
        large = size(capital="5000", price="100", stop="96")
        assert math.isclose(float(large.quantity / small.quantity), 10.0, rel_tol=1e-6)

    def test_the_binding_constraint_differs_by_portfolio_size(self):
        """A useful diagnostic: the small portfolio is running a different strategy.

        Its size is decided by what it can afford; the large one's by what it
        chooses to risk.
        """
        # The small portfolio has spent most of its cash; the large one has not.
        tiny = size(capital="50", equity="50", cash="5", price="100", stop="99.5")
        large = size(capital="5000", equity="5000", cash="5000", price="100", stop="99.5")
        assert tiny.constraint is SizingConstraint.AVAILABLE_CASH
        assert large.constraint is SizingConstraint.MAX_POSITION_PERCENT
