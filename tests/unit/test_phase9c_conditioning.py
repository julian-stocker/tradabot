"""Phase 9C machinery: the comparison must be fair and the gate must not bend.

These do not test whether volatility conditioning *works* -- that is the phase's
finding, and a test asserting an outcome would be assuming the answer. They test
the things that would make a positive result untrustworthy: a conditional
measurement taken on different bucketing than its own baseline, a match that
quietly drops rows, a verdict that can be reached without the evidence the brief
requires, and a cost check flattered by quoting raw return instead of edge.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import polars as pl
import pytest

from app.research.phase9c import (
    COST_SCENARIOS,
    DIRECTIONAL_FEATURES,
    ELEVATED_REGIMES,
    EXTENSION_BUCKETS,
    MATCHES,
    MIN_EPISODES_FOR_CLAIM,
    REGIME_LABELS,
    analyse_conditional,
    analyse_extension,
    analyse_matches,
    classify_candidate,
    fixed_band_coverage,
    match_masks,
    measure_calibration,
    survives_costs,
)

START = datetime(2023, 1, 3, 14, 0, tzinfo=UTC)


def dataset(n: int = 2400, *, seed: int = 3) -> pl.DataFrame:
    """Rows shaped like a loaded, regime-labelled phase-6 dataset."""
    rng = random.Random(seed)
    regimes = [rng.choice(REGIME_LABELS) for _ in range(n)]
    return pl.DataFrame(
        {
            "symbol": [f"S{i % 40}" for i in range(n)],
            "direction": ["LONG"] * n,
            "timestamp": [START + timedelta(hours=i) for i in range(n)],
            "year": [2023 + (i % 3) for i in range(n)],
            "vol_regime": regimes,
            "raw_return": [rng.gauss(0, 2) for _ in range(n)],
            "mfe": [abs(rng.gauss(1, 1)) for _ in range(n)],
            "mae": [-abs(rng.gauss(1, 1)) for _ in range(n)],
            "bars_above_ema50": [rng.randint(0, 90) for _ in range(n)],
            "dist_ema20_atr": [rng.gauss(0, 1.6) for _ in range(n)],
            "relative_strength_market_1d": [rng.gauss(0, 1) for _ in range(n)],
            "relative_strength_sector_1d": [rng.gauss(0, 1) for _ in range(n)],
            "index_above_ema50": [rng.random() > 0.4 for _ in range(n)],
            "sector_above_ema50": [rng.random() > 0.4 for _ in range(n)],
            "trend_stacked": [rng.random() > 0.5 for _ in range(n)],
            "realised_range_pct": [abs(rng.gauss(2.0, 0.8)) for _ in range(n)],
        }
    )


# ---------------------------------------------------------------------------
# The hypotheses are frozen
# ---------------------------------------------------------------------------
def test_the_feature_set_is_small_and_previously_researched() -> None:
    """The brief forbids inventing a feature zoo to search for an edge."""
    assert len(DIRECTIONAL_FEATURES) <= 12
    assert len(DIRECTIONAL_FEATURES) == len(set(DIRECTIONAL_FEATURES))


def test_all_five_briefed_matches_are_declared() -> None:
    assert set(MATCHES) == {
        "A_vol_and_trend",
        "B_vol_and_market_rs",
        "C_vol_and_sector_rs",
        "D_vol_and_full_alignment",
        "E_vol_and_extended",
    }


def test_elevated_means_the_top_two_phase_eight_bands() -> None:
    """Reusing phase 8's bands is what keeps the two phases comparable."""
    assert ELEVATED_REGIMES == ("HIGH_VOL", "EXTREME_VOL")
    assert set(ELEVATED_REGIMES).issubset(set(REGIME_LABELS))


def test_extension_buckets_are_symmetric_around_the_mean() -> None:
    """Testing only the upper tail would make the asymmetry question unanswerable."""
    lows = [low for _, low, _ in EXTENSION_BUCKETS]
    highs = [high for _, _, high in EXTENSION_BUCKETS]
    assert min(lows) == -max(highs)
    assert any(low < 0 for _, low, _ in EXTENSION_BUCKETS)
    assert any(high > 0 for _, _, high in EXTENSION_BUCKETS)


def test_extension_buckets_partition_without_overlap() -> None:
    edges = [(low, high) for _, low, high in EXTENSION_BUCKETS]
    for (_, upper), (lower, _) in pairwise(edges):
        assert upper == lower


# ---------------------------------------------------------------------------
# Part C fairness
# ---------------------------------------------------------------------------
def test_conditional_and_unconditional_share_one_baseline() -> None:
    """Every regime row must compare against the *same* unconditional result."""
    results = analyse_conditional(
        dataset(), feature="bars_above_ema50", horizon="1d", stream="test"
    )
    assert results
    baselines = {id(r.unconditional) for r in results}
    assert len(baselines) == 1


def test_every_regime_plus_elevated_is_reported() -> None:
    results = analyse_conditional(
        dataset(), feature="bars_above_ema50", horizon="1d", stream="test"
    )
    assert [r.regime for r in results] == [*REGIME_LABELS, "ELEVATED"]


def test_improvement_compares_magnitudes_not_signs() -> None:
    """A feature that reverses under conditioning is unstable, not improved."""
    results = analyse_conditional(
        dataset(), feature="bars_above_ema50", horizon="1d", stream="test"
    )
    for result in results:
        if result.improvement is None:
            continue
        assert result.unconditional is not None
        assert result.conditional is not None
        expected = abs(result.conditional.spread or 0) - abs(result.unconditional.spread or 0)
        assert result.improvement == pytest.approx(expected)


