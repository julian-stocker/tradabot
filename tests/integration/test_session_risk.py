"""Session-aware risk inside the paper engine.

Phase 3 counted holding periods in bars and stored ``max_daily_loss`` without
enforcing it, because "a day" had no definition. Phase 3b gave it one. These
tests exercise the wiring rather than the pure functions, which
``tests/unit/test_calendars_and_quality.py`` already covers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.models import Instrument
from app.domain.enums import ExitReason
from app.domain.quotes import Quote
from app.market_data.calendars import get_trading_calendar
from app.paper.engine import PaperTradingEngine
from app.paper.exits import BarPrices
from app.paper.repository import PaperTradingRepository
from app.simulation.defaults import build_default_profiles
from app.simulation.repository import SimulationProfileRepository
from tests.integration.test_paper_lifecycle import make_instrument

pytestmark = pytest.mark.integration

# Friday 28 June 2024. The following two weeks contain a weekend and the
# Independence Day holiday, which is exactly what a bar counter cannot see.
SIGNAL_BAR = datetime(2024, 6, 27, 20, 0, tzinfo=UTC)
ENTRY = datetime(2024, 6, 28, 20, 0, tzinfo=UTC)


async def engine_with_calendar(session, name: str = "5000eur-balanced") -> PaperTradingEngine:
    profiles = SimulationProfileRepository(session)
    await profiles.upsert_many(build_default_profiles())
    await session.flush()
    repository = PaperTradingRepository(session)
    profile = await profiles.get_profile(name)
    portfolio = await repository.ensure_portfolio(profile)
    return PaperTradingEngine(repository, profile, portfolio, calendar=get_trading_calendar("XNYS"))


def bar_at(moment: datetime, price: str = "100.00") -> BarPrices:
    """A flat bar: no stop or target can fire, so only time exits can."""
    value = Decimal(price)
    return BarPrices(
        timestamp=moment,
        open=value,
        high=value + Decimal("0.10"),
        low=value - Decimal("0.10"),
        close=value,
    )


def quote_at(moment: datetime, price: str = "100.00") -> Quote:
    mid = Decimal(price)
    return Quote(
        symbol="TEST", timestamp=moment, bid=mid - Decimal("0.05"), ask=mid + Decimal("0.05")
    )


async def enter(session, engine: PaperTradingEngine, instrument: Instrument) -> None:
    await engine.open_from_decision(
        instrument=instrument,
        trade_decision_id=1,
        signal_id=None,
        signal_bar_timestamp=SIGNAL_BAR,
        execution_timestamp=ENTRY,
        execution_price=Decimal("100.00"),
        quote=quote_at(ENTRY),
        atr=Decimal("2.00"),
    )
    await session.flush()


class TestHoldingPeriod:
    async def test_a_stalled_feed_does_not_extend_a_holding_period(self, session) -> None:
        """The reason the calendar deadline exists at all.

        The bar counter only advances on bars the engine sees. If a symbol stops
        printing -- a halt, a data outage, a provider gap -- a 10-bar limit can
        keep a position open for months while the counter reads 1. Counting
        sessions from the entry makes the limit a fact about the market.
        """
        instrument = await make_instrument(session)
        engine = await engine_with_calendar(session)
        await enter(session, engine, instrument)

        # One bar, months later. The counter has advanced by a single bar.
        late = ENTRY + timedelta(days=90)
        outcome = await engine.process_bar(
            instrument_id=instrument.id, bar=bar_at(late), quote=quote_at(late)
        )

        assert outcome.positions_closed == 1

        positions = await PaperTradingRepository(session).open_positions(
            engine.portfolio.simulation_profile_id
        )
        assert positions == []

    async def test_a_position_survives_a_weekend_and_a_holiday(self, session) -> None:
        """Friday entry, Wednesday bar: three sessions, not five calendar days.

        The balanced profile allows 15 trading days, so nothing should close here.
        A ``timedelta`` limit would count the weekend and be two days ahead.
        """
        instrument = await make_instrument(session)
        engine = await engine_with_calendar(session)
        await enter(session, engine, instrument)

        for day in (1, 2, 3, 4, 5):  # Sat 29 June .. Wed 3 July
            moment = ENTRY + timedelta(days=day)
            await engine.process_bar(
                instrument_id=instrument.id, bar=bar_at(moment), quote=quote_at(moment)
            )
        await session.flush()

        positions = await PaperTradingRepository(session).open_positions(
            engine.portfolio.simulation_profile_id
        )
        assert len(positions) == 1, "the weekend and 4 July must not consume the budget"

    async def test_the_bar_counter_still_closes_a_contiguous_replay(self, session) -> None:
        """The counter remains the rule when every bar arrives, and intraday."""
        instrument = await make_instrument(session)
        engine = await engine_with_calendar(session)
        await enter(session, engine, instrument)
        limit = engine._profile.risk.max_holding_bars
        assert limit is not None

        closed = 0
        for index in range(1, limit + 2):
            moment = ENTRY + timedelta(hours=index)
            outcome = await engine.process_bar(
                instrument_id=instrument.id, bar=bar_at(moment), quote=quote_at(moment)
            )
            closed += outcome.positions_closed

        assert closed == 1, "the position closed once, on the bar count"

    async def test_a_time_exit_is_recorded_as_one(self, session) -> None:
        """The exit reason has to survive, or the trade log misattributes it."""
        instrument = await make_instrument(session)
        engine = await engine_with_calendar(session)
        await enter(session, engine, instrument)

        late = ENTRY + timedelta(days=90)
        await engine.process_bar(
            instrument_id=instrument.id, bar=bar_at(late), quote=quote_at(late)
        )
        await session.flush()

        trades = await PaperTradingRepository(session).trades(
            engine.portfolio.simulation_profile_id
        )
        assert [t.exit_reason for t in trades] == [ExitReason.MAX_HOLDING_PERIOD]


class TestDailyLoss:
    async def test_a_session_baseline_is_recorded_on_the_first_bar(self, session) -> None:
        instrument = await make_instrument(session)
        engine = await engine_with_calendar(session)

        await engine.process_bar(
            instrument_id=instrument.id, bar=bar_at(ENTRY), quote=quote_at(ENTRY)
        )

        assert engine.portfolio.session_date == ENTRY.date()
        assert engine.portfolio.session_start_equity is not None

    async def test_a_session_spanning_utc_midnight_keeps_one_baseline(self, session) -> None:
        """A US session runs past 20:00 UTC; a midnight reset would split it."""
        instrument = await make_instrument(session)
        engine = await engine_with_calendar(session)

        await engine.process_bar(
            instrument_id=instrument.id, bar=bar_at(ENTRY), quote=quote_at(ENTRY)
        )
        baseline = engine.portfolio.session_start_equity

        late = ENTRY.replace(hour=23, minute=45)
        await engine.process_bar(
            instrument_id=instrument.id, bar=bar_at(late), quote=quote_at(late)
        )

        assert engine.portfolio.session_date == ENTRY.date()
        assert engine.portfolio.session_start_equity == baseline

    async def open_the_session_high(self, engine: PaperTradingEngine) -> None:
        """Backdate the session baseline so the portfolio is already down on the day.

        Set directly rather than traded into. Losing 4% of *portfolio* equity
        through marks alone requires a price move far past the position's own 4%
        stop, so any attempt to trade into it closes the position and trips the
        sticky drawdown halt instead -- testing the wrong branch. The baseline is
        an ordinary stored field, and setting it is exactly what a previous
        session's close would have done.
        """
        engine.portfolio.session_date = ENTRY.date()
        engine.portfolio.session_start_equity = engine.portfolio.cash * Decimal("1.10")

    async def test_a_breached_daily_loss_halts_the_portfolio(self, session) -> None:
        instrument = await make_instrument(session)
        engine = await engine_with_calendar(session)
        await self.open_the_session_high(engine)

        await engine.process_bar(
            instrument_id=instrument.id, bar=bar_at(ENTRY), quote=quote_at(ENTRY)
        )

        assert engine.portfolio.halted_reason is not None
        assert "daily" in engine.portfolio.halted_reason.lower()

    async def test_the_daily_halt_clears_at_the_next_session(self, session) -> None:
        """ "Daily" has to mean daily, or one bad day ends the simulation."""
        instrument = await make_instrument(session)
        engine = await engine_with_calendar(session)
        await self.open_the_session_high(engine)

        await engine.process_bar(
            instrument_id=instrument.id, bar=bar_at(ENTRY), quote=quote_at(ENTRY)
        )
        assert engine.portfolio.halted_reason is not None

        # Monday 1 July: the next session, at a recovered price.
        next_session = datetime(2024, 7, 1, 20, 0, tzinfo=UTC)
        await engine.process_bar(
            instrument_id=instrument.id,
            bar=bar_at(next_session),
            quote=quote_at(next_session),
        )

        assert engine.portfolio.halted_reason is None
        assert engine.portfolio.session_date == next_session.date()

    async def test_a_drawdown_halt_does_not_clear_at_a_new_session(self, session) -> None:
        """Only the daily budget is daily. Everything else waits for a human."""
        instrument = await make_instrument(session)
        engine = await engine_with_calendar(session)
        engine.portfolio.halted_reason = "max_drawdown breached: manual"

        next_session = datetime(2024, 7, 1, 20, 0, tzinfo=UTC)
        await engine.process_bar(
            instrument_id=instrument.id,
            bar=bar_at(next_session),
            quote=quote_at(next_session),
        )

        assert engine.portfolio.halted_reason == "max_drawdown breached: manual"
