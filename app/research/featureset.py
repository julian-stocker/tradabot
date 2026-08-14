"""Causal research features, reconstructed from stored candles.

Phase 6 asks whether *any* feature in this data carries repeatable out-of-sample
information. Answering that needs more features than ``signal_evaluations``
happens to have persisted, so they are recomputed here from the candle table --
which is immutable, already downloaded, and the same series the scanner read.

Causality is structural, not checked afterwards
-----------------------------------------------
Every feature is a rolling or exponentially-weighted function over a
**time-sorted series, using the current row and earlier rows only**. There is no
``shift(-1)``, no centred window, no forward fill, and no reindexing that could
pull a later value backwards. Where a comparison must exclude the current bar --
"did today break the prior 20-bar high?" -- the window is explicitly shifted
forward by one so the bar cannot break its own high.

The join back to observations uses ``signal_evaluations.market_data_timestamp``,
which the engine recorded as *the newest bar that evaluation actually saw*. That
is the causal key by construction: an observation stamped 16:00 saw the bar
starting 15:00, and joining on the stored value means the feature row is exactly
the information the scanner had, not an approximation of it.

What is deliberately absent
---------------------------
* **Spread / liquidity.** Historical quotes do not exist in this database.
  Phase 5 recorded ``spread_bps`` as NULL on every replayed row rather than
  applying a 2026 spread to a 2021 bar. Marked NOT_CAUSALLY_AVAILABLE.
* **Time of day on the coarse stream.** Both streams are hourly, so a bucket
  exists -- but the coarse window has no 5m/15m context, so intraday behaviour
  there is not comparable to production. Reported separately, never pooled.
* **A real index.** *(No longer true -- see below.)* Phase 6 ran with a market
  proxy that was an **equal-weight mean of the same 52 symbols** it was
  providing context for. It is still computed, still named ``proxy``, and still
  the basis of every published phase-6 number.

Real context, added alongside
-----------------------------
Twelve reference instruments (SPY and QQQ for the market, nine sector funds,
and SOXX retained as an alternate -- see ``app.market_data.benchmarks``) are now
stored and featured exactly like any other symbol, and
:func:`attach_real_context` joins them on as
``index_*``/``nasdaq_*``/``sector_etf_*`` columns. They are **additive**: proxy columns are
untouched, so a single row carries both readings and an analysis can ask whether
they disagree rather than assuming the newer one is better.

The reference instruments are excluded from the proxy cross-section. Leaving SPY
in an equal-weight mean of its own constituents would let the reference vote on
itself; ``app.market_data.benchmarks.is_benchmark`` is the filter.

Prices are split-adjusted before any feature is computed
--------------------------------------------------------
Phase 6 read the raw candle table, in which eleven splits appear as genuine
one-bar returns of -75% to -95% -- and ``ret_1d_pct`` is the input to the market
proxy, so each one moved "the market" too. ``app.research.adjustments`` now
back-adjusts each series first, and only for splits the price series itself
corroborates.
"""

from __future__ import annotations

from typing import Final

import polars as pl

from app.market_data.benchmarks import (
    MARKET_BENCHMARK,
    SECONDARY_MARKET_BENCHMARK,
    SECTOR_BENCHMARKS,
)

BARS_PER_DAY: Final = 7
"""Hourly bars in one US regular session (09:30-16:00 -> 7 partial/whole hours).

Approximate by nature: the session is 6.5 hours, so a "1 day" return over hourly
bars is 7 bars rather than exactly one calendar day. Stated rather than hidden --
the alternative is a calendar join that would silently span weekends.
"""
BARS_PER_WEEK: Final = BARS_PER_DAY * 5

RSI_PERIOD: Final = 14
ATR_PERIOD: Final = 14
VOL_WINDOW: Final = 20
PERCENTILE_WINDOW: Final = 252
"""Roughly one trading year of hourly bars for a percentile rank... deliberately
not: 252 *bars* is about 36 sessions. Named by its length in bars, because
calling it a year would be wrong by an order of magnitude."""

STRUCTURE_SHORT: Final = 20
STRUCTURE_LONG: Final = 60
DISCOVERY_WINDOW: Final = 252

TRADING_DAYS_PER_YEAR: Final = 252
ANNUALISATION: Final = (TRADING_DAYS_PER_YEAR * BARS_PER_DAY) ** 0.5


