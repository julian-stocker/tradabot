"""Phase 6 feature research: does anything here carry repeatable information?

The question is deliberately not "can we build a better score". It is whether
any individual, causally-available feature separates outcomes in a way that
survives being tested on periods it was not chosen on. Phase 5.9 ended with
``NO_STABLE_OUT_OF_SAMPLE_EDGE`` for signal-v1, so nothing may be assumed.

Independence
------------
Every headline number is **episode level**. Fifty-two symbols scanned hourly
produce runs of near-identical rows; counting them as separate opportunities is
what turns noise into a confident-looking win rate. Episodes use the frozen
24-hour rule (:mod:`app.research.episodes`) -- same definition, applied to bucket
membership instead of the production ``qualified`` flag, which is structurally
false across the whole coarse window.

Quantiles, not thresholds
-------------------------
Continuous features are cut at quantiles of their own observed distribution.
Searching thresholds would be fitting, and the brief forbids it. Quartiles are
the default because a monotone pattern across four buckets is legible and a
pattern across twenty is a scatter plot with error bars nobody reads.

The classification at the end is mechanical (:func:`classify_feature`) so a
feature is labelled by its numbers rather than by how interesting it looked.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from itertools import pairwise
from typing import Any, Final

import polars as pl

from app.market_data.benchmarks import is_benchmark
from app.research.adjustments import adjust_all, load_splits
from app.research.episodes import MAX_EPISODE_GAP
from app.research.featureset import attach_context, attach_real_context, per_symbol_features

QUANTILES: Final = 4
MIN_EPISODES: Final = 30
"""Below this an episode-level bucket result is not interpretable.

Higher than the walk-forward's per-fold floor because these are pooled figures
used to *select* features, and a selection made on twelve episodes is a coin
flip with a name.
"""

BOOTSTRAP_RESAMPLES: Final = 1000
BOOTSTRAP_SEED: Final = 20260813

MONOTONE_TOLERANCE: Final = 0.02
"""Bucket-to-bucket wobble tolerated while still calling a trend monotone.

Two percentage points: smaller than any effect worth acting on, larger than the
noise four buckets of a few hundred episodes will always show.
"""

MEANINGFUL_SPREAD: Final = 0.05
"""Q1-to-Q4 positive-rate spread below which a feature is NO_INFORMATION.

