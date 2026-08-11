"""Paper-trading API contracts."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.paper.demo import run_demo

HTTP_OK = 200
HTTP_NOT_FOUND = 404


@pytest.fixture
async def demo_client(client, engine):
    """A client whose database contains a completed demo simulation."""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        await run_demo(session)
        await session.commit()
    return client


class TestOverview:
    async def test_lists_every_portfolio(self, demo_client):
        response = await demo_client.get("/api/v1/simulation/overview")
        assert response.status_code == HTTP_OK

        body = response.json()
        assert body["count"] == 9
        assert {r["profile_name"] for r in body["portfolios"]} >= {
            "50eur-balanced",
            "5000eur-balanced",
        }

    async def test_carries_the_synthetic_data_disclaimer(self, demo_client):
        """The endpoint most likely to be misread as a performance report."""
        body = (await demo_client.get("/api/v1/simulation/overview")).json()
        assert "profitability" in body["disclaimer"]

    async def test_portfolios_diverge(self, demo_client):
        body = (await demo_client.get("/api/v1/simulation/overview")).json()
        equities = {r["profile_name"]: float(r["equity"]) for r in body["portfolios"]}
        assert equities["50eur-balanced"] == 50.0, "fee dominates; no trade taken"
        assert equities["5000eur-balanced"] > 5000.0


class TestPortfolio:
    async def test_returns_portfolio_state(self, demo_client):
        response = await demo_client.get("/api/v1/simulation/profiles/5000eur-balanced/portfolio")
        assert response.status_code == HTTP_OK

        body = response.json()
        assert body["profile_name"] == "5000eur-balanced"
        assert body["currency"] == "EUR"
        assert float(body["initial_capital"]) == 5000.0
        assert body["drawdown"] <= 0

    async def test_equity_equals_cash_plus_positions(self, demo_client):
        from decimal import Decimal

        body = (
            await demo_client.get("/api/v1/simulation/profiles/5000eur-balanced/portfolio")
        ).json()
        assert Decimal(body["equity"]) == Decimal(body["cash"]) + Decimal(body["positions_value"])

    async def test_unknown_profile_is_404(self, demo_client):
        response = await demo_client.get("/api/v1/simulation/profiles/nope/portfolio")
        assert response.status_code == HTTP_NOT_FOUND


class TestPositionsOrdersTrades:
    async def test_positions_are_listed(self, demo_client):
        response = await demo_client.get("/api/v1/simulation/profiles/5000eur-balanced/positions")
        assert response.status_code == HTTP_OK
        assert response.json()

    async def test_positions_can_be_filtered_by_status(self, demo_client):
        body = (
            await demo_client.get(
                "/api/v1/simulation/profiles/5000eur-balanced/positions?status=CLOSED"
            )
        ).json()
        assert all(p["status"] == "CLOSED" for p in body)

    async def test_decision_stage_refusals_never_reach_the_broker(self, demo_client):
        """The two-stage pipeline, visible from outside.

        A 50 EUR portfolio declines this signal at the *decision* stage -- the
        expected move does not survive the round-trip cost at its position size --
        so no order is ever placed. Its order log is empty, while its portfolio
        still exists and is untouched. That distinction is why
        ``DecisionReason`` and ``OrderRejectionReason`` are separate enums.
        """
        orders = (await demo_client.get("/api/v1/simulation/profiles/50eur-balanced/orders")).json()
        assert orders == [], "declined before an order was ever created"

        portfolio = (
            await demo_client.get("/api/v1/simulation/profiles/50eur-balanced/portfolio")
        ).json()
        assert float(portfolio["cash"]) == 50.0
        assert portfolio["trade_count"] == 0

    async def test_execution_stage_rejections_are_recorded_as_orders(self, demo_client, engine):
        """An order refused by the broker is still stored, with its reason."""
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.instruments.repository import InstrumentRepository
        from app.paper.engine import PaperTradingEngine
        from app.paper.repository import PaperTradingRepository
        from app.simulation.repository import SimulationProfileRepository

        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            instrument = await InstrumentRepository(session).get_by_symbol("DEMO")
            assert instrument is not None
            profiles = SimulationProfileRepository(session)
            profile = await profiles.get_profile("5000eur-conservative")
            repository = PaperTradingRepository(session)
            portfolio = await repository.get_portfolio(profile.id)
            trading = PaperTradingEngine(repository, profile, portfolio)

            # No ATR -> no stop -> refused at the broker, and recorded.
            await trading.open_from_decision(
                instrument=instrument,
                trade_decision_id=9999,
                signal_id=None,
                signal_bar_timestamp=datetime(2024, 3, 1, tzinfo=UTC),
                execution_timestamp=datetime(2024, 3, 20, tzinfo=UTC) + timedelta(days=1),
                execution_price=Decimal("100"),
                quote=None,
                atr=None,
            )
            await session.commit()

        body = (
            await demo_client.get("/api/v1/simulation/profiles/5000eur-conservative/orders")
        ).json()
        rejected = [o for o in body if o["status"] == "REJECTED"]
        assert rejected
        assert rejected[0]["rejection_reason"] == "INVALID_STOP"
        assert rejected[0]["rejection_detail"]

    async def test_filled_orders_expose_touch_and_fill(self, demo_client):
        """A buy must fill above the ask, and both prices are recorded."""
        from decimal import Decimal

        body = (await demo_client.get("/api/v1/simulation/profiles/5000eur-balanced/orders")).json()
        buys = [o for o in body if o["side"] == "LONG" and o["status"] == "FILLED"]
        assert buys
        for order in buys:
            assert Decimal(order["executed_price"]) > Decimal(order["touch_price"])

    async def test_trades_expose_the_full_cost_breakdown(self, demo_client):
        from decimal import Decimal

        body = (await demo_client.get("/api/v1/simulation/profiles/5000eur-balanced/trades")).json()
        assert body
        for trade in body:
            reconciled = (
                Decimal(trade["gross_pnl"])
                - Decimal(trade["total_fees"])
                - Decimal(trade["total_spread_cost"])
                - Decimal(trade["total_slippage_cost"])
            )
            assert reconciled == Decimal(trade["net_pnl"])
            assert trade["outcome"] in {"WIN", "LOSS", "BREAKEVEN"}


class TestPerformance:
    async def test_returns_a_summary(self, demo_client):
        response = await demo_client.get("/api/v1/simulation/profiles/5000eur-balanced/performance")
        assert response.status_code == HTTP_OK

        body = response.json()
        assert body["profile_name"] == "5000eur-balanced"
        assert body["trade_count"] >= 1
        assert body["cost_drag_pct"] >= 0

    async def test_rates_are_null_on_a_small_sample(self, demo_client):
        """A win rate from one trade is not a win rate."""
        body = (
            await demo_client.get("/api/v1/simulation/profiles/5000eur-balanced/performance")
        ).json()
        assert body["win_rate"] is None
        assert body["profit_factor"] is None

    async def test_no_sharpe_ratio_is_reported(self, demo_client):
        """Deliberately absent: event-driven snapshots are not a return series."""
        body = (
            await demo_client.get("/api/v1/simulation/profiles/5000eur-balanced/performance")
        ).json()
        assert "sharpe_ratio" not in body


class TestReadOnly:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/simulation/profiles/5000eur-balanced/portfolio",
            "/api/v1/simulation/profiles/5000eur-balanced/positions",
            "/api/v1/simulation/overview",
        ],
    )
    async def test_no_mutation_endpoints_exist(self, demo_client, path):
        """Paper trading is driven by the engine, not by HTTP."""
        for method in ("post", "put", "patch", "delete"):
            response = await getattr(demo_client, method)(path)
            assert response.status_code >= 400
