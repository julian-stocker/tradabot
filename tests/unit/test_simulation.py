"""Simulation profiles, position sizing and trade decisions."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.costs.models import NetEdge
from app.domain.enums import Classification, DecisionReason, Horizon, Timeframe, TradeDecisionType
from app.domain.quotes import Quote
from app.signals.models import SignalResult
from app.simulation.decisions import cost_bps_for_notional, evaluate_decision
from app.simulation.defaults import (
    AGGRESSIVE,
    BALANCED,
    CONSERVATIVE,
    DEFAULT_CAPITAL_SIZES,
    DEFAULT_RISK_PROFILES,
    FLAT_FEE_BROKER,
    PERCENTAGE_BROKER,
    ZERO_COST_BROKER,
    build_default_profiles,
)
from app.simulation.models import BrokerCostConfig, RiskConfig, SimulationProfileConfig

NOW = datetime(2024, 6, 3, 12, 0, tzinfo=UTC)


def make_signal(
    *,
    score: float = 70.0,
    classification: Classification = Classification.STRONG_BULLISH,
    confidence: float = 0.8,
    price: str = "100",
    expected_move_bps: str = "150",
    spread_bps: str = "8",
) -> SignalResult:
    return SignalResult(
        symbol="TEST",
        timestamp=NOW,
        generated_at=NOW,
        timeframe=Timeframe.D1,
        horizon=Horizon.D5,
        score=score,
        classification=classification,
        confidence=confidence,
        components=(),
        feature_snapshot={"rsi_14": 60.0},
        reference_price=Decimal(price),
        spread_bps=Decimal(spread_bps),
        net_edge=NetEdge(
            expected_move_bps=Decimal(expected_move_bps),
            cost_bps=Decimal("19"),
            net_edge_bps=Decimal(expected_move_bps) - Decimal("19"),
        ),
        bars_used=200,
        engine_version="test-v1",
    )


def profile(
    capital: str,
    risk: RiskConfig = BALANCED,
    costs: BrokerCostConfig = FLAT_FEE_BROKER,
    *,
    name: str | None = None,
    enabled: bool = True,
) -> SimulationProfileConfig:
    return SimulationProfileConfig(
        id=1,
        name=name or f"{capital}-{risk.name}",
        initial_capital=Decimal(capital),
        currency="EUR",
        risk=risk,
        costs=costs,
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# Profile validation
# ---------------------------------------------------------------------------
class TestProfileValidation:
    def test_risk_per_trade_cannot_exceed_position_cap(self):
        """Risking 20% on a trade capped at 10% of equity is incoherent."""
        with pytest.raises(ValueError, match="exceeds max_position_percent"):
            RiskConfig(
                name="broken",
                risk_per_trade=Decimal("0.20"),
                max_position_percent=Decimal("0.10"),
                max_total_exposure=Decimal("1"),
                max_open_positions=5,
                max_daily_loss=Decimal("0.05"),
                max_drawdown=Decimal("0.2"),
                min_signal_score=Decimal("30"),
                min_confidence=Decimal("0.4"),
            )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("risk_per_trade", Decimal("0")),
            ("risk_per_trade", Decimal("1.5")),
            ("max_position_percent", Decimal("0")),
            ("max_daily_loss", Decimal("2")),
            ("max_drawdown", Decimal("0")),
            ("min_signal_score", Decimal("101")),
            ("min_confidence", Decimal("1.5")),
            ("max_open_positions", 0),
        ],
    )
    def test_out_of_range_values_rejected(self, field, value):
        base = {
            "name": "test",
            "risk_per_trade": Decimal("0.01"),
            "max_position_percent": Decimal("0.2"),
            "max_total_exposure": Decimal("1"),
            "max_open_positions": 5,
            "max_daily_loss": Decimal("0.05"),
            "max_drawdown": Decimal("0.2"),
            "min_signal_score": Decimal("30"),
            "min_confidence": Decimal("0.4"),
        }
        with pytest.raises(ValueError, match="validation error"):
            RiskConfig(**{**base, field: value})

    def test_zero_capital_rejected(self):
        with pytest.raises(ValueError, match="greater than 0"):
            profile("0")

    def test_cost_config_converts_to_cost_settings(self):
        """One cost model in the system; a profile is a parameterisation of it."""
        settings = FLAT_FEE_BROKER.to_cost_settings()
        assert settings.order_fee == FLAT_FEE_BROKER.order_fee
        assert settings.variable_fee_rate == FLAT_FEE_BROKER.variable_fee_rate
        assert settings.min_order_notional == FLAT_FEE_BROKER.min_order_notional


# ---------------------------------------------------------------------------
# Capital size is independent of risk configuration
# ---------------------------------------------------------------------------
class TestCapitalAndRiskAreIndependent:
    def test_one_risk_profile_serves_every_capital_size(self):
        """The normalisation requirement, stated as a test.

        Nine portfolios, three risk configurations -- not nine copies.
        """
        profiles = build_default_profiles()
        assert len(profiles) == len(DEFAULT_CAPITAL_SIZES) * len(DEFAULT_RISK_PROFILES)
        assert len({p.risk.name for p in profiles}) == len(DEFAULT_RISK_PROFILES)

    def test_the_same_risk_object_is_shared(self):
        """Identity, not just equality: editing one risk profile moves all of them."""
        balanced = [p for p in build_default_profiles() if p.risk.name == "balanced"]
        assert len(balanced) == len(DEFAULT_CAPITAL_SIZES)
        first = balanced[0].risk
        assert all(p.risk is first for p in balanced)

    def test_risk_fractions_are_capital_independent(self):
        """A fraction means the same thing at every size; the money does not."""
        small, large = profile("50", BALANCED), profile("5000", BALANCED)
        assert small.risk.risk_per_trade == large.risk.risk_per_trade
        assert small.risk_budget == Decimal("0.50")
        assert large.risk_budget == Decimal("50.00")

    def test_max_position_scales_with_capital(self):
        """balanced caps a position at 30% of equity."""
        assert profile("50", BALANCED).max_position_notional == Decimal("15.00")
        assert profile("500", BALANCED).max_position_notional == Decimal("150.00")
        assert profile("5000", BALANCED).max_position_notional == Decimal("1500.00")

    def test_one_capital_size_runs_every_risk_profile(self):
        """A 500 EUR portfolio may run conservative, balanced and aggressive."""
        at_500 = [p for p in build_default_profiles() if p.initial_capital == Decimal("500")]
        assert {p.risk.name for p in at_500} == {"conservative", "balanced", "aggressive"}

    def test_risk_appetite_orders_as_named(self):
        assert CONSERVATIVE.risk_per_trade < BALANCED.risk_per_trade < AGGRESSIVE.risk_per_trade
        assert CONSERVATIVE.min_signal_score > BALANCED.min_signal_score
        assert BALANCED.min_signal_score > AGGRESSIVE.min_signal_score


# ---------------------------------------------------------------------------
# Fixed fees bite differently by size
# ---------------------------------------------------------------------------
class TestFixedFeeImpactByPositionSize:
    def test_identical_fee_is_a_different_percentage(self):
        """The economic fact the whole multi-profile design exists to expose."""
        price, spread = Decimal("100"), Decimal("0")
        flat = profile("5000", costs=FLAT_FEE_BROKER)

        tiny = cost_bps_for_notional(
            notional=Decimal("50"), price=price, spread_bps=spread, profile=flat
        )
        large = cost_bps_for_notional(
            notional=Decimal("5000"), price=price, spread_bps=spread, profile=flat
        )
        # 2.00 EUR round trip on 50 EUR = 400 bps; on 5000 EUR = 4 bps.
        assert tiny == Decimal("400.0000")
        assert large == Decimal("4.0000")
        assert tiny == large * 100

    def test_percentage_fees_are_size_invariant(self):
        """The control: a purely proportional broker charges the same rate."""
        percentage_only = BrokerCostConfig(
            name="pct-only",
            order_fee=Decimal("0"),
            variable_fee_rate=Decimal("0.001"),
            slippage_spread_multiple=Decimal("0"),
            default_spread_bps=Decimal("0"),
            min_order_notional=Decimal("0"),
        )
        pct = profile("5000", costs=percentage_only)
        args = {"price": Decimal("100"), "spread_bps": Decimal("0"), "profile": pct}
        assert cost_bps_for_notional(notional=Decimal("50"), **args) == cost_bps_for_notional(
            notional=Decimal("5000"), **args
        )

    def test_spread_cost_is_size_invariant(self):
        """Spread is proportional; only the fixed fee creates size dependence."""
        zero_fee = profile("5000", costs=ZERO_COST_BROKER)
        args = {"price": Decimal("100"), "spread_bps": Decimal("20"), "profile": zero_fee}
        small = cost_bps_for_notional(notional=Decimal("50"), **args)
        large = cost_bps_for_notional(notional=Decimal("5000"), **args)
        assert small == large == Decimal("20.0000")

    def test_cost_decreases_monotonically_with_size(self):
        flat = profile("5000", costs=FLAT_FEE_BROKER)
        costs = [
            cost_bps_for_notional(
                notional=Decimal(n), price=Decimal("100"), spread_bps=Decimal("10"), profile=flat
            )
            for n in ("25", "50", "250", "1000", "10000")
        ]
        assert costs == sorted(costs, reverse=True)

    def test_cost_converges_to_the_spread_for_large_positions(self):
        """As the fixed fee amortises, only proportional costs remain."""
        flat = profile("5000", costs=FLAT_FEE_BROKER)
        huge = cost_bps_for_notional(
            notional=Decimal("10000000"),
            price=Decimal("100"),
            spread_bps=Decimal("10"),
            profile=flat,
        )
        # 10 bps spread + 5 bps slippage (0.5 x half-spread), fee negligible.
        assert math.isclose(float(huge), 15.0, abs_tol=0.01)


# ---------------------------------------------------------------------------
# Decision gates
# ---------------------------------------------------------------------------
class TestDecisionGates:
    def test_accepted_signal_produces_a_trade(self):
        decision = evaluate_decision(
            signal=make_signal(), profile=profile("5000", AGGRESSIVE), now=NOW
        )
        assert decision.decision is TradeDecisionType.TRADE
        assert decision.reason is DecisionReason.ACCEPTED
        assert decision.position_quantity > 0
        assert decision.side is not None

    def test_neutral_signal_is_skipped(self):
        decision = evaluate_decision(
            signal=make_signal(score=5.0, classification=Classification.NEUTRAL),
            profile=profile("5000", AGGRESSIVE),
            now=NOW,
        )
        assert decision.reason is DecisionReason.CLASSIFICATION_NEUTRAL
        assert decision.position_quantity == 0

    def test_low_score_is_skipped(self):
        decision = evaluate_decision(
            signal=make_signal(score=25.0, classification=Classification.BULLISH),
            profile=profile("5000", CONSERVATIVE),
            now=NOW,
        )
        assert decision.reason is DecisionReason.SCORE_BELOW_THRESHOLD

    def test_low_confidence_is_skipped(self):
        """Score must clear its gate first, or the score gate fires instead."""
        decision = evaluate_decision(
            signal=make_signal(score=90.0, confidence=0.10),
            profile=profile("5000", CONSERVATIVE),
            now=NOW,
        )
        assert decision.reason is DecisionReason.CONFIDENCE_BELOW_THRESHOLD

    def test_bearish_signal_skipped_when_shorts_disallowed(self):
        decision = evaluate_decision(
            signal=make_signal(score=-70.0, classification=Classification.STRONG_BEARISH),
            profile=profile("5000", BALANCED),
            now=NOW,
        )
        assert decision.reason is DecisionReason.SHORT_NOT_PERMITTED

    def test_bearish_signal_traded_when_shorts_allowed(self):
        shorting = RiskConfig(**{**AGGRESSIVE.model_dump(exclude={"id"}), "allow_short": True})
        decision = evaluate_decision(
            signal=make_signal(score=-70.0, classification=Classification.STRONG_BEARISH),
            profile=profile("5000", shorting),
            now=NOW,
        )
        assert decision.decision is TradeDecisionType.TRADE
        assert decision.side is not None
        assert decision.side.value == "SHORT"

    def test_disabled_profile_is_skipped(self):
        decision = evaluate_decision(
            signal=make_signal(), profile=profile("5000", AGGRESSIVE, enabled=False), now=NOW
        )
        assert decision.reason is DecisionReason.PROFILE_DISABLED

    def test_position_below_broker_minimum_is_skipped(self):
        """25 EUR minimum against a 50 EUR portfolio's 10 EUR position."""
        decision = evaluate_decision(
            signal=make_signal(),
            profile=profile("50", BALANCED, costs=PERCENTAGE_BROKER),
            now=NOW,
        )
        assert decision.reason is DecisionReason.POSITION_BELOW_MIN_NOTIONAL

    def test_negative_net_edge_is_skipped_with_the_numbers_recorded(self):
        decision = evaluate_decision(
            signal=make_signal(expected_move_bps="30"),
            profile=profile("50", AGGRESSIVE),
            now=NOW,
        )
        assert decision.reason is DecisionReason.NEGATIVE_NET_EDGE
        assert decision.net_edge_bps_at_size < 0
        assert "bps" in decision.reason_detail

    def test_net_edge_gate_can_be_disabled(self):
        """Configurable so the counterfactual value of taking them is measurable."""
        permissive = RiskConfig(
            **{**AGGRESSIVE.model_dump(exclude={"id"}), "require_positive_net_edge": False}
        )
        decision = evaluate_decision(
            signal=make_signal(expected_move_bps="30"),
            profile=profile("50", permissive),
            now=NOW,
        )
        assert decision.decision is TradeDecisionType.TRADE
        assert decision.net_edge_bps_at_size < 0, "recorded honestly even though taken"

    def test_zero_capital_is_skipped(self):
        decision = evaluate_decision(
            signal=make_signal(),
            profile=profile("5000", AGGRESSIVE),
            available_capital=Decimal("0"),
            now=NOW,
        )
        assert decision.reason is DecisionReason.INSUFFICIENT_CAPITAL

    def test_conviction_gates_run_before_economic_ones(self):
        """A weak signal is rejected on conviction regardless of size.

        Gate ordering is behaviour, not an implementation detail: it decides which
        reason gets recorded, and therefore what the decision log can tell you.
        """
        weak = make_signal(score=5.0, classification=Classification.BULLISH)
        for capital in ("50", "5000"):
            decision = evaluate_decision(signal=weak, profile=profile(capital, BALANCED), now=NOW)
            assert decision.reason is DecisionReason.SCORE_BELOW_THRESHOLD


