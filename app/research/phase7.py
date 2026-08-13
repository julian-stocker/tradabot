"""Phase 7: relative strength, regime conditioning, and magnitude persistence.

Phase 6 asked "does this stock's own feature value predict its outcome?" and got
128 nulls out of 130. Phase 7 asks three structurally different questions, none
of which that result rules out:

1. **Cross-sectional.** Not "is NVDA's 20-day return high?" but "is it high
   *relative to the other 51 names at this same instant?*". A feature can be
   uninformative in its own time series and still rank usefully across a
   universe -- that is the entire premise of relative-strength investing, and
   Phase 6 never tested it.
2. **Conditional on regime.** Whether outcomes differ between market-strong and
   market-weak states, and whether sector strength adds anything on top.
3. **Magnitude rather than direction.** Whether volatility persists well enough
   to trade size and range, given that direction does not predict.

The third is the one with a strong prior: volatility clustering is among the most
replicated facts in finance. It is included precisely because a phase that only
tests things likely to fail is not a fair test of the architecture.

Everything is episode level under the frozen 24-hour rule, and every ranking is
computed **within a timestamp**, so no future cross-section can leak backwards.
"""

from __future__ import annotations

from typing import Final

import polars as pl

from app.research.featureset import BARS_PER_DAY
from app.research.phase6 import BucketResult, _summarise, collapse_to_episodes

RANK_BUCKETS: Final[tuple[tuple[str, float, float], ...]] = (
    ("top 10%", 0.90, 1.01),
    ("top 20%", 0.80, 1.01),
    ("middle 60%", 0.20, 0.80),
    ("bottom 20%", 0.00, 0.20),
)
"""Cross-sectional slices, declared before any of them was run.

Overlapping on purpose: "top 10%" is inside "top 20%", because the operational
question is whether concentrating further helps, which nesting answers and a
partition does not.
"""

RANKING_DIMENSIONS: Final[tuple[str, ...]] = (
    "ret_1d_pct",
    "ret_5d_pct",
    "px_vs_ema50_pct",
    "px_vs_ema200_pct",
    "ema50_slope_pct",
    "rel_strength_market_pct",
    "rel_strength_sector_pct",
    "bars_above_ema50",
    "rel_volume",
    "atr_pct",
    "dist_from_high20_pct",
)
"""One ranking dimension per concept, chosen before running any of them.

Deliberately not every feature: phase 6 measured correlations up to 0.98 between
these families, so adding more would be re-testing the same variable and
inflating the multiple-comparison count for nothing.
"""

MIN_PERSISTENCE_PAIRS: Final = 1000
"""Paired observations required before quoting a persistence coefficient."""

MIN_CROSS_SECTION: Final = 20
"""Symbols required at a timestamp before its ranking means anything.

A "top 10%" drawn from six names is the top one name, which is a different
statement from the one being tested.
"""


def add_cross_sectional_rank(frame: pl.DataFrame, *, feature: str) -> pl.DataFrame:
    """Percentile rank of ``feature`` **within each timestamp**.

    The ``over("timestamp")`` is the whole point: every comparison is against
    other symbols at the same instant, so nothing from a later cross-section can
    influence an earlier rank. Timestamps with too few symbols are dropped rather
    than ranked, because a percentile over a handful of names is noise with a
    decimal point.
    """
    counted = frame.with_columns(
        pl.col(feature).is_not_null().sum().over("timestamp").alias("_cross_n")
    ).filter(pl.col("_cross_n") >= MIN_CROSS_SECTION)

    return counted.with_columns(
        (pl.col(feature).rank(method="average").over("timestamp") / pl.col("_cross_n")).alias(
            "cross_rank"
        )
    ).drop("_cross_n")


def analyse_cross_section(frame: pl.DataFrame, *, feature: str) -> list[BucketResult]:
    """Forward outcomes by cross-sectional rank bucket.

    Answers the leaders-versus-laggards question directly: if today's strongest
    names continue to outperform, the top bucket must beat the bottom by more
    than noise, at the episode level, after the same collapse everything else
    here uses.
    """
    if feature not in frame.columns:
        return []
    ranked = add_cross_sectional_rank(
        frame.filter(pl.col(feature).is_not_null() & pl.col("raw_return").is_not_null()),
        feature=feature,
    )
    if ranked.height == 0:
        return []

    results: list[BucketResult] = []
    for label, low, high in RANK_BUCKETS:
        subset = ranked.filter((pl.col("cross_rank") > low) & (pl.col("cross_rank") <= high))
        if subset.height == 0:
            continue
        results.append(
            _summarise(collapse_to_episodes(subset), label=f"{feature} {label}", low=low, high=high)
        )
    return results


