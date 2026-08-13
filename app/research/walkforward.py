"""Chronological out-of-sample validation of a **frozen** scoring rule.

There is no training step here, and calling this "walk-forward" is a statement
about the *evaluation*, not about a fit. The thresholds (75, 85), the signal
model, the feature set and the episode definition are all frozen before any of
this runs. What walks forward is the question: in each successive block of
history, held out from every other, does a higher score band actually produce
better outcomes than the baseline?

That framing matters because it removes the usual walk-forward temptation. There
is no parameter to re-fit per fold, so there is nothing to overfit *within* a
fold. The remaining ways to fool yourself are structural, and each has a
countermeasure here:

* **Fold boundaries chosen after seeing results.** Folds are equal-duration
  blocks derived from the window alone -- :func:`build_folds` never sees an
  outcome, and its output is a pure function of (start, end, count).
* **Outcome windows crossing a boundary.** An observation near a fold's end has
  its return measured after the fold closes, partly in the next block. Those
  observations are **purged** rather than kept, so each fold's evidence resolves
  inside the fold.
* **Correlated observations counted as independent.** Fifty-two symbols scanned
  hourly produce runs of near-identical rows. Episodes collapse each run to one
  opportunity, and are assigned **within** a fold so no episode spans a boundary.

Score bands, not ``qualified``
------------------------------
For the coarse long-history window the production ``qualified`` flag is
structurally false -- context quality is the worst of four timeframes and two of
them do not exist before 2024-08. Grouping on it there would compare an empty set
against everything. So the grouping variable is the **score band**, which is
computed from the primary timeframe alone and therefore means the same thing in
both windows. See :mod:`app.backtesting.modes`.

The bands are named ``SCORE_GE_75`` / ``SCORE_GE_85`` rather than "qualified" on
purpose: a historical row that scored 86 was never *qualified* by the production
rule, and blurring the two would be a claim about the scanner that the data
cannot support.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from app.research.analytics import GroupStats, Observation, summarise
from app.research.episodes import MAX_EPISODE_GAP, EpisodeKey, assign_episodes

THRESHOLD_75: Final = 75.0
THRESHOLD_85: Final = 85.0
"""The frozen production thresholds, named so analysis code refers to them
rather than repeating the literals. **Not tunable** -- they are inputs to this
measurement, never outputs of it."""

BASELINE: Final = "baseline_lt_75"
SCORE_GE_75: Final = "SCORE_GE_75"
SCORE_GE_85: Final = "SCORE_GE_85"

COMPARISON_BANDS: Final[tuple[tuple[str, float, float], ...]] = (
    (BASELINE, float("-inf"), THRESHOLD_75),
    (SCORE_GE_75, THRESHOLD_75, float("inf")),
    (SCORE_GE_85, THRESHOLD_85, float("inf")),
)
"""The three aggregates the decision rests on.

Deliberately overlapping: ``SCORE_GE_85`` is a subset of ``SCORE_GE_75``, because
the operational question is "does raising the bar help?" and that is answered by
nesting, not by partitioning.
"""

DETAIL_BANDS: Final[tuple[tuple[str, float, float], ...]] = (
    ("<60", float("-inf"), 60.0),
    ("60-70", 60.0, 70.0),
    ("70-75", 70.0, 75.0),
    ("75-80", 75.0, 80.0),
    ("80-85", 80.0, 85.0),
    (">=85", 85.0, float("inf")),
)
"""Non-overlapping bands, for reading the shape of the relationship."""

HORIZON_WINDOWS: Final[dict[str, timedelta]] = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "5d": timedelta(days=5),
    "20d": timedelta(days=20),
}
"""How far past an observation its outcome is measured. Drives the purge."""

BOOTSTRAP_RESAMPLES: Final = 2000
BOOTSTRAP_SEED: Final = 20260812
"""Fixed, so a quoted interval is reproducible.

A seed chosen after seeing the interval would be a way of picking one, which is
why it is a module constant rather than a parameter with a tempting default.
"""

MIN_FOLD_EPISODES: Final = 5
"""Below this an episode-level fold result is reported with its n and not read.