# ---------------------------------------------------------------------------
# The fan-out
# ---------------------------------------------------------------------------
class TestSameSignalAcrossProfiles:
    def test_identical_signal_yields_different_verdicts(self):
        """The core claim of multi-profile simulation."""
        signal = make_signal(expected_move_bps="120")
        decisions = {
            p.name: evaluate_decision(signal=signal, profile=p, now=NOW)
            for p in build_default_profiles()
        }
        verdicts = {d.decision for d in decisions.values()}
        assert verdicts == {TradeDecisionType.TRADE, TradeDecisionType.SKIP}, (
            "the same signal should divide the profiles"
        )

    def test_small_portfolios_reject_what_large_ones_accept(self):
        signal = make_signal(expected_move_bps="120")
        profiles = {p.name: p for p in build_default_profiles()}

        small = evaluate_decision(signal=signal, profile=profiles["50eur-balanced"], now=NOW)
        large = evaluate_decision(signal=signal, profile=profiles["5000eur-balanced"], now=NOW)

        assert small.decision is TradeDecisionType.SKIP
        assert small.reason is DecisionReason.NEGATIVE_NET_EDGE
        assert large.decision is TradeDecisionType.TRADE
        assert small.cost_bps_at_size > large.cost_bps_at_size * 10

    def test_cost_bps_falls_monotonically_with_capital(self):
        signal = make_signal()
        costs = [
            evaluate_decision(
                signal=signal, profile=profile(str(c), BALANCED), now=NOW
            ).cost_bps_at_size
            for c in ("50", "500", "5000")
        ]
        assert costs == sorted(costs, reverse=True)

    def test_every_profile_records_a_decision(self):
        """Skips are recorded as deliberately as trades."""
        signal = make_signal(expected_move_bps="120")
        decisions = [
            evaluate_decision(signal=signal, profile=p, now=NOW) for p in build_default_profiles()
        ]
        assert len(decisions) == 9
        assert all(d.reason_detail for d in decisions), "every decision must explain itself"

    def test_decision_is_deterministic(self):
        signal, prof = make_signal(), profile("5000", BALANCED)
        first = evaluate_decision(signal=signal, profile=prof, now=NOW)
        second = evaluate_decision(signal=signal, profile=prof, now=NOW)
        assert first == second


