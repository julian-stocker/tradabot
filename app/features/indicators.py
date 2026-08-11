"""Technical indicators as Polars expressions.

Every function returns a :class:`polars.Expr` rather than a materialised Series.
Three reasons:

* expressions compose, so a feature can be defined in terms of others without
  recomputing shared sub-expressions;
* Polars evaluates them in parallel over the whole frame;
* **they are structurally causal**. Every primitive used here -- ``shift(k)`` with
  ``k > 0``, ``rolling_*`` with the default ``center=False``, ``ewm_mean`` with
  ``adjust=False`` -- reads only the current and preceding rows. There is no
  centred window, no negative shift, and no ``reverse()`` anywhere in this module.
  That is the mechanical basis of the no-look-ahead guarantee, and
  ``tests/unit/test_no_lookahead.py`` verifies it empirically for every registered
  feature rather than trusting this paragraph.

Warm-up handling: indicators return ``null`` until they have enough history.
They never back-fill, never seed from the first value, and never partially warm.
A partially-warmed indicator is a silent correctness bug; a null is a loud one.
"""

from __future__ import annotations

import polars as pl

from app.features.frame import CLOSE, HIGH, LOW, VOLUME

TRADING_DAYS_PER_YEAR = 252
RSI_NEUTRAL = 50.0


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------
def simple_return(periods: int = 1, column: str = CLOSE) -> pl.Expr:
    """Arithmetic return over ``periods`` bars: ``P_t / P_{t-n} - 1``.

    Expressed as a fraction (0.01 == +1%).
    """
    _require_positive(periods, "periods")
    price = pl.col(column)
    return (price / price.shift(periods) - 1.0).alias(f"return_{periods}")


def log_return(periods: int = 1, column: str = CLOSE) -> pl.Expr:
    """Log return over ``periods`` bars: ``ln(P_t / P_{t-n})``.

    Preferred for aggregation and volatility because log returns are additive
    across time; arithmetic returns are not.
    """
    _require_positive(periods, "periods")
    price = pl.col(column)
    return (price / price.shift(periods)).log().alias(f"log_return_{periods}")


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------
def sma(window: int, column: str = CLOSE) -> pl.Expr:
    """Simple moving average over a trailing window."""
    _require_min_window(window, 2)
    return (
        pl.col(column).rolling_mean(window_size=window, min_samples=window).alias(f"sma_{window}")
    )


def ema(span: int, column: str = CLOSE) -> pl.Expr:
    """Exponential moving average, ``alpha = 2 / (span + 1)``.

    ``adjust=False`` gives the recursive form used by charting platforms:
    ``EMA_t = alpha * P_t + (1 - alpha) * EMA_{t-1}``. ``min_samples=span``
    suppresses output until a full span of history exists, so the initial
    transient never reaches a signal.
    """
    _require_min_window(span, 2)
    return pl.col(column).ewm_mean(span=span, adjust=False, min_samples=span).alias(f"ema_{span}")


def wilder_ma(period: int, column: str) -> pl.Expr:
    """Wilder's smoothing (``alpha = 1 / period``).

    Distinct from :func:`ema`: Wilder's RMA with period *n* corresponds to an EMA
    with span ``2n - 1``. RSI and ATR are defined with this smoothing, and using a
    standard EMA instead yields numbers that disagree with every other platform.
    """
    _require_min_window(period, 2)
    return pl.col(column).ewm_mean(alpha=1.0 / period, adjust=False, min_samples=period)


def distance_from_ma(window: int, column: str = CLOSE) -> pl.Expr:
    """Percentage distance of price from its SMA: ``(P - SMA) / SMA * 100``.

    Scale-free, so it is comparable across a EUR 8 stock and a USD 800 one.
    """
    price = pl.col(column)
    moving_average = price.rolling_mean(window_size=window, min_samples=window)
    return ((price - moving_average) / moving_average * 100.0).alias(f"dist_sma_{window}")