def _rsi(close: pl.Expr, period: int = RSI_PERIOD) -> pl.Expr:
    """Wilder's RSI, causal by construction.

    ``diff`` looks backwards and the smoothing is exponential over past values,
    so the value at row *i* depends on rows <= *i* only.
    """
    delta = close.diff()
    gain = pl.when(delta > 0).then(delta).otherwise(0.0)
    loss = pl.when(delta < 0).then(-delta).otherwise(0.0)
    avg_gain = gain.ewm_mean(alpha=1.0 / period, adjust=False, min_samples=period)
    avg_loss = loss.ewm_mean(alpha=1.0 / period, adjust=False, min_samples=period)
    rs = avg_gain / (avg_loss + 1e-12)
    return 100.0 - (100.0 / (1.0 + rs))


def _true_range() -> pl.Expr:
    previous_close = pl.col("close").shift(1)
    return pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - previous_close).abs(),
        (pl.col("low") - previous_close).abs(),
    )


def _consecutive_true(flag: pl.Expr) -> pl.Expr:
    """Length of the current run of ``True``, counting backwards from this row.

    Implemented as "rows since the last False", which needs a cumulative count of
    resets -- a backwards-looking quantity. A forward-looking formulation would
    be shorter and would leak.
    """
    # `run_id` increments on every False, so it is constant within a run of
    # Trues. Counting Trues *within* that group restarts the count at each break.
    run_id = (~flag).cum_sum()
    return flag.cast(pl.Int32).cum_sum().over(run_id)


def per_symbol_features(candles: pl.DataFrame) -> pl.DataFrame:
    """Compute every per-symbol feature for one instrument's hourly series.

    Args:
        candles: columns ``instrument_id, timestamp, open, high, low, close,
            volume``, ascending by timestamp.

    Returns the same rows with feature columns appended. **Row order is the
    causal order** -- the caller must not sort descending and reuse this.
    """
    # Money is persisted as TEXT to keep decimal exactness, so the OHLCV columns
    # arrive as strings. Cast once, here, rather than in each caller: a missed
    # cast fails loudly on the first division, but a *partial* cast would not.
    candles = candles.with_columns(
        pl.col(name).cast(pl.Float64)
        for name in ("open", "high", "low", "close", "volume")
        if name in candles.columns
    )

    close = pl.col("close")
    ema20 = close.ewm_mean(span=20, adjust=False)
    ema50 = close.ewm_mean(span=50, adjust=False)
    ema200 = close.ewm_mean(span=200, adjust=False)
    atr = _true_range().ewm_mean(alpha=1.0 / ATR_PERIOD, adjust=False, min_samples=ATR_PERIOD)
    log_return = (close / close.shift(1)).log()

    frame = candles.with_columns(
        # -- trend ---------------------------------------------------------
        ema20.alias("ema20"),
        ema50.alias("ema50"),
        ema200.alias("ema200"),
        ((close / ema20 - 1.0) * 100).alias("px_vs_ema20_pct"),
        ((close / ema50 - 1.0) * 100).alias("px_vs_ema50_pct"),
        ((close / ema200 - 1.0) * 100).alias("px_vs_ema200_pct"),
        ((ema20 / ema20.shift(BARS_PER_DAY) - 1.0) * 100).alias("ema20_slope_pct"),
        ((ema50 / ema50.shift(BARS_PER_WEEK) - 1.0) * 100).alias("ema50_slope_pct"),
        ((ema20 > ema50) & (ema50 > ema200)).alias("trend_stacked"),
        # -- momentum ------------------------------------------------------
        _rsi(close).alias("rsi14"),
        ((close / close.shift(1) - 1.0) * 100).alias("ret_1h_pct"),
        ((close / close.shift(4) - 1.0) * 100).alias("ret_4h_pct"),
        ((close / close.shift(BARS_PER_DAY) - 1.0) * 100).alias("ret_1d_pct"),
        ((close / close.shift(BARS_PER_WEEK) - 1.0) * 100).alias("ret_5d_pct"),
        # -- volatility ----------------------------------------------------
        atr.alias("atr"),
        (atr / close * 100).alias("atr_pct"),
        (log_return.rolling_std(VOL_WINDOW) * ANNUALISATION * 100).alias("realised_vol_pct"),
        ((pl.col("high") - pl.col("low")) / atr).alias("range_vs_atr"),
        ((close - pl.col("open")).abs() / atr).alias("body_vs_atr"),
        # -- volume --------------------------------------------------------
        (pl.col("volume") / pl.col("volume").rolling_mean(VOL_WINDOW)).alias("rel_volume"),
        # -- structure: the shift(1) is what stops a bar breaking its own high
        pl.col("high").rolling_max(STRUCTURE_SHORT).shift(1).alias("prior_high_20"),
        pl.col("low").rolling_min(STRUCTURE_SHORT).shift(1).alias("prior_low_20"),
        pl.col("high").rolling_max(STRUCTURE_LONG).shift(1).alias("prior_high_60"),
        pl.col("high").rolling_max(DISCOVERY_WINDOW).shift(1).alias("prior_high_252"),
    )

    return frame.with_columns(
        ((close - pl.col("ema20")) / pl.col("atr")).alias("dist_ema20_atr"),
        ((close - pl.col("ema50")) / pl.col("atr")).alias("dist_ema50_atr"),
        (pl.col("rsi14") - pl.col("rsi14").shift(BARS_PER_DAY)).alias("rsi_change_1d"),
        (pl.col("ret_1d_pct") - pl.col("ret_1d_pct").shift(BARS_PER_DAY)).alias("momentum_accel"),
        (pl.col("atr_pct").rolling_map(_rank_last, PERCENTILE_WINDOW, min_samples=60)).alias(
            "atr_pct_percentile"
        ),
        (pl.col("rel_volume") - pl.col("rel_volume").shift(BARS_PER_DAY)).alias("volume_accel"),
        ((close / pl.col("prior_high_20") - 1.0) * 100).alias("dist_from_high20_pct"),
        ((close / pl.col("prior_low_20") - 1.0) * 100).alias("dist_from_low20_pct"),
        (close > pl.col("prior_high_20")).alias("breakout_20"),
        (close < pl.col("prior_low_20")).alias("breakdown_20"),
        (close > pl.col("prior_high_252")).alias("price_discovery"),
        _consecutive_true(close > pl.col("ema50")).alias("bars_above_ema50"),
    )


