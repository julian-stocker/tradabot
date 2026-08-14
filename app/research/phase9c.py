"""Phase 9C: can volatility persistence make weak directional information useful?

The state of the evidence entering this phase
---------------------------------------------
Phase 6 tested 130 feature x stream x horizon combinations and found 128
``NO_INFORMATION``. Phase 7 found cross-sectional ranking unstable across years.
Phase 8 found every volatility-based *strategy* at or below buy-and-hold. Phase
9A found real ETF context ``NO_ADDITIONAL_INFORMATION``, correlating +0.978 with
the proxy it replaced. Phase 9B confirmed none of that was a data artefact.

One result survived all of it: **volatility persists**. Spearman 0.96 at one
session, 0.68 at twenty, over ~690,000 observations.

So this phase asks the only remaining question that does not require new data:
*conditional on expected movement being elevated, does any existing directional
feature become materially more informative?* Not "does high volatility predict
up" -- that is a different and much sillier question.

Why a null result is the expected outcome
-----------------------------------------
Volatility clustering says nothing about sign. If a feature carries no
directional information unconditionally, conditioning on a sign-agnostic state
has no mechanism by which to create it -- the honest prior is that the spreads
stay flat and the sample simply gets smaller. This module is built to detect a
real effect if one exists and, failing that, to say so precisely rather than
mining until something clears a threshold.

The one place a positive result is genuinely likely is part E, and it is not
about direction at all: if volatility-v1 predicts *magnitude* well, it improves
stop distance, position size and holding period regardless of whether direction
is ever predictable. That is a risk finding, and it counts.

Everything here is frozen before any outcome was inspected
----------------------------------------------------------
Regimes, feature list, match definitions, extension buckets, calibration
multipliers and cost scenarios are all module constants, fixed in advance. The
brief forbids tuning after seeing results, and a constant that a reader can
diff against the report is the only version of that promise worth making.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import polars as pl

from app.research.phase6 import (
    MEANINGFUL_SPREAD,
    BucketResult,
    FeatureResult,
    _summarise,
    analyse_feature,
    collapse_to_episodes,
)
from app.research.phase8 import VOL_REGIMES

REGIME_LABELS: Final[tuple[str, ...]] = tuple(label for label, _, _ in VOL_REGIMES)

ELEVATED_REGIMES: Final[tuple[str, ...]] = ("HIGH_VOL", "EXTREME_VOL")
"""What "elevated expected movement" means throughout this phase.

The top 30% of a stock's own trailing ATR% distribution. Reusing phase 8's bands
rather than inventing new ones is what makes the two phases comparable; a fresh
cut here would be a free parameter.
"""

DIRECTIONAL_FEATURES: Final[tuple[str, ...]] = (
    # Trend and persistence
    "bars_above_ema50",
    "px_vs_ema50_pct",
    "ema50_slope_pct",
    # Momentum
    "ret_1d_pct",
    "ret_5d_pct",
    # Exhaustion / extension
    "rsi14",
    "dist_ema20_atr",
    # Participation
    "rel_volume",
    # Context, real references from phase 9A
    "relative_strength_market_1d",
    "relative_strength_sector_1d",
    # The production model itself
    "score",
)
"""Eleven features, all previously researched. **Deliberately not a zoo.**

The brief forbids inventing new indicators to search for an edge, and phase 6
already established that this universe's features are 80-98% correlated with
each other -- a wider net would add tests, not information. One representative
per concept, each with a documented phase-6 or phase-9A verdict to compare
against.
"""

MATCHES: Final[dict[str, str]] = {
    "A_vol_and_trend": "elevated volatility + strong trend persistence",
    "B_vol_and_market_rs": "elevated volatility + positive relative strength vs market",
    "C_vol_and_sector_rs": "elevated volatility + positive relative strength vs sector",
    "D_vol_and_full_alignment": "elevated volatility + market up + sector up + stock trend up",
    "E_vol_and_extended": "elevated volatility + stock extended above EMA20",
}
"""The five pre-registered cross-sectional combinations, in the brief's order.

Intuitive rather than exhaustive: each states a hypothesis someone would
actually hold, which is what makes a flat result informative. A grid search over
feature pairs would guarantee a winner and mean nothing.
"""

TREND_PERSISTENCE_BARS: Final = 35
"""Bars above EMA50 that count as "strong trend persistence" (match A).

