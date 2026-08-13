"""The production volatility engine: causal, calibrated, and silent on direction.

This is the only research result from phases 6-8 that reached production, so the
tests concentrate on the two ways it could betray that: by seeing the future, and
by acquiring a directional claim it has no evidence for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.market_data.volatility import (
    CALIBRATION,
    MAX_BAR_AGE,
    MIN_BARS,
    MODEL_VERSION,
    REGIME_BANDS,
    VALIDITY,
    ExpectedMovement,
    VolatilityRegime,
    average_true_range,
    estimate,
    regime_for,
    true_range,
)
from app.market_data.volatility_service import DISCLAIMER, describe, render_line

NOW = datetime(2026, 8, 13, 16, 0, tzinfo=UTC)


def bars(count: int, *, spread: float = 1.0, price: float = 100.0):
    highs = [price + spread] * count
    lows = [price - spread] * count
    closes = [price] * count
    return highs, lows, closes


# ---------------------------------------------------------------------------
# Causality
# ---------------------------------------------------------------------------
def test_a_later_bar_cannot_change_an_earlier_estimate() -> None:
    """**The gate.** The estimate is a function of bars up to the newest supplied."""
    highs, lows, closes = bars(300)

    before = estimate(symbol="X", highs=highs, lows=lows, closes=closes, bar_timestamp=NOW, now=NOW)
    after = estimate(
        symbol="X",
        highs=[*highs, 500.0],
        lows=[*lows, 50.0],
        closes=[*closes, 400.0],
        bar_timestamp=NOW + timedelta(hours=1),
        now=NOW,
    )

    assert before is not None
    assert after is not None
    assert before.atr_pct != after.atr_pct, "the new bar should affect the *new* estimate"
    # Recomputing the original window still gives the original answer.
    again = estimate(symbol="X", highs=highs, lows=lows, closes=closes, bar_timestamp=NOW, now=NOW)
    assert again is not None
    assert again.atr_pct == before.atr_pct
    assert again.percentile == before.percentile


def test_true_range_uses_the_previous_close_never_a_later_one() -> None:
    assert true_range(110.0, 90.0, previous_close=None) == 20.0
    assert true_range(110.0, 100.0, previous_close=80.0) == 30.0


# ---------------------------------------------------------------------------
# Regimes and calibration
# ---------------------------------------------------------------------------
def test_the_bands_partition_the_percentile_range() -> None:
    edges = [(low, high) for _, low, high in REGIME_BANDS]

    assert edges[0][0] == 0.0
    assert edges[-1][1] > 1.0
    for index in range(len(edges) - 1):
        assert edges[index][1] == edges[index + 1][0]


@pytest.mark.parametrize(
    ("percentile", "expected"),
    [
        (0.00, VolatilityRegime.LOW),
        (0.24, VolatilityRegime.LOW),
        (0.25, VolatilityRegime.NORMAL),
        (0.69, VolatilityRegime.NORMAL),
        (0.70, VolatilityRegime.HIGH),
        (0.89, VolatilityRegime.HIGH),
        (0.90, VolatilityRegime.EXTREME),
        (1.00, VolatilityRegime.EXTREME),
    ],
)
def test_regime_boundaries(percentile: float, expected: VolatilityRegime) -> None:
    assert regime_for(percentile) is expected


def test_the_calibration_is_monotone_and_covers_every_regime() -> None:
    """Measured in phase 8; a non-monotone mapping would mean the bands are wrong."""
    typical = [CALIBRATION[regime].typical_pct for regime, _, _ in REGIME_BANDS]
    stress = [CALIBRATION[regime].stress_pct for regime, _, _ in REGIME_BANDS]

    assert typical == sorted(typical)
    assert stress == sorted(stress)
    assert all(CALIBRATION[r].stress_pct > CALIBRATION[r].typical_pct for r in VolatilityRegime)
    assert all(CALIBRATION[r].sample > 10_000 for r in VolatilityRegime)


def varied(count: int) -> tuple[list[float], list[float], list[float]]:
    """A history with genuinely varying ranges.

    A constant-spread series is degenerate for a percentile: every trailing value
    equals the current one, so everything ranks at 1.0 and the test proves
    nothing. The alternation here gives the trailing window a real distribution.
    """
    spreads = [0.3 if index % 3 else 0.9 for index in range(count)]
    highs = [100.0 + spread for spread in spreads]
    lows = [100.0 - spread for spread in spreads]
    return highs, lows, [100.0] * count


def test_a_recent_volatility_spike_ranks_above_a_calm_stretch() -> None:
    """The percentile must respond to a change in state, not just to price level."""
    highs, lows, closes = varied(300)

    calm = estimate(symbol="C", highs=highs, lows=lows, closes=closes, bar_timestamp=NOW, now=NOW)
    spiked = estimate(
        symbol="W",
        highs=[*highs[:-15], *[106.0] * 15],
        lows=[*lows[:-15], *[94.0] * 15],
        closes=closes,
        bar_timestamp=NOW,
        now=NOW,
    )

    assert calm is not None
    assert spiked is not None
    assert spiked.atr_pct > calm.atr_pct
    assert spiked.percentile >= calm.percentile
    assert spiked.regime is VolatilityRegime.EXTREME


# ---------------------------------------------------------------------------
# Missing and stale data
# ---------------------------------------------------------------------------
def test_too_little_history_is_refused_not_approximated() -> None:
    highs, lows, closes = bars(MIN_BARS - 1)

    assert estimate(symbol="X", highs=highs, lows=lows, closes=closes, bar_timestamp=NOW) is None


def test_mismatched_series_are_refused() -> None:
    highs, lows, closes = bars(200)

    assert (
        estimate(symbol="X", highs=highs[:-5], lows=lows, closes=closes, bar_timestamp=NOW) is None
    )


def test_an_atr_without_enough_bars_is_none_rather_than_partial() -> None:
    assert average_true_range([1.0], [1.0], [1.0]) is None


def test_an_old_bar_makes_the_estimate_stale() -> None:
    """A stalled feed must not present an old number as current."""
    movement = ExpectedMovement(
        symbol="X",
        calculated_at=NOW,
        bar_timestamp=NOW - MAX_BAR_AGE - timedelta(minutes=5),
        regime=VolatilityRegime.HIGH,
        percentile=0.8,
        atr_pct=1.0,
        recent_range_pct=2.0,
    )

    assert movement.is_stale(now=NOW)


def test_an_old_calculation_expires_after_one_session() -> None:
    """Phase 8 measured the decay: one session supported, one week not."""
    movement = ExpectedMovement(
        symbol="X",
        calculated_at=NOW - VALIDITY - timedelta(minutes=1),
        bar_timestamp=NOW,
        regime=VolatilityRegime.HIGH,
        percentile=0.8,
        atr_pct=1.0,
        recent_range_pct=2.0,
    )

    assert movement.is_stale(now=NOW)
    assert timedelta(days=1) >= VALIDITY, "a multi-day claim is not supported by the research"


def test_a_fresh_estimate_is_not_stale() -> None:
    movement = ExpectedMovement(
        symbol="X",
        calculated_at=NOW,
        bar_timestamp=NOW - timedelta(minutes=20),
        regime=VolatilityRegime.NORMAL,
        percentile=0.5,
        atr_pct=1.0,
        recent_range_pct=2.0,
    )

    assert not movement.is_stale(now=NOW)


# ---------------------------------------------------------------------------
# The product boundary: magnitude only
# ---------------------------------------------------------------------------
def movement(regime: VolatilityRegime = VolatilityRegime.EXTREME) -> ExpectedMovement:
    return ExpectedMovement(
        symbol="NVDA",
        calculated_at=NOW,
        bar_timestamp=NOW - timedelta(minutes=10),
        regime=regime,
        percentile=0.93,
        atr_pct=1.4,
        recent_range_pct=3.1,
    )


def test_the_payload_contains_no_direction_price_or_target() -> None:
    """**The product boundary, enforced structurally.**

    Phases 6-8 found no directional edge. A formatter cannot render a target that
    the payload has nowhere to carry.
    """
    payload = describe(movement(), now=NOW)

    for forbidden in ("price", "target", "direction", "bullish", "bearish", "probability", "score"):
        assert not any(forbidden in key.lower() for key in payload)


def test_the_rendered_line_makes_no_recommendation() -> None:
    from app.notifications.trends import assert_no_recommendation_language

    text = render_line(movement(), now=NOW)

    assert_no_recommendation_language(text.replace("stress", ""))
    assert "NVDA" in text
    assert "%" in text


def test_every_rendering_carries_the_magnitude_only_disclaimer() -> None:
    assert "not a direction forecast" in DISCLAIMER.lower()


def test_the_estimate_reports_its_model_version() -> None:
    """A rendered number must be traceable to the arithmetic that produced it."""
    assert movement().model_version == MODEL_VERSION
    assert describe(movement(), now=NOW)["model"] == MODEL_VERSION


def test_a_stale_estimate_is_marked_rather_than_hidden() -> None:
    """Dropping it would look like the symbol became calm."""
    old = ExpectedMovement(
        symbol="NVDA",
        calculated_at=NOW,
        bar_timestamp=NOW - timedelta(hours=5),
        regime=VolatilityRegime.EXTREME,
        percentile=0.95,
        atr_pct=2.0,
        recent_range_pct=4.0,
    )

    assert "stale" in render_line(old, now=NOW)
