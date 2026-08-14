"""risk-v1: calibrated only where it was validated, and silent about direction.

Two failure modes matter for a risk service. The first is presenting an
uncalibrated number in the same shape as a calibrated one — 5d, 10d and 20d were
measured and failed, so the engine must refuse them rather than widen a band.
The second is a magnitude product quietly acquiring a sign.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from app.market_data import risk as risk_module
from app.market_data.risk import (
    GAP_ATR_MULTIPLE,
    K_BAND,
    K_STRESS,
    K_TYPICAL,
    MIN_PRACTICAL_POSITION,
    MODEL_VERSION,
    SUPPORTED_HORIZONS,
    PositionRisk,
    ShortHorizonRisk,
    UnsupportedHorizonError,
    assess,
    size_position,
)
from app.market_data.volatility import (
    MAX_BAR_AGE,
    VALIDITY,
    ExpectedMovement,
    VolatilityRegime,
)

NOW = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)
REGIMES = list(VolatilityRegime)


def movement(
    *,
    regime: VolatilityRegime = VolatilityRegime.NORMAL,
    atr_pct: float = 1.0,
    bar_age: timedelta = timedelta(minutes=5),
    calculated_age: timedelta = timedelta(minutes=5),
) -> ExpectedMovement:
    return ExpectedMovement(
        symbol="TEST",
        calculated_at=NOW - calculated_age,
        bar_timestamp=NOW - bar_age,
        regime=regime,
        percentile=0.5,
        atr_pct=atr_pct,
        recent_range_pct=2.0,
    )


# ---------------------------------------------------------------------------
# A: the scope is enforced, not documented
# ---------------------------------------------------------------------------
def test_only_one_and_three_day_horizons_are_supported() -> None:
    assert SUPPORTED_HORIZONS == (1, 3)


@pytest.mark.parametrize("horizon", [5, 10, 20])
def test_an_uncalibrated_horizon_raises_rather_than_estimating(horizon: int) -> None:
    """**The gate.** 5d/10d/20d failed validation at 5.16-8.82pp."""
    result = assess(movement(), now=NOW)
    with pytest.raises(UnsupportedHorizonError, match="no validated calibration"):
        result.band(horizon)


@pytest.mark.parametrize("horizon", [1, 3])
def test_a_supported_horizon_returns_its_band(horizon: int) -> None:
    result = assess(movement(), now=NOW)
    assert result.band(horizon) > 0


def test_the_engine_carries_its_own_version() -> None:
    """risk-v1 and volatility-v1 change independently."""
    result = assess(movement(), now=NOW)
    assert result.model_version == MODEL_VERSION == "risk-v1"
    assert result.volatility_model_version == "volatility-v1"


# ---------------------------------------------------------------------------
# Band structure
# ---------------------------------------------------------------------------
def test_bands_widen_from_typical_to_stress() -> None:
    """Ordering must hold in every regime, or the numbers contradict each other."""
    for regime in REGIMES:
        r = assess(movement(regime=regime), now=NOW)
        assert r.expected_move_1d < r.risk_band_1d < r.stress_move_1d
        assert r.expected_move_3d < r.risk_band_3d < r.stress_move_3d


def test_three_day_bands_exceed_one_day_bands() -> None:
    for regime in REGIMES:
        r = assess(movement(regime=regime), now=NOW)
        assert r.risk_band_3d > r.risk_band_1d
        assert r.risk_band_3d == pytest.approx(r.risk_band_1d * 3**0.5)


def test_bands_scale_linearly_with_atr() -> None:
    """Doubling volatility doubles every band. The model is a multiplier."""
    calm = assess(movement(atr_pct=1.0), now=NOW)
    wild = assess(movement(atr_pct=2.0), now=NOW)
    assert wild.risk_band_1d == pytest.approx(calm.risk_band_1d * 2)
    assert wild.overnight_gap_pct == pytest.approx(calm.overnight_gap_pct * 2)


def test_the_multiplier_falls_as_the_regime_rises() -> None:
    """Volatility mean-reverts, so a wilder regime needs a smaller multiple.

    Counter-intuitive but measured: the *band* still widens, because ATR% is
    already larger.
    """
    order = [
        VolatilityRegime.LOW,
        VolatilityRegime.NORMAL,
        VolatilityRegime.HIGH,
        VolatilityRegime.EXTREME,
    ]
    for table in (K_TYPICAL, K_BAND, K_STRESS):
        multiples = [table[r] for r in order]
        assert multiples == sorted(multiples, reverse=True)


def test_every_regime_has_a_complete_parameter_set() -> None:
    for table in (K_TYPICAL, K_BAND, K_STRESS, GAP_ATR_MULTIPLE):
        assert set(table) == set(REGIMES)


# ---------------------------------------------------------------------------
# F: the gap component is separate and never absorbed
# ---------------------------------------------------------------------------
def test_the_gap_is_a_fraction_of_the_band_not_an_addition() -> None:
    """Reported inside the band, so a reader cannot double-count it."""
    for regime in REGIMES:
        r = assess(movement(regime=regime), now=NOW)
        assert 0 < r.overnight_gap_pct < r.risk_band_1d
        assert 0.3 < r.gap_share_of_band < 0.6


def test_extreme_volatility_has_the_largest_gap_share() -> None:
    """Measured at ~50% of the band: half the day's risk before it opens."""
    shares = {
        regime: assess(movement(regime=regime), now=NOW).gap_share_of_band for regime in REGIMES
    }
    assert shares[VolatilityRegime.EXTREME] > shares[VolatilityRegime.LOW]


