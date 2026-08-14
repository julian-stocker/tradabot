"""The risk layer constrains entries. It must never select them.

The danger in bolting a risk model onto a trading engine is that the model
starts making trading decisions — refusing a candidate because it "looks bad"
rather than because the arithmetic does not work. These tests pin the boundary:
every rejection reason must be about budget, practicality, cost or data, and the
gate must be incapable of expressing a directional opinion.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.config import CostSettings
from app.market_data.risk import assess
from app.market_data.volatility import MAX_BAR_AGE, ExpectedMovement, VolatilityRegime
from app.paper import risk_gate
from app.paper.risk_gate import (
    MATERIAL_RISK_CHANGE,
    MAX_COST_SHARE_OF_RISK,
    MIN_PRACTICAL_NOTIONAL,
    RiskDecision,
    RiskFlag,
    RiskGateDecision,
    RiskRejectionReason,
    evaluate_entry,
    flag_position,
)

NOW = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)
PRICE = Decimal("100")

CHEAP = CostSettings(
    order_fee=Decimal("0"),
    variable_fee_rate=Decimal("0"),
    slippage_spread_multiple=Decimal("0.5"),
    default_spread_bps=Decimal("5"),
)
"""A zero-fee, tight-spread profile: costs exist but never bind."""

RETAIL = CostSettings(order_fee=Decimal("1.00"))
"""The configured default -- a flat EUR 1.00 per order, so EUR 2.00 round trip."""


def risk(
    *,
    regime: VolatilityRegime = VolatilityRegime.NORMAL,
    atr_pct: float = 1.0,
    bar_age: timedelta = timedelta(minutes=5),
):
    return assess(
        ExpectedMovement(
            symbol="TEST",
            calculated_at=NOW - timedelta(minutes=5),
            bar_timestamp=NOW - bar_age,
            regime=regime,
            percentile=0.5,
            atr_pct=atr_pct,
            recent_range_pct=2.0,
        ),
        now=NOW,
    )


def gate(**overrides) -> RiskGateDecision:
    kwargs = {
        "risk": risk(),
        "entry_price": PRICE,
        "structural_stop": Decimal("96"),
        "risk_budget": Decimal("50"),
        "costs": CHEAP,
    }
    kwargs.update(overrides)
    return evaluate_entry(**kwargs)


# ---------------------------------------------------------------------------
# B: the boundary is structural
# ---------------------------------------------------------------------------
def test_the_gate_carries_no_directional_field() -> None:
    """**The gate.** There is nowhere to put an opinion about the candidate."""
    fields = set(RiskGateDecision.__dataclass_fields__)
    for forbidden in (
        "direction",
        "score",
        "attractive",
        "target",
        "expected_return",
        "bullish",
        "bearish",
        "confidence",
    ):
        assert forbidden not in fields


def test_every_rejection_reason_is_arithmetic_or_data_quality() -> None:
    """No reason may mean 'the risk model dislikes this trade'."""
    allowed = {
        "IMPRACTICAL_SIZE",
        "COST_EXCEEDS_RISK_SHARE",
        "STALE_RISK_DATA",
        "NO_RISK_ESTIMATE",
        "ALLOCATION_CAP",
        "RISK_BUDGET_EXHAUSTED",
    }
    assert {r.value for r in RiskRejectionReason} == allowed


def test_the_gate_returns_a_distance_never_a_quantity() -> None:
    """Sizing owns quantity; two modules owning it is how they diverge."""
    fields = set(RiskGateDecision.__dataclass_fields__)
    for forbidden in ("quantity", "shares", "size", "notional"):
        assert forbidden not in fields


def test_the_module_contains_no_order_path() -> None:
    source = inspect.getsource(risk_gate).lower()
    for forbidden in ("submit_order", "tradingclient", "def buy", "def sell"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# C: the four outcomes
# ---------------------------------------------------------------------------
def test_a_normal_candidate_is_acceptable() -> None:
    decision = gate()
    assert decision.decision is RiskDecision.ACCEPTABLE
    assert decision.permits_entry
    assert decision.reason is None


def test_a_missing_estimate_is_unavailable_not_rejected() -> None:
    """The trade was never judged, which reads differently from failing."""
    decision = gate(risk=None)
    assert decision.decision is RiskDecision.UNAVAILABLE
    assert decision.reason is RiskRejectionReason.NO_RISK_ESTIMATE
    assert not decision.permits_entry


def test_stale_risk_data_rejects_by_default() -> None:
    """A number computed from bars that stopped arriving is not a risk number."""
    decision = gate(risk=risk(bar_age=MAX_BAR_AGE + timedelta(minutes=1)))
    assert decision.decision is RiskDecision.REJECTED
    assert decision.reason is RiskRejectionReason.STALE_RISK_DATA


def test_stale_data_can_be_explicitly_allowed() -> None:
    decision = gate(risk=risk(bar_age=MAX_BAR_AGE + timedelta(minutes=1)), allow_stale=True)
    assert decision.permits_entry


def test_an_exhausted_budget_rejects() -> None:
    assert gate(risk_budget=Decimal(0)).reason is RiskRejectionReason.RISK_BUDGET_EXHAUSTED


# ---------------------------------------------------------------------------
# G: the noise floor widens, and never tightens
# ---------------------------------------------------------------------------
def test_a_stop_inside_the_noise_floor_is_widened() -> None:
    """**The gate.** A sub-noise stop is a coin flip, not a risk control."""
    decision = gate(structural_stop=Decimal("99.8"))
    assert decision.tighter_than_noise
    assert decision.decision is RiskDecision.LIMITED
    assert decision.risk_distance == decision.noise_floor
    assert decision.risk_distance > Decimal("0.2")


def test_a_wider_structural_stop_is_kept() -> None:
    """The floor is a minimum, not a target -- it must not pull a stop in."""
    decision = gate(structural_stop=Decimal("80"))
    assert not decision.tighter_than_noise
    assert decision.risk_distance == Decimal("20")
    assert decision.decision is RiskDecision.ACCEPTABLE


def test_the_distance_is_never_tightened_to_make_a_trade_fit() -> None:
    """Sizing must not drive risk. A tight budget rejects; it does not narrow."""
    wide = gate(structural_stop=Decimal("80"), risk_budget=Decimal("1"))
    assert wide.risk_distance == Decimal("20")
    assert wide.reason is RiskRejectionReason.IMPRACTICAL_SIZE


def test_a_missing_structural_stop_falls_back_to_the_floor() -> None:
    decision = gate(structural_stop=None)
    assert decision.risk_distance == decision.noise_floor
    assert decision.structural_distance is None


def test_the_floor_scales_with_volatility() -> None:
    calm = gate(risk=risk(atr_pct=0.5), structural_stop=None)
    wild = gate(risk=risk(atr_pct=2.0), structural_stop=None)
    assert wild.noise_floor is not None
    assert calm.noise_floor is not None
    assert wild.noise_floor > calm.noise_floor * 3


# ---------------------------------------------------------------------------
# E: practicality, and the €100 case
# ---------------------------------------------------------------------------
def test_an_impractical_position_is_rejected_with_its_reason() -> None:
    """paper-100 at 0.25% sizes to about €10. It is refused, not rounded up."""
    decision = gate(risk_budget=Decimal("0.25"), structural_stop=Decimal("96"))
    assert decision.decision is RiskDecision.REJECTED
    assert decision.reason is RiskRejectionReason.IMPRACTICAL_SIZE
    assert "practical minimum" in decision.detail


def test_a_larger_budget_on_the_same_account_becomes_practical() -> None:
    """Above the practical minimum the size objection goes away."""
    assert gate(risk_budget=Decimal("1.00")).permits_entry


def test_the_practical_minimum_matches_the_research_finding() -> None:
    assert Decimal("20") == MIN_PRACTICAL_NOTIONAL


def test_excessive_cost_share_rejects() -> None:
    """A flat fee against a tiny budget is the pathological case the cap exists for.

    A EUR 1.00 round-trip-doubled order fee is EUR 2.00. Against a EUR 1.00 risk
    budget that is 200% -- the trade cannot pay for itself, whatever it does.
    """
    decision = gate(risk_budget=Decimal("1.00"), costs=RETAIL)
    assert decision.reason is RiskRejectionReason.COST_EXCEEDS_RISK_SHARE


def test_ordinary_cost_does_not_bind() -> None:
    """Measured cost share is ~8%; the 35% backstop must not fire routinely."""
    assert Decimal("0.10") < MAX_COST_SHARE_OF_RISK
    assert gate(costs=RETAIL).permits_entry


def test_the_backstop_can_be_disabled_for_measurement() -> None:
    """So a replay can measure what the cap actually changes, rather than assume."""
    blocked = gate(risk_budget=Decimal("1.00"), costs=RETAIL)
    assert not blocked.permits_entry
    assert gate(risk_budget=Decimal("1.00"), costs=RETAIL, enforce_cost_share=False).permits_entry


# ---------------------------------------------------------------------------
# B: one cost model, not two
# ---------------------------------------------------------------------------
def test_cost_is_modelled_not_supplied() -> None:
    """**The gate.** There is no parameter through which a caller can assert a
    cost that differs from the one execution will charge."""
    import inspect as _inspect

    params = _inspect.signature(evaluate_entry).parameters
    assert "estimated_cost" not in params
    assert "costs" in params


def test_the_modelled_cost_is_reported() -> None:
    decision = gate(costs=RETAIL)
    assert decision.estimated_cost is not None
    assert decision.estimated_cost > Decimal("2")  # two order fees, plus spread
    assert decision.intended_notional is not None


def test_cost_scales_with_the_position_not_the_account() -> None:
    small = gate(risk_budget=Decimal("5"), costs=CHEAP)
    large = gate(risk_budget=Decimal("500"), costs=CHEAP)
    assert small.estimated_cost is not None
    assert large.estimated_cost is not None
    assert large.estimated_cost > small.estimated_cost * 50


# ---------------------------------------------------------------------------
# I/J: rolling recompute and flags
# ---------------------------------------------------------------------------
def test_a_stable_position_is_flagged_stable() -> None:
    entry = risk(atr_pct=1.0)
    assert flag_position(entry_risk=entry, current_risk=risk(atr_pct=1.05)) is RiskFlag.STABLE


def test_a_material_rise_is_flagged_increased() -> None:
    entry = risk(atr_pct=1.0)
    later = risk(atr_pct=1.5)
    assert flag_position(entry_risk=entry, current_risk=later) is RiskFlag.INCREASED


def test_a_material_fall_is_flagged_decreased() -> None:
    entry = risk(atr_pct=2.0)
    later = risk(atr_pct=1.0)
    assert flag_position(entry_risk=entry, current_risk=later) is RiskFlag.DECREASED


def test_an_extreme_regime_is_flagged_regardless_of_change() -> None:
    entry = risk(regime=VolatilityRegime.NORMAL, atr_pct=1.0)
    later = risk(regime=VolatilityRegime.EXTREME, atr_pct=1.0)
    assert flag_position(entry_risk=entry, current_risk=later) is RiskFlag.EXTREME


def test_a_missing_or_stale_recompute_is_flagged_not_guessed() -> None:
    entry = risk()
    assert flag_position(entry_risk=entry, current_risk=None) is RiskFlag.DATA_STALE
    stale = risk(bar_age=MAX_BAR_AGE + timedelta(minutes=1))
    assert flag_position(entry_risk=entry, current_risk=stale) is RiskFlag.DATA_STALE


def test_small_drift_does_not_raise_a_flag() -> None:
    """A flag on every tick trains a reader to ignore flags."""
    entry = risk(atr_pct=1.0)
    drift = risk(atr_pct=float(1 + MATERIAL_RISK_CHANGE / 2))
    assert flag_position(entry_risk=entry, current_risk=drift) is RiskFlag.STABLE


def test_no_flag_means_exit() -> None:
    """**The gate.** Flags are inputs to a phase that does not exist yet."""
    for flag in RiskFlag:
        assert "EXIT" not in flag.value
        assert "SELL" not in flag.value
        assert "CLOSE" not in flag.value


def test_flagging_cannot_close_a_position() -> None:
    source = inspect.getsource(flag_position)
    for forbidden in ("close", "exit", "sell"):
        assert forbidden not in source.lower().split('"""')[-1]
