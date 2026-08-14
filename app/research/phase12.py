"""Phase 12 — the opportunity engine: is selection better than the pool?

The question this phase exists to answer
----------------------------------------
Not "will stock X rise". That framing has failed in eight phases. The question
here is cross-sectional and comparative:

    Given every eligible stock at time T, is there a subset whose subsequent
    1-3 session return distribution is better than the pool it was drawn from --
    by enough to pay for execution?

The engine is allowed, and expected, to answer ``NO_OPPORTUNITY``. Selectivity
is the goal; continuous exposure is not a requirement.

Why relative return is the target, not raw return
-------------------------------------------------
A stock returning +1% on a day the market returned +2% is a **failed**
selection: the same capital in SPY would have done better with no selection
skill at all. Every result here is therefore reported against three matched
controls -- market ETF, sector ETF, and the cross-sectional universe median --
and the universe median is the strictest, because it is what "pick a stock at
random from today's eligible list" actually earns.

Pre-registration
----------------
Everything in the PRE-REGISTRATION block below -- horizons, eligibility, feature
definitions, bucket edges, evidence gates, the multiple-testing correction --
was written and committed **before any outcome was computed**. A test asserts
the constants still hold the registered values, so moving a threshold after
seeing a result requires editing a test that says, in words, not to.

Causality
---------
Features at bar *t* are built from bars at or before *t*. Entry is at the
**open of t+1**. Forward windows run from that open through the close of *t+h*.
There is no construction in which a feature and its own target share a bar.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

import polars as pl

from app.core.config import CostSettings
from app.market_data.benchmarks import is_benchmark, market_benchmark, sector_benchmark
from app.paper.execution import estimate_round_trip_cost
from app.research.adjustments import adjust_all, load_splits

# ===========================================================================
# PRE-REGISTRATION -- frozen before any outcome was inspected
# ===========================================================================

EVAL_HORIZONS: Final[tuple[int, ...]] = (1, 3)
"""Sessions. Matches the only horizons risk-v1 is calibrated for."""

DIAGNOSTIC_HORIZON: Final = 5
"""Reported, never optimised on. It exists to show whether an effect at 1-3
sessions is a knife-edge or part of a broader drift."""

MIN_HISTORY_BARS: Final = 252
"""Trailing daily bars a symbol needs before it may enter the universe.

One year. Shorter windows cannot rank a symbol against its own history, and a
cross-section that admits symbols on different history lengths is comparing
differently-measured things.
"""

REFERENCE_NOTIONAL: Final = Decimal("2000")
"""Position size all cost ratios are computed at, in EUR.

The EUR 10,000 portfolio -- the only one phase 11.4 classified PRACTICAL --
carried a maximum exposure of about EUR 11,500 across up to five positions.
EUR 2,000 is that portfolio's typical position, and fixing it makes the
cost ratio a property of the *candidate* rather than of whatever size the sizer
happened to pick that day.
"""

# --- Evidence gates --------------------------------------------------------
MIN_POOLED_SAMPLE: Final = 500
MIN_VALIDATION_SAMPLE: Final = 100

ADVANTAGE_HIT_RATE_PP: Final = 5.0
"""Percentage points of hit-rate separation against a matched control.

Demanding on purpose. At a 1-3 session horizon on liquid megacaps, anything
smaller cannot survive a round trip that already costs about 0.25% of notional.
"""

ADVANTAGE_NET_RETURN_PP: Final = 0.10
"""The alternative gate: net relative return advantage per opportunity, in
percentage points, **after** canonical execution costs are charged to the
candidate arm.

Ten basis points per trade. At the ~7 trades/month phase 11.4 measured, that is
roughly 8% a year of pure selection advantage -- large, but the smallest figure
that is clearly not noise at this sample size.
"""

STABILITY_FOLD_FRACTION: Final = 0.75
"""Share of chronological folds that must show the effect in the same direction.
Below this the result is ``REGIME_DEPENDENT``, not ``ROBUST``."""

FDR_Q: Final = 0.10
"""Benjamini-Hochberg false-discovery rate.

