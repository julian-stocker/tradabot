"""Phase 7: cross-sectional ranking and exit simulation.

Two things must be true for the phase-7 numbers to mean anything: a rank must be
computed against *the same instant's* peers, and a simulated exit must never
resolve an ambiguous bar in its own favour. Both are easy to get subtly wrong and
neither shows up in the output when you do.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from app.research.phase7 import (
    MIN_CROSS_SECTION,
    RANK_BUCKETS,
    RANKING_DIMENSIONS,
    add_cross_sectional_rank,
    classify_regimes,
)
from app.research.position_management import (
    EXIT_FAMILIES,
    INITIAL_STOPS,
    MAX_HOLDING_BARS,
    Bar,
    TradeResult,
    simulate_trade,
    summarise,
)

NOW = datetime(2022, 6, 1, 15, 0, tzinfo=UTC)


def cross_section(values: list[float], *, at: datetime = NOW) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [f"S{i}" for i in range(len(values))],
            "timestamp": [at] * len(values),
            "feature": values,
        }
    )


# ---------------------------------------------------------------------------
# Cross-sectional ranking
# ---------------------------------------------------------------------------
def test_rank_is_computed_within_a_timestamp() -> None:
    """**The whole premise.** A later cross-section must not shift an earlier rank.

    If ranking leaked across time, "today's leaders" would be chosen partly with
    tomorrow's information and the result would be guaranteed and worthless.
    """
    early = cross_section([float(i) for i in range(30)], at=NOW)
    late = cross_section([float(100 - i) for i in range(30)], at=NOW + timedelta(hours=1))
    combined = pl.concat([early, late])

    ranked = add_cross_sectional_rank(combined, feature="feature")

    early_only = add_cross_sectional_rank(early, feature="feature")
    assert (
        ranked.filter(pl.col("timestamp") == NOW).sort("symbol")["cross_rank"].to_list()
        == early_only.sort("symbol")["cross_rank"].to_list()
    )


def test_the_largest_value_ranks_at_the_top() -> None:
    ranked = add_cross_sectional_rank(
        cross_section([float(i) for i in range(40)]), feature="feature"
    )

    best = ranked.sort("feature").tail(1)
    assert best["cross_rank"][0] > 0.9


def test_a_thin_cross_section_is_dropped_rather_than_ranked() -> None:
    """A 'top 10%' drawn from six names is the top one name."""
    thin = cross_section([1.0, 2.0, 3.0])

    assert add_cross_sectional_rank(thin, feature="feature").height == 0
    assert MIN_CROSS_SECTION >= 10


def test_the_rank_buckets_are_declared_and_nested() -> None:
    labels = [label for label, _, _ in RANK_BUCKETS]

    assert "top 10%" in labels
    assert "bottom 20%" in labels
    top10 = next(b for b in RANK_BUCKETS if b[0] == "top 10%")
    top20 = next(b for b in RANK_BUCKETS if b[0] == "top 20%")
    assert top20[1] < top10[1], "top 10% must sit inside top 20%"


def test_the_ranking_dimensions_are_a_small_declared_set() -> None:
    """Phase 6 found correlations to 0.98; adding more re-tests one variable."""
    assert len(RANKING_DIMENSIONS) <= 12
    assert len(set(RANKING_DIMENSIONS)) == len(RANKING_DIMENSIONS)


def test_regime_labels_partition_every_observation() -> None:
    frame = pl.DataFrame({"ema50_slope_pct": [float(i) - 50 for i in range(100)]})

    labelled = classify_regimes(frame)

    assert set(labelled["regime"].unique().to_list()) <= {"TRENDING", "RANGING", "UNCERTAIN"}
    assert labelled["regime"].null_count() == 0


# ---------------------------------------------------------------------------
# Exit simulation
# ---------------------------------------------------------------------------
def flat(n: int, price: float = 100.0) -> list[Bar]:
    return [Bar(price, price, price) for _ in range(n)]


def test_an_ambiguous_bar_resolves_against_the_trade() -> None:
    """**The single easiest way to fake a profitable backtest.**

    When one bar's range contains both the stop and the target, intrabar order is
    unknowable from OHLC. Taking the target would inflate every result; this
    takes the stop.
    """
    bars = [Bar(high=130.0, low=70.0, close=100.0)]

    result = simulate_trade(entry=100.0, atr=10.0, bars=bars, stop_multiple=1.5, family="fixed_2R")

    assert result.exit_reason == "STOP"
    assert result.gross_return_pct < 0


def test_a_stop_is_hit_when_price_falls_through_it() -> None:
    bars = [Bar(101.0, 99.0, 100.0), Bar(100.0, 80.0, 82.0)]

    result = simulate_trade(entry=100.0, atr=5.0, bars=bars, stop_multiple=2.0, family="time_only")

    assert result.exit_reason == "STOP"
    assert result.gross_return_pct == pytest.approx(-10.0)


def test_a_trailing_stop_only_ever_ratchets_upward() -> None:
    """A stop that could loosen would be re-underwriting a losing trade."""
    rising = [Bar(100.0 + i * 2, 99.0 + i * 2, 100.0 + i * 2) for i in range(20)]
    then_collapse = [*rising, Bar(138.0, 100.0, 101.0)]

    result = simulate_trade(
        entry=100.0, atr=4.0, bars=then_collapse, stop_multiple=2.5, family="trail_atr_3"
    )

    assert result.exit_reason == "STOP"
    assert result.gross_return_pct > 0, "the ratcheted stop should have locked in a profit"


def test_a_position_is_closed_at_the_holding_limit() -> None:
    result = simulate_trade(
        entry=100.0,
        atr=5.0,
        bars=flat(MAX_HOLDING_BARS + 50),
        stop_multiple=2.0,
        family="time_only",
    )

    assert result.exit_reason == "TIME"
    assert result.bars_held == MAX_HOLDING_BARS


def test_a_partial_take_moves_the_remainder_to_break_even() -> None:
    """Otherwise 'partial profit' would still allow a full-size loss."""
    up_then_down = [Bar(112.0, 100.0, 111.0), Bar(111.0, 80.0, 85.0)]

    result = simulate_trade(
        entry=100.0, atr=10.0, bars=up_then_down, stop_multiple=1.0, family="partial_1R_trail"
    )

    assert result.gross_return_pct > 0, "half was banked at +1R and the rest stopped at entry"


def test_degenerate_geometry_is_refused_not_guessed() -> None:
    assert (
        simulate_trade(
            entry=100.0, atr=0.0, bars=flat(10), stop_multiple=2.0, family="time_only"
        ).exit_reason
        == "NO_DATA"
    )
    assert (
        simulate_trade(
            entry=100.0, atr=5.0, bars=[], stop_multiple=2.0, family="time_only"
        ).exit_reason
        == "NO_DATA"
    )


def test_the_exit_grid_is_small_and_declared() -> None:
    """Fifteen combinations, fixed before they were run."""
    assert len(INITIAL_STOPS) * len(EXIT_FAMILIES) <= 20


# ---------------------------------------------------------------------------
# Expectancy accounting
# ---------------------------------------------------------------------------
def test_costs_are_charged_per_trade_not_once() -> None:
    """A high-turnover rule must be penalised as often as it trades."""
    results = [TradeResult("TIME", 1.0, 10, 1.0, -1.0) for _ in range(100)]

    stats = summarise(results, label="x", cost_pct=0.20)

    assert stats.gross_expectancy == 1.0
    assert abs(stats.net_expectancy - 0.80) < 1e-9


def test_a_strategy_profitable_gross_can_fail_net() -> None:
    """The phase-7 gate: gross profit is not a result."""
    results = [TradeResult("TIME", 0.10, 5, 0.5, -0.5) for _ in range(50)]

    stats = summarise(results, label="x", cost_pct=0.20)

    assert stats.gross_expectancy > 0
    assert stats.net_expectancy < 0


def test_profit_factor_and_drawdown_are_reported() -> None:
    results = [
        TradeResult("TIME", 2.0, 5, 2.0, 0.0),
        TradeResult("STOP", -1.0, 5, 0.0, -1.0),
        TradeResult("TIME", 1.0, 5, 1.0, 0.0),
    ]

    stats = summarise(results, label="x", cost_pct=0.0)

    assert stats.profit_factor > 1.0
    assert stats.max_drawdown <= 0.0
    assert stats.trades == 3
