"""Phase 8: is predictable *magnitude* actually tradable?

Phase 7 established that volatility persists strongly (Spearman 0.95 at one
session, 0.65 at twenty, over 550k observations) while direction does not. That
is a statistical fact, not a strategy. This module tests the gap between the two.

The architecture under test inverts the usual order. Instead of predicting
direction and then sizing the bet, it:

1. identifies where large movement is *likely* -- which the data supports;
2. waits for the market to reveal direction through a causal breakout -- which
   removes the need to predict it;
3. sizes and stops the position against expected movement rather than a fixed
   percentage.

The central experiment is a 2x2: breakout with and without a volatility filter,
volatility without a breakout, and the whole universe. It answers one question --
**does knowing a big move is coming improve trade selection, given that you still
do not know which way?** A flat answer there falsifies the architecture, and that
is a perfectly good outcome.

Every threshold in this module was fixed before any of it was run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import polars as pl

from app.research.featureset import BARS_PER_DAY
from app.research.phase6 import _as_float

VOL_REGIMES: Final[tuple[tuple[str, float, float], ...]] = (
    ("LOW_VOL", 0.00, 0.25),
    ("NORMAL_VOL", 0.25, 0.70),
    ("HIGH_VOL", 0.70, 0.90),
    ("EXTREME_VOL", 0.90, 1.01),
)
"""Percentile bands of a stock's ATR% **within its own trailing history**.

Relative rather than absolute because a 2% daily range is ordinary for a
semiconductor and extraordinary for a utility. One universal percentage would
classify the universe by sector rather than by state.

EXTREME is separated from HIGH deliberately: part F asks whether the top decile
is a better opportunity or a worse one, and merging them would make that
unanswerable.
"""

RANGE_LOOKBACK: Final = 60
"""Bars used to establish a range. Roughly nine sessions of hourly bars."""

RANGE_ENTRY_ZONE: Final = 0.25
"""Fraction of the established range, measured from its low, that counts as a
lower-range entry. Fixed in advance; the brief forbids tuning it to outcomes."""

MAX_RANGE_TREND_SLOPE: Final = 0.5
"""Above this |EMA50 slope| a window is trending, not ranging, so a
mean-reversion entry there is a different (and unasked) experiment."""

COST_SCENARIOS: Final[tuple[tuple[str, float], ...]] = (
    ("modelled ~20bps", 0.20),
    ("stress 50bps", 0.50),
    ("extreme 100bps", 1.00),
)
"""Round-trip costs charged per trade. Part L's requirement, declared up front."""


def add_volatility_regime(frame: pl.DataFrame) -> pl.DataFrame:
    """Label each row LOW / NORMAL / HIGH / EXTREME from its own ATR percentile.

    Uses ``atr_pct_percentile``, which is a trailing rank computed in
    :mod:`app.research.featureset` and therefore causal: it compares today
    against this stock's recent past and never against its future.
    """
    if "atr_pct_percentile" not in frame.columns:
        return frame

    # Built as an untyped chain: polars' when/then builder changes class as it is
    # extended, so a single annotation cannot describe both ends of the loop.
    expression: Any = pl.when(pl.col("atr_pct_percentile").is_null()).then(
        pl.lit(None, dtype=pl.String)
    )
    for label, low, high in VOL_REGIMES:
        expression = expression.when(
            (pl.col("atr_pct_percentile") >= low) & (pl.col("atr_pct_percentile") < high)
        ).then(pl.lit(label))
    labelled: pl.Expr = expression.otherwise(pl.lit("EXTREME_VOL"))
    return frame.with_columns(labelled.alias("vol_regime"))


@dataclass(frozen=True, slots=True)
class CalibrationRow:
    """What a volatility bucket actually delivered."""

    regime: str
    observations: int
    median_predicted_atr_pct: float
    median_realised_range_pct: float
    median_abs_return_pct: float
    p90_range_pct: float

    def render(self) -> str:
        return (
            f"    {self.regime:<13}n={self.observations:>7,}  "
            f"predicted ATR={self.median_predicted_atr_pct:>5.2f}%  "
            f"realised range median={self.median_realised_range_pct:>5.2f}%  "
            f"p90={self.p90_range_pct:>5.2f}%  "
            f"|return| median={self.median_abs_return_pct:>5.2f}%"
        )