Five points. A feature that moves the win rate by less than that cannot survive
costs, and calling it informative would be technically true and practically
useless.
"""


@dataclass(frozen=True, slots=True)
class BucketResult:
    """One quantile bucket of one feature."""

    label: str
    low: float
    high: float
    observations: int
    episodes: int
    positive_rate: float | None
    mean_return: float | None
    median_return: float | None
    mean_mfe: float | None
    mean_mae: float | None
    ci: tuple[float, float] | None

    @property
    def thin(self) -> bool:
        return self.episodes < MIN_EPISODES


@dataclass(frozen=True, slots=True)
class FeatureResult:
    """Every bucket of one feature, plus the verdict."""

    feature: str
    horizon: str
    stream: str
    buckets: list[BucketResult]
    baseline_rate: float | None
    verdict: str
    monotone: bool
    spread: float | None

    @property
    def usable(self) -> bool:
        return self.verdict in {"ROBUST", "PROMISING_BUT_UNSTABLE"}


def assign_episode_ids(
    frame: pl.DataFrame, *, max_gap: timedelta = MAX_EPISODE_GAP
) -> pl.DataFrame:
    """Attach an episode id using the **frozen** 24-hour rule.

    A new episode starts when this is the first row for a ``(symbol, direction)``
    pair or the previous one was more than ``max_gap`` ago. Identical to
    :func:`app.research.episodes.assign_episodes`, expressed as a window function
    so it can run over hundreds of thousands of rows.

    Applied to whatever subset is passed in: for a feature bucket, membership *is*
    the bucket, exactly as the walk-forward applies it to a score band.
    """
    ordered = frame.sort("timestamp")
    gap = pl.col("timestamp").diff().over(["symbol", "direction"])
    starts = gap.is_null() | (gap > max_gap)
    return ordered.with_columns(starts.cum_sum().over(["symbol", "direction"]).alias("episode_id"))


def collapse_to_episodes(frame: pl.DataFrame) -> pl.DataFrame:
    """One row per episode: the **first** observation.

    First, not best. A human acts when the setup appears; taking the strongest
    reading of a run already known to have worked is the retrospective choice
    that flatters every rule ever tested.
    """
    # Only the columns the summaries need. `pl.all().first()` over the full
    # 65-column frame was the second bottleneck after the bootstrap.
    wanted = [
        c for c in ("raw_return", "mfe", "mae", "timestamp", "year", "sector") if c in frame.columns
    ]
    return (
        assign_episode_ids(frame)
        .group_by(["symbol", "direction", "episode_id"])
        .agg([pl.col(c).first() for c in wanted])
    )


def _bootstrap(values: list[float], *, seed: int = BOOTSTRAP_SEED) -> tuple[float, float] | None:
    """Percentile bootstrap interval for the positive rate.

    Drawing the *count* from a binomial is not an approximation of resampling the
    indicator vector -- it is the same distribution. Resampling n episodes with
    replacement and counting positives is exactly Binomial(n, p), so this is the
    identical estimator at O(resamples) instead of O(resamples x n). On 20,000
    episodes that is the difference between minutes and milliseconds, and the
    slow form made the analysis infeasible rather than merely slower.
    """
    if len(values) < MIN_EPISODES:
        return None
    size = len(values)
    positives = sum(1 for value in values if value > 0)
    rate = positives / size

    rng = random.Random(seed)
    rates = sorted(rng.binomialvariate(size, rate) / size for _ in range(BOOTSTRAP_RESAMPLES))
    lower = int(0.025 * BOOTSTRAP_RESAMPLES)
    upper = int(0.975 * BOOTSTRAP_RESAMPLES) - 1
    return rates[lower], rates[upper]


def _summarise(episodes: pl.DataFrame, *, label: str, low: float, high: float) -> BucketResult:
    returns = episodes["raw_return"].drop_nulls().to_list()
    if not returns:
        return BucketResult(
            label, low, high, episodes.height, 0, None, None, None, None, None, None
        )

    positives = sum(1 for value in returns if value > 0)
    return BucketResult(
        label=label,
        low=low,
        high=high,
        observations=episodes.height,
        episodes=len(returns),
        positive_rate=positives / len(returns),
        mean_return=float(sum(returns) / len(returns)),
        median_return=float(sorted(returns)[len(returns) // 2]),
        mean_mfe=_mean(episodes, "mfe"),
        mean_mae=_mean(episodes, "mae"),
        ci=_bootstrap(returns),
    )


def _as_float(value: Any) -> float | None:
    """Narrow a polars aggregate to a float.

    Polars types these as a broad union covering dates and lists; every call
    here is on a numeric column, so anything else is a bug worth surfacing as
    ``None`` rather than a crash inside a long analysis run.
    """
    return float(value) if isinstance(value, int | float) else None


def _mean(frame: pl.DataFrame, column: str) -> float | None:
    if column not in frame.columns:
        return None
    return _as_float(frame[column].drop_nulls().mean())


def analyse_feature(
    frame: pl.DataFrame,
    *,
    feature: str,
    horizon: str,
    stream: str,
    buckets: int = QUANTILES,
) -> FeatureResult | None:
    """Quantile buckets of one continuous feature, episode level throughout.

    Returns ``None`` when the feature is absent or too sparse to bucket, rather
    than returning an empty result that would read as "no information".
    """
    if feature not in frame.columns:
        return None
    present = frame.filter(pl.col(feature).is_not_null() & pl.col("raw_return").is_not_null())
    if present.height < buckets * MIN_EPISODES:
        return None

    raw_edges = [present[feature].quantile(i / buckets) for i in range(buckets + 1)]
    edges: list[float] = [_as_float(edge) or 0.0 for edge in raw_edges]
    edges[0] = float("-inf")
    edges[-1] = float("inf")

    baseline = collapse_to_episodes(present)
    baseline_returns = baseline["raw_return"].drop_nulls().to_list()
    baseline_rate = (
        sum(1 for v in baseline_returns if v > 0) / len(baseline_returns)
        if baseline_returns
        else None
    )

    results: list[BucketResult] = []
    for index in range(buckets):
        low, high = edges[index], edges[index + 1]
        subset = present.filter((pl.col(feature) >= low) & (pl.col(feature) < high))
        if subset.height == 0:
            continue
        episodes = collapse_to_episodes(subset)
        results.append(
            _summarise(
                episodes,
                label=f"Q{index + 1} [{_fmt(low)}, {_fmt(high)})",
                low=low,
                high=high,
            )
        )

    monotone, spread = _shape(results)
    return FeatureResult(
        feature=feature,
        horizon=horizon,
        stream=stream,
        buckets=results,
        baseline_rate=baseline_rate,
        verdict=classify_feature(results, monotone=monotone, spread=spread),
        monotone=monotone,
        spread=spread,
    )


def _fmt(value: float) -> str:
    if value == float("-inf"):
        return "-inf"
    if value == float("inf"):
        return "inf"
    return f"{value:.3g}"


def _shape(buckets: list[BucketResult]) -> tuple[bool, float | None]:
    """Whether positive rate moves consistently, and how far end to end."""
    rates = [b.positive_rate for b in buckets if b.positive_rate is not None]
    if len(rates) < 2:  # noqa: PLR2004
        return False, None

    rising = all(later >= earlier - MONOTONE_TOLERANCE for earlier, later in _pairs(rates))
    falling = all(later <= earlier + MONOTONE_TOLERANCE for earlier, later in _pairs(rates))
    return (rising or falling), rates[-1] - rates[0]


def _pairs(values: list[float]) -> list[tuple[float, float]]:
    return list(pairwise(values))


def classify_feature(buckets: list[BucketResult], *, monotone: bool, spread: float | None) -> str:
    """Label a feature from its numbers alone.

    Mechanical on purpose. The brief requires a classification "defined from the
    evidence", and a human choosing between PROMISING and NO_INFORMATION after
    looking at a table is exactly the step that converts noise into a finding.

    Note this is the *pooled* verdict. Stability across folds and years is
    assessed separately, and a feature can only reach ROBUST by passing both.
    """
    usable = [b for b in buckets if not b.thin]
    if len(usable) < len(buckets):
        return "INSUFFICIENT_SAMPLE"
    if spread is None:
        return "INSUFFICIENT_SAMPLE"
    if abs(spread) < MEANINGFUL_SPREAD:
        return "NO_INFORMATION"

    # A confident interval separation between the extreme buckets is the minimum
    # evidence of real separation; overlapping intervals mean the ordering could
    # be resampling noise.
    first, last = buckets[0], buckets[-1]
    separated = bool(
        first.ci and last.ci and (first.ci[1] < last.ci[0] or last.ci[1] < first.ci[0])
    )

    if monotone and separated:
        return "PROMISING_BUT_UNSTABLE"
    if separated:
        return "REGIME_DEPENDENT"
    return "NO_INFORMATION"


# ---------------------------------------------------------------------------
# Part F: setup quality vs entry risk
# ---------------------------------------------------------------------------
INTERACTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("px_vs_ema50_pct", "rsi14"),
    ("px_vs_ema50_pct", "dist_ema20_atr"),
    ("ema50_slope_pct", "rsi14"),
    ("rel_strength_market_pct", "rsi14"),
    ("rel_volume", "dist_ema20_atr"),
)
"""**Five** predefined combinations, chosen before any of them was run.