class TestDecisionRecord:
    def test_records_the_economics_behind_the_verdict(self):
        quote = Quote(
            symbol="TEST",
            timestamp=NOW,
            bid=Decimal("99.96"),
            ask=Decimal("100.04"),
        )
        decision = evaluate_decision(
            signal=make_signal(), profile=profile("5000", BALANCED), quote=quote, now=NOW
        )
        assert decision.bid == Decimal("99.96")
        assert decision.ask == Decimal("100.04")
        assert math.isclose(float(decision.spread_bps), 8.0, abs_tol=0.01)
        assert decision.estimated_fees > 0
        assert decision.estimated_spread_cost > 0
        assert decision.position_notional > 0
        assert decision.available_capital == Decimal("5000")

    def test_cost_components_sum_to_the_total(self):
        decision = evaluate_decision(
            signal=make_signal(), profile=profile("5000", BALANCED), now=NOW
        )
        total = (
            decision.estimated_fees + decision.estimated_spread_cost + decision.estimated_slippage
        )
        assert total == decision.estimated_total_cost

    def test_live_quote_overrides_the_signal_spread(self):
        wide = Quote(symbol="TEST", timestamp=NOW, bid=Decimal("99"), ask=Decimal("101"))
        decision = evaluate_decision(
            signal=make_signal(spread_bps="2"),
            profile=profile("5000", BALANCED),
            quote=wide,
            now=NOW,
        )
        assert math.isclose(float(decision.spread_bps), 200.0, abs_tol=0.1)

    def test_falls_back_to_the_signal_spread_without_a_quote(self):
        decision = evaluate_decision(
            signal=make_signal(spread_bps="12"), profile=profile("5000", BALANCED), now=NOW
        )
        assert decision.spread_bps == Decimal("12")
        assert decision.bid is None
        assert decision.ask is None
