"""Full paper-trading lifecycle against a real database.

Covers the properties that only exist once persistence is involved: portfolio
isolation, idempotency, restart recovery, transactional atomicity and the
no-look-ahead timing guard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.costs.models import NetEdge
from app.db.models import Instrument, VirtualOrder, VirtualPosition
from app.domain.enums import (
    AssetType,
    Classification,
    ExitReason,
    Horizon,
    OrderRejectionReason,
    PositionStatus,
    PriceSeriesAdjustment,
    Timeframe,
)
from app.domain.quotes import Quote
from app.instruments.repository import InstrumentRepository
from app.market_data.provider import InstrumentInfo
from app.paper.engine import LookAheadError, PaperTradingEngine
from app.paper.exits import BarPrices
from app.paper.repository import PaperTradingRepository
from app.paper.service import PaperTradingService
from app.signals.models import SignalResult
from app.signals.repository import SignalRepository
from app.simulation.defaults import build_default_profiles
from app.simulation.repository import SimulationProfileRepository, TradeDecisionRepository

T0 = datetime(2024, 3, 1, tzinfo=UTC)
EXEC_AT = T0 + timedelta(days=1)


async def make_instrument(session, symbol: str = "TEST") -> Instrument:
    repo = InstrumentRepository(session)
    await repo.upsert_many(
        [
            InstrumentInfo(
                symbol=symbol,
                name=f"{symbol} Inc.",
                exchange="XNAS",
                currency="EUR",
                asset_type=AssetType.STOCK,
                listed_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        ]
    )
    await session.flush()
    instrument = await repo.get_by_symbol(symbol)
    assert instrument is not None
    return instrument


def make_signal(
    *, score: float = 82.0, price: str = "100.00", symbol: str = "TEST"
) -> SignalResult:
    return SignalResult(
        symbol=symbol,
        timestamp=T0,
        generated_at=T0,
        timeframe=Timeframe.D1,
        horizon=Horizon.D5,
        score=score,
        classification=Classification.STRONG_BULLISH,
        confidence=0.75,
        components=(),
        feature_snapshot={"atr_pct_14": 2.0},
        reference_price=Decimal(price),
        spread_bps=Decimal("10"),
        net_edge=NetEdge(
            expected_move_bps=Decimal("300"),
            cost_bps=Decimal("19"),
            net_edge_bps=Decimal("281"),
        ),
        bars_used=200,
        engine_version="test-v1",
    )


def quote(price: str = "100.00", at: datetime = EXEC_AT) -> Quote:
    mid = Decimal(price)
    return Quote(symbol="TEST", timestamp=at, bid=mid - Decimal("0.05"), ask=mid + Decimal("0.05"))


def bar(day: int, o: str, h: str, low: str, c: str) -> BarPrices:
    return BarPrices(
        timestamp=EXEC_AT + timedelta(days=day),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
    )


async def build_service(session) -> PaperTradingService:
    profiles = SimulationProfileRepository(session)
    await profiles.upsert_many(build_default_profiles())
    await session.flush()
    return PaperTradingService(
        repository=PaperTradingRepository(session),
        profiles=profiles,
        signals=SignalRepository(session),
        decisions=TradeDecisionRepository(session),
    )


async def engine_for(session, name: str = "5000eur-balanced") -> PaperTradingEngine:
    """Build an engine for one profile.

    ``engine.profile_id`` is attached because profile ids depend on insertion
    order -- hardcoding 1 silently tests the wrong portfolio.
    """
    profiles = SimulationProfileRepository(session)
    await profiles.upsert_many(build_default_profiles())
    await session.flush()
    repository = PaperTradingRepository(session)
    profile = await profiles.get_profile(name)
    portfolio = await repository.ensure_portfolio(profile)
    engine = PaperTradingEngine(repository, profile, portfolio)
    engine.profile_id = profile.id  # type: ignore[attr-defined]
    return engine


async def open_one(session, engine: PaperTradingEngine, instrument: Instrument, *, decision_id=1):
    return await engine.open_from_decision(
        instrument=instrument,
        trade_decision_id=decision_id,
        signal_id=None,
        signal_bar_timestamp=T0,
        execution_timestamp=EXEC_AT,
        execution_price=Decimal("100.00"),
        quote=quote(),
        atr=Decimal("2.00"),
    )


class TestNoLookAhead:
    """Part Z: the most important requirement in the phase."""

    async def test_executing_on_the_signal_bar_raises(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        with pytest.raises(LookAheadError, match="strictly later"):
            await engine.open_from_decision(
                instrument=instrument,
                trade_decision_id=1,
                signal_id=None,
                signal_bar_timestamp=T0,
                execution_timestamp=T0,
                execution_price=Decimal("100"),
                quote=quote(at=T0),
                atr=Decimal("2"),
            )

    async def test_executing_before_the_signal_bar_raises(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        with pytest.raises(LookAheadError):
            await engine.open_from_decision(
                instrument=instrument,
                trade_decision_id=1,
                signal_id=None,
                signal_bar_timestamp=T0,
                execution_timestamp=T0 - timedelta(days=1),
                execution_price=Decimal("100"),
                quote=quote(at=T0 - timedelta(days=1)),
                atr=Decimal("2"),
            )

    async def test_executing_after_the_signal_bar_is_allowed(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        result = await open_one(session, engine, instrument)
        assert result.accepted

    async def test_the_fill_is_never_the_signal_bar_price(self, session):
        """Execution prices come from the execution bar, never the signal bar."""
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await engine.open_from_decision(
            instrument=instrument,
            trade_decision_id=1,
            signal_id=None,
            signal_bar_timestamp=T0,
            execution_timestamp=EXEC_AT,
            execution_price=Decimal("110.00"),
            quote=quote("110.00"),
            atr=Decimal("2.00"),
        )
        await session.flush()
        positions = await PaperTradingRepository(session).open_positions(engine.profile_id)
        assert positions[0].average_entry_price > Decimal("110")
        assert positions[0].entry_timestamp == EXEC_AT


class TestEntryLifecycle:
    async def test_entry_creates_order_and_position_and_moves_cash(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        starting_cash = engine.portfolio.cash

        result = await open_one(session, engine, instrument)
        await session.flush()

        assert result.accepted
        repository = PaperTradingRepository(session)
        positions = await repository.open_positions(engine.profile_id)
        assert len(positions) == 1
        assert engine.portfolio.cash < starting_cash

    async def test_position_records_full_provenance(self, session):
        """Every position traces back to the evidence that opened it."""
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await engine.open_from_decision(
            instrument=instrument,
            trade_decision_id=42,
            signal_id=None,
            signal_bar_timestamp=T0,
            execution_timestamp=EXEC_AT,
            execution_price=Decimal("100"),
            quote=quote(),
            atr=Decimal("2"),
        )
        await session.flush()
        position = (await PaperTradingRepository(session).open_positions(engine.profile_id))[0]
        assert position.originating_trade_decision_id == 42
        assert position.stop_loss is not None
        assert position.take_profit is not None

    async def test_stale_quote_is_rejected(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        result = await engine.open_from_decision(
            instrument=instrument,
            trade_decision_id=1,
            signal_id=None,
            signal_bar_timestamp=T0,
            execution_timestamp=EXEC_AT,
            execution_price=Decimal("100"),
            quote=quote(at=EXEC_AT - timedelta(hours=5)),
            atr=Decimal("2"),
        )
        assert not result.accepted
        assert result.rejection is OrderRejectionReason.STALE_QUOTE

    async def test_missing_atr_rejects_with_invalid_stop(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        result = await engine.open_from_decision(
            instrument=instrument,
            trade_decision_id=1,
            signal_id=None,
            signal_bar_timestamp=T0,
            execution_timestamp=EXEC_AT,
            execution_price=Decimal("100"),
            quote=quote(),
            atr=None,
        )
        assert result.rejection is OrderRejectionReason.INVALID_STOP

    async def test_second_position_in_same_instrument_is_rejected(self, session):
        """Pyramiding is disabled by default."""
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await open_one(session, engine, instrument, decision_id=1)
        await session.flush()
        second = await open_one(session, engine, instrument, decision_id=2)
        assert second.rejection is OrderRejectionReason.POSITION_ALREADY_OPEN

    async def test_delisted_instrument_is_rejected(self, session):
        repo = InstrumentRepository(session)
        await repo.upsert_many(
            [
                InstrumentInfo(
                    symbol="DEAD",
                    name="Dead",
                    exchange="XNAS",
                    currency="EUR",
                    listed_at=datetime(2020, 1, 1, tzinfo=UTC),
                    delisted_at=datetime(2023, 1, 1, tzinfo=UTC),
                )
            ]
        )
        await session.flush()
        instrument = await repo.get_by_symbol("DEAD")
        assert instrument is not None
        engine = await engine_for(session)
        result = await open_one(session, engine, instrument)
        assert result.rejection is OrderRejectionReason.INSTRUMENT_NOT_TRADABLE

    async def test_disabled_profile_cannot_trade(self, session):
        profiles = SimulationProfileRepository(session)
        configs = [
            c.model_copy(update={"enabled": False}) if c.name == "5000eur-balanced" else c
            for c in build_default_profiles()
        ]
        await profiles.upsert_many(configs)
        await session.flush()

        instrument = await make_instrument(session)
        repository = PaperTradingRepository(session)
        profile = await profiles.get_profile("5000eur-balanced")
        portfolio = await repository.ensure_portfolio(profile)
        engine = PaperTradingEngine(repository, profile, portfolio)

        result = await open_one(session, engine, instrument)
        assert result.rejection is OrderRejectionReason.PROFILE_DISABLED


class TestExitsAndPnL:
    async def test_take_profit_closes_and_records_a_trade(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await open_one(session, engine, instrument)
        await session.flush()

        outcome = await engine.process_bar(
            instrument_id=instrument.id, bar=bar(1, "100", "115", "99", "114")
        )
        await session.flush()

        assert outcome.positions_closed == 1
        trades = await PaperTradingRepository(session).trades(engine.profile_id)
        assert len(trades) == 1
        assert trades[0].exit_reason is ExitReason.TAKE_PROFIT

    async def test_stop_loss_closes_with_a_loss(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await open_one(session, engine, instrument)
        await session.flush()

        await engine.process_bar(instrument_id=instrument.id, bar=bar(1, "100", "101", "90", "92"))
        await session.flush()

        trades = await PaperTradingRepository(session).trades(engine.profile_id)
        assert trades[0].exit_reason is ExitReason.STOP_LOSS
        assert trades[0].net_pnl < 0

    async def test_realized_pnl_matches_cash_movement(self, session):
        """The accounting identity: with no open positions, equity - start == realised."""
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        start_cash = engine.portfolio.cash

        await open_one(session, engine, instrument)
        await session.flush()
        await engine.process_bar(instrument_id=instrument.id, bar=bar(1, "100", "115", "99", "114"))
        await session.flush()

        assert engine.portfolio.cash - start_cash == engine.portfolio.realized_pnl

    async def test_trade_cost_breakdown_reconciles(self, session):
        """gross - fees - spread - slippage == net, exactly."""
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await open_one(session, engine, instrument)
        await session.flush()
        await engine.process_bar(instrument_id=instrument.id, bar=bar(1, "100", "115", "99", "114"))
        await session.flush()

        trade = (await PaperTradingRepository(session).trades(engine.profile_id))[0]
        assert (
            trade.gross_pnl - trade.total_fees - trade.total_spread_cost - trade.total_slippage_cost
        ) == trade.net_pnl

    async def test_max_holding_period_closes_the_position(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await open_one(session, engine, instrument)
        await session.flush()

        # balanced max_holding_bars is 15; quiet bars so no stop or target fires.
        for day in range(1, 17):
            await engine.process_bar(
                instrument_id=instrument.id, bar=bar(day, "100", "101", "99", "100")
            )
        await session.flush()

        trades = await PaperTradingRepository(session).trades(engine.profile_id)
        assert len(trades) == 1
        assert trades[0].exit_reason is ExitReason.MAX_HOLDING_PERIOD

    async def test_signal_reversal_exit(self, session):
        instrument = await make_instrument(session)
        service = await build_service(session)
        engine = await engine_for(session)
        await open_one(session, engine, instrument)
        await session.flush()

        closed = await service.close_on_signal_reversal(
            instrument_id=instrument.id,
            timestamp=EXEC_AT + timedelta(days=1),
            mark_price=Decimal("101"),
        )
        await session.flush()
        assert closed >= 1
        trades = await PaperTradingRepository(session).trades(engine.profile_id)
        assert trades[0].exit_reason is ExitReason.SIGNAL_REVERSAL

    async def test_unrealized_pnl_tracks_the_mark(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await open_one(session, engine, instrument)
        await session.flush()

        outcome = await engine.process_bar(
            instrument_id=instrument.id,
            bar=bar(1, "100", "104", "99", "103"),
            quote=quote("103.00", at=EXEC_AT + timedelta(days=1)),
        )
        assert outcome.positions_closed == 0
        assert outcome.valuation.unrealized_pnl > 0

    async def test_mark_to_market_uses_the_bid(self, session):
        """Marking at the mid overstates equity by half a spread per position."""
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await open_one(session, engine, instrument)
        await session.flush()

        mark_quote = Quote(
            symbol="TEST",
            timestamp=EXEC_AT + timedelta(days=1),
            bid=Decimal("102.00"),
            ask=Decimal("104.00"),
        )
        outcome = await engine.process_bar(
            instrument_id=instrument.id,
            bar=bar(1, "100", "104", "99", "103"),
            quote=mark_quote,
        )
        position = (await PaperTradingRepository(session).open_positions(engine.profile_id))[0]
        assert position.current_mark_price == Decimal("102.000000")
        assert outcome.valuation.positions_value == Decimal("102.00") * position.quantity

    async def test_drawdown_is_tracked(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await open_one(session, engine, instrument)
        await session.flush()

        outcome = await engine.process_bar(
            instrument_id=instrument.id, bar=bar(1, "100", "100.5", "97", "97.5")
        )
        assert outcome.valuation.drawdown < 0
        assert engine.portfolio.max_drawdown < 0

    async def test_excursions_are_recorded(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await open_one(session, engine, instrument)
        await session.flush()
        await engine.process_bar(instrument_id=instrument.id, bar=bar(1, "100", "107", "97", "103"))
        await engine.process_bar(
            instrument_id=instrument.id, bar=bar(2, "103", "115", "102", "114")
        )
        await session.flush()

        trade = (await PaperTradingRepository(session).trades(engine.profile_id))[0]
        assert trade.max_favorable_excursion is not None
        assert trade.max_adverse_excursion is not None
        assert trade.max_favorable_excursion > 0
        assert trade.max_adverse_excursion < 0


class TestIdempotency:
    async def test_replaying_a_signal_does_not_trade_twice(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)

        for _ in range(3):
            await open_one(session, engine, instrument, decision_id=1)
        await session.flush()

        repository = PaperTradingRepository(session)
        assert len(await repository.open_positions(engine.profile_id)) == 1
        assert await repository.count_orders(engine.profile_id) == 1

    async def test_replaying_a_candle_does_not_close_twice(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await open_one(session, engine, instrument)
        await session.flush()

        exit_bar = bar(1, "100", "115", "99", "114")
        for _ in range(3):
            await engine.process_bar(instrument_id=instrument.id, bar=exit_bar, advance_clock=False)
        await session.flush()

        trades = await PaperTradingRepository(session).trades(engine.profile_id)
        assert len(trades) == 1

    async def test_replaying_a_candle_does_not_duplicate_snapshots(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await open_one(session, engine, instrument)
        await session.flush()

        quiet = bar(1, "100", "101", "99", "100")
        for _ in range(3):
            await engine.process_bar(instrument_id=instrument.id, bar=quiet, advance_clock=False)
        await session.flush()

        snapshots = await PaperTradingRepository(session).snapshots(engine.profile_id)
        assert len(snapshots) == 1

    async def test_idempotency_key_is_unique_in_the_database(self, session):
        """A database constraint, not just an application check."""
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await open_one(session, engine, instrument)
        await session.flush()

        session.add(
            VirtualOrder(
                simulation_profile_id=engine.profile_id,
                instrument_id=instrument.id,
                idempotency_key="entry:1",
                side="LONG",
                order_type="MARKET",
                status="FILLED",
                quantity=Decimal(1),
                requested_at=EXEC_AT,
                filled_at=EXEC_AT,
                executed_price=Decimal(100),
            )
        )
        with pytest.raises(Exception, match="idempotency_key"):
            await session.flush()


class TestProfileIsolation:
    async def test_portfolios_are_independent(self, session):
        """A loss in one portfolio must not touch another."""
        instrument = await make_instrument(session)
        service = await build_service(session)
        repository = PaperTradingRepository(session)

        profiles = SimulationProfileRepository(session)
        balanced = await profiles.get_profile("5000eur-balanced")
        aggressive = await profiles.get_profile("5000eur-aggressive")

        engine_a = await service.engine_for(balanced)
        engine_b = await service.engine_for(aggressive)

        await engine_a.open_from_decision(
            instrument=instrument,
            trade_decision_id=1,
            signal_id=None,
            signal_bar_timestamp=T0,
            execution_timestamp=EXEC_AT,
            execution_price=Decimal("100"),
            quote=quote(),
            atr=Decimal("2"),
        )
        await session.flush()

        assert balanced.id is not None
        assert aggressive.id is not None
        assert len(await repository.open_positions(balanced.id)) == 1
        assert len(await repository.open_positions(aggressive.id)) == 0
        assert engine_b.portfolio.cash == aggressive.initial_capital

    async def test_one_signal_produces_independent_outcomes(self, session):
        instrument = await make_instrument(session)
        service = await build_service(session)

        run = await service.run_signal(
            signal=make_signal(),
            instrument=instrument,
            adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
            execution_timestamp=EXEC_AT,
            execution_price=Decimal("100"),
            quote=quote(),
            atr=Decimal("2.00"),
            now=EXEC_AT,
        )
        await session.flush()

        assert run.positions_opened >= 1
        # The 50 EUR portfolios cannot afford a position the fee does not destroy.
        rows = (await session.execute(select(VirtualPosition))).scalars().all()
        profile_ids = {r.simulation_profile_id for r in rows}
        assert len(profile_ids) == len(rows), "one position per profile at most"

    async def test_closing_one_portfolio_leaves_others_untouched(self, session):
        instrument = await make_instrument(session)
        service = await build_service(session)
        profiles = SimulationProfileRepository(session)
        repository = PaperTradingRepository(session)

        for name, decision in (("5000eur-balanced", 1), ("5000eur-aggressive", 2)):
            profile = await profiles.get_profile(name)
            engine = await service.engine_for(profile)
            await engine.open_from_decision(
                instrument=instrument,
                trade_decision_id=decision,
                signal_id=None,
                signal_bar_timestamp=T0,
                execution_timestamp=EXEC_AT,
                execution_price=Decimal("100"),
                quote=quote(),
                atr=Decimal("2"),
            )
        await session.flush()

        balanced = await profiles.get_profile("5000eur-balanced")
        aggressive = await profiles.get_profile("5000eur-aggressive")
        assert balanced.id is not None
        assert aggressive.id is not None

        engine = await service.engine_for(balanced)
        position = (await repository.open_positions(balanced.id))[0]
        await engine.close_position(
            position,
            exit_price=Decimal("90"),
            timestamp=EXEC_AT + timedelta(days=1),
            reason=ExitReason.MANUAL,
        )
        await session.flush()

        assert len(await repository.open_positions(balanced.id)) == 0
        assert len(await repository.open_positions(aggressive.id)) == 1
        other = await repository.get_portfolio(aggressive.id)
        assert other.realized_pnl == 0


class TestRestartRecovery:
    async def test_a_position_survives_a_simulated_restart(self, engine):
        """Part U: open a trade, drop all in-memory state, continue and exit."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(bind=engine, expire_on_commit=False)

        # --- process 1: open a position, then "crash" ---------------------
        async with factory() as session:
            instrument = await make_instrument(session)
            instrument_id = instrument.id
            trading_engine = await engine_for(session)
            await open_one(session, trading_engine, instrument)
            await session.commit()

        # --- process 2: nothing carried over but the database -------------
        async with factory() as session:
            profiles = SimulationProfileRepository(session)
            repository = PaperTradingRepository(session)
            profile = await profiles.get_profile("5000eur-balanced")
            assert profile.id is not None

            positions = await repository.open_positions(profile.id)
            assert len(positions) == 1, "the open position must survive the restart"

            portfolio = await repository.ensure_portfolio(profile)
            assert portfolio.cash < profile.initial_capital, "cash state survived"

            resumed = PaperTradingEngine(repository, profile, portfolio)
            outcome = await resumed.process_bar(
                instrument_id=instrument_id, bar=bar(1, "100", "115", "99", "114")
            )
            await session.commit()

            assert outcome.positions_closed == 1
            trades = await repository.trades(profile.id)
            assert len(trades) == 1
            assert trades[0].exit_reason is ExitReason.TAKE_PROFIT