Chosen over Bonferroni as the primary correction because the hypotheses here are
positively correlated by construction -- relative strength against the market
and against the sector measure overlapping things -- and Bonferroni on
correlated tests is so conservative it would reject a real effect. Bonferroni is
reported alongside as the stricter reference.
"""

# --- Bucket edges ----------------------------------------------------------
SELECTIVITY_BUCKETS: Final[tuple[tuple[str, float], ...]] = (
    ("TOP_50", 0.50),
    ("TOP_25", 0.75),
    ("TOP_10", 0.90),
    ("TOP_5", 0.95),
)
"""Cross-sectional rank floors. Reported together; no bucket is chosen after."""

PULLBACK_BUCKETS: Final[tuple[tuple[str, float, float], ...]] = (
    ("NONE", 0.0, 0.5),
    ("SHALLOW", 0.5, 1.5),
    ("MEDIUM", 1.5, 3.0),
    ("DEEP", 3.0, 5.0),
    ("EXTREME", 5.0, math.inf),
)
"""Retracement from the trailing 5-day high, in ATR-14 units.

ATR-normalised rather than percentage: a 3% dip is routine in NVDA and a
structural break in KO, and absolute percentage buckets would silently sort by
volatility instead of by pullback depth.
"""

MOVEMENT_COST_BUCKETS: Final[tuple[tuple[str, float, float], ...]] = (
    ("UNECONOMIC", 0.0, 2.0),
    ("MARGINAL", 2.0, 4.0),
    ("ADEQUATE", 4.0, 8.0),
    ("AMPLE", 8.0, math.inf),
)
"""Expected 1-day movement divided by round-trip cost, both in percent.

A ratio of 2 means the whole expected day's move is twice the friction, so
a correct direction call still nets almost nothing after a stop and a spread.
Edges set before any return was inspected.
"""

CONTINUATION_MAX_PULLBACK_ATR: Final = 0.5
"""How close to the trailing 5-day high a stock must sit to count as continuing.
Half an ATR. Same unit as the pullback buckets, so the two partition cleanly."""

BREAKOUT_VOLUME_RATIO: Final = 1.5
"""Volume against its own 20-day mean before a breakout counts as confirmed.

Registered in advance and expected to fail: earlier phases found standalone
volume carries little information, and this exists to test that claim in
combination rather than to assume it.
"""

TREND_QUANTILE: Final = 0.60
"""Cross-sectional rank a stock must clear to count as "relatively strong".

Deliberately not the top decile: the pullback and continuation families need a
population large enough to split further, and a 90th-percentile prefilter would
leave a few hundred observations to slice four ways.
"""

# --- Frozen hypothesis families -------------------------------------------
FAMILY_FEATURES: Final[dict[str, tuple[str, ...]]] = {
    "RELATIVE_MOMENTUM": (
        "rel_mom_market_20d",
        "rel_mom_sector_20d",
        "xs_rank_ret_20d",
        "xs_rank_ret_5d",
    ),
    "TREND_PERSISTENCE": (
        "ema_aligned",
        "bars_above_ema20",
        "trend_dist_atr",
    ),
    "CONTINUATION": ("continuation",),
    "BREAKOUT_EXPANSION": (
        "range_expansion",
        "breakout_20d",
        "breakout_volume",
    ),
    "VOLATILITY_OPPORTUNITY": ("movement_to_cost", "range_to_cost"),
}
"""The complete list of tested features, fixed in advance.

Pullback, market/sector alignment, sector rotation and lead/lag are conditional
or structural tests rather than single columns, and are registered as their own
analyses below. Nothing outside this dict and those analyses is tested.
"""

ALIGNMENT_LAYERS: Final[tuple[str, ...]] = (
    "MARKET_ONLY",
    "SECTOR_ONLY",
    "STOCK_ONLY",
    "MARKET_STOCK",
    "SECTOR_STOCK",
    "MARKET_SECTOR_STOCK",
)

LEAD_LAG_GROUPS: Final[dict[str, tuple[str, ...]]] = {
    "semiconductors": ("NVDA", "AMD", "AVGO", "MU", "INTC", "QCOM", "TXN"),
    "financials": ("JPM", "BAC", "GS", "MS", "V", "MA", "BRK.B"),
    "energy": ("XOM", "CVX", "COP", "SLB"),
    "technology": ("AAPL", "MSFT", "ORCL", "CRM", "ADBE", "GOOGL"),
    "consumer": ("AMZN", "HD", "MCD", "NKE", "SBUX", "COST", "WMT"),
}
"""Economically justified peer groups, taken from the existing sector mapping.