# ---------------------------------------------------------------------------
# Causality and freshness
# ---------------------------------------------------------------------------
def test_a_stale_input_cannot_produce_a_fresh_risk_claim() -> None:
    """**The gate.** Staleness is inherited, never recomputed optimistically."""
    stale = assess(movement(bar_age=MAX_BAR_AGE + timedelta(minutes=1)), now=NOW)
    assert stale.stale
    assert stale.data_quality == "STALE"


def test_an_old_calculation_is_also_stale() -> None:
    old = assess(movement(calculated_age=VALIDITY + timedelta(minutes=1)), now=NOW)
    assert old.stale


def test_a_fresh_input_is_marked_ok() -> None:
    assert assess(movement(), now=NOW).data_quality == "OK"


def test_assessment_reads_nothing_beyond_its_input() -> None:
    """Pure arithmetic: no session, no provider, no clock beyond ``now``."""
    parameters = set(inspect.signature(assess).parameters)
    assert parameters == {"movement", "now"}

    # The body only -- the docstring legitimately names what the function does
    # not touch, and matching that would test the prose rather than the code.
    body = inspect.getsource(assess).split('"""')[-1]
    for forbidden in ("session", "provider", "select(", "fetch", "await"):
        assert forbidden not in body


def test_rolling_recalculation_tracks_new_information() -> None:
    """A position open for days gets a new estimate, not the entry one."""
    day_one = assess(movement(regime=VolatilityRegime.NORMAL, atr_pct=1.0), now=NOW)
    day_two = assess(
        movement(regime=VolatilityRegime.HIGH, atr_pct=1.8),
        now=NOW + timedelta(days=1),
    )
    assert day_two.risk_band_1d > day_one.risk_band_1d
    assert day_two.calculated_at > day_one.calculated_at


# ---------------------------------------------------------------------------
# G: hypothetical position risk reports, never advises
# ---------------------------------------------------------------------------
def test_a_regime_change_is_reported_as_a_transition() -> None:
    position = PositionRisk(
        symbol="TEST",
        entry_price=100.0,
        current_price=104.0,
        entry_regime=VolatilityRegime.NORMAL,
        risk=assess(movement(regime=VolatilityRegime.EXTREME), now=NOW),
        risk_budget_amount=50.0,
    )
    assert position.regime_changed
    assert position.regime_transition == "NORMAL_VOL -> EXTREME_VOL"
    assert position.unrealised_pct == pytest.approx(4.0)


def test_an_unchanged_regime_reports_no_transition() -> None:
    position = PositionRisk(
        symbol="TEST",
        entry_price=100.0,
        current_price=99.0,
        entry_regime=VolatilityRegime.NORMAL,
        risk=assess(movement(regime=VolatilityRegime.NORMAL), now=NOW),
        risk_budget_amount=50.0,
    )
    assert not position.regime_changed
    assert position.regime_transition is None