def calibrate(candles: pl.DataFrame, *, sessions: int) -> list[CalibrationRow]:
    """Predicted movement versus realised movement, by regime.

    The forward window is built with a negative shift, which makes this a
    **measurement of the future and not a feature**. Nothing here may be read at
    time T; it exists to answer "when the system says HIGH, how much bigger is
    what follows?".
    """
    bars = sessions * BARS_PER_DAY
    forward = candles.with_columns(
        pl.col("high").rolling_max(bars).shift(-bars).over("instrument_id").alias("fwd_high"),
        pl.col("low").rolling_min(bars).shift(-bars).over("instrument_id").alias("fwd_low"),
        pl.col("close").shift(-bars).over("instrument_id").alias("fwd_close"),
    ).drop_nulls(["fwd_high", "fwd_low", "fwd_close", "vol_regime", "atr_pct"])

    scored = forward.with_columns(
        ((pl.col("fwd_high") - pl.col("fwd_low")) / pl.col("close") * 100).alias("realised_range"),
        ((pl.col("fwd_close") / pl.col("close") - 1).abs() * 100).alias("abs_return"),
    )

    rows: list[CalibrationRow] = []
    for label, _, _ in VOL_REGIMES:
        subset = scored.filter(pl.col("vol_regime") == label)
        if subset.height == 0:
            continue
        rows.append(
            CalibrationRow(
                regime=label,
                observations=subset.height,
                median_predicted_atr_pct=_as_float(subset["atr_pct"].median()) or 0.0,
                median_realised_range_pct=_as_float(subset["realised_range"].median()) or 0.0,
                median_abs_return_pct=_as_float(subset["abs_return"].median()) or 0.0,
                p90_range_pct=_as_float(subset["realised_range"].quantile(0.90)) or 0.0,
            )
        )
    return rows


def transition_matrix(candles: pl.DataFrame, *, sessions: int) -> list[str]:
    """Probability of moving between volatility regimes over ``sessions``.

    Persistence has to be strong here for a live "expected movement" field to be
    honest: a regime that dissolves within a day would make the label stale by
    the time anyone read it.
    """
    bars = sessions * BARS_PER_DAY
    paired = (
        candles.with_columns(
            pl.col("vol_regime").shift(-bars).over("instrument_id").alias("next_regime")
        )
        .drop_nulls(["vol_regime", "next_regime"])
        .group_by(["vol_regime", "next_regime"])
        .len()
    )
    if paired.height == 0:
        return []

    totals = paired.group_by("vol_regime").agg(pl.col("len").sum().alias("total"))
    joined = paired.join(totals, on="vol_regime").with_columns(
        (pl.col("len") / pl.col("total") * 100).alias("pct")
    )

    lines: list[str] = []
    for label, _, _ in VOL_REGIMES:
        row = joined.filter(pl.col("vol_regime") == label)
        if row.height == 0:
            continue
        parts = [
            f"{nxt}={pct:.0f}%"
            for nxt, pct in sorted(
                zip(row["next_regime"].to_list(), row["pct"].to_list(), strict=True),
                key=lambda item: -item[1],
            )
        ]
        lines.append(f"    {label:<13}-> " + "  ".join(parts))
    return lines


def breakout_entries(frame: pl.DataFrame) -> pl.DataFrame:
    """Rows where price closed above the prior 20-bar high.

    The prior window is shifted by one in :mod:`app.research.featureset`, so a
    bar cannot break a range it is itself the top of. Long only -- the brief
    forbids quietly introducing short selling, and the production system is
    long-oriented.
    """
    if "breakout_20" not in frame.columns:
        return frame.head(0)
    return frame.filter(pl.col("breakout_20"))


def range_entries(frame: pl.DataFrame) -> pl.DataFrame:
    """Rows in the lower quarter of an established, non-trending range.

    Causal throughout: the range comes from ``prior_high_20``/``prior_low_20``,
    both of which exclude the current bar, and the trend filter uses the EMA50
    slope known at T. **No historical bottom is used** -- the entry is defined by
    position within a range that already existed, not by hindsight about where
    the low turned out to be.
    """
    needed = {"prior_high_20", "prior_low_20", "close", "ema50_slope_pct"}
    if not needed.issubset(frame.columns):
        return frame.head(0)

    width = pl.col("prior_high_20") - pl.col("prior_low_20")
    position = (pl.col("close") - pl.col("prior_low_20")) / width
    return frame.filter(
        (width > 0)
        & (position >= 0)
        & (position <= RANGE_ENTRY_ZONE)
        & (pl.col("ema50_slope_pct").abs() <= MAX_RANGE_TREND_SLOPE)
    )


@dataclass(frozen=True, slots=True)
class BreakoutOutcome:
    """Follow-through statistics for one breakout cohort (part F)."""

    label: str
    trades: int
    follow_through_rate: float
    failure_rate: float
    median_mfe_pct: float
    median_mae_pct: float
    median_bars_to_extreme: float

    def render(self) -> str:
        return (
            f"    {self.label:<24}n={self.trades:>6,}  "
            f"follow-through={self.follow_through_rate * 100:>5.1f}%  "
            f"failed={self.failure_rate * 100:>5.1f}%  "
            f"medMFE={self.median_mfe_pct:>5.2f}%  medMAE={self.median_mae_pct:>6.2f}%"
        )


FOLLOW_THROUGH_ATR: Final = 1.0
FAILURE_ATR: Final = 1.0
"""A breakout follows through if it gains one ATR before losing one, and fails
if the reverse. Symmetric on purpose: an asymmetric definition would decide the
answer by construction."""
