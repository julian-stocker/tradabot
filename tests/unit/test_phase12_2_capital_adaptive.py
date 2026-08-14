"""Capital-adaptive execution: the economics must do the work, not a rule table.

The hypothesis under test is that a small account trades less *because trades
stop being economic*, not because someone wrote "€1,000 = 3 trades/month". These
tests pin that: the cost share is a parameter on the one existing gate rather
than a second engine, capital never reaches the opportunity rule, and the
contribution ledger cannot report a deposit as a profit.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.config import CostSettings
from app.market_data.risk import assess
from app.market_data.volatility import ExpectedMovement, VolatilityRegime
from app.paper import risk_gate
from app.paper.execution import estimate_round_trip_cost
from app.paper.risk_gate import (
    MAX_COST_SHARE_OF_RISK,
    MIN_PRACTICAL_NOTIONAL,
    RiskDecision,
    RiskRejectionReason,
    evaluate_entry,
)

NOW = datetime(2026, 8, 15, 20, 0, tzinfo=UTC)
CANONICAL = CostSettings(order_fee=Decimal("1.00"), default_spread_bps=Decimal("10"))


def risk(atr_pct: float = 2.0):
    return assess(
        ExpectedMovement(
            symbol="TEST",
            calculated_at=NOW - timedelta(minutes=5),
            bar_timestamp=NOW - timedelta(minutes=5),
            regime=VolatilityRegime.NORMAL,
            percentile=0.5,
            atr_pct=atr_pct,
            recent_range_pct=4.0,
        ),
        now=NOW,
    )


def gate(capital: str, *, cost_share: Decimal = MAX_COST_SHARE_OF_RISK, risk_pct: str = "0.01"):
    """Evaluate one entry for an account of ``capital``, at the canonical cost."""
    equity = Decimal(capital)
    return evaluate_entry(
        risk=risk(),
        entry_price=Decimal("100"),
        structural_stop=Decimal("96"),
        risk_budget=equity * Decimal(risk_pct),
        costs=CANONICAL,
        max_cost_share=cost_share,
    )


# ---------------------------------------------------------------------------
# C: the fixed fee is what breaks small accounts
# ---------------------------------------------------------------------------
class TestExecutionEconomics:
    def test_round_trip_cost_is_dominated_by_the_flat_fee_at_small_notionals(self) -> None:
        """**The gate.** EUR 2.00 of fixed fee is 87% of the cost of a EUR 200
        trade and 21% of a EUR 5,000 one. That ratio is the whole phase."""
        small = estimate_round_trip_cost(settings=CANONICAL, notional=Decimal("200"))
        large = estimate_round_trip_cost(settings=CANONICAL, notional=Decimal("5000"))
        assert Decimal("2.00") / small > Decimal("0.85")
        assert Decimal("2.00") / large < Decimal("0.25")

    def test_cost_percentage_falls_monotonically_with_notional(self) -> None:
        previous = None
        for notional in (25, 50, 100, 200, 500, 1000, 2000, 5000):
            value = Decimal(notional)
            share = estimate_round_trip_cost(settings=CANONICAL, notional=value) / value
            if previous is not None:
                assert share < previous
            previous = share

    def test_removing_the_flat_fee_collapses_the_small_account_penalty(self) -> None:
        """Sensitivity, not a proposal to change the production model."""
        free = CostSettings(order_fee=Decimal("0"), default_spread_bps=Decimal("10"))
        value = Decimal("200")
        assert estimate_round_trip_cost(settings=free, notional=value) / value < Decimal("0.002")


# ---------------------------------------------------------------------------
# D: capital reaches execution feasibility, never the opportunity rule
# ---------------------------------------------------------------------------
class TestCapitalAdaptiveGate:
    def test_a_small_account_is_refused_where_a_large_one_is_permitted(self) -> None:
        """The behaviour the phase is named for, and it emerges from arithmetic."""
        assert not gate("1000", cost_share=Decimal("0.15")).permits_entry
        assert gate("10000", cost_share=Decimal("0.15")).permits_entry

    def test_the_refusal_reason_is_economic_not_directional(self) -> None:
        decision = gate("1000", cost_share=Decimal("0.15"))
        assert decision.reason is RiskRejectionReason.COST_EXCEEDS_RISK_SHARE
        assert decision.decision is RiskDecision.REJECTED

    def test_permission_is_monotone_in_capital(self) -> None:
        """More capital may open doors; it must never close them."""
        permitted = [
            gate(str(c), cost_share=Decimal("0.20")).permits_entry
            for c in (100, 500, 1000, 2000, 5000, 10000)
        ]
        assert permitted == sorted(permitted, key=bool)

    def test_a_hundred_euro_account_is_refused_at_every_registered_threshold(self) -> None:
        for share in ("0.10", "0.15", "0.20", "0.25", "0.35"):
            assert not gate("100", cost_share=Decimal(share)).permits_entry

    def test_capital_never_enters_the_opportunity_rule(self) -> None:
        """**The gate.** Capital may decide whether a trade is executable. It may
        not decide whether an opportunity exists."""
        from app.research import phase12_1

        source = inspect.getsource(phase12_1.match_b_mask)
        for forbidden in ("capital", "equity", "cash", "budget", "notional"):
            assert forbidden not in source.lower()

    def test_the_cost_share_is_a_parameter_on_the_one_gate(self) -> None:
        """Not a second gate, and not a second cost model."""
        params = inspect.signature(evaluate_entry).parameters
        assert "max_cost_share" in params
        assert params["max_cost_share"].default == MAX_COST_SHARE_OF_RISK
        source = inspect.getsource(risk_gate)
        assert source.count("def evaluate_entry") == 1
        assert "estimate_round_trip_cost" in source

    def test_the_practical_minimum_still_refuses_tiny_positions(self) -> None:
        """Independent of cost share: a position too small to matter is refused."""
        decision = gate("100", risk_pct="0.0025")
        assert decision.reason is RiskRejectionReason.IMPRACTICAL_SIZE
        assert Decimal("20") == MIN_PRACTICAL_NOTIONAL


# ---------------------------------------------------------------------------
# The Phase 12.1 defect this phase exists to correct
# ---------------------------------------------------------------------------
def test_the_gate_is_inert_when_the_risk_layer_is_disabled() -> None:
    """**The gate.** Phase 12.1 concluded EUR 1,000 was unviable with
    ``risk_layer_enabled=False``, so neither the practical minimum nor the cost
    backstop ever ran. Pinned so that a future economic claim cannot be made
    again with the economic safeguards switched off."""
    from app.paper.engine import PaperTradingEngine

    source = inspect.getsource(PaperTradingEngine.open_from_decision)
    assert "if self._risk_layer_enabled:" in source
    assert (
        inspect.signature(PaperTradingEngine.__init__).parameters["risk_layer_enabled"].default
        is False
    )


# ---------------------------------------------------------------------------
# L/M/N: contributions are capital, never performance
# ---------------------------------------------------------------------------
class TestContributionValueAttribution:
    def ledger(self):
        from app.research.phase12_1 import WealthLedger

        return WealthLedger(initial_capital=Decimal("1000"))

    def test_value_add_is_measured_against_the_no_trading_control(self) -> None:
        """**The gate.** Ending equity above the start proves nothing when
        EUR 200 a month is arriving regardless."""
        traded = self.ledger()
        control = self.ledger()
        for _ in range(24):
            traded.contribute(Decimal("200"))
            control.contribute(Decimal("200"))
        traded.record_trading(Decimal("-500"))
        assert traded.total_equity > traded.initial_capital  # looks like growth
        assert traded.total_equity - control.total_equity == Decimal("-500")  # is not

    def test_trading_pnl_is_the_only_component_tradabot_created(self) -> None:
        ledger = self.ledger()
        for _ in range(10):
            ledger.contribute(Decimal("200"))
        ledger.record_trading(Decimal("75"))
        assert ledger.invested == Decimal("3000")
        assert ledger.total_equity == Decimal("3075")
        assert ledger.total_equity - ledger.invested == Decimal("75")

    @pytest.mark.parametrize("monthly", ["100", "200", "300"])
    def test_a_larger_contribution_never_changes_trading_pnl(self, monthly: str) -> None:
        """Otherwise a bigger deposit would read as a better strategy."""
        ledger = self.ledger()
        for _ in range(12):
            ledger.contribute(Decimal(monthly))
        ledger.record_trading(Decimal("-120"))
        assert ledger.trading_pnl == Decimal("-120")


# ---------------------------------------------------------------------------
# P: the reserve stays inactive
# ---------------------------------------------------------------------------
def test_reserve_capital_remains_unimplemented() -> None:
    from app.db.models import VirtualPortfolio

    columns = set(VirtualPortfolio.__table__.columns.keys())
    for forbidden in ("locked_reserve", "reserve_payable", "active_capital"):
        assert forbidden not in columns


# ---------------------------------------------------------------------------
# A: malformed provider records are skipped, not fatal
# ---------------------------------------------------------------------------
def test_one_malformed_corporate_action_cannot_abort_a_universe_fetch() -> None:
    """A dividend paying before its ex-date killed a 1,000-instrument fetch."""
    from app.market_data.providers import alpaca

    source = inspect.getsource(alpaca.normalise_corporate_actions)
    assert "except ValidationError" in source
    assert "rejected" in source