# ---------------------------------------------------------------------------
# Part C: market x sector regime
# ---------------------------------------------------------------------------
def analyse_regime_cells(frame: pl.DataFrame) -> list[BucketResult]:
    """The four market x sector states.

    Market state uses the equal-weight proxy's position versus its own average;
    sector state uses the sector proxy's return against the market proxy's. Both
    are cross-sectional at each instant and therefore causal.

    A flat result here is meaningful in itself: it would say sector membership
    carries no conditioning information, which is the assumption most rotation
    strategies are built on.
    """
    needed = {"market_above_ema50", "sector_ret_1d_pct", "proxy_ret_1d_pct", "raw_return"}
    if not needed.issubset(frame.columns):
        return []

    present = frame.filter(
        pl.col("raw_return").is_not_null()
        & pl.col("sector_ret_1d_pct").is_not_null()
        & pl.col("proxy_ret_1d_pct").is_not_null()
    ).with_columns(
        (pl.col("sector_ret_1d_pct") > pl.col("proxy_ret_1d_pct")).alias("sector_strong")
    )

    results: list[BucketResult] = []
    for market_strong in (True, False):
        for sector_strong in (True, False):
            subset = present.filter(
                (pl.col("market_above_ema50") == market_strong)
                & (pl.col("sector_strong") == sector_strong)
            )
            if subset.height == 0:
                continue
            label = (
                f"market {'strong' if market_strong else 'weak'} / "
                f"sector {'strong' if sector_strong else 'weak'}"
            )
            results.append(_summarise(collapse_to_episodes(subset), label=label, low=0.0, high=0.0))
    return results


# ---------------------------------------------------------------------------
# Part G: does magnitude persist even though direction does not?
# ---------------------------------------------------------------------------
def volatility_persistence(candles: pl.DataFrame, *, horizons: tuple[int, ...]) -> list[str]:
    """Rank correlation between current ATR% and ATR% over the next N sessions.

    Deliberately a *forward* measurement and therefore not a feature -- it is a
    property of the data being characterised, not something a model may read at
    time T. Spearman rather than Pearson because volatility is heavy-tailed and a
    handful of crisis bars would otherwise carry the whole coefficient.
    """
    lines: list[str] = []
    for days in horizons:
        bars = days * BARS_PER_DAY
        paired = (
            candles.with_columns(
                pl.col("atr_pct").shift(-bars).over("instrument_id").alias("future_atr_pct")
            )
            .select("atr_pct", "future_atr_pct")
            .drop_nulls()
        )
        if paired.height < MIN_PERSISTENCE_PAIRS:
            lines.append(f"  {days:>2}d: insufficient")
            continue
        rho = paired.select(
            pl.corr(pl.col("atr_pct").rank(), pl.col("future_atr_pct").rank())
        ).item()
        lines.append(f"  {days:>2}d ahead: spearman rho = {float(rho):.3f}  (n={paired.height:,})")
    return lines


# ---------------------------------------------------------------------------
# Part I: can trending and ranging states be told apart at all?
# ---------------------------------------------------------------------------
TREND_STRENGTH_QUANTILE: Final = 0.70
RANGE_STRENGTH_QUANTILE: Final = 0.30
"""Fixed quantiles of an existing trend measure, declared in advance.

Not tuned: the question is whether the *states* differ in behaviour, and moving
the cut until they do would answer a different question.
"""


def classify_regimes(frame: pl.DataFrame) -> pl.DataFrame:
    """Label each observation TRENDING / RANGING / UNCERTAIN.

    Uses the absolute EMA50 slope as the trend measure -- one existing quantity,
    not a new composite. Strong slope in either direction is TRENDING, flat is
    RANGING, and the middle is UNCERTAIN and expected to be untradeable.
    """
    if "ema50_slope_pct" not in frame.columns:
        return frame
    strength = pl.col("ema50_slope_pct").abs()
    upper = frame.select(strength.quantile(TREND_STRENGTH_QUANTILE)).item()
    lower = frame.select(strength.quantile(RANGE_STRENGTH_QUANTILE)).item()

    return frame.with_columns(
        pl.when(strength >= upper)
        .then(pl.lit("TRENDING"))
        .when(strength <= lower)
        .then(pl.lit("RANGING"))
        .otherwise(pl.lit("UNCERTAIN"))
        .alias("regime")
    )


def analyse_regimes(frame: pl.DataFrame) -> list[BucketResult]:
    """Outcomes per regime label, episode level."""
    labelled = classify_regimes(frame.filter(pl.col("raw_return").is_not_null()))
    if "regime" not in labelled.columns:
        return []

    results: list[BucketResult] = []
    for regime in ("TRENDING", "RANGING", "UNCERTAIN"):
        subset = labelled.filter(pl.col("regime") == regime)
        if subset.height == 0:
            continue
        results.append(_summarise(collapse_to_episodes(subset), label=regime, low=0.0, high=0.0))
    return results


def spread_of(buckets: list[BucketResult]) -> float | None:
    """Positive-rate spread between the first and last bucket, in points."""
    rates = [b.positive_rate for b in buckets if b.positive_rate is not None]
    if len(rates) < 2:  # noqa: PLR2004
        return None
    return rates[0] - rates[-1]
