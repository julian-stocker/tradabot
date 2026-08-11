"""Fill pricing, costs and slippage for a single order leg."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.config import CostSettings
from app.costs.calculator import estimate_round_trip_cost
from app.domain.enums import Side
from app.domain.quotes import Quote
from app.paper.execution import liquidation_value, price_fill

NOW = datetime(2024, 6, 3, 12, 0, tzinfo=UTC)


def quote(bid: str = "99.95", ask: str = "100.05") -> Quote:
    return Quote(symbol="TEST", timestamp=NOW, bid=Decimal(bid), ask=Decimal(ask))


def settings(
    *, order_fee: str = "1.00", variable: str = "0", slippage: str = "0.5", spread_bps: str = "10"
) -> CostSettings:
    return CostSettings(
        order_fee=Decimal(order_fee),
        variable_fee_rate=Decimal(variable),
        slippage_spread_multiple=Decimal(slippage),
        default_spread_bps=float(spread_bps),
    )


class TestMarketBuyFillsFromAsk:
    def test_buy_starts_at_the_ask_not_the_mid(self):
        """A market buy crosses the book. Filling at the mid invents liquidity."""
        pricing = price_fill(
            side=Side.LONG, quantity=Decimal(10), settings=settings(slippage="0"), quote=quote()
        )
        assert pricing.touch_price == Decimal("100.050000")
        assert pricing.fill_price == Decimal("100.050000")
        assert pricing.fill_price > pricing.mid_price

    def test_slippage_pushes_the_buy_above_the_ask(self):
        """The spec's requirement: virtual buy fill > ask."""
        pricing = price_fill(
            side=Side.LONG, quantity=Decimal(10), settings=settings(), quote=quote()
        )
        assert pricing.fill_price > quote().ask
        assert pricing.slippage_cost > 0

    def test_slippage_is_never_favourable(self):
        for multiple in ("0", "0.5", "1", "3"):
            pricing = price_fill(
                side=Side.LONG,
                quantity=Decimal(10),
                settings=settings(slippage=multiple),
                quote=quote(),
            )
            assert pricing.fill_price >= pricing.touch_price


class TestMarketSellFillsFromBid:
    def test_sell_starts_at_the_bid(self):
        pricing = price_fill(
            side=Side.SHORT, quantity=Decimal(10), settings=settings(slippage="0"), quote=quote()
        )
        assert pricing.touch_price == Decimal("99.950000")
        assert pricing.fill_price < pricing.mid_price

    def test_slippage_pushes_the_sell_below_the_bid(self):
        pricing = price_fill(
            side=Side.SHORT, quantity=Decimal(10), settings=settings(), quote=quote()
        )
        assert pricing.fill_price < quote().bid


class TestCosts:
    def test_fixed_fee_is_charged_per_leg(self):
        pricing = price_fill(
            side=Side.LONG, quantity=Decimal(10), settings=settings(order_fee="2.50"), quote=quote()
        )
        assert pricing.fee == Decimal("2.500000")

    def test_percentage_fee_scales_with_notional(self):
        small = price_fill(
            side=Side.LONG,
            quantity=Decimal(1),
            settings=settings(order_fee="0", variable="0.001"),
            quote=quote(),
        )
        large = price_fill(
            side=Side.LONG,
            quantity=Decimal(100),
            settings=settings(order_fee="0", variable="0.001"),
            quote=quote(),
        )
        assert math.isclose(float(large.fee / small.fee), 100.0, rel_tol=1e-6)

    def test_fixed_and_percentage_fees_combine(self):
        pricing = price_fill(
            side=Side.LONG,
            quantity=Decimal(10),
            settings=settings(order_fee="1.00", variable="0.001"),
            quote=quote(),
        )
        # 1.00 fixed + 0.1% of ~1000.75 notional
        assert pricing.fee > Decimal("2.00")
        assert pricing.fee < Decimal("2.10")

    def test_cost_components_sum_to_total(self):
        pricing = price_fill(
            side=Side.LONG, quantity=Decimal(37), settings=settings(), quote=quote()
        )
        assert pricing.spread_cost + pricing.slippage_cost + pricing.fee == pricing.total_cost

    def test_per_leg_reconciles_with_round_trip(self):
        """The per-leg model and the phase 1 round-trip model must agree exactly.

        Two cost models that disagree would mean a signal's net-edge gate and the
        broker's actual fills were computed against different assumptions.
        """
        cost_settings = settings()
        q = quote()
        qty = Decimal(50)

        buy = price_fill(side=Side.LONG, quantity=qty, settings=cost_settings, quote=q)
        sell = price_fill(side=Side.SHORT, quantity=qty, settings=cost_settings, quote=q)
        round_trip = estimate_round_trip_cost(
            entry_mid=q.mid_price,
            exit_mid=q.mid_price,
            quantity=qty,
            spread_bps=q.spread_bps,
            settings=cost_settings,
        )
        assert buy.total_cost + sell.total_cost == round_trip.total_cost

    def test_cash_delta_signs(self):
        """A buy consumes cash; a sell returns it. Fees always reduce."""
        buy = price_fill(side=Side.LONG, quantity=Decimal(10), settings=settings(), quote=quote())
        sell = price_fill(side=Side.SHORT, quantity=Decimal(10), settings=settings(), quote=quote())
        assert buy.cash_delta < 0
        assert sell.cash_delta > 0
        assert buy.cash_delta == -(buy.notional + buy.fee)
        assert sell.cash_delta == sell.notional - sell.fee


