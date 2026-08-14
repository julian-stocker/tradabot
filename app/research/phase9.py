"""Phase 9A: does *real* market and sector context say anything the proxy did not?

Phase 6 tested a market-context family built entirely out of the universe it was
providing context for: an equal-weight mean of the same 52 symbols, and a
"sector" that was the first watchlist tag. It returned the largest separation in
the whole study -- 5.5pp, ``REGIME_DEPENDENT`` -- and also the least trustworthy,
because a stock is *definitionally* correlated with the average of 51 others
measured at the same instant. That number could be information about the market,
or it could be an artefact of a reference that contains the thing being
referenced. Nothing in phase 6 could tell the two apart.

This phase can, because there is now a reference the universe does not appear
in: SPY and QQQ for the market, nine sector funds, and SMH carrying XLK as a
parent for the chipmakers -- all stored and featured exactly like any other
symbol (``app.market_data.benchmarks``).

The four questions, fixed before any of them was run
----------------------------------------------------
**Part A -- does real context separate outcomes at all?** The same quantile
methodology phase 6 used, applied to ``index_*`` and ``sector_etf_*``. Same
episode collapse, same 5pp floor, same mechanical verdict. A flat answer here is
a real answer: it would say the 5.5pp was the proxy measuring itself.

**Part B -- proxy against real, head to head.** Each proxy feature paired with
its real counterpart on the same rows, same buckets. The question is not which
is larger but whether they *agree*: a proxy that tracks the real reference at
r > 0.9 and produces a different verdict is measuring its own construction.

**Part C -- what the split adjustment moved.** Phase 6 ran on raw candles in
which eleven splits appear as one-bar returns of -75% to -95%, and
``ret_1d_pct`` is the proxy's input, so each contaminated "the market" too. This
part re-measures the market-context family with and without adjustment, which is
the only way to say how much of the published 5.5pp was a data defect.

**Part D -- the real regime 2x2.** Phase 7 Part C conditioned outcomes on
market-strong/weak x sector-strong/weak using proxies for both. Re-run against
SPY and the sector funds. If sector membership carries information, this is
where it shows up; if the earlier flatness was the proxies cancelling out, this
is where that would surface.

**Part G -- the pre-registered hypothesis.** Four frozen states, all conditioned
on the stock already being cross-sectionally strong, asking whether *context*
improves an already-selected name. Plus relative-strength rank slices at
10/20/60/20. Every cutoff fixed before an outcome was inspected.

**Part I -- walk-forward stability.** The same analysis partitioned by calendar
year, never shuffled. A pooled spread that reverses sign between years is not an
edge, and this is the view that shows it.

**Part J -- redundancy.** Pairwise correlation across stock momentum, trend
persistence, the old proxy features and the new real ones. A feature 0.95
correlated with one already present is not independent evidence.

What a null result means here
------------------------------
That the market-context family joins the other 128. That is a finding, not a
failure, and it is the more likely outcome: phase 6's own correlation table
already showed ``rel_strength_market_pct`` at r = +0.950 with 1-day return,
which is the signature of a feature that is mostly restating the stock's own
move. Real context does not fix that; it just stops the reference from being
circular, so the restatement becomes visible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import polars as pl

from app.research.phase6 import (
    BucketResult,
    FeatureResult,
    _summarise,
    analyse_feature,
    collapse_to_episodes,
)

REAL_CONTEXT_FEATURES: Final[tuple[str, ...]] = (
    "index_ret_1d_pct",
    "index_ret_5d_pct",
    "index_px_vs_ema50_pct",
    "nasdaq_ret_1d_pct",
    "nasdaq_ret_5d_pct",
    "sector_etf_ret_1d_pct",
    "sector_etf_ret_5d_pct",
    "parent_etf_ret_1d_pct",
    "relative_strength_market_1d",
    "relative_strength_market_5d",
    "relative_strength_nasdaq_1d",
    "relative_strength_sector_1d",
    "relative_strength_sector_5d",
    "sector_relative_to_market",
)
"""The features under test in part A. Fixed before running."""

PROXY_VERSUS_REAL: Final[tuple[tuple[str, str], ...]] = (
    ("proxy_ret_1d_pct", "index_ret_1d_pct"),
    ("rel_strength_market_pct", "relative_strength_market_1d"),
    ("sector_ret_1d_pct", "sector_etf_ret_1d_pct"),
    ("rel_strength_sector_pct", "relative_strength_sector_1d"),
)
"""Part B pairings: ``(proxy feature, real counterpart)``.