# ---------------------------------------------------------------------------
# Oscillators
# ---------------------------------------------------------------------------
def rsi(period: int = 14, column: str = CLOSE) -> pl.Expr:
    """Wilder's Relative Strength Index, in ``[0, 100]``.

    ``RSI = 100 - 100 / (1 + avg_gain / avg_loss)``, where both averages use
    Wilder smoothing over ``period``.

    When ``avg_loss`` is zero the ratio is undefined; the limit is 100, and that
    is what is returned. Returning a division-by-zero NaN here would silently
    poison every downstream aggregate.
    """
    _require_min_window(period, 2)
    delta = pl.col(column).diff()

    # The `delta.is_null()` branch is load-bearing. `diff()` is null on the first
    # bar, and a plain `when(delta > 0).then(delta).otherwise(0.0)` would map that
    # null to 0.0 -- inventing a zero-change bar that then seeds the Wilder
    # average and shifts every early RSI value. Preserving the null keeps the
    # first bar out of the calculation entirely, where it belongs.
    gain = pl.when(delta.is_null()).then(None).when(delta > 0).then(delta).otherwise(0.0)
    loss = pl.when(delta.is_null()).then(None).when(delta < 0).then(-delta).otherwise(0.0)

    avg_gain = gain.ewm_mean(alpha=1.0 / period, adjust=False, min_samples=period)
    avg_loss = loss.ewm_mean(alpha=1.0 / period, adjust=False, min_samples=period)

    return (
        pl.when(avg_loss == 0.0)
        .then(pl.when(avg_gain == 0.0).then(RSI_NEUTRAL).otherwise(100.0))
        .otherwise(100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
        .alias(f"rsi_{period}")
    )


# ---------------------------------------------------------------------------
# Volatility / range
# ---------------------------------------------------------------------------
def true_range() -> pl.Expr:
    """True Range: ``max(H - L, |H - C_prev|, |L - C_prev|)``.

    The two close-based terms are what make TR account for overnight gaps, which
    a plain high-minus-low range ignores.
    """
    high, low = pl.col(HIGH), pl.col(LOW)
    prev_close = pl.col(CLOSE).shift(1)
    return pl.max_horizontal(
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ).alias("true_range")


def atr(period: int = 14) -> pl.Expr:
    """Average True Range with Wilder smoothing. Absolute price units."""
    _require_min_window(period, 2)
    return (
        true_range()
        .ewm_mean(alpha=1.0 / period, adjust=False, min_samples=period)
        .alias(f"atr_{period}")
    )


def atr_percent(period: int = 14) -> pl.Expr:
    """ATR as a percentage of close -- comparable across instruments."""
    return (atr(period) / pl.col(CLOSE) * 100.0).alias(f"atr_pct_{period}")


def rolling_volatility(
    window: int = 20,
    *,
    annualize: bool = True,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> pl.Expr:
    """Rolling standard deviation of 1-bar log returns.

    Args:
        window: number of bars in the trailing window.
        annualize: scale by ``sqrt(periods_per_year)``. Annualised volatility is
            the only form that is comparable across timeframes.
        periods_per_year: bars per year for the annualisation factor. The caller
            must pass the value matching the data's timeframe -- annualising
            5-minute bars with 252 understates volatility by roughly 9x.

    Returned as a fraction (0.25 == 25% annualised). Sample standard deviation
    (``ddof=1``) is used, matching the finance convention.
    """
    _require_min_window(window, 2)
    returns = (pl.col(CLOSE) / pl.col(CLOSE).shift(1)).log()
    std = returns.rolling_std(window_size=window, min_samples=window, ddof=1)
    if annualize:
        std = std * (periods_per_year**0.5)
    suffix = "" if annualize else "_raw"
    return std.alias(f"volatility_{window}{suffix}")


def volatility_ratio(fast: int = 10, slow: int = 60) -> pl.Expr:
    """Short-window volatility divided by long-window volatility.

    Above 1.0 means volatility is expanding relative to its own baseline. Used as
    a crude regime proxy -- see ``app.signals.components.regime``.
    """
    if fast >= slow:
        msg = f"fast window ({fast}) must be shorter than slow window ({slow})"
        raise ValueError(msg)
    returns = (pl.col(CLOSE) / pl.col(CLOSE).shift(1)).log()
    fast_std = returns.rolling_std(window_size=fast, min_samples=fast, ddof=1)
    slow_std = returns.rolling_std(window_size=slow, min_samples=slow, ddof=1)
    return (pl.when(slow_std > 0).then(fast_std / slow_std).otherwise(None)).alias(
        f"vol_ratio_{fast}_{slow}"
    )


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------
def relative_volume(window: int = 20) -> pl.Expr:
    """Current bar volume divided by the mean of the ``window`` *preceding* bars.

    The current bar is excluded from the baseline via ``shift(1)``. Including it
    would make the ratio mechanically self-damping: an enormous volume spike
    would inflate its own denominator and understate itself.

    2.0 means "twice the recent typical volume".
    """
    _require_min_window(window, 2)
    volume = pl.col(VOLUME)
    baseline = volume.shift(1).rolling_mean(window_size=window, min_samples=window)
    return (pl.when(baseline > 0).then(volume / baseline).otherwise(None)).alias(
        f"rel_volume_{window}"
    )


def volume_sma(window: int = 20) -> pl.Expr:
    """Simple moving average of volume."""
    _require_min_window(window, 2)
    return (
        pl.col(VOLUME)
        .rolling_mean(window_size=window, min_samples=window)
        .alias(f"volume_sma_{window}")
    )


# ---------------------------------------------------------------------------
# Trend structure
# ---------------------------------------------------------------------------
def ma_spread_percent(fast: int, slow: int, column: str = CLOSE) -> pl.Expr:
    """Percentage gap between a fast and a slow EMA: ``(fast - slow) / slow * 100``.

    Positive means the fast EMA is above the slow one -- the classic trend-up
    configuration -- and the magnitude expresses how stretched the trend is.
    """
    if fast >= slow:
        msg = f"fast span ({fast}) must be shorter than slow span ({slow})"
        raise ValueError(msg)
    price = pl.col(column)
    fast_ema = price.ewm_mean(span=fast, adjust=False, min_samples=fast)
    slow_ema = price.ewm_mean(span=slow, adjust=False, min_samples=slow)
    return ((fast_ema - slow_ema) / slow_ema * 100.0).alias(f"ema_spread_{fast}_{slow}")


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def _require_positive(value: int, name: str) -> None:
    if value < 1:
        msg = f"{name} must be >= 1, got {value}"
        raise ValueError(msg)


def _require_min_window(value: int, minimum: int) -> None:
    if value < minimum:
        msg = f"window must be >= {minimum}, got {value}"
        raise ValueError(msg)
