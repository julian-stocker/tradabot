"""Descriptive statistics over observations and their outcomes.

**Measurement only.** Nothing here changes a threshold, fits a parameter or
selects a feature. The score-band report exists to answer "does outcome quality
increase with score?" -- and if the answer is no, that is a finding to sit with,
not a signal to move the threshold until the table looks better. Tuning a cutoff
on the same data you then quote as validation produces a number that describes
the tuning, not the strategy.

Small samples are the normal case here, so every aggregate carries its ``n`` and
none of them are reported without it. A 71% win rate over seven trades is noise
with a decimal point.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Instrument, SignalEvaluation, SignalOutcome, WatchlistEntry
from app.domain.enums import Horizon, LabelStatus

MIN_REPORTABLE_SAMPLE: Final = 5
"""Below this, an aggregate is shown with its ``n`` but flagged as unreliable."""

SCORE_BANDS: Final[tuple[tuple[str, float, float], ...]] = (
    ("<0", float("-inf"), 0.0),
    ("0-25", 0.0, 25.0),
    ("25-50", 25.0, 50.0),
    ("50-60", 50.0, 60.0),
    ("60-70", 60.0, 70.0),
    ("70-75", 70.0, 75.0),
    ("75-80", 75.0, 80.0),
    ("80-85", 80.0, 85.0),
    (">=85", 85.0, float("inf")),
)
"""Part Y's bands. The boundaries straddle the live 75/85 thresholds so the
question "is the cutoff in the right place?" is answerable without moving it."""

THRESHOLD_BANDS: Final[tuple[tuple[str, float, float], ...]] = (
    ("60-65", 60.0, 65.0),
    ("65-70", 65.0, 70.0),
    ("70-75", 70.0, 75.0),
    ("75-80", 75.0, 80.0),
    ("80-85", 80.0, 85.0),
    (">=85", 85.0, float("inf")),
)
"""Part AM's finer view around the 75 threshold."""


@dataclass(frozen=True, slots=True)
class GroupStats:
    """Descriptive statistics for one group of observations."""

    label: str
    n: int
    mean_return: float | None = None
    median_return: float | None = None
    positive_rate: float | None = None
    mean_mfe: float | None = None
    mean_mae: float | None = None
    stdev_return: float | None = None

    @property
    def is_reportable(self) -> bool:
        """Whether the sample is large enough to quote without a caveat."""
        return self.n >= MIN_REPORTABLE_SAMPLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "n": self.n,
            "mean_return": self.mean_return,
            "median_return": self.median_return,
            "positive_rate": self.positive_rate,
            "mean_mfe": self.mean_mfe,
            "mean_mae": self.mean_mae,
            "stdev_return": self.stdev_return,
            "reportable": self.is_reportable,
        }


@dataclass(frozen=True, slots=True)
class Observation:
    """One joined (features, outcome) pair, flattened for grouping."""

    evaluation_id: int
    symbol: str
    sector: str | None
    score: float
    horizon: str
    year: int
    raw_return: float | None
    mfe: float | None
    mae: float | None
    features: dict[str, float | None]


def summarise(rows: Sequence[Observation], *, label: str) -> GroupStats:
    """Describe one group, tolerating missing labels.

    Rows whose outcome is unlabelled are excluded from the return statistics but
    still counted in ``n``, so a band that is large but mostly ``PENDING`` cannot
    masquerade as a well-measured one.
    """
    returns = [row.raw_return for row in rows if row.raw_return is not None]
    if not returns:
        return GroupStats(label=label, n=len(rows))

    mfes = [row.mfe for row in rows if row.mfe is not None]
    maes = [row.mae for row in rows if row.mae is not None]
    positives = sum(1 for value in returns if value > 0)

    return GroupStats(
        label=label,
        n=len(returns),
        mean_return=statistics.fmean(returns),
        median_return=statistics.median(returns),
        positive_rate=positives / len(returns),
        mean_mfe=statistics.fmean(mfes) if mfes else None,
        mean_mae=statistics.fmean(maes) if maes else None,
        stdev_return=statistics.stdev(returns) if len(returns) > 1 else None,
    )


def by_score_band(
    rows: Sequence[Observation],
    *,
    bands: tuple[tuple[str, float, float], ...] = SCORE_BANDS,
) -> list[GroupStats]:
    """Group observations into score bands, lowest first.

    Bands are half-open ``[low, high)`` so a score of exactly 75 lands in
    ``75-80`` and not in both neighbours.
    """
    return [
        summarise([row for row in rows if low <= row.score < high], label=label)
        for label, low, high in bands
    ]


def by_feature_quantile(
    rows: Sequence[Observation], *, feature: str, buckets: int = 5
) -> list[GroupStats]:
    """Group by quantiles of one feature (part Z).

    Quantiles rather than fixed cut points, because the useful ranges differ per
    feature and per instrument: a fixed "RSI > 70" bucket is meaningful, a fixed
    "relative volume > 3" bucket is mostly empty. Returns an empty list when too
    few rows carry the feature to form the requested buckets.
    """
    present: list[tuple[float, Observation]] = [
        (value, row)
        for value, row in ((row.features.get(feature), row) for row in rows)
        if value is not None
    ]
    if len(present) < buckets:
        return []

    present.sort(key=lambda pair: pair[0])
    size = len(present) // buckets
    groups: list[GroupStats] = []
    for index in range(buckets):
        start = index * size
        end = len(present) if index == buckets - 1 else (index + 1) * size
        chunk = present[start:end]
        if not chunk:
            continue
        low = chunk[0][0]
        high = chunk[-1][0]
        groups.append(
            summarise(
                [row for _, row in chunk],
                label=f"{feature} q{index + 1} [{low:.3g}, {high:.3g}]",
            )
        )
    return groups