def _rank_last(window: pl.Series) -> float:
    """Percentile rank of the window's final value **within that window**.

    The window ends at the current row, so this is "where does today sit relative
    to the recent past" -- and never relative to the future.
    """
    if window.len() == 0:
        return float("nan")
    last = window[-1]
    if last is None:
        return float("nan")
    return float((window <= last).sum()) / float(window.len())


def market_proxy(frame: pl.DataFrame) -> pl.DataFrame:
    """An equal-weight breadth proxy from the universe itself.

    **Not an index.** There is no ETF in the watchlist, so this averages the
    per-bar returns of whatever symbols traded in that bar. Cross-sectional at
    each timestamp and therefore causal: it uses no information from later bars,
    only from other symbols at the same instant, which a live scanner also had.
    """
    return (
        frame.group_by("timestamp")
        .agg(
            pl.col("ret_1d_pct").mean().alias("proxy_ret_1d_pct"),
            pl.col("px_vs_ema50_pct").mean().alias("proxy_px_vs_ema50_pct"),
            pl.col("trend_stacked").mean().alias("proxy_breadth_stacked"),
            pl.len().alias("proxy_symbols"),
        )
        .sort("timestamp")
    )


def sector_proxy(frame: pl.DataFrame) -> pl.DataFrame:
    """The same construction, within each sector."""
    return (
        frame.group_by(["timestamp", "sector"])
        .agg(
            pl.col("ret_1d_pct").mean().alias("sector_ret_1d_pct"),
            pl.len().alias("sector_symbols"),
        )
        .sort("timestamp")
    )


def attach_context(frame: pl.DataFrame) -> pl.DataFrame:
    """Join the market and sector proxies and derive relative strength."""
    market = market_proxy(frame)
    joined = frame.join(market, on="timestamp", how="left")

    if "sector" in frame.columns:
        joined = joined.join(sector_proxy(frame), on=["timestamp", "sector"], how="left")
        joined = joined.with_columns(
            (pl.col("ret_1d_pct") - pl.col("sector_ret_1d_pct")).alias("rel_strength_sector_pct")
        )

    return joined.with_columns(
        (pl.col("ret_1d_pct") - pl.col("proxy_ret_1d_pct")).alias("rel_strength_market_pct"),
        (pl.col("proxy_px_vs_ema50_pct") > 0).alias("market_above_ema50"),
    )


