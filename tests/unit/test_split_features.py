"""Feature-engine behaviour across a stock split.

A split is a non-event economically and a violent one arithmetically. On raw
prices a 4-for-1 split is a -75% return, and that single bar contaminates
*every* feature at once: momentum, volatility, ATR, relative volume and every
moving average. This file pins the distinction between the two series.

The tests use a perfectly flat price series so any non-zero reading is
unambiguously an artefact of the split rather than of the underlying data.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.corporate_actions.adjust import AdjustedCandle, adjust_candles
from app.corporate_actions.models import CorporateAction
from app.domain.enums import CorporateActionType, PriceSeriesAdjustment, Timeframe
from app.features.engine import FeatureEngine
from app.features.frame import candles_to_frame

T0 = datetime(2024, 1, 1, tzinfo=UTC)
SPLIT_INDEX = 90
TOTAL_BARS = 140
RATIO = Decimal(4)

PRE_PRICE = Decimal("400")
POST_PRICE = PRE_PRICE / RATIO
PRE_VOLUME = Decimal("1000")
POST_VOLUME = PRE_VOLUME * RATIO


def flat_series_with_split() -> list[AdjustedCandle]:
    """A constant-value instrument that undergoes a 4-for-1 split.

    Nothing about this instrument changes. Every feature on the adjusted series
    should therefore read as perfectly quiet.
    """
    bars: list[AdjustedCandle] = []
    for index in range(TOTAL_BARS):
        pre = index < SPLIT_INDEX
        price = PRE_PRICE if pre else POST_PRICE
        volume = PRE_VOLUME if pre else POST_VOLUME
        bars.append(
            AdjustedCandle(
                timestamp=T0 + timedelta(days=index),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
            )
        )
    return bars


SPLIT_ACTION = CorporateAction(
    symbol="TEST",
    action_type=CorporateActionType.SPLIT,
    effective_at=T0 + timedelta(days=SPLIT_INDEX),
    from_shares=Decimal(1),
    to_shares=RATIO,
)


def computed(adjustment: PriceSeriesAdjustment):
    engine = FeatureEngine.for_timeframe(Timeframe.D1)
    candles = adjust_candles(flat_series_with_split(), [SPLIT_ACTION], adjustment)
    return engine.compute(candles_to_frame(candles))


@pytest.fixture(scope="module")
def raw_frame():
    return computed(PriceSeriesAdjustment.RAW)


@pytest.fixture(scope="module")
def adjusted_frame():
    return computed(PriceSeriesAdjustment.SPLIT_ADJUSTED)


def column(frame, name: str) -> list[float | None]:
    return frame.get_column(name).to_list()


class TestRawSeriesShowsTheDistortion:
    """Control group. If these fail, the test data is not exercising the problem."""

    def test_raw_return_is_a_75_percent_crash(self, raw_frame):
        assert math.isclose(column(raw_frame, "return_1")[SPLIT_INDEX], -0.75, rel_tol=1e-9)

    def test_raw_volatility_spikes(self, raw_frame):
        after = column(raw_frame, "volatility_20")[SPLIT_INDEX + 1]
        assert after > 1.0, "a split should wreck volatility on the raw series"

    def test_raw_atr_spikes(self, raw_frame):
        assert column(raw_frame, "atr_pct_14")[SPLIT_INDEX] > 10.0

    def test_raw_moving_average_is_discontinuous(self, raw_frame):
        """Price ends up far below an SMA still averaging pre-split levels."""
        assert column(raw_frame, "dist_sma_20")[SPLIT_INDEX] < -50.0


class TestAdjustedSeriesIsClean:
    """The actual requirement."""

    def test_no_return_spike(self, adjusted_frame):
        returns = [r for r in column(adjusted_frame, "return_1") if r is not None]
        assert max(abs(r) for r in returns) < 1e-9, (
            "the split produced a return in the adjusted series"
        )

    def test_no_volatility_spike(self, adjusted_frame):
        values = [v for v in column(adjusted_frame, "volatility_20") if v is not None]
        assert max(values) < 1e-9

    def test_no_atr_distortion(self, adjusted_frame):
        values = [v for v in column(adjusted_frame, "atr_pct_14") if v is not None]
        assert max(values) < 1e-9

    def test_no_moving_average_discontinuity(self, adjusted_frame):
        """Price stays on its moving average throughout."""
        values = [v for v in column(adjusted_frame, "dist_sma_20") if v is not None]
        assert max(abs(v) for v in values) < 1e-9

    def test_ema_spread_stays_flat(self, adjusted_frame):
        values = [v for v in column(adjusted_frame, "ema_spread_20_50") if v is not None]
        assert max(abs(v) for v in values) < 1e-9

    def test_rsi_stays_neutral(self, adjusted_frame):
        """No gains and no losses: Wilder's RSI is undefined, and 50 is the answer."""
        values = [v for v in column(adjusted_frame, "rsi_14") if v is not None]
        assert all(math.isclose(v, 50.0) for v in values)

    def test_relative_volume_stays_at_one(self, adjusted_frame):
        """Volume is rescaled with price, so the split is invisible here too.

        Missing this is a subtle bug: adjusting prices but not volume turns a
        4-for-1 split into a permanent 4x relative-volume signal.
        """
        values = [v for v in column(adjusted_frame, "rel_volume_20") if v is not None]
        assert all(math.isclose(v, 1.0, rel_tol=1e-9) for v in values)

    def test_every_feature_is_finite_and_warmed_up(self, adjusted_frame):
        """No feature is left NaN or infinite by the adjustment.

        ``vol_ratio_10_60`` is the one legitimate null here: it divides short-window
        volatility by long-window volatility, and on a perfectly flat series the
        denominator is exactly zero. Returning null rather than NaN or infinity is
        the divide-by-zero guard working -- an undefined ratio must not leak into
        a weighted score.
        """
        engine = FeatureEngine.for_timeframe(Timeframe.D1)
        row = adjusted_frame.row(-1, named=True)
        undefined_on_a_flat_series = {"vol_ratio_10_60"}

        for name in engine.feature_set.names():
            value = row[name]
            if name in undefined_on_a_flat_series:
                assert value is None, f"{name} should be undefined on a zero-volatility series"
                continue
            assert value is not None, f"{name} did not warm up"
            assert math.isfinite(value), f"{name} is not finite"


