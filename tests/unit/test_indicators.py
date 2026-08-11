"""Indicator tests against hand-computed expected values.

Deliberately *not* compared against another library. Comparing two
implementations only proves they agree, including on a shared misunderstanding.
Each expected value below is derived from the indicator's definition on inputs
chosen so the arithmetic can be done by hand.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import polars as pl
import pytest

from app.core.errors import ValidationError
from app.features import indicators as ind
from app.features.frame import candles_to_frame
from app.market_data.provider import CandleData

START = datetime(2024, 1, 1, tzinfo=UTC)


def series(closes: list[str], *, highs=None, lows=None, volumes=None) -> pl.DataFrame:
    """Build a frame from exact decimal strings."""
    candles = []
    for i, close in enumerate(closes):
        close_value = Decimal(close)
        high = Decimal(highs[i]) if highs else close_value
        low = Decimal(lows[i]) if lows else close_value
        volume = Decimal(volumes[i]) if volumes else Decimal(1000)
        candles.append(
            CandleData(
                timestamp=START + timedelta(days=i),
                open=close_value,
                high=max(high, close_value),
                low=min(low, close_value),
                close=close_value,
                volume=volume,
            )
        )
    return candles_to_frame(candles)


def column(frame: pl.DataFrame, expr: pl.Expr) -> list[float | None]:
    return frame.select(expr).to_series().to_list()


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------
class TestReturns:
    def test_simple_return_one_bar(self):
        values = column(series(["100", "110", "99"]), ind.simple_return(1))
        assert values[0] is None, "first bar has no prior close"
        assert math.isclose(values[1], 0.10)
        assert math.isclose(values[2], -0.10)

    def test_simple_return_multi_bar(self):
        # 100 -> 125 over 2 bars = +25%
        values = column(series(["100", "110", "125"]), ind.simple_return(2))
        assert values[0] is None
        assert values[1] is None
        assert math.isclose(values[2], 0.25)

    def test_log_return_is_additive(self):
        """Log returns sum across time; that is the whole reason to use them."""
        frame = series(["100", "110", "121"])
        one_bar = column(frame, ind.log_return(1))
        two_bar = column(frame, ind.log_return(2))
        assert math.isclose(one_bar[1] + one_bar[2], two_bar[2], rel_tol=1e-12)

    def test_zero_periods_rejected(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            ind.simple_return(0)


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------
class TestMovingAverages:
    def test_sma_exact(self):
        # mean(1..5) = 3, mean(2..6) = 4
        values = column(series(["1", "2", "3", "4", "5", "6"]), ind.sma(5))
        assert values[:4] == [None] * 4, "SMA(5) needs 5 bars"
        assert math.isclose(values[4], 3.0)
        assert math.isclose(values[5], 4.0)

    def test_sma_warmup_is_never_partial(self):
        """A partially-warmed SMA is a silent bug; nulls are the correct answer."""
        values = column(series(["10", "20", "30"]), ind.sma(5))
        assert values == [None, None, None]

    def test_ema_recursive_form(self):
        """EMA(3) with adjust=False: alpha = 2/4 = 0.5.

        Seeded at bar 3 with the recursive form applied from bar 1:
            e1 = 10
            e2 = 0.5*20 + 0.5*10 = 15
            e3 = 0.5*30 + 0.5*15 = 22.5     <- first reported value
            e4 = 0.5*40 + 0.5*22.5 = 31.25
        """
        values = column(series(["10", "20", "30", "40"]), ind.ema(3))
        assert values[0] is None
        assert values[1] is None
        assert math.isclose(values[2], 22.5)
        assert math.isclose(values[3], 31.25)

    def test_ema_of_constant_series_is_the_constant(self):
        values = column(series(["50"] * 10), ind.ema(5))
        assert math.isclose(values[-1], 50.0)

    def test_distance_from_ma(self):
        # SMA(3) at the last bar = mean(10, 20, 30) = 20; close = 30
        # distance = (30 - 20) / 20 * 100 = +50%
        values = column(series(["10", "20", "30"]), ind.distance_from_ma(3))
        assert math.isclose(values[2], 50.0)

    def test_ma_spread_requires_fast_shorter_than_slow(self):
        with pytest.raises(ValueError, match="must be shorter"):
            ind.ma_spread_percent(50, 20)


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------
class TestRSI:
    def test_monotonic_rise_gives_rsi_100(self):
        """With no down bars avg_loss is 0, and the limit of the formula is 100."""
        closes = [str(100 + i) for i in range(20)]
        values = column(series(closes), ind.rsi(14))
        assert math.isclose(values[-1], 100.0)

    def test_monotonic_fall_gives_rsi_0(self):
        closes = [str(200 - i) for i in range(20)]
        values = column(series(closes), ind.rsi(14))
        assert math.isclose(values[-1], 0.0)

    def test_flat_series_is_neutral(self):
        """Zero gain and zero loss is genuinely undefined; 50 is the neutral answer."""
        values = column(series(["100"] * 20), ind.rsi(14))
        assert math.isclose(values[-1], 50.0)

    def test_rsi_stays_in_range(self):
        closes = [
            "100",
            "104",
            "98",
            "107",
            "103",
            "111",
            "105",
            "115",
            "109",
            "119",
            "112",
            "123",
            "116",
            "127",
            "120",
            "131",
        ]
        values = [v for v in column(series(closes), ind.rsi(14)) if v is not None]
        assert values, "RSI produced no values"
        assert all(0.0 <= v <= 100.0 for v in values)

    def test_warmup_is_period_plus_one(self):
        """RSI(14) consumes one bar for the first diff, so 15 bars are needed."""
        values = column(series([str(100 + i) for i in range(20)]), ind.rsi(14))
        assert values[13] is None
        assert values[14] is not None


# ---------------------------------------------------------------------------
# True range / ATR
# ---------------------------------------------------------------------------
class TestTrueRange:
    def test_true_range_uses_intrabar_range_when_no_gap(self):
        frame = series(["100", "100"], highs=["105", "104"], lows=["95", "96"])
        values = column(frame, ind.true_range())
        assert math.isclose(values[0], 10.0), "first bar: high - low"
        assert math.isclose(values[1], 8.0)

    def test_true_range_captures_gaps(self):
        """A bar that gaps above the previous close has TR > its own high-low range."""
        # bar 0: close 100. bar 1: high 130, low 125, close 128.
        # high-low = 5, |high - prev_close| = 30, |low - prev_close| = 25 -> TR = 30
        frame = series(["100", "128"], highs=["100", "130"], lows=["100", "125"])
        values = column(frame, ind.true_range())
        assert math.isclose(values[1], 30.0), "TR must account for the overnight gap"

    def test_atr_of_constant_range_equals_that_range(self):
        closes = ["100"] * 20
        highs = ["102"] * 20
        lows = ["98"] * 20
        values = column(series(closes, highs=highs, lows=lows), ind.atr(14))
        assert math.isclose(values[-1], 4.0, rel_tol=1e-9)

    def test_atr_percent_is_scale_free(self):
        """Doubling every price leaves ATR% unchanged; that is the point of it."""
        closes = ["100"] * 20
        cheap = column(series(closes, highs=["102"] * 20, lows=["98"] * 20), ind.atr_percent(14))
        rich = column(
            series(["200"] * 20, highs=["204"] * 20, lows=["196"] * 20),
            ind.atr_percent(14),
        )
        assert math.isclose(cheap[-1], rich[-1], rel_tol=1e-9)


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------
class TestVolatility:
    def test_constant_series_has_zero_volatility(self):
        values = column(series(["100"] * 30), ind.rolling_volatility(20))
        assert math.isclose(values[-1], 0.0, abs_tol=1e-12)

    def test_annualisation_scales_by_sqrt_periods(self):
        closes = [
            "100",
            "102",
            "99",
            "103",
            "101",
            "105",
            "102",
            "107",
            "104",
            "109",
            "106",
            "111",
            "108",
            "113",
            "110",
            "115",
            "112",
            "117",
            "114",
            "119",
            "116",
            "121",
        ]
        raw = column(series(closes), ind.rolling_volatility(20, annualize=False))
        annual = column(series(closes), ind.rolling_volatility(20, periods_per_year=252))
        assert math.isclose(annual[-1], raw[-1] * math.sqrt(252), rel_tol=1e-9)

    def test_volatility_ratio_reflects_the_sample_std_correction(self):
        """On a perfectly alternating series, the ratio is set purely by ddof=1.

        Log returns alternate +r, -r with mean zero, so the sample variance over
        n bars is ``n*r^2 / (n-1)``. The ratio of the 10-bar to the 60-bar std is
        therefore ``sqrt((10/9) / (60/59))`` = 1.0453 -- not 1.0.

        Pinning the exact value documents that ``ddof=1`` is intentional (the
        finance convention) rather than an accident, and would catch a silent
        switch to a population standard deviation.
        """
        closes = [str(100 + (i % 2) * 2) for i in range(80)]
        values = column(series(closes), ind.volatility_ratio(10, 60))
        expected = math.sqrt((10 / 9) / (60 / 59))
        assert math.isclose(values[-1], expected, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------
class TestVolume:
    def test_relative_volume_excludes_the_current_bar(self):
        """The baseline is the PRIOR window, so a spike does not damp itself.

        20 bars at 1000, then one at 5000. Baseline = 1000, so rel volume = 5.0.
        Including the current bar would give 5000/1190 = 4.2 -- understating it.
        """
        volumes = ["1000"] * 20 + ["5000"]
        values = column(series(["100"] * 21, volumes=volumes), ind.relative_volume(20))
        assert math.isclose(values[-1], 5.0)

    def test_relative_volume_of_flat_volume_is_one(self):
        values = column(series(["100"] * 25, volumes=["1000"] * 25), ind.relative_volume(20))
        assert math.isclose(values[-1], 1.0)

    def test_zero_baseline_volume_yields_null_not_infinity(self):
        volumes = ["0"] * 20 + ["500"]
        values = column(series(["100"] * 21, volumes=volumes), ind.relative_volume(20))
        assert values[-1] is None, "division by a zero baseline must be null, not inf"


# ---------------------------------------------------------------------------
# Frame validation
# ---------------------------------------------------------------------------
class TestFrameValidation:
    def test_duplicate_timestamps_rejected(self):
        candle = CandleData(
            timestamp=START,
            open=Decimal(100),
            high=Decimal(100),
            low=Decimal(100),
            close=Decimal(100),
            volume=Decimal(1),
        )
        with pytest.raises(ValidationError, match="duplicate"):
            candles_to_frame([candle, candle])

    def test_unsorted_timestamps_rejected(self):
        """Unsorted input silently breaks every trailing-window calculation."""

        def at(day: int) -> CandleData:
            return CandleData(
                timestamp=START + timedelta(days=day),
                open=Decimal(100),
                high=Decimal(100),
                low=Decimal(100),
                close=Decimal(100),
                volume=Decimal(1),
            )

        with pytest.raises(ValidationError, match="not sorted"):
            candles_to_frame([at(1), at(0), at(2)])

    def test_empty_input_gives_empty_frame(self):
        frame = candles_to_frame([])
        assert frame.height == 0
        assert "close" in frame.columns