Not a filter -- the fold is still shown. Hiding thin folds would make the
protocol look steadier than it is, which is the specific failure this whole
module exists to avoid.
"""


@dataclass(frozen=True, slots=True)
class Fold:
    """One chronological test block."""

    index: int
    start: datetime
    end: datetime
    purge_after: datetime
    """Observations at or after this instant are dropped from the fold.

    Their outcome window would resolve past ``end``, i.e. inside the next fold's
    territory, so counting them here would leak future information across a
    boundary that exists precisely to prevent it.
    """

    @property
    def label(self) -> str:
        return f"F{self.index} {self.start:%Y-%m}>{self.end:%Y-%m}"

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.purge_after


def build_folds(*, start: datetime, end: datetime, count: int, horizon: str) -> list[Fold]:
    """Equal-duration chronological blocks. **Never sees an outcome.**

    A pure function of the window, the fold count and the horizon's outcome
    window, so the same arguments always produce the same folds and no fold
    boundary can be nudged after a result is seen.

    Raises:
        ValueError: on a non-positive count, or a window too short to purge.
    """
    if count < 1:
        msg = f"count must be >= 1, got {count}"
        raise ValueError(msg)
    if end <= start:
        msg = "end must be after start"
        raise ValueError(msg)

    purge = HORIZON_WINDOWS.get(horizon, timedelta(days=5))
    span = (end - start) / count
    if span <= purge:
        msg = (
            f"each fold would be {span} long, shorter than the {horizon} outcome "
            f"window ({purge}); use fewer folds"
        )
        raise ValueError(msg)

    folds: list[Fold] = []
    for index in range(count):
        fold_start = start + span * index
        fold_end = start + span * (index + 1)
        folds.append(
            Fold(
                index=index + 1,
                start=fold_start,
                end=fold_end,
                purge_after=fold_end - purge,
            )
        )
    return folds


@dataclass(frozen=True, slots=True)
class BandResult:
    """One score band's outcome within one fold, at both levels of independence."""

    band: str
    observations: GroupStats
    episodes: GroupStats
    episode_count: int

    @property
    def is_thin(self) -> bool:
        return self.episode_count < MIN_FOLD_EPISODES


@dataclass(frozen=True, slots=True)
class FoldResult:
    """Every band's result for one fold."""

    fold: Fold
    horizon: str
    bands: dict[str, BandResult]
    total_observations: int

    def delta_positive_rate(self, band: str) -> float | None:
        """Band positive rate minus baseline. ``None`` when either is unmeasured."""
        target = self.bands.get(band)
        base = self.bands.get(BASELINE)
        if target is None or base is None:
            return None
        if target.episodes.positive_rate is None or base.episodes.positive_rate is None:
            return None
        return target.episodes.positive_rate - base.episodes.positive_rate


def episodes_for(
    rows: Sequence[Observation], *, threshold: float, max_gap: timedelta = MAX_EPISODE_GAP
) -> list[tuple[EpisodeKey, Observation]]:
    """Collapse correlated rows into one opportunity each, by **score band**.

    Reuses :func:`~app.research.episodes.assign_episodes` unchanged -- the 24-hour
    gap rule and the direction-reversal rule are the frozen definition and are not
    touched here. What changes is only *which observations count as members*: the
    production flag would be structurally false across the whole coarse window, so
    membership is "score at or above this band" instead.

    Returns the **first** observation of each episode. First, not best: a human
    reading the channel acts on the setup when it appears, and taking the peak
    score of a run that is already known to have worked is the retrospective
    choice that makes any rule look good.
    """
    ordered = sorted(rows, key=_timestamp_of)
    members = [_as_episode_row(row, threshold) for row in ordered]
    keys = assign_episodes(members, max_gap=max_gap, qualified_only=True)

    first: dict[str, tuple[EpisodeKey, Observation]] = {}
    for key, row in zip(keys, ordered, strict=True):
        if row.score < threshold:
            continue
        identity = key.as_str()
        if identity not in first:
            first[identity] = (key, row)
    return list(first.values())


def evaluate_fold(
    rows: Sequence[Observation],
    fold: Fold,
    *,
    horizon: str,
    bands: tuple[tuple[str, float, float], ...] = COMPARISON_BANDS,
) -> FoldResult:
    """Score bands within one fold, purged and episode-collapsed."""
    inside = [row for row in rows if fold.contains(_timestamp_of(row))]

    results: dict[str, BandResult] = {}
    for label, low, high in bands:
        selected = [row for row in inside if low <= row.score < high]
        collapsed = episodes_for(selected, threshold=low) if low > float("-inf") else []
        if not collapsed:
            # The baseline band has no lower bound to form episodes from, so it
            # is collapsed on its own membership instead of a threshold.
            collapsed = _collapse_baseline(selected)
        episode_rows = [row for _, row in collapsed]
        results[label] = BandResult(
            band=label,
            observations=summarise(selected, label=f"{label} (obs)"),
            episodes=summarise(episode_rows, label=f"{label} (episodes)"),
            episode_count=len(episode_rows),
        )

    return FoldResult(fold=fold, horizon=horizon, bands=results, total_observations=len(inside))


def bootstrap_positive_rate(
    values: Sequence[float],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float] | None:
    """Percentile bootstrap interval for a positive rate. ``None`` when too thin.

    Resampled over **episodes**, which is the level at which the observations are
    approximately independent. Bootstrapping raw observations would treat forty
    hourly readings of one setup as forty opportunities and report an interval
    several times too narrow -- the specific way this analysis could produce a
    confident-looking number that means nothing.
    """
    if len(values) < MIN_FOLD_EPISODES:
        return None

    rng = random.Random(seed)
    size = len(values)
    rates: list[float] = []
    for _ in range(resamples):
        sample = [values[rng.randrange(size)] for _ in range(size)]
        rates.append(sum(1 for value in sample if value > 0) / size)
    rates.sort()
    return rates[int(0.025 * resamples)], rates[int(0.975 * resamples) - 1]