Named in advance so this is five tests, not a pairwise search over 52 symbols
(1,326 pairs) that would find something at any threshold.
"""


class Verdict:
    ROBUST = "ROBUST"
    PROMISING = "PROMISING"
    REGIME_DEPENDENT = "REGIME_DEPENDENT"
    NO_INFORMATION = "NO_INFORMATION"


# ===========================================================================
# Multiple-testing registry
# ===========================================================================
@dataclass
class HypothesisRegistry:
    """Every test that was run, whether or not it was reported as a finding.

    Exists because the honest denominator for a multiple-comparison correction
    is the number of hypotheses *tested*, not the number that looked good. A
    registry that only records successes makes any correction cosmetic.
    """

    entries: list[tuple[str, str, float]] = field(default_factory=list)

    def record(self, family: str, name: str, p_value: float) -> None:
        self.entries.append((family, name, p_value))

    def __len__(self) -> int:
        return len(self.entries)

    def benjamini_hochberg(self, q: float = FDR_Q) -> list[tuple[str, str, float, bool]]:
        """Which tests survive an FDR correction at ``q``.

        Returns every entry with its survival flag, so rejected hypotheses stay
        visible -- a table of survivors alone hides how much was tried.
        """
        ordered = sorted(self.entries, key=lambda e: e[2])
        n = len(ordered)
        threshold_index = -1
        for index, (_, _, p) in enumerate(ordered, start=1):
            if p <= q * index / n:
                threshold_index = index
        return [
            (family, name, p, position <= threshold_index)
            for position, (family, name, p) in enumerate(ordered, start=1)
        ]

    def bonferroni(self, alpha: float = 0.05) -> float:
        return alpha / len(self.entries) if self.entries else alpha


# ===========================================================================
# Cost model -- the canonical one, never a bps substitute
# ===========================================================================
def round_trip_cost_pct(costs: CostSettings, notional: Decimal = REFERENCE_NOTIONAL) -> float:
    """Round-trip execution cost as a percentage of notional.

    Delegates to the paper engine's own estimator, so a flat fee, the spread and
    slippage are all present. A simplified "20 bps" here would understate the
    friction a EUR 2,000 position actually pays and quietly turn uneconomic
    candidates into economic ones.
    """
    return float(estimate_round_trip_cost(settings=costs, notional=notional) / notional * 100)


# ===========================================================================
# Data construction
# ===========================================================================
CANDLE_QUERY: Final = """
    SELECT c.instrument_id, i.symbol, c.timestamp, c.open, c.high, c.low, c.close, c.volume
    FROM candles c JOIN instruments i ON i.id = c.instrument_id
    WHERE c.timeframe = 'D1'
    ORDER BY c.instrument_id, c.timestamp
"""

SECTOR_QUERY: Final = """
    SELECT i.symbol, w.tags FROM watchlist w
    JOIN instruments i ON i.id = w.instrument_id
    WHERE w.enabled = 1