class TestTransactionAtomicity:
    async def test_a_rolled_back_entry_leaves_no_trace(self, engine):
        """Order, position and cash move together or not at all."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(bind=engine, expire_on_commit=False)

        async with factory() as session:
            await make_instrument(session)
            await session.commit()

        async with factory() as session:
            await engine_for(session)
            await session.commit()

        async with factory() as session:
            profiles = SimulationProfileRepository(session)
            repository = PaperTradingRepository(session)
            profile = await profiles.get_profile("5000eur-balanced")
            portfolio = await repository.ensure_portfolio(profile)
            trading_engine = PaperTradingEngine(repository, profile, portfolio)
            instrument = await InstrumentRepository(session).get_by_symbol("TEST")
            assert instrument is not None

            await open_one(session, trading_engine, instrument)
            await session.flush()
            await session.rollback()

        async with factory() as session:
            repository = PaperTradingRepository(session)
            profile = await SimulationProfileRepository(session).get_profile("5000eur-balanced")
            assert profile.id is not None
            assert len(await repository.open_positions(profile.id)) == 0
            assert await repository.count_orders(profile.id) == 0
            portfolio = await repository.get_portfolio(profile.id)
            assert portfolio.cash == profile.initial_capital, "cash was not deducted"


class TestAuditTrail:
    async def test_every_order_is_recorded_including_rejections(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)

        await open_one(session, engine, instrument, decision_id=1)
        await open_one(session, engine, instrument, decision_id=2)  # rejected: already open
        await session.flush()

        orders = await PaperTradingRepository(session).orders(engine.profile_id)
        assert len(orders) == 2
        statuses = {o.status.value for o in orders}
        assert statuses == {"FILLED", "REJECTED"}
        rejected = next(o for o in orders if o.rejection_reason is not None)
        assert rejected.rejection_detail

    async def test_a_closed_trade_traces_back_to_its_orders(self, session):
        instrument = await make_instrument(session)
        engine = await engine_for(session)
        await open_one(session, engine, instrument)
        await session.flush()
        await engine.process_bar(instrument_id=instrument.id, bar=bar(1, "100", "115", "99", "114"))
        await session.flush()

        repository = PaperTradingRepository(session)
        position = (await repository.positions(engine.profile_id, status=PositionStatus.CLOSED))[0]
        orders = await repository.orders_for_position(position.id)
        assert len(orders) == 2, "one entry and one exit"
        assert {o.side.value for o in orders} == {"LONG", "SHORT"}

    async def test_portfolio_state_after_multiple_trades(self, session):
        instrument_a = await make_instrument(session, "AAA")
        instrument_b = await make_instrument(session, "BBB")
        engine = await engine_for(session)

        for index, instrument in enumerate((instrument_a, instrument_b), start=1):
            await engine.open_from_decision(
                instrument=instrument,
                trade_decision_id=index,
                signal_id=None,
                signal_bar_timestamp=T0,
                execution_timestamp=EXEC_AT,
                execution_price=Decimal("100"),
                quote=quote(),
                atr=Decimal("2"),
            )
        await session.flush()

        await engine.process_bar(
            instrument_id=instrument_a.id, bar=bar(1, "100", "115", "99", "114")
        )
        await engine.process_bar(
            instrument_id=instrument_b.id, bar=bar(1, "100", "101", "90", "92")
        )
        await session.flush()

        portfolio = engine.portfolio
        assert portfolio.trade_count == 2
        assert portfolio.winning_trades == 1
        assert portfolio.losing_trades == 1
        assert portfolio.total_fees > 0

        trades = await PaperTradingRepository(session).trades(engine.profile_id)
        assert sum(t.net_pnl for t in trades) == portfolio.realized_pnl