Five sessions of hourly bars. Fixed in advance; the brief forbids moving it
after seeing outcomes.
"""

EXTENSION_ATR: Final = 2.0
"""Distance above EMA20, in ATR, that counts as "extended" (match E)."""

EXTENSION_BUCKETS: Final[tuple[tuple[str, float, float], ...]] = (
    ("deeply below (< -2 ATR)", -99.0, -2.0),
    ("below (-2 to -0.5)", -2.0, -0.5),
    ("near EMA20 (-0.5 to 0.5)", -0.5, 0.5),
    ("extended (0.5 to 2)", 0.5, 2.0),
    ("deeply extended (> 2 ATR)", 2.0, 99.0),
)
"""Symmetric bands for part D's extension question.

Symmetric on purpose. "Overbought means sell" is a belief, not a measurement,
and testing only the upper tail would make the asymmetry question unanswerable.
Phase 6 already found ATR extension moves MFE and MAE by the same factor; this
re-asks it conditional on elevated volatility, where the folk claim is loudest.
"""

CALIBRATION_MULTIPLIERS: Final[tuple[float, ...]] = (0.5, 1.0, 1.5, 2.0)
"""Fractions of the predicted range tested for containment in part E."""

COST_SCENARIOS: Final[tuple[tuple[str, float], ...]] = (
    ("modelled", 0.0),
    ("20 bps round trip", 0.20),
    ("50 bps round trip", 0.50),
)
"""Round-trip costs in percent, applied to a subgroup's mean return.