def by_year(rows: Sequence[Observation]) -> list[GroupStats]:
    """Group by calendar year (part W).

    Calendar years are a crude regime proxy and are used *because* they are
    crude: they are defined by the calendar rather than by anything fitted to
    this data, so they cannot be tuned to flatter a result. A volatility- or
    trend-based split would be more informative and would also be a modelling
    choice that needs its own justification.
    """
    years = sorted({row.year for row in rows})
    return [summarise([row for row in rows if row.year == year], label=str(year)) for year in years]


def by_sector(rows: Sequence[Observation]) -> list[GroupStats]:
    sectors = sorted({row.sector for row in rows if row.sector})
    return [
        summarise([row for row in rows if row.sector == sector], label=sector) for sector in sectors
    ]


async def load_observations(
    session: AsyncSession,
    *,
    horizon: Horizon,
    backtest_run_id: int | None = None,
    include_backtest: bool = True,
    complete_only: bool = True,
) -> list[Observation]:
    """Join evaluations to their outcomes for one horizon.

    The join is explicit and one-directional: features come from
    ``signal_evaluations``, labels from ``signal_outcomes``, and the two are
    combined here and nowhere else. That is the whole reason the labels were not
    stored as columns on the evaluation.
    """
    # The sector lives on the watchlist entry, not the instrument, so it is
    # outer-joined: an instrument that has since left the watchlist must still
    # contribute its observations rather than vanish from the dataset.
    stmt = (
        select(SignalEvaluation, SignalOutcome, Instrument, WatchlistEntry.tags)
        .join(SignalOutcome, SignalOutcome.evaluation_id == SignalEvaluation.id)
        .join(Instrument, Instrument.id == SignalEvaluation.instrument_id)
        .outerjoin(WatchlistEntry, WatchlistEntry.instrument_id == SignalEvaluation.instrument_id)
        .where(SignalOutcome.horizon == horizon.value)
    )
    if complete_only:
        stmt = stmt.where(SignalOutcome.status == LabelStatus.COMPLETE.value)
    if backtest_run_id is not None:
        stmt = stmt.where(SignalEvaluation.backtest_run_id == backtest_run_id)
    elif not include_backtest:
        stmt = stmt.where(SignalEvaluation.backtest_run_id.is_(None))

    rows = (await session.execute(stmt)).all()
    return [
        Observation(
            evaluation_id=evaluation.id,
            symbol=instrument.symbol,
            sector=_sector_of(tags),
            score=evaluation.score,
            horizon=outcome.horizon,
            year=outcome.reference_timestamp.year,
            raw_return=outcome.raw_return,
            mfe=outcome.mfe,
            mae=outcome.mae,
            features=_feature_view(evaluation),
        )
        for evaluation, outcome, instrument, tags in rows
    ]


RESEARCH_FEATURES: Final[tuple[str, ...]] = (
    "score",
    "confidence",
    "agreement",
    "relative_volume",
    "rsi",
    "atr_pct",
    "volatility",
    "ema_spread_pct",
    "expected_move_bps",
    "cost_bps",
    "net_edge_bps",
    "spread_bps",
)
"""Features offered to the feature-vs-outcome report (part Z)."""


def _feature_view(evaluation: SignalEvaluation) -> dict[str, float | None]:
    """Flatten the stored metric blobs into a single feature mapping.

    Only signal-time values. Every key here comes from a column or JSON blob that
    :func:`~app.scanner.service._build_evaluation` filled from information
    available at the evaluation instant -- no outcome field is reachable.
    """
    trend = evaluation.trend_metrics or {}
    momentum = evaluation.momentum_metrics or {}
    volume = evaluation.volume_metrics or {}
    volatility = evaluation.volatility_metrics or {}

    return {
        "score": evaluation.score,
        "confidence": evaluation.confidence,
        "agreement": evaluation.agreement,
        "relative_volume": _as_float(volume.get("relative_volume")),
        "rsi": _as_float(momentum.get("rsi")),
        "atr_pct": _as_float(volatility.get("atr_pct")),
        "volatility": _as_float(volatility.get("volatility")),
        "ema_spread_pct": _as_float(trend.get("ema_spread_pct")),
        "expected_move_bps": evaluation.expected_move_bps,
        "cost_bps": evaluation.cost_bps,
        "net_edge_bps": evaluation.net_edge_bps,
        "spread_bps": evaluation.spread_bps,
    }


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _sector_of(tags: Any) -> str | None:
    """First watchlist tag, which the universe seeds as the sector."""
    if isinstance(tags, list | tuple) and tags:
        return str(tags[0])
    return None
