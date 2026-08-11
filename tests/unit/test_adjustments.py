"""Corporate-action modelling and price-series adjustment.

The correctness question this file exists to answer:

    Does a 2-for-1 split look like a -50% market move?

It must not, in the adjusted series -- and it must, in the raw one, because that
is what actually traded.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.errors import ValidationError
from app.corporate_actions.adjust import (
    AdjustedCandle,
    adjust_candles,
    cumulative_split_factors,
    has_price_affecting_actions,
)
from app.corporate_actions.models import CorporateAction
from app.domain.enums import CorporateActionType, PriceSeriesAdjustment

T0 = datetime(2024, 1, 1, tzinfo=UTC)
SPLIT_DAY = 10


def bar(day: int, price: str, volume: str = "1000") -> AdjustedCandle:
    """A flat bar; only the close matters for most of these assertions."""
    value = Decimal(price)
    return AdjustedCandle(
        timestamp=T0 + timedelta(days=day),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal(volume),
    )


def split(day: int, from_shares: str, to_shares: str, symbol: str = "TEST") -> CorporateAction:
    return CorporateAction(
        symbol=symbol,
        action_type=CorporateActionType.SPLIT,
        effective_at=T0 + timedelta(days=day),
        from_shares=Decimal(from_shares),
        to_shares=Decimal(to_shares),
    )


def dividend(day: int, amount: str, currency: str = "USD") -> CorporateAction:
    return CorporateAction(
        symbol="TEST",
        action_type=CorporateActionType.CASH_DIVIDEND,
        effective_at=T0 + timedelta(days=day),
        payment_at=T0 + timedelta(days=day + 5),
        cash_amount=Decimal(amount),
        currency=currency,
    )


def split_series() -> list[AdjustedCandle]:
    """100 before the split, 50 after -- a textbook 2-for-1."""
    return [bar(d, "100", "1000") for d in range(SPLIT_DAY)] + [
        bar(d, "50", "2000") for d in range(SPLIT_DAY, 20)
    ]


def closes(candles: list[AdjustedCandle]) -> list[float]:
    return [float(c.close) for c in candles]


def returns(candles: list[AdjustedCandle]) -> list[float]:
    values = closes(candles)
    return [values[i] / values[i - 1] - 1 for i in range(1, len(values))]


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------
class TestCorporateActionModel:
    def test_split_ratio(self):
        assert split(1, "1", "2").split_ratio == Decimal(2)
        assert split(1, "2", "3").split_ratio == Decimal("1.5")

    def test_reverse_split_ratio_is_below_one(self):
        action = split(1, "10", "1")
        assert action.split_ratio == Decimal("0.1")
        assert action.is_reverse_split

    def test_forward_split_is_not_reverse(self):
        assert not split(1, "1", "4").is_reverse_split

    def test_split_requires_a_ratio(self):
        with pytest.raises(ValueError, match="requires both from_shares and to_shares"):
            CorporateAction(
                symbol="TEST",
                action_type=CorporateActionType.SPLIT,
                effective_at=T0,
            )

    def test_split_must_not_carry_cash(self):
        with pytest.raises(ValueError, match="must not carry a cash_amount"):
            CorporateAction(
                symbol="TEST",
                action_type=CorporateActionType.SPLIT,
                effective_at=T0,
                from_shares=Decimal(1),
                to_shares=Decimal(2),
                cash_amount=Decimal("1.5"),
            )

    def test_dividend_requires_an_amount(self):
        with pytest.raises(ValueError, match="requires a cash_amount"):
            CorporateAction(
                symbol="TEST",
                action_type=CorporateActionType.CASH_DIVIDEND,
                effective_at=T0,
                currency="USD",
            )

    def test_dividend_requires_a_currency(self):
        """An amount without a currency is not money."""
        with pytest.raises(ValueError, match="requires a currency"):
            CorporateAction(
                symbol="TEST",
                action_type=CorporateActionType.CASH_DIVIDEND,
                effective_at=T0,
                cash_amount=Decimal("0.5"),
            )

    def test_dividend_must_not_carry_a_split_ratio(self):
        with pytest.raises(ValueError, match="must not carry a split ratio"):
            CorporateAction(
                symbol="TEST",
                action_type=CorporateActionType.CASH_DIVIDEND,
                effective_at=T0,
                cash_amount=Decimal("0.5"),
                currency="USD",
                from_shares=Decimal(1),
                to_shares=Decimal(2),
            )

    def test_dividend_records_its_dates_and_money(self):
        action = dividend(5, "0.24", "EUR")
        assert action.cash_amount == Decimal("0.24")
        assert action.currency == "EUR"
        assert action.payment_at is not None
        assert action.payment_at > action.effective_at
        assert action.split_ratio == Decimal(1), "a dividend does not change the share count"

    def test_payment_before_ex_date_rejected(self):
        with pytest.raises(ValueError, match="cannot pay before it goes ex"):
            CorporateAction(
                symbol="TEST",
                action_type=CorporateActionType.CASH_DIVIDEND,
                effective_at=T0 + timedelta(days=10),
                payment_at=T0,
                cash_amount=Decimal("1"),
                currency="USD",
            )

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValueError, match="naive datetime"):
            CorporateAction(
                symbol="TEST",
                action_type=CorporateActionType.SPLIT,
                effective_at=datetime(2024, 1, 1),  # noqa: DTZ001 -- deliberately naive
                from_shares=Decimal(1),
                to_shares=Decimal(2),
            )

    def test_describe_is_human_readable(self):
        assert "2-for-1 split" in split(1, "1", "2").describe()
        assert "reverse split" in split(1, "10", "1").describe()
        assert "cash dividend" in dividend(1, "0.5").describe()

    def test_only_price_affecting_types_flagged(self):
        assert CorporateActionType.SPLIT.affects_price_series
        assert not CorporateActionType.CASH_DIVIDEND.affects_price_series
        assert not CorporateActionType.SYMBOL_CHANGE.affects_price_series


# ---------------------------------------------------------------------------
# The headline requirement
# ---------------------------------------------------------------------------
class TestSplitDoesNotLookLikeAReturn:
    def test_raw_series_shows_the_split_as_a_50_percent_drop(self):
        """Raw must keep the discontinuity: that is what actually traded."""
        raw = adjust_candles(
            split_series(), [split(SPLIT_DAY, "1", "2")], PriceSeriesAdjustment.RAW
        )
        assert math.isclose(returns(raw)[SPLIT_DAY - 1], -0.5)

    def test_adjusted_series_has_no_artificial_return(self):
        """The requirement, stated directly."""
        adjusted = adjust_candles(
            split_series(), [split(SPLIT_DAY, "1", "2")], PriceSeriesAdjustment.SPLIT_ADJUSTED
        )
        assert all(abs(r) < 1e-12 for r in returns(adjusted)), (
            "a 2-for-1 split must not appear as a market move in the adjusted series"
        )

    def test_pre_split_prices_are_halved(self):
        adjusted = adjust_candles(
            split_series(), [split(SPLIT_DAY, "1", "2")], PriceSeriesAdjustment.SPLIT_ADJUSTED
        )
        assert closes(adjusted)[0] == 50.0
        assert closes(adjusted)[-1] == 50.0, "post-split bars are already correct and untouched"

    def test_volume_scales_inversely_to_price(self):
        """Twice the shares at half the price; the traded value is unchanged."""
        adjusted = adjust_candles(
            split_series(), [split(SPLIT_DAY, "1", "2")], PriceSeriesAdjustment.SPLIT_ADJUSTED
        )
        assert float(adjusted[0].volume) == 2000.0
        assert float(adjusted[-1].volume) == 2000.0

    def test_adjusted_series_anchors_on_the_latest_real_price(self):
        """The most recent bar must equal what the market currently quotes.

        Adjusting forwards instead would leave the newest price scaled by every
        historical split -- unrecognisable against a broker screen.
        """
        candles = split_series()
        adjusted = adjust_candles(
            candles, [split(SPLIT_DAY, "1", "2")], PriceSeriesAdjustment.SPLIT_ADJUSTED
        )
        assert adjusted[-1].close == candles[-1].close


class TestReverseSplit:
    def test_reverse_split_multiplies_past_prices(self):
        """1-for-10: price x10, and the same code path handles it."""
        candles = [bar(d, "2", "10000") for d in range(SPLIT_DAY)] + [
            bar(d, "20", "1000") for d in range(SPLIT_DAY, 20)
        ]
        adjusted = adjust_candles(
            candles, [split(SPLIT_DAY, "10", "1")], PriceSeriesAdjustment.SPLIT_ADJUSTED
        )
        assert closes(adjusted)[0] == 20.0
        assert all(abs(r) < 1e-12 for r in returns(adjusted))


class TestMultipleSplits:
    def test_factors_compound(self):
        """A 2-for-1 then a 3-for-1 leaves the earliest bars scaled by 1/6."""
        candles = (
            [bar(d, "600") for d in range(5)]
            + [bar(d, "300") for d in range(5, 10)]
            + [bar(d, "100") for d in range(10, 15)]
        )
        actions = [split(5, "1", "2"), split(10, "1", "3")]
        adjusted = adjust_candles(candles, actions, PriceSeriesAdjustment.SPLIT_ADJUSTED)

        assert closes(adjusted)[0] == 100.0
        assert closes(adjusted)[6] == 100.0
        assert all(abs(r) < 1e-12 for r in returns(adjusted))

    def test_action_order_does_not_matter(self):
        """Actions arrive unsorted from providers; the result must not depend on it."""
        candles = (
            [bar(d, "600") for d in range(5)]
            + [bar(d, "300") for d in range(5, 10)]
            + [bar(d, "100") for d in range(10, 15)]
        )
        ordered = adjust_candles(
            candles, [split(5, "1", "2"), split(10, "1", "3")], PriceSeriesAdjustment.SPLIT_ADJUSTED
        )
        shuffled = adjust_candles(
            candles, [split(10, "1", "3"), split(5, "1", "2")], PriceSeriesAdjustment.SPLIT_ADJUSTED
        )
        assert closes(ordered) == closes(shuffled)


class TestAdjustmentInvariants:
    def test_scaling_preserves_every_return(self):
        """Why retrospective adjustment is safe despite using future knowledge.

        Multiplying a whole prefix by a constant cannot change any return inside
        it. That is the reason a split adjustment removes an artefact without
        manufacturing an edge.
        """
        candles = [bar(d, str(100 + d)) for d in range(SPLIT_DAY)] + [
            bar(d, str((100 + d) / 2)) for d in range(SPLIT_DAY, 20)
        ]
        raw = adjust_candles(candles, [], PriceSeriesAdjustment.RAW)
        adjusted = adjust_candles(
            candles, [split(SPLIT_DAY, "1", "2")], PriceSeriesAdjustment.SPLIT_ADJUSTED
        )

        raw_prefix = returns(raw[:SPLIT_DAY])
        adjusted_prefix = returns(adjusted[:SPLIT_DAY])
        for original, scaled in zip(raw_prefix, adjusted_prefix, strict=True):
            assert math.isclose(original, scaled, rel_tol=1e-9)

    def test_no_actions_is_identity(self):
        candles = split_series()
        adjusted = adjust_candles(candles, [], PriceSeriesAdjustment.SPLIT_ADJUSTED)
        assert closes(adjusted) == closes(candles)

    def test_dividends_do_not_change_a_split_adjusted_series(self):
        """SPLIT_ADJUSTED means splits only. Mixing in dividends silently would
        produce a series that is neither price nor total return."""
        candles = [bar(d, "100") for d in range(10)]
        with_dividend = adjust_candles(
            candles, [dividend(5, "2.50")], PriceSeriesAdjustment.SPLIT_ADJUSTED
        )
        assert closes(with_dividend) == closes(candles)

    def test_ohlc_ordering_survives_adjustment(self):
        candles = [
            AdjustedCandle(
                timestamp=T0 + timedelta(days=d),
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("90"),
                close=Decimal("105"),
                volume=Decimal("1000"),
            )
            for d in range(10)
        ]
        adjusted = adjust_candles(
            candles, [split(5, "1", "2")], PriceSeriesAdjustment.SPLIT_ADJUSTED
        )
        for candle in adjusted:
            assert candle.high >= candle.low
            assert candle.high >= max(candle.open, candle.close)
            assert candle.low <= min(candle.open, candle.close)

    def test_adjustment_happens_in_decimal(self):
        """Exactness matters: repeated splits would compound binary error."""
        candles = [bar(d, "100") for d in range(10)]
        adjusted = adjust_candles(
            candles, [split(5, "1", "3")], PriceSeriesAdjustment.SPLIT_ADJUSTED
        )
        assert isinstance(adjusted[0].close, Decimal)
        assert adjusted[0].close == Decimal("33.333333")

    def test_empty_series(self):
        assert adjust_candles([], [split(1, "1", "2")], PriceSeriesAdjustment.SPLIT_ADJUSTED) == []

    def test_unsorted_series_rejected(self):
        """Factors are assigned positionally; unsorted input would mis-assign them."""
        candles = [bar(5, "100"), bar(1, "100"), bar(9, "100")]
        with pytest.raises(ValidationError, match="sorted ascending"):
            adjust_candles(candles, [split(3, "1", "2")], PriceSeriesAdjustment.SPLIT_ADJUSTED)

    def test_total_return_is_explicitly_unimplemented(self):
        """A wrong reinvestment assumption biases every return; refuse instead."""
        with pytest.raises(NotImplementedError, match="TOTAL_RETURN"):
            adjust_candles(split_series(), [], PriceSeriesAdjustment.TOTAL_RETURN)


class TestCumulativeFactors:
    def test_identity_without_splits(self):
        stamps = [T0 + timedelta(days=d) for d in range(5)]
        assert all(f.is_identity for f in cumulative_split_factors(stamps, []))

    def test_bar_exactly_at_effective_time_is_post_split(self):
        """Half-open boundary: the effective bar already quotes the new share."""
        stamps = [T0 + timedelta(days=d) for d in range(3)]
        factors = cumulative_split_factors(stamps, [split(1, "1", "2")])
        assert factors[0].price == Decimal("0.5"), "bar before the split is adjusted"
        assert factors[1].is_identity, "bar at the effective instant is not"
        assert factors[2].is_identity

    def test_price_and_volume_factors_are_reciprocal(self):
        stamps = [T0 + timedelta(days=d) for d in range(3)]
        for factor in cumulative_split_factors(stamps, [split(2, "1", "4")]):
            assert factor.price * factor.volume == Decimal(1)

    def test_has_price_affecting_actions(self):
        assert has_price_affecting_actions([split(1, "1", "2")])
        assert not has_price_affecting_actions([dividend(1, "0.5")])
        assert not has_price_affecting_actions([])