The modelled figure is carried as zero here and quoted separately from the
project's own cost model: this part asks whether an observed advantage has room
to survive *any* plausible friction, and a 0.2% edge that dies at 20 bps is not
a finding whatever the internal model says.
"""

MIN_EPISODES_FOR_CLAIM: Final = 200
"""Episodes required before a conditional result may be called anything but
``INSUFFICIENT``. Conditioning shrinks samples fast, and a 5pp spread on 40
episodes is noise wearing a verdict."""


@dataclass(frozen=True, slots=True)
class ConditionalResult:
    """One feature measured unconditionally and inside one volatility regime."""

    feature: str
    regime: str
    horizon: str
    stream: str
    unconditional: FeatureResult | None
    conditional: FeatureResult | None

    @property
    def episodes(self) -> int:
        if self.conditional is None:
            return 0
        return sum(b.episodes for b in self.conditional.buckets)

    @property
    def improvement(self) -> float | None:
        """Conditional |spread| minus unconditional |spread|, in rate points.

        Absolute values because the question is whether the feature *separates*
        outcomes better, not whether it points the same way. A feature that
        reverses sign under conditioning is not improved; it is unstable, and
        part H is where that gets caught.
        """
        if self.unconditional is None or self.conditional is None:
            return None
        if self.unconditional.spread is None or self.conditional.spread is None:
            return None
        return abs(self.conditional.spread) - abs(self.unconditional.spread)

    @property
    def verdict(self) -> str:
        if self.conditional is None or self.episodes < MIN_EPISODES_FOR_CLAIM:
            return "INSUFFICIENT"
        if self.conditional.spread is None:
            return "INSUFFICIENT"
        if abs(self.conditional.spread) < MEANINGFUL_SPREAD:
            return "NO_INFORMATION"
        return self.conditional.verdict


def analyse_conditional(
    frame: pl.DataFrame, *, feature: str, horizon: str, stream: str
) -> list[ConditionalResult]:
    """Part C. One feature, measured unconditionally then per regime.

    The comparison the brief asks for is *the same feature against itself*, so
    both sides use identical bucketing and the only difference is the rows.
    """
    if "vol_regime" not in frame.columns:
        return []

    baseline = analyse_feature(frame, feature=feature, horizon=horizon, stream=stream)
    results: list[ConditionalResult] = []

    for regime in (*REGIME_LABELS, "ELEVATED"):
        subset = (
            frame.filter(pl.col("vol_regime").is_in(ELEVATED_REGIMES))
            if regime == "ELEVATED"
            else frame.filter(pl.col("vol_regime") == regime)
        )
        results.append(
            ConditionalResult(
                feature=feature,
                regime=regime,
                horizon=horizon,
                stream=stream,
                unconditional=baseline,
                conditional=analyse_feature(
                    subset, feature=feature, horizon=horizon, stream=stream
                ),
            )
        )
    return results


def _elevated() -> pl.Expr:
    return pl.col("vol_regime").is_in(ELEVATED_REGIMES)


def match_masks(frame: pl.DataFrame) -> dict[str, pl.Expr]:
    """Part D. The five frozen combinations as boolean expressions.

    Returned as expressions rather than filtered frames so a caller can compose
    them with a baseline on identical rows.
    """
    masks: dict[str, pl.Expr] = {}
    elevated = _elevated()

    if "bars_above_ema50" in frame.columns:
        masks["A_vol_and_trend"] = elevated & (pl.col("bars_above_ema50") >= TREND_PERSISTENCE_BARS)
    if "relative_strength_market_1d" in frame.columns:
        masks["B_vol_and_market_rs"] = elevated & (pl.col("relative_strength_market_1d") > 0)
    if "relative_strength_sector_1d" in frame.columns:
        masks["C_vol_and_sector_rs"] = elevated & (pl.col("relative_strength_sector_1d") > 0)
    if {"index_above_ema50", "sector_above_ema50", "trend_stacked"}.issubset(frame.columns):
        masks["D_vol_and_full_alignment"] = (
            elevated
            & pl.col("index_above_ema50")
            & pl.col("sector_above_ema50")
            & pl.col("trend_stacked")
        )
    if "dist_ema20_atr" in frame.columns:
        masks["E_vol_and_extended"] = elevated & (pl.col("dist_ema20_atr") >= EXTENSION_ATR)

    return masks


def analyse_matches(frame: pl.DataFrame) -> list[BucketResult]:
    """Part D. Each pre-registered match against the universe baseline.

    The baseline row is included in the output deliberately: a match that
    delivers 53% means nothing until the reader can see the base rate beside it.
    """
    if "raw_return" not in frame.columns:
        return []

    present = frame.filter(pl.col("raw_return").is_not_null())
    if present.is_empty():
        return []

    results = [
        _summarise(collapse_to_episodes(present), label="baseline (all rows)", low=0.0, high=0.0)
    ]
    if "vol_regime" in present.columns:
        elevated = present.filter(_elevated())
        if not elevated.is_empty():
            results.append(
                _summarise(
                    collapse_to_episodes(elevated),
                    label="elevated volatility only",
                    low=0.0,
                    high=0.0,
                )
            )

    for name, mask in match_masks(present).items():
        subset = present.filter(mask)
        if subset.is_empty():
            continue
        results.append(_summarise(collapse_to_episodes(subset), label=name, low=0.0, high=0.0))
    return results


def analyse_extension(frame: pl.DataFrame, *, elevated_only: bool = True) -> list[BucketResult]:
    """Part D(E). Outcome asymmetry across distance from EMA20, in ATR.

    Reports MFE and MAE alongside the positive rate because the folk claim is
    specifically about asymmetry -- "overbought falls harder" -- and a positive
    rate alone cannot see that. Phase 6 found MFE and MAE deteriorate by the
    same factor unconditionally; this asks whether elevated volatility changes
    that.
    """
    needed = {"dist_ema20_atr", "raw_return"}
    if not needed.issubset(frame.columns):
        return []

    present = frame.filter(
        pl.col("dist_ema20_atr").is_not_null() & pl.col("raw_return").is_not_null()
    )
    if elevated_only and "vol_regime" in present.columns:
        present = present.filter(_elevated())
    if present.is_empty():
        return []

    results: list[BucketResult] = []
    for label, low, high in EXTENSION_BUCKETS:
        subset = present.filter(
            (pl.col("dist_ema20_atr") >= low) & (pl.col("dist_ema20_atr") < high)
        )
        if subset.is_empty():
            continue
        results.append(_summarise(collapse_to_episodes(subset), label=label, low=low, high=high))
    return results


@dataclass(frozen=True, slots=True)
class CalibrationCoverage:
    """Part E. How often realised movement stayed inside a predicted band."""

    regime: str
    basis: str
    """``typical`` or ``stress`` -- which frozen figure the band came from."""
    multiplier: float
    predicted_pct: float
    observations: int
    contained: int

    @property
    def coverage(self) -> float:
        return self.contained / self.observations if self.observations else 0.0


def measure_calibration(
    frame: pl.DataFrame,
    calibration: dict[str, tuple[float, float]],
    *,
    multipliers: Sequence[float] = CALIBRATION_MULTIPLIERS,
) -> list[CalibrationCoverage]:
    """Part E. Containment of realised range inside volatility-v1's prediction.

    Args:
        frame: rows carrying ``vol_regime`` and ``realised_range_pct``, the
            forward high-low range as a percentage of the entry close.
        calibration: ``{regime: (typical_pct, stress_pct)}``, the **frozen**
            phase-8 figures. Passed in rather than imported so a caller cannot
            accidentally test the model against numbers it just derived from the
            same rows.

    Calibration, not profitability. A band that contains 50% of outcomes is not
    "wrong" -- ``typical`` is a median, so 50% is exactly right, and reading it
    as a failure is how a risk model gets quietly widened until it says nothing.
    """
    needed = {"vol_regime", "realised_range_pct"}
    if not needed.issubset(frame.columns):
        return []

    present = frame.filter(
        pl.col("vol_regime").is_not_null() & pl.col("realised_range_pct").is_not_null()
    )
    results: list[CalibrationCoverage] = []

    for regime, (typical, stress) in calibration.items():
        subset = present.filter(pl.col("vol_regime") == regime)
        if subset.is_empty():
            continue
        realised = subset["realised_range_pct"]
        for basis, predicted in (("typical", typical), ("stress", stress)):
            for multiplier in multipliers:
                band = predicted * multiplier
                results.append(
                    CalibrationCoverage(
                        regime=regime,
                        basis=basis,
                        multiplier=multiplier,
                        predicted_pct=band,
                        observations=realised.len(),
                        contained=int((realised <= band).sum()),
                    )
                )
    return results


def fixed_band_coverage(
    frame: pl.DataFrame, *, bands_pct: Sequence[float]
) -> list[tuple[float, int, float]]:
    """Part E control: the same containment using one fixed percentage for all.

    The comparison that decides whether volatility conditioning is worth
    anything. A regime-aware band is only better if it is *more evenly*
    calibrated across regimes than a single number -- being right on average is
    what the fixed band already does.
    """
    if "realised_range_pct" not in frame.columns:
        return []
    realised = frame["realised_range_pct"].drop_nulls()
    if realised.is_empty():
        return []
    return [
        (band, int((realised <= band).sum()), int((realised <= band).sum()) / realised.len())
        for band in bands_pct
    ]


def survives_costs(mean_return_pct: float | None, *, edge_over_baseline: float | None) -> str:
    """Part G. Whether an observed advantage has room for friction.

    Judged on the **edge over baseline**, not the raw return. A subgroup
    returning +0.30% where the universe returns +0.28% has a 0.02pp advantage,
    and quoting the 0.30% against a 20 bps cost would flatter it by an order of
    magnitude.
    """
    if mean_return_pct is None or edge_over_baseline is None:
        return "UNKNOWN"
    for label, cost in COST_SCENARIOS:
        if cost > 0 and edge_over_baseline * 100 <= cost:
            return f"CONSUMED_BY_{label.split()[0]}_BPS"
    return "SURVIVES_50_BPS"


def classify_candidate(
    *,
    episodes: int,
    edge_pp: float | None,
    sign_stable: bool,
    cost_verdict: str,
) -> str:
    """Part I. The decision gate, applied mechanically.

    ``ROBUST`` requires all four conditions from the brief at once. Written as
    one function so no candidate can be promoted by a reader who liked its
    table, and so the bar is visible rather than argued.
    """
    if episodes < MIN_EPISODES_FOR_CLAIM or edge_pp is None:
        return "INSUFFICIENT"
    if abs(edge_pp) < MEANINGFUL_SPREAD:
        return "NO_INFORMATION"
    if not sign_stable:
        return "NO_EDGE"
    if not cost_verdict.startswith("SURVIVES"):
        return "NO_EDGE"
    return "ROBUST"