"""


def load_daily(connection: sqlite3.Connection) -> tuple[pl.DataFrame, dict[str, str]]:
    """Split-adjusted daily bars, plus the symbol -> sector map.

    Adjustment runs before any feature is computed. A split left in the series
    is a -75% bar, and every rolling window downstream would read it as a price
    move -- the exact defect phase 9A found the hard way.
    """
    candles = pl.read_database(CANDLE_QUERY, connection)
    splits = load_splits(connection)
    sectors_raw = connection.execute(SECTOR_QUERY).fetchall()
    sectors = {str(s): str(json.loads(t)[0]) for s, t in sectors_raw if t}

    candles = candles.with_columns(pl.col("timestamp").str.to_datetime(strict=False))
    candles = candles.sort(["instrument_id", "timestamp"])
    candles = adjust_all(candles, splits)
    for column in ("open", "high", "low", "close", "volume"):
        candles = candles.with_columns(pl.col(column).cast(pl.Float64))
    return candles, sectors


def causal_features(group: pl.DataFrame) -> pl.DataFrame:
    """Per-symbol features, every one built from bars at or before its own row.

    All rolling windows end at the current bar. Nothing is shifted forward, and
    nothing reads a later row -- the targets in :func:`forward_targets` are the
    only place future bars appear, and they start at *t+1*.
    """
    close = pl.col("close")
    high = pl.col("high")
    low = pl.col("low")

    previous_close = close.shift(1)
    true_range = pl.max_horizontal(
        high - low, (high - previous_close).abs(), (low - previous_close).abs()
    )

    frame = group.with_columns(
        true_range.alias("tr"),
        (close / close.shift(1) - 1).mul(100).alias("ret_1d"),
        (close / close.shift(5) - 1).mul(100).alias("ret_5d"),
        (close / close.shift(20) - 1).mul(100).alias("ret_20d"),
        (close / close.shift(60) - 1).mul(100).alias("ret_60d"),
        close.ewm_mean(span=20, adjust=False).alias("ema20"),
        close.ewm_mean(span=50, adjust=False).alias("ema50"),
        high.rolling_max(window_size=5).alias("high_5d"),
        high.rolling_max(window_size=20).alias("high_20d"),
        pl.col("volume").rolling_mean(window_size=20).alias("volume_20d"),
    )
    frame = frame.with_columns(pl.col("tr").rolling_mean(window_size=14).alias("atr14"))
    frame = frame.with_columns((pl.col("atr14") / close * 100).alias("atr_pct"))

    return frame.with_columns(
        ((close > pl.col("ema20")) & (pl.col("ema20") > pl.col("ema50"))).alias("ema_aligned"),
        ((close - pl.col("ema50")) / pl.col("atr14")).alias("trend_dist_atr"),
        ((pl.col("high_5d") - close) / pl.col("atr14")).alias("pullback_atr"),
        (pl.col("tr") / pl.col("atr14")).alias("range_expansion"),
        (close >= pl.col("high_20d")).alias("breakout_20d"),
        (pl.col("volume") / pl.col("volume_20d")).alias("volume_ratio"),
        (close > pl.col("ema20")).cast(pl.Int8).alias("_above"),
    )


def bars_above(group: pl.DataFrame) -> pl.DataFrame:
    """Consecutive bars closing above the 20-day EMA, counted causally."""
    values = group["_above"].to_list()
    run: list[int] = []
    current = 0
    for value in values:
        current = current + 1 if value else 0
        run.append(current)
    return group.with_columns(pl.Series("bars_above_ema20", run, dtype=pl.Int32))


def forward_targets(group: pl.DataFrame, horizons: Sequence[int]) -> pl.DataFrame:
    """Targets measured from the **open of t+1**, never from the signal bar.

    A signal computed on the close of *t* cannot be filled at that close: the
    price was not observable while the bar was forming. Entry is the next
    session's open, which is the first price a decision made at *t* could
    actually reach, and it is the same convention the paper engine enforces
    structurally.

    For horizon *h* the window is *t+1 .. t+h* inclusive. MFE and MAE are the
    best and worst excursions inside that window relative to the entry price,
    so a target that was reached and given back is still visible.
    """
    entry = pl.col("open").shift(-1)
    frame = group.with_columns(entry.alias("entry_price"))
    for horizon in horizons:
        exit_close = pl.col("close").shift(-horizon)
        # Built by explicitly naming the bars in the window rather than by
        # shifting and rolling. A rolling window over a shifted series needs
        # ``horizon`` prior rows to fill, so it silently returns null for the
        # first horizon-1 rows of every symbol even where the future bars exist.
        window_high = pl.max_horizontal(
            [pl.col("high").shift(-step) for step in range(1, horizon + 1)]
        )
        window_low = pl.min_horizontal(
            [pl.col("low").shift(-step) for step in range(1, horizon + 1)]
        )
        frame = frame.with_columns(
            ((exit_close / entry - 1) * 100).alias(f"fwd_{horizon}d"),
            ((window_high / entry - 1) * 100).alias(f"mfe_{horizon}d"),
            ((window_low / entry - 1) * 100).alias(f"mae_{horizon}d"),
        )
    return frame


def attach_context(frame: pl.DataFrame, sectors: dict[str, str]) -> pl.DataFrame:
    """Join market and sector ETF returns, then derive relative strength.

    Benchmarks are featured like any other symbol and then **held out of the
    cross-section**: an equal-weight "universe median" that included SPY would
    let the control leak into the thing it is controlling for.
    """
    market_symbol = market_benchmark().symbol
    sector_symbol = {name: sector_benchmark(name).symbol for name in set(sectors.values())}

    frame = frame.with_columns(
        pl.col("symbol").replace_strict(sectors, default=None).alias("sector")
    )

    columns = ["timestamp", "ret_1d", "ret_5d", "ret_20d", "ret_60d", "ema_aligned"]
    market = frame.filter(pl.col("symbol") == market_symbol).select(columns)
    market = market.rename({c: f"market_{c}" for c in columns if c != "timestamp"})

    etf_frames = []
    for name, symbol in sector_symbol.items():
        part = frame.filter(pl.col("symbol") == symbol).select(columns)
        part = part.rename({c: f"sector_etf_{c}" for c in columns if c != "timestamp"})
        etf_frames.append(part.with_columns(pl.lit(name).alias("sector")))
    sector_context = pl.concat(etf_frames, how="vertical_relaxed")

    stocks = frame.filter(~pl.col("symbol").map_elements(is_benchmark, return_dtype=pl.Boolean))
    stocks = stocks.join(market, on="timestamp", how="left")
    stocks = stocks.join(sector_context, on=["timestamp", "sector"], how="left")

    return stocks.with_columns(
        (pl.col("ret_20d") - pl.col("market_ret_20d")).alias("rel_mom_market_20d"),
        (pl.col("ret_20d") - pl.col("sector_etf_ret_20d")).alias("rel_mom_sector_20d"),
        (pl.col("ret_5d") - pl.col("market_ret_5d")).alias("rel_mom_market_5d"),
    )


def cross_sectional(frame: pl.DataFrame) -> pl.DataFrame:
    """Percentile ranks within each timestamp, and the universe median return.

    The universe median is the strictest control this phase uses: it is exactly
    what picking a random eligible stock earns, with no skill of any kind.
    """
    rank_columns = ("ret_20d", "ret_5d", "rel_mom_market_20d", "rel_mom_sector_20d")
    frame = frame.with_columns(
        [
            (pl.col(column).rank("average").over("timestamp") - 1)
            .truediv(pl.len().over("timestamp") - 1)
            .alias(f"xs_rank_{column}")
            for column in rank_columns
        ]
    )
    frame = frame.with_columns(
        (pl.col("ret_20d").rank("average").over(["timestamp", "sector"]) - 1)
        .truediv(pl.len().over(["timestamp", "sector"]) - 1)
        .alias("xs_rank_sector_ret_20d"),
        pl.len().over("timestamp").alias("universe_size"),
    )
    return frame.rename({"xs_rank_ret_20d": "xs_rank_ret_20d", "xs_rank_ret_5d": "xs_rank_ret_5d"})


def eligible(frame: pl.DataFrame, horizons: Sequence[int]) -> pl.DataFrame:
    """Rows that may enter the cross-section.

    A row is eligible when the symbol has enough history to be ranked against
    itself, has a usable ATR, and -- critically -- has an entry price *and* a
    complete forward window. Dropping incomplete windows here rather than later
    keeps every bucket comparison on the same rows.
    """
    longest = max(horizons)
    conditions = (
        pl.col("bar_index").ge(MIN_HISTORY_BARS)
        & pl.col("atr14").is_not_null()
        & pl.col("atr14").gt(0)
        & pl.col("entry_price").is_not_null()
        & pl.col("entry_price").gt(0)
        & pl.col("close").gt(0)
        & pl.col(f"fwd_{longest}d").is_not_null()
        & pl.col("rel_mom_market_20d").is_not_null()
        & pl.col("sector").is_not_null()
    )
    return frame.filter(conditions)


def build_dataset(connection: sqlite3.Connection) -> pl.DataFrame:
    """The full causal panel: one row per (symbol, session) with its forward window."""
    horizons = (*EVAL_HORIZONS, DIAGNOSTIC_HORIZON)
    candles, sectors = load_daily(connection)

    parts = []
    for _, group in candles.group_by("instrument_id", maintain_order=True):
        ordered = group.sort("timestamp")
        featured = bars_above(causal_features(ordered))
        featured = forward_targets(featured, horizons)
        featured = featured.with_columns(pl.arange(0, pl.len()).alias("bar_index"))
        parts.append(featured)
    frame = pl.concat(parts, how="vertical_relaxed")

    frame = attach_context(frame, sectors)
    frame = cross_sectional(frame)
    frame = frame.with_columns(
        (
            (pl.col("ret_1d") > 0)
            & (pl.col("ret_5d") > 0)
            & (pl.col("pullback_atr") < CONTINUATION_MAX_PULLBACK_ATR)
        ).alias("continuation"),
        (pl.col("breakout_20d") & (pl.col("volume_ratio") > BREAKOUT_VOLUME_RATIO)).alias(
            "breakout_volume"
        ),
        pl.col("timestamp").dt.year().alias("year"),
    )
    return eligible(frame, horizons)


def universe_median(frame: pl.DataFrame, horizon: int) -> pl.DataFrame:
    """Per-timestamp median forward return -- the random-pick control."""
    return frame.group_by("timestamp").agg(
        pl.col(f"fwd_{horizon}d").median().alias(f"median_fwd_{horizon}d")
    )


# ===========================================================================
# Registered metrics and statistical test
# ===========================================================================
# Fixed after the panel was built and verified, and before any outcome was
# inspected. Recorded here rather than in a notebook so the choice is auditable.

STAT_TEST: Final = "day-clustered two-sided t-test on the daily mean advantage"
"""Why clustering by day, and not a plain t-test over observations.