One pair per concept the proxy claimed to measure. Each pair is measured
on **identical rows** -- the intersection where both are non-null -- so a
difference in verdict cannot come from a difference in sample.
"""

MEANINGFUL_SPREAD: Final = 0.05
"""The phase-6 floor, restated here so this phase cannot quietly lower it.

Five percentage points of end-to-end separation in positive rate. Kept
identical to phase 6 because the entire point of this phase is comparability.
"""


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """One proxy feature and its real counterpart, measured on the same rows."""

    proxy_feature: str
    real_feature: str
    horizon: str
    stream: str
    episodes: int
    correlation: float | None
    proxy: FeatureResult | None
    real: FeatureResult | None

    @property
    def verdicts_agree(self) -> bool:
        if self.proxy is None or self.real is None:
            return False
        return self.proxy.verdict == self.real.verdict

    @property
    def spread_gap(self) -> float | None:
        """Real spread minus proxy spread, in positive-rate points.

        Positive means the real reference separated outcomes *more* than the
        proxy did. Near zero with a high correlation is the expected result and
        the least interesting one.
        """
        if self.proxy is None or self.real is None:
            return None
        if self.proxy.spread is None or self.real.spread is None:
            return None
        return self.real.spread - self.proxy.spread


def analyse_real_context(frame: pl.DataFrame, *, horizon: str, stream: str) -> list[FeatureResult]:
    """Part A. Every real-context feature, phase-6 methodology unchanged."""
    results = []
    for feature in REAL_CONTEXT_FEATURES:
        result = analyse_feature(frame, feature=feature, horizon=horizon, stream=stream)
        if result is not None:
            results.append(result)
    return results


def compare_proxy_and_real(
    frame: pl.DataFrame, *, horizon: str, stream: str
) -> list[ComparisonResult]:
    """Part B. Each pair measured on the rows where **both** are available.

    Restricting to the intersection is what makes the comparison honest. The
    real reference is missing wherever an ETF had no bar, and letting the proxy
    run on rows the real one never saw would compare two different studies.
    """
    comparisons: list[ComparisonResult] = []

    for proxy_feature, real_feature in PROXY_VERSUS_REAL:
        if proxy_feature not in frame.columns or real_feature not in frame.columns:
            continue

        both = frame.filter(
            pl.col(proxy_feature).is_not_null()
            & pl.col(real_feature).is_not_null()
            & pl.col("raw_return").is_not_null()
        )
        if both.is_empty():
            continue

        correlation = both.select(pl.corr(proxy_feature, real_feature)).item()
        comparisons.append(
            ComparisonResult(
                proxy_feature=proxy_feature,
                real_feature=real_feature,
                horizon=horizon,
                stream=stream,
                episodes=collapse_to_episodes(both).height,
                correlation=float(correlation) if correlation is not None else None,
                proxy=analyse_feature(both, feature=proxy_feature, horizon=horizon, stream=stream),
                real=analyse_feature(both, feature=real_feature, horizon=horizon, stream=stream),
            )
        )

    return comparisons


@dataclass(frozen=True, slots=True)
class AdjustmentImpact:
    """Part C. What split adjustment did to one feature's distribution."""

    feature: str
    stream: str
    rows_changed: int
    raw_min: float | None
    raw_max: float | None
    adjusted_min: float | None
    adjusted_max: float | None

    @property
    def extreme_shrinkage(self) -> float | None:
        """How much the worst one-bar reading shrank, as a ratio.

        A value near 1 means adjustment changed nothing at the tail. The
        contaminated features sit far below it.
        """
        if self.raw_min is None or self.adjusted_min is None or self.raw_min == 0:
            return None
        return abs(self.adjusted_min) / abs(self.raw_min)