def attach_real_context(frame: pl.DataFrame, benchmarks: pl.DataFrame) -> pl.DataFrame:
    """Join real index and sector-ETF context, and derive relative strength.

    The counterpart to :func:`attach_context`, and deliberately additive rather
    than a replacement: both sets of columns coexist on the same rows so a
    single analysis can ask whether the proxy and the real reference disagree.
    Replacing the proxy in place would have made that comparison impossible and
    silently invalidated every phase-6 number at the same time.

    Args:
        frame: featured rows for the **tradable universe only**. Benchmarks must
            already be excluded -- an ETF left in here would be given context
            about itself.
        benchmarks: featured rows for the reference instruments, carrying
            ``symbol``, ``timestamp``, ``ret_1d_pct`` and ``px_vs_ema50_pct``.

    Rows whose reference has no bar at that timestamp keep NULL context. That is
    an honest gap -- an ETF can halt while its constituents trade -- and a
    forward fill would invent a reference price nobody could have seen.
    """
    joined = frame
    for benchmark, prefix in (
        (MARKET_BENCHMARK, "index"),
        (SECONDARY_MARKET_BENCHMARK, "nasdaq"),
    ):
        series = (
            benchmarks.filter(pl.col("symbol") == benchmark.symbol)
            .select(
                "timestamp",
                pl.col("ret_1d_pct").alias(f"{prefix}_ret_1d_pct"),
                pl.col("ret_5d_pct").alias(f"{prefix}_ret_5d_pct"),
                pl.col("px_vs_ema50_pct").alias(f"{prefix}_px_vs_ema50_pct"),
            )
            .unique(subset="timestamp", keep="first")
            .sort("timestamp")
        )
        joined = joined.join(series, on="timestamp", how="left")

    joined = joined.with_columns(
        (pl.col("ret_1d_pct") - pl.col("index_ret_1d_pct")).alias("relative_strength_market_1d"),
        (pl.col("ret_5d_pct") - pl.col("index_ret_5d_pct")).alias("relative_strength_market_5d"),
        (pl.col("ret_1d_pct") - pl.col("nasdaq_ret_1d_pct")).alias("relative_strength_nasdaq_1d"),
        (pl.col("index_px_vs_ema50_pct") > 0).alias("index_above_ema50"),
    ).with_columns(
        # A named state as well as the boolean: part L renders words, and
        # deriving them at the formatter would put the definition in two places.
        pl.when(pl.col("index_px_vs_ema50_pct").is_null())
        .then(None)
        .when(pl.col("index_above_ema50"))
        .then(pl.lit("UP"))
        .otherwise(pl.lit("DOWN"))
        .alias("market_trend_state"),
    )

    if "sector" not in frame.columns:
        return joined

    # Key the sector funds by the watchlist tag they stand in for, so the join
    # is on the same `sector` column the proxy uses and the two are comparable
    # row for row. `parent` carries the broader context a sector sits inside --
    # SMH's is XLK -- which is NULL for every sector except semiconductors.
    tags = pl.DataFrame(
        {
            "symbol": [b.symbol for b in SECTOR_BENCHMARKS],
            "sector": [b.sector for b in SECTOR_BENCHMARKS],
            "parent_symbol": [b.parent for b in SECTOR_BENCHMARKS],
        }
    )
    # Drop any `sector` the reference rows already carry before joining the tag
    # table. A benchmark is not on the watchlist, so its own `sector` is NULL --
    # and letting both columns exist would have polars suffix the tag to
    # `sector_right`, leaving the join keyed on a column of nulls.
    reference = benchmarks.drop("sector", strict=False)
    sectors = (
        reference.join(tags, on="symbol", how="inner")
        .select(
            "timestamp",
            "sector",
            "parent_symbol",
            pl.col("ret_1d_pct").alias("sector_etf_ret_1d_pct"),
            pl.col("ret_5d_pct").alias("sector_etf_ret_5d_pct"),
            pl.col("px_vs_ema50_pct").alias("sector_etf_px_vs_ema50_pct"),
        )
        .unique(subset=["timestamp", "sector"], keep="first")
        .sort("timestamp")
    )

    parents = (
        reference.select(
            "timestamp",
            pl.col("symbol").alias("parent_symbol"),
            pl.col("ret_1d_pct").alias("parent_etf_ret_1d_pct"),
        )
        .unique(subset=["timestamp", "parent_symbol"], keep="first")
        .sort("timestamp")
    )

    return (
        joined.join(sectors, on=["timestamp", "sector"], how="left")
        .join(parents, on=["timestamp", "parent_symbol"], how="left")
        .with_columns(
            (pl.col("ret_1d_pct") - pl.col("sector_etf_ret_1d_pct")).alias(
                "relative_strength_sector_1d"
            ),
            (pl.col("ret_5d_pct") - pl.col("sector_etf_ret_5d_pct")).alias(
                "relative_strength_sector_5d"
            ),
            (pl.col("sector_etf_ret_1d_pct") - pl.col("index_ret_1d_pct")).alias(
                "sector_relative_to_market"
            ),
            (pl.col("sector_etf_px_vs_ema50_pct") > 0).alias("sector_above_ema50"),
        )
        .with_columns(
            pl.when(pl.col("sector_etf_px_vs_ema50_pct").is_null())
            .then(None)
            .when(pl.col("sector_above_ema50"))
            .then(pl.lit("UP"))
            .otherwise(pl.lit("DOWN"))
            .alias("sector_trend_state"),
        )
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
"""Context measured against instruments that are not in the universe.

Named ``index``/``nasdaq``/``sector_etf`` against the proxy's
``proxy``/``sector`` so no table, log line or column reference can confuse the
two. The pairings are exact:
``proxy_ret_1d_pct`` <-> ``index_ret_1d_pct``,
``rel_strength_market_pct`` <-> ``relative_strength_market_1d``,
``sector_ret_1d_pct`` <-> ``sector_etf_ret_1d_pct``,
``rel_strength_sector_pct`` <-> ``relative_strength_sector_1d``.

``parent_etf_ret_1d_pct`` is non-null only for semiconductors, whose sector
reference (SMH) declares XLK as its parent. Every other sector has no broader
context, and a NULL says so rather than silently substituting the market.
"""

REAL_CONTEXT_BOOLEANS: Final[tuple[str, ...]] = ("index_above_ema50", "sector_above_ema50")

REAL_CONTEXT_STATES: Final[tuple[str, ...]] = ("market_trend_state", "sector_trend_state")
"""Categorical mirrors of the booleans, for rendering rather than analysis."""


CONTINUOUS_FEATURES: Final[tuple[str, ...]] = (
    "px_vs_ema20_pct",
    "px_vs_ema50_pct",
    "px_vs_ema200_pct",
    "ema20_slope_pct",
    "ema50_slope_pct",
    "rsi14",
    "rsi_change_1d",
    "dist_ema20_atr",
    "dist_ema50_atr",
    "ret_1h_pct",
    "ret_4h_pct",
    "ret_1d_pct",
    "ret_5d_pct",
    "momentum_accel",
    "atr_pct",
    "atr_pct_percentile",
    "realised_vol_pct",
    "range_vs_atr",
    "body_vs_atr",
    "rel_volume",
    "volume_accel",
    "dist_from_high20_pct",
    "dist_from_low20_pct",
    "bars_above_ema50",
    "rel_strength_market_pct",
    "rel_strength_sector_pct",
    "proxy_ret_1d_pct",
    "proxy_breadth_stacked",
)
"""Features analysed as continuous quantiles in part D."""

BOOLEAN_FEATURES: Final[tuple[str, ...]] = (
    "trend_stacked",
    "breakout_20",
    "breakdown_20",
    "price_discovery",
    "market_above_ema50",
)

NOT_CAUSALLY_AVAILABLE: Final[tuple[tuple[str, str], ...]] = (
    ("spread_bps", "historical quotes are not stored; every replayed row is NULL"),
    ("spread_vs_atr", "depends on spread_bps"),
    ("liquidity_flag", "depends on spread_bps"),
    (
        "failed_breakout",
        "requires knowing the breakout later failed -- an outcome, not an input",
    ),
    (
        "support_resistance_confirmed",
        "phase 5.7 confirms a level only on a later bar; using it at T would leak",
    ),
    ("time_of_day (coarse stream)", "hourly bars exist, but no 5m/15m context to compare against"),
)
"""Features deliberately excluded, each with the reason.

Listed rather than silently dropped: a reader comparing this to the phase-6
brief should be able to see that every requested family was considered and why
four of them cannot be reconstructed honestly.
"""
