"""Phase 6 analysis machinery: independence, honesty about thin samples, and a
classification that cannot be talked into a result.

The analysis produced an almost entirely null answer, which is exactly the case
where the machinery must be trustworthy: a bug that *hides* an effect is as bad
as one that invents it, and neither is visible in the output.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from app.research.phase6 import (
    INTERACTIONS,
    MEANINGFUL_SPREAD,
    MIN_EPISODES,
    BucketResult,
    analyse_feature,
    assign_episode_ids,
    classify_feature,
    collapse_to_episodes,
    correlation_matrix,
)

START = datetime(2021, 3, 1, 15, 0, tzinfo=UTC)


def frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def observation(
    *, symbol: str = "NVDA", hours: int = 0, value: float = 1.0, ret: float = 0.01
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "direction": 1,
        "timestamp": START + timedelta(hours=hours),
        "feature": value,
        "raw_return": ret,
        "mfe": abs(ret),
        "mae": -abs(ret),
    }


# ---------------------------------------------------------------------------
# Independence
# ---------------------------------------------------------------------------
def test_a_run_of_hourly_rows_collapses_to_one_episode() -> None:
    """**The correction the whole analysis depends on.**

    Without it, 52 symbols scanned hourly turn a few hundred opportunities into
    hundreds of thousands of "independent" samples and every interval collapses
    to nothing.
    """
    rows = frame([observation(hours=h) for h in range(10)])

    assert collapse_to_episodes(rows).height == 1


def test_a_gap_past_the_frozen_rule_starts_a_new_episode() -> None:
    rows = frame([observation(hours=0), observation(hours=30)])

    assert collapse_to_episodes(rows).height == 2


def test_different_symbols_are_always_different_episodes() -> None:
    rows = frame([observation(symbol="NVDA"), observation(symbol="AMD")])

    assert collapse_to_episodes(rows).height == 2


def test_the_first_observation_represents_the_episode() -> None:
    """First, not best -- taking the best would flatter every rule ever tested."""
    rows = frame([observation(hours=0, ret=-0.05), observation(hours=1, ret=0.20)])

    collapsed = collapse_to_episodes(rows)

    assert collapsed["raw_return"].to_list() == [-0.05]


def test_episode_ids_increase_with_time_and_never_reorder() -> None:
    rows = frame([observation(hours=h) for h in (0, 30, 60)])

    ids = assign_episode_ids(rows)["episode_id"].to_list()

    assert ids == sorted(ids)
    assert len(set(ids)) == 3


# ---------------------------------------------------------------------------
# The classifier is mechanical
# ---------------------------------------------------------------------------
def bucket(rate: float, *, episodes: int = 500, ci: tuple[float, float] | None) -> BucketResult:
    return BucketResult(
        label="Q",
        low=0.0,
        high=1.0,
        observations=episodes,
        episodes=episodes,
        positive_rate=rate,
        mean_return=0.0,
        median_return=0.0,
        mean_mfe=0.0,
        mean_mae=0.0,
        ci=ci,
    )


def test_a_tiny_spread_is_no_information_however_clean_it_looks() -> None:
    """A perfectly monotone 1pp gradient is still not worth acting on."""
    buckets = [bucket(0.50 + i * 0.003, ci=(0.49, 0.51)) for i in range(4)]

    assert classify_feature(buckets, monotone=True, spread=0.009) == "NO_INFORMATION"


def test_a_thin_bucket_makes_the_whole_feature_insufficient() -> None:
    buckets = [
        bucket(0.50, ci=(0.45, 0.55)),
        bucket(0.70, episodes=MIN_EPISODES - 1, ci=None),
        bucket(0.55, ci=(0.50, 0.60)),
        bucket(0.60, ci=(0.55, 0.65)),
    ]

    assert classify_feature(buckets, monotone=False, spread=0.10) == "INSUFFICIENT_SAMPLE"


def test_overlapping_intervals_are_not_evidence_of_separation() -> None:
    """Two buckets that could be the same number are not a finding."""
    buckets = [bucket(0.50, ci=(0.44, 0.56)), bucket(0.58, ci=(0.52, 0.64))]

    assert classify_feature(buckets, monotone=True, spread=0.08) == "NO_INFORMATION"


def test_a_monotone_separated_gradient_is_the_best_available_label() -> None:
    """Even a clean result only reaches PROMISING -- ROBUST needs stability too."""
    buckets = [bucket(0.45, ci=(0.42, 0.48)), bucket(0.62, ci=(0.59, 0.65))]

    assert classify_feature(buckets, monotone=True, spread=0.17) == "PROMISING_BUT_UNSTABLE"


def test_a_separated_but_unordered_pattern_is_regime_dependent() -> None:
    buckets = [bucket(0.45, ci=(0.42, 0.48)), bucket(0.62, ci=(0.59, 0.65))]

    assert classify_feature(buckets, monotone=False, spread=0.17) == "REGIME_DEPENDENT"


def test_the_meaningful_spread_floor_is_economically_motivated() -> None:
    """Below a few points a 'signal' cannot survive costs."""
    assert 0.02 <= MEANINGFUL_SPREAD <= 0.10


# ---------------------------------------------------------------------------
# Guardrails on the analysis itself
# ---------------------------------------------------------------------------
def test_a_sparse_feature_returns_nothing_rather_than_a_confident_nothing() -> None:
    rows = frame([observation(hours=h * 30, value=float(h)) for h in range(5)])

    assert analyse_feature(rows, feature="feature", horizon="1d", stream="test") is None


def test_an_absent_feature_is_not_silently_treated_as_null() -> None:
    rows = frame([observation(hours=h * 30) for h in range(200)])

    assert analyse_feature(rows, feature="does_not_exist", horizon="1d", stream="test") is None


def test_the_interaction_set_is_small_and_predefined() -> None:
    """Brute-forcing pairs guarantees a winner and means nothing."""
    assert len(INTERACTIONS) <= 6
    assert len(set(INTERACTIONS)) == len(INTERACTIONS)


def test_correlation_needs_enough_overlap_to_be_meaningful() -> None:
    rows = frame([observation(hours=h * 30, value=float(h)) for h in range(3)])

    assert correlation_matrix(rows, ["feature", "raw_return"]) == []


def test_correlation_finds_a_near_duplicate() -> None:
    """Part H exists to stop five weights being given to one variable."""
    rows = pl.DataFrame(
        {
            "a": [float(i) for i in range(100)],
            "b": [float(i) * 2.0 + 1.0 for i in range(100)],
        }
    )

    pairs = correlation_matrix(rows, ["a", "b"])

    assert pairs
    assert pairs[0][2] > 0.99