class TestAdjustmentScaleInvariance:
    """The property that justifies retrospective adjustment.

    Adjusting rescales a prefix by a constant. Scale-free features must be
    completely unaffected by it; only absolute-price features may move, and only
    by exactly that scale.
    """

    def test_scale_free_features_are_identical_before_the_split(self, raw_frame, adjusted_frame):
        """Within the pre-split prefix, raw and adjusted must agree exactly.

        Both are flat series differing only by a factor of 4, so any scale-free
        feature that disagrees is reading absolute price levels it should not.
        """
        scale_free = ("return_1", "return_5", "rsi_14", "dist_sma_20", "atr_pct_14")
        for name in scale_free:
            raw_values = column(raw_frame, name)[:SPLIT_INDEX]
            adjusted_values = column(adjusted_frame, name)[:SPLIT_INDEX]
            for index, (raw_value, adjusted_value) in enumerate(
                zip(raw_values, adjusted_values, strict=True)
            ):
                if raw_value is None or adjusted_value is None:
                    assert raw_value is None
                    assert adjusted_value is None
                    continue
                assert math.isclose(raw_value, adjusted_value, rel_tol=1e-9, abs_tol=1e-12), (
                    f"{name} differs at bar {index} purely because of price scale"
                )

    def test_absolute_price_features_differ_by_exactly_the_ratio(self, raw_frame, adjusted_frame):
        for name in ("sma_20", "ema_20"):
            raw_value = column(raw_frame, name)[SPLIT_INDEX - 1]
            adjusted_value = column(adjusted_frame, name)[SPLIT_INDEX - 1]
            assert math.isclose(raw_value / float(RATIO), adjusted_value, rel_tol=1e-9)


class TestPrefixInvarianceStillHolds:
    """Phase 1's no-look-ahead guarantee, re-checked on an adjusted series.

    Adjustment must not smuggle future information into feature *relationships*.
    Given a fixed action set, truncating the series after bar i must leave every
    feature at bar i unchanged -- exactly the phase 1 property, now with the
    adjustment layer in the path.
    """

    @pytest.mark.parametrize("truncate_at", [95, 110, 130])
    def test_features_are_prefix_invariant_on_adjusted_prices(self, truncate_at):
        engine = FeatureEngine.for_timeframe(Timeframe.D1)
        candles = flat_series_with_split()

        full = engine.compute(
            candles_to_frame(
                adjust_candles(candles, [SPLIT_ACTION], PriceSeriesAdjustment.SPLIT_ADJUSTED)
            )
        )
        truncated = engine.compute(
            candles_to_frame(
                adjust_candles(
                    candles[: truncate_at + 1], [SPLIT_ACTION], PriceSeriesAdjustment.SPLIT_ADJUSTED
                )
            )
        )

        full_row = full.row(truncate_at, named=True)
        truncated_row = truncated.row(truncate_at, named=True)
        for name in engine.feature_set.names():
            expected, actual = full_row[name], truncated_row[name]
            if expected is None or actual is None:
                assert expected is None, f"{name} warm-up differs"
                assert actual is None, f"{name} warm-up differs"
                continue
            assert math.isclose(expected, actual, rel_tol=1e-9, abs_tol=1e-12), (
                f"feature {name!r} at bar {truncate_at} changed when later bars arrived"
            )

    def test_a_later_split_rescales_prices_but_not_returns(self):
        """The one honest caveat about retrospective adjustment.

        Learning about a *future* split does change earlier adjusted price
        levels -- that is unavoidable and is why this is documented rather than
        claimed to be point-in-time. What it must not do is change any return,
        because a constant rescaling cannot.
        """
        engine = FeatureEngine.for_timeframe(Timeframe.D1)
        candles = flat_series_with_split()[:SPLIT_INDEX]

        without = engine.compute(
            candles_to_frame(adjust_candles(candles, [], PriceSeriesAdjustment.SPLIT_ADJUSTED))
        )
        with_future_split = engine.compute(
            candles_to_frame(
                adjust_candles(candles, [SPLIT_ACTION], PriceSeriesAdjustment.SPLIT_ADJUSTED)
            )
        )

        # Absolute levels move by the split ratio...
        assert not math.isclose(
            column(without, "sma_20")[-1], column(with_future_split, "sma_20")[-1]
        )
        # ...but every return is untouched.
        for name in ("return_1", "return_5", "rsi_14", "dist_sma_20"):
            before = [v for v in column(without, name) if v is not None]
            after = [v for v in column(with_future_split, name) if v is not None]
            for old, new in zip(before, after, strict=True):
                assert math.isclose(old, new, rel_tol=1e-9, abs_tol=1e-12), (
                    f"{name} changed when a future split became known"
                )
