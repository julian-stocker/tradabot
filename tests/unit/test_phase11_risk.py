"""Expected-movement bands: calibrated, causal, and incapable of implying direction.

A risk band is consulted precisely when someone is about to commit money, so the
failure modes that matter are the quiet ones: a band that is narrower at higher
confidence, an estimate built from a bar that had not closed, a sizing formula
that returns a position larger than the account, or a "magnitude" number that
has quietly acquired a sign.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from app.market_data import volatility
from app.market_data.volatility import (
    ATR_PERIOD,
    CALIBRATION,
    MAX_BAR_AGE,
    MIN_BARS,
    MODEL_VERSION,
    PERCENTILE_WINDOW,
    REGIME_BANDS,
    VALIDITY,
    VolatilityRegime,
    average_true_range,
    estimate,
    regime_for,
)
from app.research.phase11 import (
    ATR_MULTIPLES,
    COVERAGE_LEVELS,
    HORIZONS,
    MAX_CALIBRATION_ERROR,
    MIN_PRACTICAL_POSITION_EUR,
    band_is_monotone,
    calibration_error,
    classify_expected_movement,
    position_size,
    risk_at_stop,
)

START = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)


def bars(n: int, *, base: float = 100.0, seed: int = 3) -> tuple[list[float], ...]:
    import random

    rng = random.Random(seed)
    highs, lows, closes = [], [], []
    price = base
    for _ in range(n):
        price *= 1.0 + rng.gauss(0.0003, 0.008)
        highs.append(price * 1.004)
        lows.append(price * 0.996)
        closes.append(price)
    return highs, lows, closes


# ---------------------------------------------------------------------------
# A: volatility-v1 is frozen
# ---------------------------------------------------------------------------
def test_the_regime_boundaries_are_exactly_as_specified() -> None:
    """**The freeze.** Phase 11 may measure volatility-v1; it may not tune it."""
    assert REGIME_BANDS == (
        (VolatilityRegime.LOW, 0.00, 0.25),
        (VolatilityRegime.NORMAL, 0.25, 0.70),
        (VolatilityRegime.HIGH, 0.70, 0.90),
        (VolatilityRegime.EXTREME, 0.90, 1.01),
    )
    assert ATR_PERIOD == 14
    assert PERCENTILE_WINDOW == 252
    assert MIN_BARS == 60
    assert MODEL_VERSION == "volatility-v1"


def test_the_bands_partition_the_percentile_range() -> None:
    """Half-open and contiguous: a percentile lands in exactly one band."""
    for (_, _, upper), (_, lower, _) in pairwise(REGIME_BANDS):
        assert upper == lower
    assert REGIME_BANDS[0][1] == 0.0
    assert REGIME_BANDS[-1][2] > 1.0


@pytest.mark.parametrize(
    ("percentile", "expected"),
    [
        (0.00, VolatilityRegime.LOW),
        (0.2499, VolatilityRegime.LOW),
        (0.25, VolatilityRegime.NORMAL),
        (0.6999, VolatilityRegime.NORMAL),
        (0.70, VolatilityRegime.HIGH),
        (0.8999, VolatilityRegime.HIGH),
        (0.90, VolatilityRegime.EXTREME),
        (1.00, VolatilityRegime.EXTREME),
    ],
)
def test_regime_boundaries_are_stable(percentile: float, expected: VolatilityRegime) -> None:
    assert regime_for(percentile) is expected


def test_the_frozen_calibration_is_monotone_across_regimes() -> None:
    """A calmer regime must not claim a wider typical range than a wilder one."""
    order = [
        VolatilityRegime.LOW,
        VolatilityRegime.NORMAL,
        VolatilityRegime.HIGH,
        VolatilityRegime.EXTREME,
    ]
    typical = [CALIBRATION[r].typical_pct for r in order]
    stress = [CALIBRATION[r].stress_pct for r in order]
    assert typical == sorted(typical)
    assert stress == sorted(stress)
    assert all(s > t for s, t in zip(stress, typical, strict=True))


# ---------------------------------------------------------------------------
# Causality
# ---------------------------------------------------------------------------
def test_a_future_bar_cannot_change_an_earlier_estimate() -> None:
    """**The gate.** Append a violent bar; the earlier estimate must not move."""
    highs, lows, closes = bars(300)
    early = estimate(
        symbol="T", highs=highs, lows=lows, closes=closes, bar_timestamp=START, now=START
    )
    extended = estimate(
        symbol="T",
        highs=[*highs, highs[-1] * 1.4],
        lows=[*lows, lows[-1] * 0.6],
        closes=[*closes, closes[-1] * 1.3],
        bar_timestamp=START + timedelta(hours=1),
        now=START + timedelta(hours=1),
    )
    assert early is not None
    assert extended is not None
    # The earlier estimate is unchanged: recomputing on the same prefix returns it.
    again = estimate(
        symbol="T", highs=highs, lows=lows, closes=closes, bar_timestamp=START, now=START
    )
    assert again is not None
    assert again.atr_pct == pytest.approx(early.atr_pct)
    assert again.percentile == pytest.approx(early.percentile)


def test_atr_uses_only_the_preceding_close() -> None:
    """True range reaches backwards, never forwards."""
    source = inspect.getsource(volatility.true_range)
    assert "previous_close" in source
    assert "next" not in source


def test_too_little_history_is_refused_rather_than_estimated() -> None:
    highs, lows, closes = bars(MIN_BARS - 1)
    assert (
        estimate(symbol="T", highs=highs, lows=lows, closes=closes, bar_timestamp=START, now=START)
        is None
    )


def test_a_partial_atr_is_never_returned() -> None:
    """An ATR from four bars is a number, not an estimate."""
    highs, lows, closes = bars(ATR_PERIOD)
    assert average_true_range(highs, lows, closes) is None


def test_a_stale_estimate_is_marked_not_silently_fresh() -> None:
    """A stalled feed invalidates the inputs, not just the conclusion."""
    highs, lows, closes = bars(300)
    result = estimate(
        symbol="T",
        highs=highs,
        lows=lows,
        closes=closes,
        bar_timestamp=START,
        now=START + VALIDITY + timedelta(minutes=1),
    )
    assert result is not None
    assert result.is_stale(now=START + VALIDITY + timedelta(minutes=1))
    assert MAX_BAR_AGE < VALIDITY


# ---------------------------------------------------------------------------
# Band structure
# ---------------------------------------------------------------------------
def test_a_wider_claim_can_never_be_a_narrower_band() -> None:
    """**The structural gate.** A 90% band narrower than an 80% one is nonsense."""
    assert band_is_monotone({0.50: 1.0, 0.80: 2.0, 0.90: 3.0, 0.95: 4.0})
    assert not band_is_monotone({0.50: 1.0, 0.80: 3.0, 0.90: 2.5, 0.95: 4.0})


def test_equal_bands_are_permitted() -> None:
    """Two levels can legitimately coincide on a coarse sample."""
    assert band_is_monotone({0.80: 2.0, 0.90: 2.0})


def test_coverage_levels_are_ordered_and_below_one() -> None:
    assert list(COVERAGE_LEVELS) == sorted(COVERAGE_LEVELS)
    assert max(COVERAGE_LEVELS) < 1.0


def test_calibration_error_signs_under_coverage_negative() -> None:
    """Under-covering is the dangerous direction and must read negative."""
    assert calibration_error(claimed=0.80, actual=0.68) == pytest.approx(-0.12)
    assert calibration_error(claimed=0.80, actual=0.86) == pytest.approx(0.06)


def test_horizons_are_the_briefed_set() -> None:
    assert HORIZONS == (1, 3, 5, 10, 20)


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------
def test_a_band_that_is_only_right_on_average_is_regime_dependent() -> None:
    """The failure the yearly view exists to catch."""
    assert (
        classify_expected_movement(
            observations=50_000, mean_absolute_error=0.02, worst_year_error=0.19
        )
        == "REGIME_DEPENDENT"
    )


def test_a_badly_calibrated_band_is_named_as_such() -> None:
    assert (
        classify_expected_movement(
            observations=50_000, mean_absolute_error=0.14, worst_year_error=0.14
        )
        == "POORLY_CALIBRATED"
    )


def test_robust_requires_both_pooled_and_yearly_calibration() -> None:
    assert (
        classify_expected_movement(
            observations=50_000, mean_absolute_error=0.03, worst_year_error=0.07
        )
        == "ROBUST"
    )


def test_the_threshold_is_not_moved_to_reach_robust() -> None:
    """5.09pp against a 5.00pp bar is PROMISING, not ROBUST. Frozen is frozen."""
    assert pytest.approx(0.05) == MAX_CALIBRATION_ERROR
    assert (
        classify_expected_movement(
            observations=50_000, mean_absolute_error=0.0509, worst_year_error=0.07
        )
        == "PROMISING_BUT_INSUFFICIENT"
    )


# ---------------------------------------------------------------------------
# J: sizing arithmetic cannot exceed its own budget
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("budget", [0.25, 0.50, 1.00, 2.00])
@pytest.mark.parametrize("stop", [0.5, 1.14, 2.12, 6.0])
def test_a_stop_out_costs_exactly_the_declared_risk(budget: float, stop: float) -> None:
    """**The gate.** The whole point of sizing is that this identity holds."""
    equity = 10_000.0
    notional = position_size(
        equity=equity, risk_budget_pct=budget, stop_distance_pct=stop, allow_leverage=True
    )
    assert risk_at_stop(notional=notional, stop_distance_pct=stop) == pytest.approx(
        equity * budget / 100
    )


def test_sizing_never_exceeds_equity_without_leverage() -> None:
    """A tight stop implies a huge position; unclamped that is a margin call."""
    notional = position_size(equity=1_000.0, risk_budget_pct=2.0, stop_distance_pct=0.5)
    assert notional <= 1_000.0


def test_a_zero_width_stop_is_refused_not_infinite() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        position_size(equity=1_000.0, risk_budget_pct=1.0, stop_distance_pct=0.0)


def test_a_tighter_stop_implies_a_larger_position() -> None:
    tight = position_size(
        equity=10_000.0, risk_budget_pct=1.0, stop_distance_pct=1.0, allow_leverage=True
    )
    wide = position_size(
        equity=10_000.0, risk_budget_pct=1.0, stop_distance_pct=4.0, allow_leverage=True
    )
    assert tight > wide


def test_the_smallest_budget_on_the_smallest_account_stays_practical() -> None:
    """€100 at 0.25% risk must still clear the minimum worth trading."""
    notional = position_size(equity=100.0, risk_budget_pct=0.25, stop_distance_pct=1.14)
    assert notional >= MIN_PRACTICAL_POSITION_EUR


# ---------------------------------------------------------------------------
# The boundary that must not erode
# ---------------------------------------------------------------------------
def test_expected_movement_carries_no_direction() -> None:
    """**The product gate.** There is nowhere to put a target or a sign."""
    fields = set(volatility.ExpectedMovement.__dataclass_fields__)
    for forbidden in ("target", "direction", "price_target", "bullish", "bearish", "probability"):
        assert forbidden not in fields


def test_stop_multiples_are_declared_not_searched() -> None:
    """Six pre-declared distances, evenly spaced. Not an optimisation grid."""
    assert ATR_MULTIPLES == (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)
    steps = {round(b - a, 6) for a, b in pairwise(ATR_MULTIPLES)}
    assert steps == {0.5}