Each pairs a *setup quality* dimension (trend, slope, relative strength, volume)
with an *entry risk* dimension (exhaustion, extension). That is the phase-5.9
hypothesis stated as a design: strength and timing may be different questions,
and a single score that adds them together cannot express "good company, bad
moment". Brute-forcing pairs would guarantee a winner and mean nothing.
"""


def analyse_interaction(
    frame: pl.DataFrame, *, quality: str, risk: str, splits: int = 2
) -> list[BucketResult]:
    """Median-split both dimensions and report all four cells.

    A median split rather than quartiles: sixteen cells of a few hundred episodes
    would be unreadable and mostly thin. Reports upside (positive rate, mean, MFE)
    and downside (MAE) for each cell, because the question is whether high
    extension buys worse entries at the *same* setup quality.
    """
    if quality not in frame.columns or risk not in frame.columns:
        return []
    present = frame.filter(
        pl.col(quality).is_not_null()
        & pl.col(risk).is_not_null()
        & pl.col("raw_return").is_not_null()
    )
    if present.height < MIN_EPISODES * splits * splits:
        return []

    quality_cut = _as_float(present[quality].median()) or 0.0
    risk_cut = _as_float(present[risk].median()) or 0.0

    cells: list[BucketResult] = []
    for quality_high in (False, True):
        for risk_high in (False, True):
            subset = present.filter(
                (pl.col(quality) >= quality_cut if quality_high else pl.col(quality) < quality_cut)
                & (pl.col(risk) >= risk_cut if risk_high else pl.col(risk) < risk_cut)
            )
            if subset.height == 0:
                continue
            label = (
                f"{'high' if quality_high else 'low'} {quality} / "
                f"{'high' if risk_high else 'low'} {risk}"
            )
            cells.append(_summarise(collapse_to_episodes(subset), label=label, low=0.0, high=0.0))
    return cells


# ---------------------------------------------------------------------------
# Part G: time of day
# ---------------------------------------------------------------------------
SESSION_BUCKETS: Final[tuple[tuple[str, int, int], ...]] = (
    ("OPENING", 13, 15),
    ("EARLY_SESSION", 15, 17),
    ("MIDDAY", 17, 19),
    ("LATE_SESSION", 19, 21),
)
"""Broad UTC bands over the US regular session (13:30-20:00 UTC).

