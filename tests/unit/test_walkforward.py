"""The walk-forward protocol: the things that make an out-of-sample claim honest.

Every test here targets a specific way this analysis could produce a
confident-looking number that means nothing. None of them test statistics -- they
test that the protocol cannot be bent after the fact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from app.research.analytics import Observation
from app.research.walkforward import (
    BASELINE,
    COMPARISON_BANDS,
    SCORE_GE_75,
    SCORE_GE_85,
    assess_stability,
    bootstrap_difference,
    bootstrap_positive_rate,
    build_folds,
    episodes_for,
    evaluate_fold,
)

START = datetime(2020, 7, 27, tzinfo=UTC)
END = datetime(2024, 7, 31, tzinfo=UTC)


def observation(
    *,
    at: datetime,
    score: float,
    symbol: str = "NVDA",
    raw_return: float = 0.0,
    direction: int = 1,
    sector: str | None = None,
    ident: int = 0,
) -> Observation:
    return Observation(
        evaluation_id=ident or int(at.timestamp()),
        symbol=symbol,
        sector=sector,
        score=score,
        horizon="1d",
        year=at.year,
        raw_return=raw_return,
        mfe=abs(raw_return),
        mae=-abs(raw_return) / 2,
        features={},
        timestamp=at,
        direction=direction,
    )


# ---------------------------------------------------------------------------
# Fold construction
# ---------------------------------------------------------------------------
def test_folds_are_a_pure_function_of_the_window() -> None:
    """**Folds must not be choosable after seeing results.**

    Same arguments, same folds, every time -- so a boundary cannot be nudged
    until a result improves.
    """
    first = build_folds(start=START, end=END, count=9, horizon="1d")
    second = build_folds(start=START, end=END, count=9, horizon="1d")

    assert first == second
    assert len(first) == 9


def test_folds_are_chronological_and_do_not_overlap() -> None:
    folds = build_folds(start=START, end=END, count=8, horizon="5d")

    for earlier, later in pairwise(folds):
        assert earlier.end <= later.start
        assert earlier.start < earlier.end
    assert folds[0].start == START
    assert folds[-1].end == END


def test_each_fold_purges_its_own_outcome_window() -> None:
    """An observation whose return resolves after the fold closes is dropped.

    Keeping it would measure part of its outcome inside the next block -- future
    information crossing the boundary that exists to prevent exactly that.
    """
    folds = build_folds(start=START, end=END, count=8, horizon="5d")

    for fold in folds:
        assert fold.purge_after == fold.end - timedelta(days=5)
        assert not fold.contains(fold.end - timedelta(days=1))
        assert fold.contains(fold.start)


def test_a_longer_horizon_purges_more() -> None:
    short = build_folds(start=START, end=END, count=8, horizon="1d")[0]
    long = build_folds(start=START, end=END, count=8, horizon="20d")[0]

    assert long.purge_after < short.purge_after


def test_folds_shorter_than_the_outcome_window_are_refused() -> None:
    """Rather than silently producing folds that are entirely purge."""
    with pytest.raises(ValueError, match="use fewer folds"):
        build_folds(start=START, end=START + timedelta(days=30), count=10, horizon="20d")


# ---------------------------------------------------------------------------
# Episodes: independence, not row count
# ---------------------------------------------------------------------------
def test_a_run_of_correlated_rows_is_one_episode() -> None:
    """**The correction that matters most.**

    Fifty-two symbols scanned hourly produce long runs of near-identical rows.
    Counting them as independent opportunities is what turns noise into a
    confident-looking win rate.
    """
    rows = [
        observation(at=START + timedelta(hours=index), score=90.0, ident=index)
        for index in range(8)
    ]

    episodes = episodes_for(rows, threshold=85.0)

    assert len(episodes) == 1


def test_a_gap_beyond_the_frozen_definition_starts_a_new_episode() -> None:
    """The 24-hour rule is unchanged and is not re-derived here."""
    rows = [
        observation(at=START, score=90.0, ident=1),
        observation(at=START + timedelta(hours=30), score=90.0, ident=2),
    ]

    assert len(episodes_for(rows, threshold=85.0)) == 2


def test_an_episode_takes_its_first_observation_not_its_best() -> None:
    """A human acts when the setup appears.

    Taking the peak score of a run already known to have worked is the
    retrospective choice that makes any rule look good.
    """
    rows = [
        observation(at=START, score=86.0, raw_return=-1.0, ident=1),
        observation(at=START + timedelta(hours=2), score=99.0, raw_return=5.0, ident=2),
    ]

    ((_, first),) = episodes_for(rows, threshold=85.0)

    assert first.score == 86.0
    assert first.raw_return == -1.0


def test_direction_reversal_is_a_different_opportunity() -> None:
    rows = [
        observation(at=START, score=90.0, direction=1, ident=1),
        observation(at=START + timedelta(hours=1), score=90.0, direction=-1, ident=2),
    ]

    assert len(episodes_for(rows, threshold=85.0)) == 2


def test_episodes_never_span_a_fold_boundary() -> None:
    """Assigned within a fold, so one opportunity cannot count in two blocks."""
    folds = build_folds(start=START, end=END, count=8, horizon="1d")
    boundary = folds[0].end
    rows = [
        observation(at=boundary - timedelta(hours=2), score=90.0, ident=1),
        observation(at=boundary + timedelta(hours=2), score=90.0, ident=2),
    ]

    first = evaluate_fold(rows, folds[0], horizon="1d")
    second = evaluate_fold(rows, folds[1], horizon="1d")

    assert first.bands[SCORE_GE_85].episode_count + second.bands[SCORE_GE_85].episode_count <= 2


# ---------------------------------------------------------------------------
# Grouping: score bands, never the production flag
# ---------------------------------------------------------------------------
def test_grouping_uses_score_bands_not_qualified() -> None:
    """**Required for the coarse window to be analysable at all.**

    `qualified` is structurally false before 2024-08 -- context quality is the
    worst of four timeframes and two do not exist. Grouping on it would compare
    an empty set against everything.
    """
    labels = {label for label, _, _ in COMPARISON_BANDS}

    assert labels == {BASELINE, SCORE_GE_75, SCORE_GE_85}
    assert not any("qualif" in label.lower() for label in labels)


def test_the_high_band_is_a_subset_of_the_lower_one() -> None:
    """Nested, not partitioned: the question is "does raising the bar help?"."""
    folds = build_folds(start=START, end=END, count=8, horizon="1d")
    rows = [
        observation(at=folds[0].start + timedelta(hours=index * 30), score=score, ident=index)
        for index, score in enumerate((70.0, 78.0, 88.0, 92.0))
    ]

    result = evaluate_fold(rows, folds[0], horizon="1d")

    assert result.bands[SCORE_GE_85].episode_count <= result.bands[SCORE_GE_75].episode_count
    assert result.bands[SCORE_GE_75].episode_count >= 1


# ---------------------------------------------------------------------------
# Uncertainty
# ---------------------------------------------------------------------------
def test_a_thin_sample_gets_no_interval_rather_than_a_wrong_one() -> None:
    assert bootstrap_positive_rate([1.0, -1.0]) is None
    assert bootstrap_difference([1.0] * 3, [1.0] * 40) is None


def test_the_interval_is_reproducible() -> None:
    """A seed chosen after seeing the interval would be a way of picking one."""
    values = [1.0, -1.0, 2.0, -0.5, 3.0, -2.0, 0.5, 1.5]

    assert bootstrap_positive_rate(values) == bootstrap_positive_rate(values)


def test_the_interval_brackets_the_observed_rate() -> None:
    values = [1.0] * 12 + [-1.0] * 8  # 60% positive

    interval = bootstrap_positive_rate(values)

    assert interval is not None
    low, high = interval
    assert low <= 0.6 <= high
    assert low < high, "a degenerate interval would hide the uncertainty"


# ---------------------------------------------------------------------------
# Stability: the phase-5.8 failure, made checkable
# ---------------------------------------------------------------------------
def test_an_advantage_carried_by_one_fold_is_flagged() -> None:
    """**Exactly what went wrong in phase 5.8.**

    The >=85 advantage there survived only because a single fold carried it.
    Averaging alone would have called that a positive result.
    """
    folds = build_folds(start=START, end=END, count=4, horizon="1d")
    results = [
        _fold_with_delta(folds[0], 0.60),
        _fold_with_delta(folds[1], -0.02),
        _fold_with_delta(folds[2], -0.03),
        _fold_with_delta(folds[3], -0.01),
    ]

    verdict = assess_stability(results, band=SCORE_GE_85)

    assert verdict.dominated_by_one_fold
    assert not verdict.consistent


def test_a_consistent_advantage_is_not_flagged() -> None:
    folds = build_folds(start=START, end=END, count=4, horizon="1d")
    results = [_fold_with_delta(fold, 0.05 + index * 0.01) for index, fold in enumerate(folds)]

    verdict = assess_stability(results, band=SCORE_GE_85)

    assert not verdict.dominated_by_one_fold
    assert verdict.consistent
    assert verdict.folds_better == 4


def test_stability_reports_nothing_when_nothing_was_measurable() -> None:
    verdict = assess_stability([], band=SCORE_GE_85)

    assert verdict.folds_measured == 0
    assert verdict.median_delta is None
    assert not verdict.consistent


def _fold_with_delta(fold: object, delta: float) -> object:
    """A FoldResult whose >=85 episode rate exceeds baseline by ``delta``."""
    from app.research.analytics import GroupStats
    from app.research.walkforward import BandResult, FoldResult

    def band(label: str, rate: float) -> BandResult:
        stats = GroupStats(label=label, n=20, positive_rate=rate)
        return BandResult(band=label, observations=stats, episodes=stats, episode_count=20)

    return FoldResult(
        fold=fold,  # type: ignore[arg-type]
        horizon="1d",
        bands={
            BASELINE: band(BASELINE, 0.50),
            SCORE_GE_85: band(SCORE_GE_85, 0.50 + delta),
        },
        total_observations=40,
    )