class TestFallbackWithoutQuote:
    def test_touch_is_reconstructed_from_the_default_spread(self):
        pricing = price_fill(
            side=Side.LONG,
            quantity=Decimal(10),
            settings=settings(spread_bps="20"),
            reference_price=Decimal(100),
        )
        # 20 bps spread -> 10 bps half-spread -> ask 100.10
        assert pricing.touch_price == Decimal("100.100000")
        assert not pricing.used_quote

    def test_quote_is_marked_as_used_when_present(self):
        pricing = price_fill(
            side=Side.LONG, quantity=Decimal(10), settings=settings(), quote=quote()
        )
        assert pricing.used_quote

    def test_refuses_to_invent_a_price(self):
        with pytest.raises(ValueError, match="refusing to invent"):
            price_fill(side=Side.LONG, quantity=Decimal(10), settings=settings())

    def test_rejects_non_positive_quantity(self):
        with pytest.raises(ValueError, match="must be positive"):
            price_fill(side=Side.LONG, quantity=Decimal(0), settings=settings(), quote=quote())


class TestLiquidationValue:
    def test_marks_at_the_bid_when_a_quote_exists(self):
        """Mid-marking overstates equity by half a spread on every position."""
        value = liquidation_value(quantity=Decimal(10), quote=quote(), mark_price=Decimal(100))
        assert value == Decimal("999.500000")

    def test_falls_back_to_the_mark_without_a_quote(self):
        value = liquidation_value(quantity=Decimal(10), quote=None, mark_price=Decimal(100))
        assert value == Decimal("1000.000000")

    def test_bid_marking_is_more_conservative_than_mid(self):
        q = quote()
        at_bid = liquidation_value(quantity=Decimal(10), quote=q, mark_price=q.mid_price)
        at_mid = liquidation_value(quantity=Decimal(10), quote=None, mark_price=q.mid_price)
        assert at_bid < at_mid


class TestPositionSizeCostImpact:
    """Part Y.6: the same broker costs very different rates at different sizes."""

    @pytest.mark.parametrize(
        ("notional", "expected_bps"),
        [(Decimal(50), 415.0), (Decimal(500), 55.0), (Decimal(5000), 19.0)],
    )
    def test_cost_in_bps_by_portfolio_size(self, notional, expected_bps):
        cost_settings = settings()
        price = Decimal(100)
        quantity = notional / price
        buy = price_fill(
            side=Side.LONG, quantity=quantity, settings=cost_settings, reference_price=price
        )
        sell = price_fill(
            side=Side.SHORT, quantity=quantity, settings=cost_settings, reference_price=price
        )
        round_trip_bps = float((buy.total_cost + sell.total_cost) / notional) * 10_000
        assert math.isclose(round_trip_bps, expected_bps, rel_tol=1e-3)

    def test_cost_rate_falls_monotonically_with_size(self):
        cost_settings = settings()
        price = Decimal(100)
        rates = []
        for notional in (Decimal(50), Decimal(500), Decimal(5000), Decimal(50000)):
            quantity = notional / price
            buy = price_fill(
                side=Side.LONG, quantity=quantity, settings=cost_settings, reference_price=price
            )
            rates.append(float(buy.total_cost / notional))
        assert rates == sorted(rates, reverse=True)