Four wide buckets, fixed in advance. Optimising minute boundaries against
outcomes is precisely the search the brief forbids, and with hourly bars the
finest honest resolution is an hour anyway.
"""


def analyse_time_of_day(frame: pl.DataFrame) -> list[BucketResult]:
    """Outcomes by session bucket. Production-faithful stream only."""
    if "timestamp" not in frame.columns:
        return []
    present = frame.filter(pl.col("raw_return").is_not_null()).with_columns(
        pl.col("timestamp").dt.hour().alias("utc_hour")
    )

    results: list[BucketResult] = []
    for label, start, end in SESSION_BUCKETS:
        subset = present.filter((pl.col("utc_hour") >= start) & (pl.col("utc_hour") < end))
        if subset.height == 0:
            continue
        results.append(_summarise(collapse_to_episodes(subset), label=label, low=start, high=end))
    return results


# ---------------------------------------------------------------------------
# Part H: redundancy
# ---------------------------------------------------------------------------
def correlation_matrix(frame: pl.DataFrame, features: list[str]) -> list[tuple[str, str, float]]:
    """Pairwise Pearson correlation, strongest first.

    Reported to answer one question: are we about to give five weights to five
    representations of the same move? Pearson is adequate for that -- the aim is
    to spot near-duplicates, not to model the dependency structure.
    """
    usable = [f for f in features if f in frame.columns]
    pairs: list[tuple[str, str, float]] = []
    for index, left in enumerate(usable):
        for right in usable[index + 1 :]:
            subset = frame.select([left, right]).drop_nulls()
            if subset.height < MIN_EPISODES:
                continue
            value = _as_float(subset.select(pl.corr(left, right)).item())
            if value is not None:
                pairs.append((left, right, value))
    return sorted(pairs, key=lambda item: abs(item[2]), reverse=True)


def format_bucket(bucket: BucketResult, *, baseline: float | None = None) -> str:
    delta = (
        f"{(bucket.positive_rate - baseline) * 100:+6.1f}pp"
        if bucket.positive_rate is not None and baseline is not None
        else "      -"
    )
    interval = f"[{bucket.ci[0] * 100:.0f},{bucket.ci[1] * 100:.0f}]" if bucket.ci else "  n/a "
    return (
        f"    {bucket.label:<34}eps={bucket.episodes:>6}  "
        f"pos={_pct(bucket.positive_rate):>7}  {delta}  CI={interval:<10} "
        f"mean={_pct(bucket.mean_return):>8}  MFE={_pct(bucket.mean_mfe):>8}  "
        f"MAE={_pct(bucket.mean_mae):>8}{'  <- thin' if bucket.thin else ''}"
    )


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def as_dict(result: FeatureResult) -> dict[str, Any]:
    return {
        "feature": result.feature,
        "horizon": result.horizon,
        "stream": result.stream,
        "verdict": result.verdict,
        "monotone": result.monotone,
        "spread": result.spread,
        "baseline_rate": result.baseline_rate,
        "buckets": [
            {
                "label": b.label,
                "episodes": b.episodes,
                "positive_rate": b.positive_rate,
                "mean_return": b.mean_return,
                "mean_mfe": b.mean_mfe,
                "mean_mae": b.mean_mae,
                "ci": b.ci,
            }
            for b in result.buckets
        ],
    }


def load_phase6_dataset(
    database_url: str, *, run_id: int, horizon: str, adjust_splits: bool = True
) -> pl.DataFrame:
    """Observations joined to causal features and one horizon's outcome.

    ``adjust_splits`` defaults to True and should stay that way. It exists so
    phase 9A part C can reproduce the contaminated series deliberately and
    measure what the adjustment moved; setting it False anywhere else
    reintroduces eleven bars of -75% to -95% "returns" that are share counts
    changing, not prices.

    The join key is the **hourly bar whose close is the evaluation instant**:
    ``candle.timestamp == evaluated_at - 1h``. The replay grid yields bar closes,
    so that bar is exactly the newest hourly information available at T.

    Not ``market_data_timestamp``: that column records the newest bar seen across
    *all* timeframes, which on the production-faithful stream is a 5-minute bar
    (measured: a 5-minute lag on 114,153 of 116,838 rows). Joining hourly
    features on it matched 3 rows out of 116,838 -- a silent near-empty join that
    would have produced a confident analysis of nothing.
    """
    path = database_url.rsplit("///", maxsplit=1)[-1]
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA busy_timeout=60000")
    try:
        observations = pl.read_database(
            """
            SELECT e.id AS evaluation_id, e.instrument_id, i.symbol, w.tags AS sector_tags,
                   e.evaluated_at, e.market_data_timestamp AS bar_ts,
                   e.score, e.direction, o.raw_return, o.mfe, o.mae
            FROM signal_evaluations e
            JOIN instruments i ON i.id = e.instrument_id
            LEFT JOIN watchlist w ON w.instrument_id = e.instrument_id
            JOIN signal_outcomes o ON o.evaluation_id = e.id
            WHERE e.backtest_run_id = ? AND o.horizon = ? AND o.status = 'COMPLETE'
            """,
            connection,
            execute_options={"parameters": [run_id, horizon]},
        )
        candles = pl.read_database(
            """SELECT c.instrument_id, i.symbol, c.timestamp, c.open, c.high, c.low,
                      c.close, c.volume
               FROM candles c JOIN instruments i ON i.id = c.instrument_id
               WHERE c.timeframe = 'H1' ORDER BY c.instrument_id, c.timestamp""",
            connection,
        )
        splits = load_splits(connection)
    finally:
        connection.close()

    # SQLite hands back both timestamp columns as text; parse once, on both
    # sides, so the join key is a real instant rather than two string formats
    # that happen to match today.
    candles = candles.with_columns(pl.col("timestamp").str.to_datetime(strict=False))

    # Before any feature is computed. A split left in the series is a -75% bar
    # that every rolling window downstream would treat as a price move.
    candles = candles.sort(["instrument_id", "timestamp"])
    if adjust_splits:
        candles = adjust_all(candles, splits)

    features = pl.concat(
        [
            per_symbol_features(group.sort("timestamp"))
            for _, group in candles.group_by("instrument_id")
        ]
    )
    features = features.with_columns(
        pl.col("symbol").replace_strict(_sector_map(observations), default=None).alias("sector")
    )

    # Reference instruments are featured like anything else, then held out of
    # the cross-section: an equal-weight "market" that included SPY would let
    # the benchmark vote on itself.
    is_reference = pl.col("symbol").map_elements(is_benchmark, return_dtype=pl.Boolean)
    benchmarks = features.filter(is_reference)
    features = features.filter(~is_reference)

    features = attach_context(features)
    return _join_observations(attach_real_context(features, benchmarks), observations)


def _join_observations(features: pl.DataFrame, observations: pl.DataFrame) -> pl.DataFrame:
    """Attach each observation to the hourly bar it actually saw."""

    joined = (
        observations.with_columns(
            pl.col("evaluated_at").str.to_datetime(strict=False).alias("evaluated_ts"),
        )
        .with_columns(
            (pl.col("evaluated_ts") - pl.duration(hours=1)).alias("timestamp"),
        )
        .join(
            features.drop("symbol"),
            on=["instrument_id", "timestamp"],
            how="inner",
        )
    )
    return joined.with_columns(
        pl.col("sector_tags").str.replace_all(r'[\[\]"]', "").alias("sector"),
        pl.col("evaluated_ts").dt.year().alias("year"),
    )


def _sector_map(observations: pl.DataFrame) -> dict[str, str]:
    rows = observations.select("symbol", "sector_tags").unique().drop_nulls().iter_rows()
    return {symbol: str(tags).strip('[]"') for symbol, tags in rows}
