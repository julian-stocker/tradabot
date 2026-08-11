"""Spread arithmetic and transaction-cost accounting.

Expected values are computed by hand from the definitions, and comparisons are
exact where the arithmetic is exact -- these are Decimal calculations, so
``pytest.approx`` would hide precision regressions rather than catch them.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.config import CostSettings
from app.costs.calculator import (
    estimate_round_trip_cost,
    half_spread_rate,
    net_expected_edge,
    round_trip_cost_bps,
    spread_bps_for,
)
from app.domain.enums import Side
from app.domain.quotes import Quote, spread_bps_from_prices

NOW = datetime(2024, 6, 3, 12, 0, tzinfo=UTC)


def quote(bid: str, ask: str, symbol: str = "TEST") -> Quote:
    return Quote(symbol=symbol, timestamp=NOW, bid=Decimal(bid), ask=Decimal(ask))


# ---------------------------------------------------------------------------
# Quote / spread
# ---------------------------------------------------------------------------
class TestQuote:
    def test_mid_price_is_exact(self):
        assert quote("99.98", "100.02").mid_price == Decimal("100.00")

    def test_spread_absolute(self):
        assert quote("99.98", "100.02").spread_absolute == Decimal("0.04")

    def test_half_spread_is_one_crossing(self):
        assert quote("99.98", "100.02").half_spread == Decimal("0.02")

    def test_spread_percent(self):
        # 0.04 / 100.00 * 100 = 0.04%
        assert math.isclose(quote("99.98", "100.02").spread_percent, 0.04)

    def test_spread_bps(self):
        # 0.04 / 100.00 * 10000 = 4 bps
        assert math.isclose(quote("99.98", "100.02").spread_bps, 4.0)

    def test_bps_is_100x_percent(self):
        """The relationship that makes bps safe to add to fee rates."""
        q = quote("49.90", "50.10")
        assert math.isclose(q.spread_bps, q.spread_percent * 100)

    def test_crossed_quote_rejected(self):
        """A bid above the ask is a data error; averaging it would be nonsense."""
        with pytest.raises(ValueError, match="crossed quote"):
            quote("100.05", "100.00")

    def test_zero_prices_rejected(self):
        with pytest.raises(ValueError, match="greater than 0"):
            quote("0", "100")

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValueError, match="naive datetime"):
            Quote(
                symbol="X",
                timestamp=datetime(2024, 1, 1),  # noqa: DTZ001 -- deliberately naive
                bid=Decimal(1),
                ask=Decimal(2),
            )

    def test_timestamp_normalised_to_utc(self):
        from datetime import timedelta, timezone

        berlin = timezone(timedelta(hours=2))
        q = Quote(
            symbol="X",
            timestamp=datetime(2024, 6, 3, 14, 0, tzinfo=berlin),
            bid=Decimal(1),
            ask=Decimal(2),
        )
        assert q.timestamp == datetime(2024, 6, 3, 12, 0, tzinfo=UTC)

    def test_zero_spread_is_allowed(self):
        """A locked market is unusual but not invalid."""
        q = quote("100.00", "100.00")
        assert q.spread_bps == 0.0

    def test_helper_matches_quote_property(self):
        assert math.isclose(
            spread_bps_from_prices(Decimal("99.98"), Decimal("100.02")),
            quote("99.98", "100.02").spread_bps,
        )


# ---------------------------------------------------------------------------
# Round-trip costs
# ---------------------------------------------------------------------------
class TestRoundTripCost:
    def test_half_spread_rate(self):
        # 10 bps spread -> 5 bps per side -> 0.0005
        assert half_spread_rate(10.0) == Decimal("0.0005")

    def test_fills_walk_out_from_mid(self):
        """Buy above mid, sell below it. Never the reverse."""
        settings = CostSettings(order_fee=Decimal(0), slippage_spread_multiple=Decimal(0))
        cost = estimate_round_trip_cost(
            entry_mid=Decimal(100),
            exit_mid=Decimal(100),
            quantity=Decimal(10),
            spread_bps=10.0,
            settings=settings,
        )
        assert cost.entry_fill == Decimal("100.0500")
        assert cost.exit_fill == Decimal("99.9500")

    def test_spread_only_cost_is_the_full_spread(self):
        """A round trip crosses the book twice, so it pays the whole spread once."""
        settings = CostSettings(order_fee=Decimal(0), slippage_spread_multiple=Decimal(0))
        cost = estimate_round_trip_cost(
            entry_mid=Decimal(100),
            exit_mid=Decimal(100),
            quantity=Decimal(10),
            spread_bps=10.0,
            settings=settings,
        )
        # 10 bps of a 1000 notional = 1.00
        assert cost.total_cost == Decimal("1.00")
        assert cost.total_cost_bps == Decimal(10)

    def test_slippage_multiple_adds_proportionally(self):
        """slippage = half_spread * multiple, so 1.0 doubles the spread cost."""
        base = CostSettings(order_fee=Decimal(0), slippage_spread_multiple=Decimal(0))
        doubled = CostSettings(order_fee=Decimal(0), slippage_spread_multiple=Decimal(1))
        args = {
            "entry_mid": Decimal(100),
            "exit_mid": Decimal(100),
            "quantity": Decimal(10),
            "spread_bps": 10.0,
        }
        cheap = estimate_round_trip_cost(settings=base, **args)
        dear = estimate_round_trip_cost(settings=doubled, **args)
        assert dear.total_cost == cheap.total_cost * 2

    def test_breakdown_sums_to_total(self):
        """The itemisation must reconcile, or the accounting is decorative."""
        cost = estimate_round_trip_cost(
            entry_mid=Decimal(100),
            exit_mid=Decimal(105),
            quantity=Decimal(37),
            spread_bps=12.5,
            settings=CostSettings(),
        )
        breakdown = cost.breakdown
        assert (
            breakdown.spread_cost + breakdown.fee_cost + breakdown.slippage_cost == cost.total_cost
        )
        assert cost.net_pnl == cost.gross_pnl - cost.total_cost

    def test_fees_are_charged_on_both_legs(self):
        settings = CostSettings(order_fee=Decimal("1.50"), slippage_spread_multiple=Decimal(0))
        cost = estimate_round_trip_cost(
            entry_mid=Decimal(100),
            exit_mid=Decimal(100),
            quantity=Decimal(1),
            spread_bps=0.0,
            settings=settings,
        )
        assert cost.breakdown.fee_cost == Decimal("3.00")

    def test_variable_fee_applies_to_both_notionals(self):
        settings = CostSettings(
            order_fee=Decimal(0),
            variable_fee_rate=Decimal("0.001"),
            slippage_spread_multiple=Decimal(0),
        )
        cost = estimate_round_trip_cost(
            entry_mid=Decimal(100),
            exit_mid=Decimal(200),
            quantity=Decimal(1),
            spread_bps=0.0,
            settings=settings,
        )
        # 0.1% of (100 + 200)
        assert cost.breakdown.fee_cost == Decimal("0.300")

    def test_short_side_pays_the_same_friction(self):
        """Costs are adverse regardless of direction."""
        settings = CostSettings(order_fee=Decimal(0), slippage_spread_multiple=Decimal(0))
        args = {
            "entry_mid": Decimal(100),
            "exit_mid": Decimal(100),
            "quantity": Decimal(10),
            "spread_bps": 10.0,
            "settings": settings,
        }
        assert (
            estimate_round_trip_cost(side=Side.LONG, **args).total_cost
            == estimate_round_trip_cost(side=Side.SHORT, **args).total_cost
        )

    def test_short_fills_are_mirrored(self):
        settings = CostSettings(order_fee=Decimal(0), slippage_spread_multiple=Decimal(0))
        cost = estimate_round_trip_cost(
            entry_mid=Decimal(100),
            exit_mid=Decimal(100),
            quantity=Decimal(10),
            spread_bps=10.0,
            settings=settings,
            side=Side.SHORT,
        )
        assert cost.entry_fill == Decimal("99.9500"), "sell to open, below mid"
        assert cost.exit_fill == Decimal("100.0500"), "buy to close, above mid"

    def test_short_profits_when_price_falls(self):
        cost = estimate_round_trip_cost(
            entry_mid=Decimal(100),
            exit_mid=Decimal(90),
            quantity=Decimal(10),
            spread_bps=0.0,
            settings=CostSettings(order_fee=Decimal(0)),
            side=Side.SHORT,
        )
        assert cost.gross_pnl == Decimal(100)

    def test_breakeven_equals_cost_in_bps(self):
        cost = estimate_round_trip_cost(
            entry_mid=Decimal(50),
            exit_mid=Decimal(50),
            quantity=Decimal(100),
            spread_bps=8.0,
            settings=CostSettings(),
        )
        assert cost.breakeven_move_bps == cost.total_cost_bps

    def test_fixed_fees_make_small_positions_expensive(self):
        """The most practically important property of the cost model.

        A EUR 1 fee on a EUR 500 position is 40 bps round trip before any spread;
        the same fee on EUR 20,000 is 1 bps. Small positions are often uneconomic
        for reasons unrelated to the signal.
        """
        settings = CostSettings(order_fee=Decimal(1), slippage_spread_multiple=Decimal(0))
        small = round_trip_cost_bps(
            price=Decimal(100), quantity=Decimal(5), spread_bps=0.0, settings=settings
        )
        large = round_trip_cost_bps(
            price=Decimal(100), quantity=Decimal(200), spread_bps=0.0, settings=settings
        )
        assert small == Decimal(40)
        assert large == Decimal(1)

    def test_negative_prices_rejected(self):
        with pytest.raises(ValueError, match="must be positive"):
            estimate_round_trip_cost(
                entry_mid=Decimal(-1),
                exit_mid=Decimal(100),
                quantity=Decimal(1),
                spread_bps=1.0,
                settings=CostSettings(),
            )

    def test_negative_spread_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            half_spread_rate(-1.0)


# ---------------------------------------------------------------------------
# Net edge
# ---------------------------------------------------------------------------
class TestNetEdge:
    def test_edge_is_move_minus_cost(self):
        edge = net_expected_edge(expected_move_bps=30, cost_bps=19)
        assert edge.net_edge_bps == Decimal(11)
        assert edge.is_actionable

    def test_move_smaller_than_cost_is_not_actionable(self):
        """The central financial constraint: right direction, losing trade."""
        edge = net_expected_edge(expected_move_bps=10, cost_bps=19)
        assert edge.net_edge_bps == Decimal(-9)
        assert not edge.is_actionable

    def test_exact_breakeven_is_not_actionable(self):
        """Zero edge is not an opportunity; the boundary is strict."""
        assert not net_expected_edge(expected_move_bps=19, cost_bps=19).is_actionable

    def test_cost_coverage_ratio(self):
        edge = net_expected_edge(expected_move_bps=40, cost_bps=20)
        assert edge.cost_coverage_ratio == 2.0

    def test_cost_coverage_ratio_is_none_when_free(self):
        assert net_expected_edge(expected_move_bps=40, cost_bps=0).cost_coverage_ratio is None

    def test_negative_cost_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            net_expected_edge(expected_move_bps=10, cost_bps=-1)


class TestSpreadResolution:
    def test_uses_quote_when_available(self):
        settings = CostSettings(default_spread_bps=99.0)
        assert spread_bps_for(quote("99.98", "100.02"), settings) == Decimal("4")

    def test_falls_back_to_configured_default(self):
        settings = CostSettings(default_spread_bps=12.5)
        assert spread_bps_for(None, settings) == Decimal("12.5")