def bootstrap_difference(
    band_values: Sequence[float],
    baseline_values: Sequence[float],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float] | None:
    """Interval for (band positive rate - baseline positive rate).

    The difference is the decision-relevant quantity, and its interval is not
    recoverable from the two separate intervals: overlapping intervals do not
    imply an interval on the difference that contains zero. Resampled jointly so
    the answer is about the comparison rather than about each side.
    """
    if len(band_values) < MIN_FOLD_EPISODES or len(baseline_values) < MIN_FOLD_EPISODES:
        return None

    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(resamples):
        band = [band_values[rng.randrange(len(band_values))] for _ in range(len(band_values))]
        base = [
            baseline_values[rng.randrange(len(baseline_values))]
            for _ in range(len(baseline_values))
        ]
        band_rate = sum(1 for value in band if value > 0) / len(band)
        base_rate = sum(1 for value in base if value > 0) / len(base)
        deltas.append(band_rate - base_rate)
    deltas.sort()
    return deltas[int(0.025 * resamples)], deltas[int(0.975 * resamples) - 1]


@dataclass(frozen=True, slots=True)
class StabilityVerdict:
    """How consistently a band beat the baseline across folds."""

    band: str
    folds_measured: int
    folds_better: int
    folds_worse: int
    dominated_by_one_fold: bool
    median_delta: float | None

    @property
    def consistent(self) -> bool:
        """Better in a clear majority of measurable folds."""
        if self.folds_measured < 3:  # noqa: PLR2004
            return False
        return self.folds_better >= (self.folds_measured * 2 + 2) // 3


def assess_stability(results: Sequence[FoldResult], *, band: str) -> StabilityVerdict:
    """Whether an advantage persists, or lives in one lucky block.

    ``dominated_by_one_fold`` is the phase-5.8 finding made checkable: the ≥85
    advantage there survived only because a single fold carried it. Removing the
    largest contributor and asking whether the sign flips is a blunt test, and a
    blunt test that would have caught the thing that actually went wrong.
    """
    deltas = [
        delta
        for delta in (result.delta_positive_rate(band) for result in results)
        if delta is not None
    ]
    if not deltas:
        return StabilityVerdict(band, 0, 0, 0, dominated_by_one_fold=False, median_delta=None)

    better = sum(1 for delta in deltas if delta > 0)
    worse = sum(1 for delta in deltas if delta < 0)
    overall = statistics.fmean(deltas)

    dominated = False
    if len(deltas) > 1 and overall > 0:
        strongest = max(deltas)
        without = statistics.fmean([d for d in deltas if d != strongest] or [0.0])
        dominated = without <= 0

    return StabilityVerdict(
        band=band,
        folds_measured=len(deltas),
        folds_better=better,
        folds_worse=worse,
        dominated_by_one_fold=dominated,
        median_delta=statistics.median(deltas),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _EpisodeRow:
    """Adapts an :class:`Observation` to the episode protocol.

    Plain mutable fields, deliberately: the protocol declares mutable
    attributes, so neither a read-only property nor a frozen dataclass
    satisfies it under ``mypy --strict``. Nothing mutates one of these.

    The adapter exists so the frozen episode definition is reused verbatim
    rather than reimplemented with a different membership rule.
    """

    symbol: str
    direction: int
    timestamp: datetime
    qualified: bool
    """Band membership, **not** the production qualification flag."""


def _as_episode_row(row: Observation, threshold: float) -> _EpisodeRow:
    return _EpisodeRow(
        symbol=row.symbol,
        direction=row.direction,
        timestamp=_timestamp_of(row),
        qualified=row.score >= threshold,
    )


def _collapse_baseline(rows: Sequence[Observation]) -> list[tuple[EpisodeKey, Observation]]:
    """Episodes for the sub-75 baseline, where every row is a member."""
    ordered = sorted(rows, key=_timestamp_of)
    members = [_as_episode_row(row, float("-inf")) for row in ordered]
    keys = assign_episodes(members, qualified_only=True)

    first: dict[str, tuple[EpisodeKey, Observation]] = {}
    for key, row in zip(keys, ordered, strict=True):
        first.setdefault(key.as_str(), (key, row))
    return list(first.values())


def _timestamp_of(row: Observation) -> datetime:
    if row.timestamp is None:
        msg = (
            f"observation {row.evaluation_id} has no timestamp; walk-forward folds "
            "cannot be ordered without one"
        )
        raise ValueError(msg)
    return row.timestamp