def measure_adjustment_impact(
    raw: pl.DataFrame, adjusted: pl.DataFrame, *, feature: str, stream: str
) -> AdjustmentImpact | None:
    """Part C. Compare one feature's tails before and after split adjustment.

    Args:
        raw: the featured frame built from unadjusted candles.
        adjusted: the same frame built from split-adjusted candles.
    """
    if feature not in raw.columns or feature not in adjusted.columns:
        return None

    before = raw[feature].drop_nulls()
    after = adjusted[feature].drop_nulls()
    if before.is_empty() or after.is_empty():
        return None

    changed = min(before.len(), after.len())
    differing = 0
    if before.len() == after.len():
        differing = int((before - after).abs().gt(1e-9).sum())
        changed = differing

    return AdjustmentImpact(
        feature=feature,
        stream=stream,
        rows_changed=changed,
        raw_min=float(before.min()),  # type: ignore[arg-type]
        raw_max=float(before.max()),  # type: ignore[arg-type]
        adjusted_min=float(after.min()),  # type: ignore[arg-type]
        adjusted_max=float(after.max()),  # type: ignore[arg-type]
    )


REGIME_STATES: Final[tuple[str, ...]] = (
    "market_up_sector_strong",
    "market_up_sector_weak",
    "market_down_sector_strong",
    "market_down_sector_weak",
)
"""The four states of part D, named before running.

``market_up`` is SPY above its own EMA50 -- a state, not a prediction.
``sector_strong`` is the sector fund's return above SPY's at the same instant.
Both are causal: each uses only bars at or before the observation.
"""


def analyse_real_regime(frame: pl.DataFrame) -> list[BucketResult]:
    """Part D. Outcomes in each real market x sector state.

    The proxy version of this (phase 7 part C) was flat. Flat again here would
    say sector membership carries no conditional information; a difference would
    say the proxies were cancelling each other out, since a sector proxy drawn
    from the universe and a market proxy drawn from the same universe share most
    of their constituents.
    """
    needed = {"index_above_ema50", "sector_etf_ret_1d_pct", "index_ret_1d_pct", "raw_return"}
    if not needed.issubset(frame.columns):
        return []

    present = frame.filter(
        pl.col("index_above_ema50").is_not_null()
        & pl.col("sector_etf_ret_1d_pct").is_not_null()
        & pl.col("index_ret_1d_pct").is_not_null()
        & pl.col("raw_return").is_not_null()
    ).with_columns(
        (pl.col("sector_etf_ret_1d_pct") > pl.col("index_ret_1d_pct")).alias("sector_strong")
    )
    if present.is_empty():
        return []

    results: list[BucketResult] = []
    for label, market_up, sector_strong in (
        ("market_up_sector_strong", True, True),
        ("market_up_sector_weak", True, False),
        ("market_down_sector_strong", False, True),
        ("market_down_sector_weak", False, False),
    ):
        subset = present.filter(
            (pl.col("index_above_ema50") == market_up) & (pl.col("sector_strong") == sector_strong)
        )
        if subset.is_empty():
            continue
        results.append(_summarise(collapse_to_episodes(subset), label=label, low=0.0, high=0.0))

    return results