def test_a_frame_without_regimes_yields_nothing() -> None:
    """Better empty than a "conditional" result computed on unlabelled rows."""
    assert (
        analyse_conditional(
            dataset().drop("vol_regime"), feature="bars_above_ema50", horizon="1d", stream="test"
        )
        == []
    )


def test_a_thin_regime_cannot_earn_a_verdict() -> None:
    """Conditioning shrinks samples fast; a spread on 40 episodes is noise."""
    results = analyse_conditional(
        dataset(n=120), feature="dist_ema20_atr", horizon="1d", stream="t"
    )
    for result in results:
        if result.episodes < MIN_EPISODES_FOR_CLAIM:
            assert result.verdict == "INSUFFICIENT"


# ---------------------------------------------------------------------------
# Part D
# ---------------------------------------------------------------------------
def test_every_match_requires_elevated_volatility() -> None:
    """All five are conditional on volatility; none may fire in a calm regime."""
    frame = dataset()
    for name, mask in match_masks(frame).items():
        selected = frame.filter(mask)
        if selected.is_empty():
            continue
        regimes = set(selected["vol_regime"].to_list())
        assert regimes.issubset(set(ELEVATED_REGIMES)), f"{name} fired outside elevated"


def test_the_baseline_is_reported_beside_the_matches() -> None:
    """A match delivering 53% means nothing without the base rate next to it."""
    labels = [b.label for b in analyse_matches(dataset())]
    assert labels[0].startswith("baseline")
    assert "elevated volatility only" in labels


def test_extension_reports_both_tails() -> None:
    buckets = analyse_extension(dataset(), elevated_only=True)
    labels = " ".join(b.label for b in buckets)
    assert "below" in labels
    assert "extended" in labels


def test_extension_carries_mfe_and_mae() -> None:
    """The asymmetry claim cannot be judged from a positive rate alone."""
    for bucket in analyse_extension(dataset(), elevated_only=False):
        assert bucket.mean_mfe is not None
        assert bucket.mean_mae is not None


# ---------------------------------------------------------------------------
# Part E
# ---------------------------------------------------------------------------
def test_calibration_uses_the_figures_it_is_given() -> None:
    """Passed in, not imported: a model must not be scored against numbers
    derived from the same rows it is being tested on."""
    frame = dataset()
    coverage = measure_calibration(frame, {"LOW_VOL": (1.82, 3.85)}, multipliers=(1.0,))
    assert len(coverage) == 2
    assert {c.basis for c in coverage} == {"typical", "stress"}
    assert coverage[0].predicted_pct == pytest.approx(1.82)


def test_coverage_is_a_fraction_of_the_regimes_own_rows() -> None:
    frame = dataset()
    coverage = measure_calibration(frame, {"HIGH_VOL": (2.30, 4.82)}, multipliers=(2.0,))
    expected = frame.filter(pl.col("vol_regime") == "HIGH_VOL").height
    assert coverage[0].observations == expected
    assert 0.0 <= coverage[0].coverage <= 1.0


def test_the_fixed_band_control_exists() -> None:
    """Regime-aware bands are only worth anything against a single-number control."""
    control = fixed_band_coverage(dataset(), bands_pct=[2.05])
    assert len(control) == 1
    band, contained, fraction = control[0]
    assert band == 2.05
    assert 0.0 <= fraction <= 1.0
    assert contained >= 0


# ---------------------------------------------------------------------------
# Parts G and I: the gate
# ---------------------------------------------------------------------------
def test_costs_are_judged_on_edge_not_raw_return() -> None:
    """A subgroup returning 0.30% where the universe returns 0.28% has a 0.02pp
    advantage. Quoting the 0.30% against 20 bps would flatter it tenfold."""
    assert survives_costs(0.30, edge_over_baseline=0.0002) == "CONSUMED_BY_20_BPS"
    assert survives_costs(0.30, edge_over_baseline=0.010) == "SURVIVES_50_BPS"


def test_an_unknown_return_is_not_quietly_passed() -> None:
    assert survives_costs(None, edge_over_baseline=None) == "UNKNOWN"


def test_cost_scenarios_match_the_brief() -> None:
    assert [c for _, c in COST_SCENARIOS] == [0.0, 0.20, 0.50]


def test_robust_requires_every_condition_at_once() -> None:
    """The gate from the brief, applied mechanically so no table can argue past it."""
    passing = {
        "episodes": 5_000,
        "edge_pp": 0.08,
        "sign_stable": True,
        "cost_verdict": "SURVIVES_50_BPS",
    }
    assert classify_candidate(**passing) == "ROBUST"

    assert classify_candidate(**{**passing, "episodes": 10}) == "INSUFFICIENT"
    assert classify_candidate(**{**passing, "edge_pp": 0.01}) == "NO_INFORMATION"
    assert classify_candidate(**{**passing, "sign_stable": False}) == "NO_EDGE"
    assert classify_candidate(**{**passing, "cost_verdict": "CONSUMED_BY_20_BPS"}) == "NO_EDGE"


def test_a_large_but_unstable_effect_is_not_robust() -> None:
    """The phase-6 and 9A lesson: a pooled result that reverses is not an edge."""
    assert (
        classify_candidate(
            episodes=50_000, edge_pp=0.25, sign_stable=False, cost_verdict="SURVIVES_50_BPS"
        )
        == "NO_EDGE"
    )
