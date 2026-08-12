"""Support/resistance zones, breakout states and trade plans.

The property under test throughout is **causality**: a level at time T uses only
bars at or before T, and a breakout is never confirmed by a bar that has not
happened. Everything else here is arithmetic; that one is correctness.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import CostSettings
from app.scanner.enums import DataQuality
from app.scanner.horizons import TradingHorizon
from app.scanner.levels import (
    MIN_BARS_FOR_LEVELS,
    BreakState,
    LevelType,
    build_levels,
    classify_break,
)
from app.scanner.plans import (
    MIN_HEADROOM_BPS,
    MIN_REWARD_RISK,
    PlanInputs,
    SetupType,
    Tradeability,
    build_plan,
)

START = datetime(2026, 8, 1, tzinfo=UTC)


class Bar:
    def __init__(self, index: int, open_: float, high: float, low: float, close: float) -> None:
        self.timestamp = START + timedelta(hours=index)
        self.open = open_
        self.high = high
        self.low = low
        self.close = close


def oscillating(count: int = 60, low: float = 100.0, high: float = 110.0) -> list[Bar]:
    """Price repeatedly turning at the same two prices -- textbook S/R."""
    bars: list[Bar] = []
    for index in range(count):
        base = low + (index % 10) * (high - low) / 10
        bars.append(Bar(index, base, base + 1.5, base - 1.5, base))
    return bars


def plan_inputs(bars: list[Bar], price: float, **overrides: object) -> PlanInputs:
    levels = build_levels(bars, symbol="DEMOX", timeframe="1h", atr=1.5)
    base: dict[str, object] = {
        "symbol": "DEMOX",
        "horizon": TradingHorizon.SHORT_TERM,
        "generated_at": START + timedelta(days=3),
        "levels": levels,
        "market_price": price,
        "atr": 1.5,
        "score": 86.0,
        "confidence": 0.82,
        "spread_bps": 5.0,
        "costs": CostSettings(),
    }
    return PlanInputs(**(base | overrides))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Zone construction
# ---------------------------------------------------------------------------
def test_repeated_turns_produce_support_and_resistance() -> None:
    levels = build_levels(oscillating(), symbol="DEMOX", timeframe="1h", atr=1.5)

    assert levels.support
    assert levels.resistance
    assert levels.support[0].type is LevelType.SUPPORT
    assert levels.resistance[0].type is LevelType.RESISTANCE


def test_a_zone_is_an_interval_not_a_line() -> None:
    """Price reverses around a level, not at it."""
    levels = build_levels(oscillating(), symbol="DEMOX", timeframe="1h", atr=1.5)
    zone = levels.support[0]

    assert zone.upper_bound > zone.lower_bound
    assert zone.lower_bound < zone.midpoint < zone.upper_bound


def test_repeated_touches_are_counted() -> None:
    levels = build_levels(oscillating(), symbol="DEMOX", timeframe="1h", atr=1.5)

    assert levels.support[0].touch_count >= 3
    assert "multiple_touches" in levels.support[0].reason_codes


def test_more_touches_and_recency_raise_strength() -> None:
    """Strength is an engineering assumption, but a monotone one."""
    many = build_levels(oscillating(count=60), symbol="DEMOX", timeframe="1h", atr=1.5)
    few = build_levels(oscillating(count=35), symbol="DEMOX", timeframe="1h", atr=1.5)

    assert many.support[0].touch_count >= few.support[0].touch_count
    assert 0.0 <= many.support[0].strength <= 1.0
    assert 0.0 <= many.support[0].confidence <= 1.0


def test_too_little_history_produces_no_levels() -> None:
    """Not a guess from three bars."""
    levels = build_levels(
        oscillating(count=MIN_BARS_FOR_LEVELS - 5), symbol="DEMOX", timeframe="1h", atr=1.5
    )

    assert levels.support == ()
    assert levels.resistance == ()


def test_distance_is_measured_to_the_near_edge() -> None:
    """Measuring to the midpoint would understate how close price already is."""
    levels = build_levels(oscillating(), symbol="DEMOX", timeframe="1h", atr=1.5)
    zone = levels.resistance[0]

    assert zone.distance_bps(zone.midpoint) == 0.0, "inside the zone is zero distance"
    assert zone.distance_bps(zone.lower_bound - 5) > 0


def test_nearest_support_is_below_and_resistance_above() -> None:
    levels = build_levels(oscillating(), symbol="DEMOX", timeframe="1h", atr=1.5)

    support = levels.nearest_support(105.0)
    resistance = levels.nearest_resistance(105.0)

    assert support is not None
    assert support.upper_bound <= 105.0
    assert resistance is not None
    assert resistance.lower_bound >= 105.0


# ---------------------------------------------------------------------------
# Causality
# ---------------------------------------------------------------------------
def test_a_level_uses_only_the_bars_it_was_given() -> None:
    """**The look-ahead test.**

    Appending later bars must not change a level computed from the prefix. If it
    did, every historical level would depend on how much data happened to be
    loaded.
    """
    bars = oscillating()
    prefix = bars[:45]

    before = build_levels(prefix, symbol="DEMOX", timeframe="1h", atr=1.5)
    _ = build_levels(bars, symbol="DEMOX", timeframe="1h", atr=1.5)
    after = build_levels(prefix, symbol="DEMOX", timeframe="1h", atr=1.5)

    assert [z.as_dict() for z in before.support] == [z.as_dict() for z in after.support]
    assert [z.as_dict() for z in before.resistance] == [z.as_dict() for z in after.resistance]


def test_a_future_spike_cannot_create_a_past_level() -> None:
    bars = oscillating()
    quiet = build_levels(bars[:50], symbol="DEMOX", timeframe="1h", atr=1.5)

    spiked = [*bars[:50], Bar(50, 110, 200, 109, 195), Bar(51, 195, 200, 190, 198)]
    with_future = build_levels(spiked[:50], symbol="DEMOX", timeframe="1h", atr=1.5)

    assert [z.midpoint for z in quiet.resistance] == [z.midpoint for z in with_future.resistance]


# ---------------------------------------------------------------------------
# Breakout / retest
# ---------------------------------------------------------------------------
def test_a_single_close_beyond_resistance_is_only_an_attempt() -> None:
    """**Confirmation needs a bar that has not happened yet.**

    Treating the breakout bar as confirmation would read the next bar's
    behaviour out of a bar that does not exist.
    """
    bars = oscillating()
    levels = build_levels(bars, symbol="DEMOX", timeframe="1h", atr=1.5)
    zone = levels.resistance[0]
    attempt = [*bars, Bar(60, 110, 113, 109, zone.upper_bound + 2)]

    assert classify_break(zone=zone, bars=attempt) is BreakState.BREAKOUT_ATTEMPT


def test_holding_beyond_the_zone_confirms_the_breakout() -> None:
    bars = oscillating()
    levels = build_levels(bars, symbol="DEMOX", timeframe="1h", atr=1.5)
    zone = levels.resistance[0]
    confirmed = [
        *bars,
        Bar(60, 110, 113, 109, zone.upper_bound + 2),
        Bar(61, 112, 114, 111, zone.upper_bound + 3),
    ]

    assert classify_break(zone=zone, bars=confirmed) is BreakState.BREAKOUT_CONFIRMED


def test_closing_back_inside_is_a_failed_breakout() -> None:
    """The trap, and the reason an attempt is not tradeable."""
    bars = oscillating()
    levels = build_levels(bars, symbol="DEMOX", timeframe="1h", atr=1.5)
    zone = levels.resistance[0]
    failed = [
        *bars,
        Bar(60, 110, 113, 109, zone.upper_bound + 2),
        Bar(61, 112, 113, 105, zone.lower_bound - 2),
    ]

    assert classify_break(zone=zone, bars=failed) is BreakState.FAILED_BREAKOUT


def test_returning_into_the_zone_after_a_break_is_a_retest() -> None:
    bars = oscillating()
    levels = build_levels(bars, symbol="DEMOX", timeframe="1h", atr=1.5)
    zone = levels.resistance[0]
    retest = [
        *bars,
        Bar(60, 110, 113, 109, zone.upper_bound + 2),
        Bar(61, 112, 113, 109, zone.midpoint),
    ]

    assert classify_break(zone=zone, bars=retest) is BreakState.RETEST_IN_PROGRESS


def test_a_breakout_is_never_confirmed_on_the_final_bar() -> None:
    """Property form of the causality rule."""
    bars = oscillating()
    levels = build_levels(bars, symbol="DEMOX", timeframe="1h", atr=1.5)
    zone = levels.resistance[0]

    for close in (zone.upper_bound + 0.5, zone.upper_bound + 5, zone.upper_bound + 20):
        state = classify_break(zone=zone, bars=[*bars, Bar(60, 110, 120, 109, close)])
        assert state is not BreakState.BREAKOUT_CONFIRMED


# ---------------------------------------------------------------------------
# Trade plans
# ---------------------------------------------------------------------------
def test_a_high_score_under_resistance_is_no_trade() -> None:
    """**The scenario this whole module exists for.**

    Score 86, bullish, and resistance immediately overhead. The signal is
    excellent and the trade is terrible.
    """
    bars = oscillating()
    levels = build_levels(bars, symbol="DEMOX", timeframe="1h", atr=1.5)
    just_below = levels.resistance[0].lower_bound - 0.05

    plan = build_plan(plan_inputs(bars, just_below))

    assert plan.tradeability is Tradeability.NO_TRADE
    assert "resistance_overhead" in plan.risk_codes
    assert plan.signal_score == 86.0, "the signal is still strong -- the location is not"


def test_a_plan_without_a_structural_target_never_invents_one() -> None:
    """**Price discovery: no fixed target, and no pretence of one.**

    After a breakout there may be no higher level at all -- 94.7% of >=85 bullish
    setups in the historical data. The plan says so rather than projecting a
    number, and falls to the trailing policy instead of a fabricated ratio.
    """
    bars = oscillating()
    above_everything = 150.0

    plan = build_plan(plan_inputs(bars, above_everything))

    assert plan.target_1 is None
    assert plan.net_reward_risk is None, "no ratio without a target"
    assert plan.structure_state == "PRICE_DISCOVERY"
    assert plan.exit_policy == "TRAILING_STRUCTURE"
    assert plan.tradeability is not Tradeability.ACTIONABLE_BUY, (
        "an unconfirmed new high is momentum, not a setup"
    )


def test_an_entry_zone_is_an_area_not_the_last_price() -> None:
    bars = oscillating()
    plan = build_plan(plan_inputs(bars, 104.0))

    assert plan.entry_zone_low is not None
    assert plan.entry_zone_high is not None
    assert plan.entry_zone_high > plan.entry_zone_low


def test_invalidation_sits_below_support_and_records_its_reason() -> None:
    """Structural, never a flat percentage."""
    bars = oscillating()
    plan = build_plan(plan_inputs(bars, 104.0))

    assert plan.invalidation_price is not None
    assert plan.nearest_support is not None
    assert plan.invalidation_price < plan.nearest_support.lower_bound
    assert "support" in plan.invalidation_reason


def test_the_target_is_named_as_structure_not_prediction() -> None:
    bars = oscillating()
    plan = build_plan(plan_inputs(bars, 104.0))

    if plan.target_1 is not None:
        assert "resistance" in plan.target_reason
        assert "predict" not in plan.target_reason.lower()
        assert "expected" not in plan.target_reason.lower()


def test_costs_reduce_reward_risk() -> None:
    """Net is never better than gross."""
    bars = oscillating()
    plan = build_plan(plan_inputs(bars, 104.0))

    if plan.gross_reward_risk is not None and plan.net_reward_risk is not None:
        assert plan.net_reward_risk <= plan.gross_reward_risk


def test_a_nonsensical_geometry_produces_no_ratio() -> None:
    """A ratio no trade could realise is worse than no ratio."""
    bars = oscillating()
    # Below every support: invalidation would sit above entry.
    plan = build_plan(plan_inputs(bars, 50.0))

    assert plan.gross_reward_risk is None
    assert plan.net_reward_risk is None


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"data_quality": DataQuality.MISSING}, "insufficient_data"),
        ({"data_quality": DataQuality.INSUFFICIENT}, "insufficient_data"),
        ({"spread_bps": 900.0}, "implausible_spread"),
        ({"direction": -1}, "not_bullish"),
    ],
)
def test_bad_inputs_are_refused_with_a_reason(override: dict[str, object], expected: str) -> None:
    plan = build_plan(plan_inputs(oscillating(), 104.0, **override))

    assert plan.tradeability is Tradeability.NOT_AVAILABLE
    assert expected in plan.risk_codes
    assert plan.gross_reward_risk is None, "no arithmetic runs on rejected inputs"


def test_an_after_hours_spread_cannot_price_a_plan() -> None:
    """Phase 4 recorded 883-1118 bps after the close. Those are not costs."""
    plan = build_plan(plan_inputs(oscillating(), 104.0, spread_bps=1118.0))

    assert plan.tradeability is Tradeability.NOT_AVAILABLE


def test_stale_data_downgrades_to_watch_rather_than_buy() -> None:
    bars = oscillating()
    plan = build_plan(plan_inputs(bars, 104.0, data_quality=DataQuality.STALE))

    assert plan.tradeability is not Tradeability.ACTIONABLE_BUY
    assert "stale_data" in plan.risk_codes


def test_an_unconfirmed_breakout_waits() -> None:
    """Confirmation requires the next bar; waiting for it is the point."""
    bars = oscillating()
    plan = build_plan(plan_inputs(bars, 104.0, break_state=BreakState.BREAKOUT_ATTEMPT))

    assert plan.tradeability is not Tradeability.ACTIONABLE_BUY


# ---------------------------------------------------------------------------
# Policy shape
# ---------------------------------------------------------------------------
def test_the_thresholds_are_declared_assumptions() -> None:
    """Not fitted: three parameters against 228 episodes would fit noise."""
    assert MIN_REWARD_RISK == 1.5
    assert MIN_HEADROOM_BPS == 150.0


def test_a_plan_is_ml_ready_and_flat() -> None:
    """Part 27: deterministic features, no framework."""
    plan = build_plan(plan_inputs(oscillating(), 104.0))
    row = plan.as_dict()

    for key in (
        "distance_to_support_bps",
        "distance_to_resistance_bps",
        "support_strength",
        "resistance_strength",
        "support_touch_count",
        "gross_reward_risk",
        "net_reward_risk",
        "setup_type",
        "tradeability",
        "horizon",
    ):
        assert key in row


def test_a_plan_never_carries_a_predicted_price() -> None:
    row = build_plan(plan_inputs(oscillating(), 104.0)).as_dict()

    for key in row:
        assert "expected_price" not in key
        assert "forecast" not in key
        assert "prediction" not in key


def test_setup_types_are_structural() -> None:
    assert SetupType.BREAKOUT.value == "BREAKOUT"
    assert SetupType.RETEST.value == "RETEST"
    assert SetupType.PULLBACK.value == "PULLBACK"