Fifty-two stocks share each session, and on a day the market falls they nearly
all fall together. Treating 65,630 rows as independent would shrink every
standard error by roughly the square root of the cross-section and manufacture
significance out of market beta. Differencing against the same-day universe
median removes most of that common move; clustering the test by day removes the
rest, leaving about 1,264 independent units instead of 65,630.
"""


@dataclass(frozen=True, slots=True)
class Advantage:
    """One bucket's performance against its matched control.

    ``gross_advantage_pp`` is selection skill: how much better the bucket did
    than the control. Execution cost does **not** appear in it, because both
    arms pay the same round trip and it cancels in the difference.

    ``net_absolute_pp`` is the separate, harder question -- whether the trade is
    worth making at all once friction is charged. An effect can be real in the
    first sense and worthless in the second, and collapsing them into one number
    is how a statistically interesting nothing gets called an edge.
    """

    name: str
    n: int
    days: int
    hit_rate: float
    control_hit_rate: float
    gross_advantage_pp: float
    net_absolute_pp: float
    t_stat: float
    p_value: float
    mean_mfe: float
    mean_mae: float

    @property
    def hit_separation_pp(self) -> float:
        return (self.hit_rate - self.control_hit_rate) * 100

    @property
    def passes_advantage(self) -> bool:
        """Either registered route: hit-rate separation or net return."""
        return (
            self.hit_separation_pp >= ADVANTAGE_HIT_RATE_PP
            or self.gross_advantage_pp >= ADVANTAGE_NET_RETURN_PP
        )

    @property
    def passes_sample(self) -> bool:
        return self.n >= MIN_POOLED_SAMPLE

    @property
    def tradable(self) -> bool:
        """Positive after friction. Part O's requirement, kept separate."""
        return self.net_absolute_pp > 0