def test_position_risk_suggests_no_action() -> None:
    """It reports state. Deciding what to do is a phase that has not happened."""
    fields = set(PositionRisk.__dataclass_fields__)
    for forbidden in ("action", "signal", "should_exit", "recommendation", "close"):
        assert forbidden not in fields


# ---------------------------------------------------------------------------
# H/I: sizing arithmetic
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("equity", [100.0, 1_000.0, 10_000.0])
@pytest.mark.parametrize("budget", [0.25, 0.5, 1.0, 2.0])
def test_a_stop_out_costs_exactly_the_declared_risk(equity: float, budget: float) -> None:
    """**The gate.** The identity sizing exists to guarantee."""
    sizing = size_position(
        equity=equity,
        risk_budget_pct=budget,
        stop_distance_pct=2.6,
        max_allocation_pct=1_000.0,
    )
    implied = sizing.max_position_value * sizing.stop_distance_pct / 100
    assert implied == pytest.approx(sizing.risk_amount)


def test_allocation_is_capped_and_the_cap_is_reported() -> None:
    sizing = size_position(equity=1_000.0, risk_budget_pct=2.0, stop_distance_pct=0.5)
    assert sizing.max_position_value <= 1_000.0
    assert sizing.leverage_capped


def test_a_normal_sizing_is_not_flagged_as_capped() -> None:
    sizing = size_position(equity=10_000.0, risk_budget_pct=0.5, stop_distance_pct=2.6)
    assert not sizing.leverage_capped


def test_a_zero_stop_or_equity_is_refused() -> None:
    with pytest.raises(ValueError, match="stop distance must be positive"):
        size_position(equity=1_000.0, risk_budget_pct=1.0, stop_distance_pct=0.0)
    with pytest.raises(ValueError, match="equity must be positive"):
        size_position(equity=0.0, risk_budget_pct=1.0, stop_distance_pct=2.0)


def test_a_hundred_at_the_smallest_budget_is_impractical() -> None:
    """**Reported, not engineered around.** €100 at 0.25% is a €10 position."""
    sizing = size_position(equity=100.0, risk_budget_pct=0.25, stop_distance_pct=2.6)
    assert not sizing.practical
    assert sizing.max_position_value < MIN_PRACTICAL_POSITION


def test_a_hundred_becomes_practical_at_a_larger_budget() -> None:
    sizing = size_position(equity=100.0, risk_budget_pct=1.0, stop_distance_pct=2.6)
    assert sizing.practical


def test_whole_share_rounding_can_make_a_small_account_fail() -> None:
    """Fractional shares are what keep €100 viable at all."""
    fractional = size_position(
        equity=100.0, risk_budget_pct=1.0, stop_distance_pct=2.6, price=250.0
    )
    whole = size_position(
        equity=100.0,
        risk_budget_pct=1.0,
        stop_distance_pct=2.6,
        price=250.0,
        fractional_shares=False,
    )
    assert fractional.practical
    assert whole.max_position_value == 0.0
    assert not whole.practical


def test_cost_share_of_risk_is_reported() -> None:
    sizing = size_position(equity=10_000.0, risk_budget_pct=1.0, stop_distance_pct=2.6)
    assert 0.0 < sizing.cost_share_of_risk < 0.5


# ---------------------------------------------------------------------------
# The boundary that must not erode
# ---------------------------------------------------------------------------
def test_the_risk_object_carries_no_direction() -> None:
    """**The product gate.** Eight phases found no directional information."""
    fields = set(ShortHorizonRisk.__dataclass_fields__)
    for forbidden in (
        "direction",
        "target",
        "price_target",
        "bullish",
        "bearish",
        "probability",
        "expected_price",
        "buy",
        "sell",
    ):
        assert forbidden not in fields


def test_the_module_contains_no_recommendation_language() -> None:
    source = inspect.getsource(risk_module).lower()
    for forbidden in ("def buy", "def sell", "submit_order", "tradingclient"):
        assert forbidden not in source


def test_minimum_noise_distance_is_not_a_stop_recommendation() -> None:
    """It is a floor a future engine may consume, not a decision."""
    doc = ShortHorizonRisk.minimum_noise_distance.__doc__ or ""
    assert "not a stop recommendation" in doc.lower()
    result = assess(movement(), now=NOW)
    assert result.minimum_noise_distance(1) == result.risk_band_1d
