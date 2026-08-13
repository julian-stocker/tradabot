"""Phase 8: volatility regimes, causal ranges, and breakout triggers.

The phase-8 conclusion rests on cohort comparisons, so the cohort definitions
must be exactly what they claim. In particular a "range entry" that quietly used
a hindsight low, or a breakout that could break its own high, would manufacture
the result rather than measure it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise

import polars as pl

from app.research.phase8 import (
    COST_SCENARIOS,
    MAX_RANGE_TREND_SLOPE,
    RANGE_ENTRY_ZONE,
    VOL_REGIMES,
    add_volatility_regime,
    breakout_entries,
    range_entries,
)

NOW = datetime(2022, 4, 1, 15, 0, tzinfo=UTC)


def rows(**columns: list[object]) -> pl.DataFrame:
    return pl.DataFrame(columns)


def test_the_regime_bands_partition_the_percentile_range() -> None:
    """No gap and no overlap, or observations vanish or get counted twice."""
    edges = [(low, high) for _, low, high in VOL_REGIMES]

    assert edges[0][0] == 0.0
    assert edges[-1][1] > 1.0
    for (_, upper), (lower, _) in pairwise(edges):
        assert upper == lower


def test_a_stock_is_classified_against_its_own_history() -> None:
    """Relative, not absolute: 2% is ordinary for a chip name and wild for a utility."""
    frame = rows(atr_pct_percentile=[0.05, 0.50, 0.80, 0.97])

    labelled = add_volatility_regime(frame)

    assert labelled["vol_regime"].to_list() == [
        "LOW_VOL",
        "NORMAL_VOL",
        "HIGH_VOL",
        "EXTREME_VOL",
    ]


def test_an_unknown_percentile_is_not_silently_called_normal() -> None:
    labelled = add_volatility_regime(rows(atr_pct_percentile=[None]))

    assert labelled["vol_regime"].to_list() == [None]


def test_a_breakout_needs_the_prior_window_flag() -> None:
    """`breakout_20` is computed with a shifted window upstream, so a bar cannot
    break a range it is itself the top of."""
    frame = rows(breakout_20=[True, False, True])

    assert breakout_entries(frame).height == 2


def test_a_frame_without_breakout_information_yields_no_entries() -> None:
    assert breakout_entries(rows(close=[1.0, 2.0])).height == 0


def test_a_range_entry_uses_a_prior_range_never_a_hindsight_low() -> None:
    """**The rule the brief is emphatic about.**

    The range comes from `prior_high_20`/`prior_low_20`, both of which exclude
    the current bar. Nothing here knows where the eventual bottom was.
    """
    frame = rows(
        prior_high_20=[110.0, 110.0, 110.0],
        prior_low_20=[100.0, 100.0, 100.0],
        # 20% into the range, 50% in, and below the range entirely.
        close=[102.0, 105.0, 99.0],
        ema50_slope_pct=[0.0, 0.0, 0.0],
    )

    selected = range_entries(frame)

    assert selected.height == 1
    assert selected["close"].to_list() == [102.0]


def test_a_trending_window_is_not_treated_as_a_range() -> None:
    """Mean reversion inside a strong trend is a different, unasked experiment."""
    frame = rows(
        prior_high_20=[110.0, 110.0],
        prior_low_20=[100.0, 100.0],
        close=[102.0, 102.0],
        ema50_slope_pct=[0.0, MAX_RANGE_TREND_SLOPE * 5],
    )

    assert range_entries(frame).height == 1


def test_a_degenerate_range_is_rejected() -> None:
    frame = rows(
        prior_high_20=[100.0],
        prior_low_20=[100.0],
        close=[100.0],
        ema50_slope_pct=[0.0],
    )

    assert range_entries(frame).height == 0


def test_the_entry_zone_is_a_declared_lower_fraction() -> None:
    assert 0.0 < RANGE_ENTRY_ZONE <= 0.5


def test_cost_scenarios_span_modelled_to_extreme() -> None:
    """Part L: a strategy that dies at realistic costs must be seen to die."""
    costs = [cost for _, cost in COST_SCENARIOS]

    assert min(costs) <= 0.20
    assert max(costs) >= 1.00
    assert costs == sorted(costs)