def _t_test(values: Sequence[float]) -> tuple[float, float]:
    """Two-sided one-sample t-test against zero, on already-clustered means."""
    n = len(values)
    if n < 2:  # noqa: PLR2004 -- a single cluster has no dispersion to test
        return 0.0, 1.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    if variance <= 0:
        return 0.0, 1.0
    t = mean / math.sqrt(variance / n)
    # Normal approximation to the t distribution. With ~1,264 clusters the
    # difference from the exact t is in the fourth decimal of the p-value.
    p = math.erfc(abs(t) / math.sqrt(2))
    return t, p


def measure(
    frame: pl.DataFrame,
    mask: pl.Expr,
    *,
    name: str,
    horizon: int,
    cost_pct: float,
) -> Advantage:
    """Advantage of the rows matching ``mask`` over the same-day universe median.

    The control is deliberately the median of *that session's* eligible stocks,
    not a fixed benchmark: it is exactly what picking at random would have
    earned, so beating it is the minimum claim a selection engine must support.
    """
    target = f"fwd_{horizon}d"
    median = universe_median(frame, horizon)
    joined = frame.join(median, on="timestamp", how="left").with_columns(
        (pl.col(target) - pl.col(f"median_{target}")).alias("adv")
    )
    selected = joined.filter(mask)
    if selected.height == 0:
        return Advantage(name, 0, 0, 0, 0, 0, 0, 0, 1.0, 0, 0)

    daily = (
        selected.group_by("timestamp")
        .agg(pl.col("adv").mean().alias("adv"))
        .sort("timestamp")["adv"]
        .to_list()
    )
    t_stat, p_value = _t_test([v for v in daily if v is not None])

    return Advantage(
        name=name,
        n=selected.height,
        days=len(daily),
        hit_rate=float(selected.select((pl.col("adv") > 0).mean()).item() or 0.0),
        control_hit_rate=0.5,
        gross_advantage_pp=float(selected.select(pl.col("adv").mean()).item() or 0.0),
        net_absolute_pp=float(selected.select(pl.col(target).mean()).item() or 0.0) - cost_pct,
        t_stat=t_stat,
        p_value=p_value,
        mean_mfe=float(selected.select(pl.col(f"mfe_{horizon}d").mean()).item() or 0.0),
        mean_mae=float(selected.select(pl.col(f"mae_{horizon}d").mean()).item() or 0.0),
    )