def verdict_summary(results: list[FeatureResult]) -> dict[str, int]:
    """Count verdicts, so a run reports its shape in one line."""
    counts: dict[str, int] = {}
    for result in results:
        counts[result.verdict] = counts.get(result.verdict, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Part G: the pre-registered hypothesis
# ---------------------------------------------------------------------------
STOCK_STRONG_FEATURE: Final = "relative_strength_market_1d"
"""What "stock strong" means. Fixed before any outcome was inspected."""

STOCK_STRONG_QUANTILE: Final = 0.80
"""Top quintile of relative strength, measured **within each timestamp**.

Cross-sectional, not time-series: "strong" means strong against the other 51
names at that instant, which is what a live scanner ranking would have seen.
A trailing-window threshold would drift with the market and make the four
states incomparable across regimes.
"""

PREREGISTERED_STATES: Final[tuple[tuple[str, bool, bool], ...]] = (
    ("stock_strong / sector_strong / market_strong", True, True),
    ("stock_strong / sector_strong / market_weak", True, False),
    ("stock_strong / sector_weak / market_strong", False, True),
    ("stock_strong / sector_weak / market_weak", False, False),
)
"""The four cells of the primary hypothesis, in the brief's own order.

All are conditioned on the stock already being strong; the question is whether
*context* changes the outcome of an already-selected name. Definitions frozen
before outcomes were inspected: ``sector_strong`` is the sector fund's 1-day
return above the market fund's, ``market_strong`` is SPY above its own EMA50.
"""

RANK_BUCKETS: Final[tuple[tuple[str, float, float], ...]] = (
    ("top 10%", 0.90, 1.01),
    ("top 20%", 0.80, 1.01),
    ("middle 60%", 0.20, 0.80),
    ("bottom 20%", 0.00, 0.20),
)
"""Relative-strength rank slices, matching phase 7 exactly so the two compare.

Overlapping on purpose -- "top 10%" sits inside "top 20%" -- because the
operational question is whether concentrating further helps, which nesting
answers and a partition does not.
"""


def _with_cross_sectional_rank(frame: pl.DataFrame, feature: str) -> pl.DataFrame:
    """Percentile rank of ``feature`` **within each timestamp**.

    Causal: a rank at time T uses only other symbols' values at T, never a
    later cross-section. This is the same construction phase 7 used.
    """
    return frame.filter(pl.col(feature).is_not_null()).with_columns(
        (pl.col(feature).rank("average").over("timestamp") / pl.len().over("timestamp")).alias(
            "_rank"
        )
    )


def analyse_preregistered_states(frame: pl.DataFrame) -> list[BucketResult]:
    """Part G. The four frozen states, all conditioned on a strong stock."""
    needed = {
        STOCK_STRONG_FEATURE,
        "sector_etf_ret_1d_pct",
        "index_ret_1d_pct",
        "index_above_ema50",
        "raw_return",
    }
    if not needed.issubset(frame.columns):
        return []

    ranked = _with_cross_sectional_rank(
        frame.filter(
            pl.col("sector_etf_ret_1d_pct").is_not_null()
            & pl.col("index_ret_1d_pct").is_not_null()
            & pl.col("index_above_ema50").is_not_null()
            & pl.col("raw_return").is_not_null()
        ),
        STOCK_STRONG_FEATURE,
    )
    strong = ranked.filter(pl.col("_rank") >= STOCK_STRONG_QUANTILE).with_columns(
        (pl.col("sector_etf_ret_1d_pct") > pl.col("index_ret_1d_pct")).alias("_sector_strong")
    )
    if strong.is_empty():
        return []

    results: list[BucketResult] = []
    for label, sector_strong, market_strong in PREREGISTERED_STATES:
        subset = strong.filter(
            (pl.col("_sector_strong") == sector_strong)
            & (pl.col("index_above_ema50") == market_strong)
        )
        if subset.is_empty():
            continue
        results.append(_summarise(collapse_to_episodes(subset), label=label, low=0.0, high=0.0))
    return results


def analyse_rank_buckets(frame: pl.DataFrame, *, feature: str) -> list[BucketResult]:
    """Part G. Cross-sectional relative-strength rank slices."""
    if feature not in frame.columns or "raw_return" not in frame.columns:
        return []

    ranked = _with_cross_sectional_rank(frame.filter(pl.col("raw_return").is_not_null()), feature)
    if ranked.is_empty():
        return []

    results: list[BucketResult] = []
    for label, low, high in RANK_BUCKETS:
        subset = ranked.filter((pl.col("_rank") >= low) & (pl.col("_rank") < high))
        if subset.is_empty():
            continue
        results.append(_summarise(collapse_to_episodes(subset), label=label, low=low, high=high))
    return results


# ---------------------------------------------------------------------------
# Part I: walk-forward stability
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class YearResult:
    """One feature's separation within one calendar year."""

    feature: str
    year: int
    episodes: int
    spread: float | None
    baseline_rate: float | None


def analyse_by_year(frame: pl.DataFrame, *, feature: str, horizon: str) -> list[YearResult]:
    """Part I. The same quantile analysis, chronologically partitioned.

    Never shuffled: each block is a calendar year, evaluated on its own rows.
    A pooled spread that reverses sign between years is not an edge, and this is
    the only view that shows it -- phase 6's headline 5.5pp looked stable until
    it was split this way.
    """
    if "year" not in frame.columns or feature not in frame.columns:
        return []

    results: list[YearResult] = []
    for year in sorted({int(y) for y in frame["year"].drop_nulls().to_list()}):
        block = frame.filter(pl.col("year") == year)
        analysed = analyse_feature(block, feature=feature, horizon=horizon, stream=str(year))
        if analysed is None:
            continue
        results.append(
            YearResult(
                feature=feature,
                year=year,
                episodes=sum(b.episodes for b in analysed.buckets),
                spread=analysed.spread,
                baseline_rate=analysed.baseline_rate,
            )
        )
    return results


def is_stable(results: list[YearResult], *, floor: float = MEANINGFUL_SPREAD) -> bool:
    """Whether a feature keeps both its sign and its size across every year.

    Deliberately strict. The brief's rule is that a pooled positive which
    reverses across years is not an edge, so one sign flip is disqualifying and
    no averaging can rescue it.
    """
    spreads = [r.spread for r in results if r.spread is not None]
    if len(spreads) < 2:  # noqa: PLR2004
        return False
    if not (all(s > 0 for s in spreads) or all(s < 0 for s in spreads)):
        return False
    return all(abs(s) >= floor for s in spreads)


# ---------------------------------------------------------------------------
# Part J: redundancy
# ---------------------------------------------------------------------------
REDUNDANCY_FEATURES: Final[tuple[str, ...]] = (
    "ret_1d_pct",
    "ret_5d_pct",
    "bars_above_ema50",
    "px_vs_ema50_pct",
    "rel_strength_market_pct",
    "rel_strength_sector_pct",
    "proxy_ret_1d_pct",
    "relative_strength_market_1d",
    "relative_strength_market_5d",
    "relative_strength_sector_1d",
    "relative_strength_nasdaq_1d",
    "index_ret_1d_pct",
    "nasdaq_ret_1d_pct",
    "sector_etf_ret_1d_pct",
    "sector_relative_to_market",
)
"""Everything part J compares: stock momentum, trend persistence, the old proxy
features, and the new real-ETF ones. A new feature that is 0.95-correlated with
one already present is not independent evidence, whatever its spread says."""


def redundancy_matrix(
    frame: pl.DataFrame, *, features: Sequence[str] = REDUNDANCY_FEATURES
) -> list[tuple[str, str, float]]:
    """Pairwise Pearson correlation, descending by absolute value."""
    present = [f for f in features if f in frame.columns]
    pairs: list[tuple[str, str, float]] = []
    for i, left in enumerate(present):
        for right in present[i + 1 :]:
            both = frame.select(left, right).drop_nulls()
            if both.height < 2:  # noqa: PLR2004
                continue
            value = both.select(pl.corr(left, right)).item()
            if value is not None:
                pairs.append((left, right, float(value)))
    return sorted(pairs, key=lambda p: abs(p[2]), reverse=True)
