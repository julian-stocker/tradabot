"""Counterfactual evaluation of trade decisions.

The point of these tests is the SKIP case: a cost gate that rejects everything
looks identical, in a P&L report, to one that is correctly protecting the
portfolio. Only forward measurement distinguishes them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.models import Candle, DecisionOutcome, TradeDecisionRow
from app.domain.enums import (
    AssetType,
    Classification,
    DecisionReason,
    Timeframe,
    TradeDecisionType,
)
from app.instruments.repository import InstrumentRepository
from app.market_data.provider import InstrumentInfo
from app.paper.counterfactual import CounterfactualService, measure_forward_window

T0 = datetime(2024, 3, 1, tzinfo=UTC)
ZERO = Decimal(0)


class FakeCandle:
    """Minimal stand-in for the ORM row, for the pure-function tests."""

    def __init__(self, day: int, high: str, low: str, close: str) -> None:
        self.timestamp = T0 + timedelta(days=day)
        self.high = Decimal(high)
        self.low = Decimal(low)
        self.close = Decimal(close)


async def make_instrument(session) -> int:
    repo = InstrumentRepository(session)
    await repo.upsert_many(
        [
            InstrumentInfo(
                symbol="CF",
                name="Counterfactual Inc.",
                exchange="XNAS",
                currency="EUR",
                asset_type=AssetType.STOCK,
            )
        ]
    )
    await session.flush()
    instrument = await repo.get_by_symbol("CF")
    assert instrument is not None
    return instrument.id


async def add_candles(session, instrument_id: int, closes: list[str], *, start_day: int = 1):
    for offset, close in enumerate(closes, start=start_day):
        price = Decimal(close)
        session.add(
            Candle(
                instrument_id=instrument_id,
                timeframe=Timeframe.D1,
                timestamp=T0 + timedelta(days=offset),
                open=price,
                high=price + Decimal("2"),
                low=price - Decimal("2"),
                close=price,
                volume=Decimal(1000),
            )
        )
    await session.flush()


async def make_decision(
    session, instrument_id: int, *, decision: TradeDecisionType = TradeDecisionType.SKIP
) -> TradeDecisionRow:
    """A persisted decision, with the signal and profile its FKs require."""
    from app.domain.enums import Horizon, PriceSeriesAdjustment
    from app.signals.repository import SignalRepository
    from app.simulation.defaults import build_default_profiles
    from app.simulation.repository import SimulationProfileRepository

    profiles = SimulationProfileRepository(session)
    await profiles.upsert_many(build_default_profiles()[:1])
    await session.flush()
    profile = (await profiles.list_profiles())[0]

    from app.costs.models import NetEdge
    from app.signals.models import SignalResult

    signal_id = await SignalRepository(session).record(
        result=SignalResult(
            symbol="CF",
            timestamp=T0,
            generated_at=T0,
            timeframe=Timeframe.D1,
            horizon=Horizon.D5,
            score=70.0,
            classification=Classification.STRONG_BULLISH,
            confidence=0.8,
            components=(),
            feature_snapshot={},
            reference_price=Decimal("100"),
            spread_bps=Decimal("10"),
            net_edge=NetEdge(
                expected_move_bps=Decimal("300"),
                cost_bps=Decimal("19"),
                net_edge_bps=Decimal("281"),
            ),
            bars_used=200,
            engine_version="test-v1",
        ),
        instrument_id=instrument_id,
        adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
    )

    row = TradeDecisionRow(
        signal_id=signal_id,
        simulation_profile_id=profile.id,
        instrument_id=instrument_id,
        decided_at=T0,
        decision=decision,
        reason=(
            DecisionReason.NEGATIVE_NET_EDGE
            if decision is TradeDecisionType.SKIP
            else DecisionReason.ACCEPTED
        ),
        reason_detail="test",
        side=None,
        signal_score=70.0,
        signal_classification=Classification.STRONG_BULLISH,
        signal_confidence=0.8,
        expected_move_bps=Decimal("300"),
        reference_price=Decimal("100"),
        spread_bps=Decimal("10"),
        available_capital=Decimal("5000"),
        position_quantity=ZERO,
        position_notional=ZERO,
        estimated_fees=ZERO,
        estimated_spread_cost=ZERO,
        estimated_slippage=ZERO,
        estimated_total_cost=ZERO,
        cost_bps_at_size=ZERO,
        net_edge_bps_at_size=Decimal("-50"),
    )
    session.add(row)
    await session.flush()
    return row


class TestForwardWindowMeasurement:
    def test_measures_return_and_excursions(self):
        window = measure_forward_window(
            reference_price=Decimal("100"),
            candles=[FakeCandle(1, "105", "98", "104"), FakeCandle(2, "112", "103", "110")],
        )
        assert window is not None
        assert window.bars == 2
        assert window.forward_return == pytest.approx(0.10)
        assert window.max_favorable_excursion == pytest.approx(0.12)
        assert window.max_adverse_excursion == pytest.approx(-0.02)

    def test_excursions_use_high_and_low_not_close(self):
        """How far the trade went while open, not where each bar ended."""
        window = measure_forward_window(
            reference_price=Decimal("100"), candles=[FakeCandle(1, "150", "50", "100")]
        )
        assert window is not None
        assert window.forward_return == pytest.approx(0.0)
        assert window.max_favorable_excursion == pytest.approx(0.5)
        assert window.max_adverse_excursion == pytest.approx(-0.5)

    def test_no_candles_is_none_not_zero(self):
        """Absent data is not a zero return."""
        assert measure_forward_window(reference_price=Decimal("100"), candles=[]) is None

    def test_non_positive_reference_price_is_none(self):
        assert (
            measure_forward_window(reference_price=ZERO, candles=[FakeCandle(1, "1", "1", "1")])
            is None
        )


class TestCounterfactualService:
    async def test_evaluates_a_skip_decision(self, session):
        """The case the whole module exists for."""
        instrument_id = await make_instrument(session)
        await add_candles(session, instrument_id, ["105", "110", "115"])
        decision = await make_decision(session, instrument_id)

        outcome = await CounterfactualService(session).evaluate_decision(
            decision=decision, timeframe=Timeframe.D1, horizon_bars=3, now=T0
        )
        assert outcome is not None
        assert outcome.bars_evaluated == 3
        assert outcome.forward_return == pytest.approx(0.15)
        assert outcome.trade_decision_id == decision.id

    async def test_a_skip_that_would_have_been_profitable_is_visible(self, session):
        """If the skipped set would have made money, the gate is wrong.

        This is the finding the counterfactual exists to surface.
        """
        instrument_id = await make_instrument(session)
        await add_candles(session, instrument_id, ["120", "130", "140"])
        decision = await make_decision(session, instrument_id, decision=TradeDecisionType.SKIP)

        outcome = await CounterfactualService(session).evaluate_decision(
            decision=decision, timeframe=Timeframe.D1, horizon_bars=3, now=T0
        )
        assert outcome is not None
        assert outcome.forward_return > 0.3

    async def test_returns_none_without_enough_forward_data(self, session):
        """A decision made yesterday cannot have a 20-bar outcome."""
        instrument_id = await make_instrument(session)
        await add_candles(session, instrument_id, ["105"])
        decision = await make_decision(session, instrument_id)

        outcome = await CounterfactualService(session).evaluate_decision(
            decision=decision, timeframe=Timeframe.D1, horizon_bars=20, now=T0
        )
        assert outcome is None

    async def test_the_decision_bar_itself_is_excluded(self, session):
        """Including it would measure a return the decision maker already knew."""
        instrument_id = await make_instrument(session)
        # A bar exactly at the decision time, then the forward window.
        session.add(
            Candle(
                instrument_id=instrument_id,
                timeframe=Timeframe.D1,
                timestamp=T0,
                open=Decimal("50"),
                high=Decimal("50"),
                low=Decimal("50"),
                close=Decimal("50"),
                volume=Decimal(1),
            )
        )
        await add_candles(session, instrument_id, ["110", "120"])
        decision = await make_decision(session, instrument_id)

        outcome = await CounterfactualService(session).evaluate_decision(
            decision=decision, timeframe=Timeframe.D1, horizon_bars=2, now=T0
        )
        assert outcome is not None
        assert outcome.bars_evaluated == 2
        assert outcome.horizon_close == Decimal("120.000000")

    async def test_is_idempotent(self, session):
        instrument_id = await make_instrument(session)
        await add_candles(session, instrument_id, ["105", "110"])
        decision = await make_decision(session, instrument_id)
        service = CounterfactualService(session)

        for _ in range(3):
            await service.evaluate_decision(
                decision=decision, timeframe=Timeframe.D1, horizon_bars=2, now=T0
            )
        await session.flush()

        from sqlalchemy import select

        rows = (await session.execute(select(DecisionOutcome))).scalars().all()
        assert len(rows) == 1

    async def test_does_not_touch_portfolio_state(self, session):
        """Counterfactuals are observations, not portfolio events.

        A bug here can produce a wrong number in a report; it must not be able to
        corrupt a balance.
        """
        from sqlalchemy import select

        from app.db.models import VirtualPortfolio

        instrument_id = await make_instrument(session)
        await add_candles(session, instrument_id, ["105", "110"])
        decision = await make_decision(session, instrument_id)

        await CounterfactualService(session).evaluate_decision(
            decision=decision, timeframe=Timeframe.D1, horizon_bars=2, now=T0
        )
        await session.flush()

        portfolios = (await session.execute(select(VirtualPortfolio))).scalars().all()
        assert portfolios == []

    async def test_outcomes_can_be_fetched_for_decisions(self, session):
        instrument_id = await make_instrument(session)
        await add_candles(session, instrument_id, ["105", "110"])
        decision = await make_decision(session, instrument_id)
        service = CounterfactualService(session)
        await service.evaluate_decision(
            decision=decision, timeframe=Timeframe.D1, horizon_bars=2, now=T0
        )
        await session.flush()

        outcomes = await service.outcomes_for_decisions([decision.id])
        assert len(outcomes) == 1

    async def test_empty_id_list_returns_nothing(self, session):
        assert await CounterfactualService(session).outcomes_for_decisions([]) == []