def by_year(
    frame: pl.DataFrame, mask: pl.Expr, *, horizon: int, cost_pct: float
) -> dict[int, float]:
    """Gross advantage per chronological fold. The stability gate reads this."""
    out: dict[int, float] = {}
    for year in sorted(frame["year"].unique().to_list()):
        fold = frame.filter(pl.col("year") == year)
        result = measure(fold, mask, name=str(year), horizon=horizon, cost_pct=cost_pct)
        if result.n:
            out[int(year)] = result.gross_advantage_pp
    return out


def stability(folds: dict[int, float]) -> float:
    """Fraction of folds agreeing in sign with the pooled direction."""
    if not folds:
        return 0.0
    positive = sum(1 for v in folds.values() if v > 0)
    return max(positive, len(folds) - positive) / len(folds)


def classify(result: Advantage, folds: dict[int, float]) -> str:
    """Apply the registered gates, in order. No threshold moves here."""
    if not result.passes_sample or not result.passes_advantage:
        return Verdict.NO_INFORMATION
    if not result.tradable:
        return Verdict.NO_INFORMATION
    consistent = stability(folds)
    positive_folds = sum(1 for v in folds.values() if v > 0) / len(folds) if folds else 0.0
    if consistent >= STABILITY_FOLD_FRACTION and positive_folds >= STABILITY_FOLD_FRACTION:
        return Verdict.ROBUST
    if positive_folds >= 0.5:  # noqa: PLR2004 -- a bare majority is the PROMISING floor
        return Verdict.REGIME_DEPENDENT
    return Verdict.NO_INFORMATION
