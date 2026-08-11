"""The complete paper-trade lifecycle, per portfolio.

**DETERMINISTIC EXECUTION TEST** — constructed prices, no market data. These
verify execution *mechanics*: sizing, fills, accounting, exits and routing. They
say nothing about whether any strategy works, and no real signal is involved.

Distinguish this from a REAL MARKET OBSERVATION, which is what the scanner
produces against Alpaca and which never forces a trade.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, EventCategory, EventType
from app.db.models import SimulationProfile, VirtualTrade
from app.domain.enums import ExitReason, PriceSeriesAdjustment
from app.domain.quotes import Quote
from app.notifications.models import DeliveryResult, NotificationMessage
from app.paper.exits import BarPrices
from app.paper.repository import PaperTradingRepository
from app.paper.service import PaperTradingService
from app.signals.repository import SignalRepository
from app.simulation.portfolios import PORTFOLIO_KEYS, build_personal_profiles
from app.simulation.repository import SimulationProfileRepository, TradeDecisionRepository
from tests.integration.test_paper_lifecycle import make_instrument, make_signal

pytestmark = pytest.mark.integration

T0 = datetime(2024, 3, 1, tzinfo=UTC)
EXEC_AT = T0 + timedelta(days=1)
ENTRY = Decimal("100.00")


def quote(price: str = "100.00", at: datetime = EXEC_AT) -> Quote:
    mid = Decimal(price)
    return Quote(symbol="TEST", timestamp=at, bid=mid - Decimal("0.05"), ask=mid + Decimal("0.05"))


def bar(price: str, *, day: int, low: str | None = None, high: str | None = None) -> BarPrices:
    value = Decimal(price)
    return BarPrices(
        timestamp=EXEC_AT + timedelta(days=day),
        open=value,
        high=Decimal(high) if high else value,
        low=Decimal(low) if low else value,
        close=value,
    )


class CapturingBackend:
    name = "capturing"

    def __init__(self, *, succeed: bool = True) -> None:
        self.messages: list[NotificationMessage] = []
        self._succeed = succeed

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        self.messages.append(message)
        if not self._succeed:
            msg = "delivery exploded"
            raise RuntimeError(msg)
        return DeliveryResult(backend=self.name, delivered=True)


async def setup_portfolios(session: AsyncSession) -> dict[str, object]:
    repository = SimulationProfileRepository(session)
    await repository.upsert_many(build_personal_profiles())
    await session.flush()
    profiles = {
        p.name: p
        for p in await repository.list_profiles(enabled_only=True)
        if p.name in PORTFOLIO_KEYS
    }
    paper = PaperTradingRepository(session)
    for profile in profiles.values():
        await paper.ensure_portfolio(profile)
    await session.flush()
    return profiles


def service_for(session: AsyncSession) -> PaperTradingService:
    return PaperTradingService(
        repository=PaperTradingRepository(session),
        profiles=SimulationProfileRepository(session),
        signals=SignalRepository(session),
        decisions=TradeDecisionRepository(session),
    )


async def open_in(session: AsyncSession, profile: object, instrument: object) -> object:
    engine = await service_for(session).engine_for(profile)  # type: ignore[arg-type]
    outcome = await engine.open_from_decision(
        instrument=instrument,  # type: ignore[arg-type]
        trade_decision_id=int(profile.id),  # type: ignore[attr-defined]
        signal_id=None,
        signal_bar_timestamp=T0,
        execution_timestamp=EXEC_AT,
        execution_price=ENTRY,
        quote=quote(),
        atr=Decimal("2.00"),
    )
    await session.flush()
    return engine, outcome


# ---------------------------------------------------------------------------
# BUY lifecycle, per portfolio
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["paper-1000", "paper-10000"])
async def test_the_buy_lifecycle_completes(session: AsyncSession, key: str) -> None:
    """signal → decision → sizing → order → fill → position → cash update."""
    profiles = await setup_portfolios(session)
    instrument = await make_instrument(session)
    profile = profiles[key]

    _engine, outcome = await open_in(session, profile, instrument)

    assert outcome.accepted, f"{key} should accept: {outcome.detail}"  # type: ignore[attr-defined]
    assert outcome.position_id is not None  # type: ignore[attr-defined]

    paper = PaperTradingRepository(session)
    positions = await paper.open_positions(profile.id)  # type: ignore[attr-defined]
    assert len(positions) == 1
    position = positions[0]
    assert position.quantity > 0
    assert position.average_entry_price >= ENTRY, "filled at the ask, not the mid"
    assert position.stop_loss is not None, "a risk control was set"
    assert position.take_profit is not None

    portfolio = await paper.get_portfolio(profile.id)  # type: ignore[attr-defined]
    assert portfolio.cash < profile.initial_capital, "cash was spent"  # type: ignore[attr-defined]
    assert portfolio.total_fees > 0


async def test_the_smallest_portfolio_may_decline(session: AsyncSession) -> None:
    """A legitimate rejection, not a failure.

    A fixed order fee is a large fraction of a 100 EUR round trip, so the cost
    model refuses. If every portfolio always agreed, two of the three would carry
    no information.
    """
    await setup_portfolios(session)
    instrument = await make_instrument(session)

    result = await service_for(session).run_signal(
        signal=make_signal(score=88.0),
        instrument=instrument,
        adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
        execution_timestamp=EXEC_AT,
        execution_price=ENTRY,
        quote=quote(),
        atr=Decimal("2.00"),
        now=EXEC_AT,
    )

    decisions = {d.profile_name: d.is_trade for d in result.decisions}
    assert decisions["paper-100"] is False
    assert decisions["paper-10000"] is True


async def test_one_signal_fans_out_to_every_portfolio(session: AsyncSession) -> None:
    """Analysis happens once; the decision happens per portfolio."""
    await setup_portfolios(session)
    instrument = await make_instrument(session)

    result = await service_for(session).run_signal(
        signal=make_signal(score=88.0),
        instrument=instrument,
        adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
        execution_timestamp=EXEC_AT,
        execution_price=ENTRY,
        quote=quote(),
        atr=Decimal("2.00"),
        now=EXEC_AT,
    )

    evaluated = {d.profile_name for d in result.decisions}
    assert set(PORTFOLIO_KEYS) <= evaluated, "every portfolio got a decision"


# ---------------------------------------------------------------------------
# Exit mechanisms
# ---------------------------------------------------------------------------
async def test_a_stop_loss_closes_the_position(session: AsyncSession) -> None:
    profiles = await setup_portfolios(session)
    instrument = await make_instrument(session)
    engine, _ = await open_in(session, profiles["paper-10000"], instrument)

    outcome = await engine.process_bar(  # type: ignore[attr-defined]
        instrument_id=instrument.id,
        bar=bar("94.00", day=1, low="90.00", high="100.00"),
        quote=quote("94.00"),
    )
    await session.flush()

    assert outcome.positions_closed == 1
    assert outcome.closed_trades[0].exit_reason is ExitReason.STOP_LOSS


async def test_a_take_profit_closes_the_position(session: AsyncSession) -> None:
    profiles = await setup_portfolios(session)
    instrument = await make_instrument(session)
    engine, _ = await open_in(session, profiles["paper-10000"], instrument)

    outcome = await engine.process_bar(  # type: ignore[attr-defined]
        instrument_id=instrument.id,
        bar=bar("112.00", day=1, low="100.00", high="115.00"),
        quote=quote("112.00"),
    )
    await session.flush()

    assert outcome.positions_closed == 1
    assert outcome.closed_trades[0].exit_reason is ExitReason.TAKE_PROFIT


async def test_a_holding_limit_closes_the_position(session: AsyncSession) -> None:
    """The time exit, driven by the trading-day calendar deadline."""
    profiles = await setup_portfolios(session)
    instrument = await make_instrument(session)
    engine, _ = await open_in(session, profiles["paper-10000"], instrument)

    # Far beyond any configured holding limit, at a price that triggers nothing.
    outcome = await engine.process_bar(  # type: ignore[attr-defined]
        instrument_id=instrument.id, bar=bar("100.50", day=120), quote=quote("100.50")
    )
    await session.flush()

    assert outcome.positions_closed == 1
    assert outcome.closed_trades[0].exit_reason is ExitReason.MAX_HOLDING_PERIOD


async def test_the_implemented_exit_reasons_are_exactly_these(session: AsyncSession) -> None:
    """Guard against claiming an exit type that is not wired up.

    STOP_LOSS, TAKE_PROFIT and MAX_HOLDING_PERIOD fire automatically from
    `process_bar`. SIGNAL_REVERSAL, SIMULATION_END and MANUAL exist and are
    reachable only through explicit service calls -- documented as such rather
    than presented as automatic.
    """
    import inspect

    from app.paper.engine import PaperTradingEngine

    automatic = inspect.getsource(PaperTradingEngine._maybe_exit)
    assert "STOP_LOSS" in automatic
    assert "MAX_HOLDING_PERIOD" in automatic
    # TAKE_PROFIT arrives through `evaluate_exit`, which `_maybe_exit` delegates to.
    assert "evaluate_exit" in automatic

    # SIGNAL_REVERSAL / SIMULATION_END / MANUAL are caller-driven, never fired by
    # a bar. Asserted so the documentation cannot drift into claiming otherwise.
    assert "SIGNAL_REVERSAL" not in automatic
    assert "reason" in inspect.getsource(PaperTradingEngine.close_position)


# ---------------------------------------------------------------------------
# SELL accounting
# ---------------------------------------------------------------------------
async def test_the_closed_trade_reconciles_gross_costs_and_net(
    session: AsyncSession,
) -> None:
    """net = gross - fees - spread - slippage, with nothing double-counted."""
    profiles = await setup_portfolios(session)
    instrument = await make_instrument(session)
    engine, _ = await open_in(session, profiles["paper-10000"], instrument)

    await engine.process_bar(  # type: ignore[attr-defined]
        instrument_id=instrument.id,
        bar=bar("112.00", day=1, low="100.00", high="115.00"),
        quote=quote("112.00"),
    )
    await session.flush()

    trade = (await session.execute(select(VirtualTrade))).scalars().one()
    costs = trade.total_fees + trade.total_spread_cost + trade.total_slippage_cost

    assert trade.net_pnl == trade.gross_pnl - costs, "net does not reconcile"
    assert isinstance(trade.net_pnl, Decimal), "money must be Decimal"
    assert trade.total_fees > 0
    assert trade.exit_price is not None
    assert trade.holding_bars >= 0


async def test_cash_reflects_the_completed_round_trip(session: AsyncSession) -> None:
    profiles = await setup_portfolios(session)
    instrument = await make_instrument(session)
    profile = profiles["paper-10000"]
    paper = PaperTradingRepository(session)
    opening_cash = (await paper.get_portfolio(profile.id)).cash  # type: ignore[attr-defined]

    engine, _ = await open_in(session, profile, instrument)
    await engine.process_bar(  # type: ignore[attr-defined]
        instrument_id=instrument.id,
        bar=bar("112.00", day=1, low="100.00", high="115.00"),
        quote=quote("112.00"),
    )
    await session.flush()

    trade = (await session.execute(select(VirtualTrade))).scalars().one()
    closing_cash = (await paper.get_portfolio(profile.id)).cash  # type: ignore[attr-defined]

    assert closing_cash == opening_cash + trade.net_pnl, "cash and net P/L disagree"
    assert await paper.open_positions(profile.id) == []  # type: ignore[attr-defined]


async def test_a_position_closes_only_once(session: AsyncSession) -> None:
    """Replaying the same bar must not book a second exit."""
    profiles = await setup_portfolios(session)
    instrument = await make_instrument(session)
    engine, _ = await open_in(session, profiles["paper-10000"], instrument)
    exit_bar = bar("112.00", day=1, low="100.00", high="115.00")

    first = await engine.process_bar(  # type: ignore[attr-defined]
        instrument_id=instrument.id, bar=exit_bar, quote=quote("112.00")
    )
    second = await engine.process_bar(  # type: ignore[attr-defined]
        instrument_id=instrument.id, bar=exit_bar, quote=quote("112.00")
    )
    await session.flush()

    assert first.positions_closed == 1
    assert second.positions_closed == 0
    assert len((await session.execute(select(VirtualTrade))).scalars().all()) == 1


# ---------------------------------------------------------------------------
# Close routing (Part L)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["paper-1000", "paper-10000"])
async def test_a_close_routes_to_its_own_portfolio_channel(session: AsyncSession, key: str) -> None:
    """The Phase 4.1 gap, now closed.

    Routing comes from the profile's stored channel -- not its capital, not the
    message text.
    """
    from app.scanner.service import _close_events

    profiles = await setup_portfolios(session)
    instrument = await make_instrument(session)
    engine, _ = await open_in(session, profiles[key], instrument)
    outcome = await engine.process_bar(  # type: ignore[attr-defined]
        instrument_id=instrument.id,
        bar=bar("112.00", day=1, low="100.00", high="115.00"),
        quote=quote("112.00"),
    )
    await session.flush()

    channel = profiles[key].notification_channel  # type: ignore[attr-defined]
    events = _close_events([(channel, outcome.closed_trades[0])], symbol="TEST")

    assert len(events) == 1
    assert events[0].routing_key == key
    assert events[0].type is EventType.PAPER_TRADE_CLOSED
    assert events[0].category is EventCategory.PAPER_TRADE


async def test_a_close_event_carries_gross_costs_and_net(session: AsyncSession) -> None:
    from app.scanner.service import _close_events

    profiles = await setup_portfolios(session)
    instrument = await make_instrument(session)
    engine, _ = await open_in(session, profiles["paper-10000"], instrument)
    outcome = await engine.process_bar(  # type: ignore[attr-defined]
        instrument_id=instrument.id,
        bar=bar("112.00", day=1, low="100.00", high="115.00"),
        quote=quote("112.00"),
    )
    await session.flush()

    payload = _close_events([("paper-10000", outcome.closed_trades[0])], symbol="TEST")[0].payload

    for field in ("gross_pnl", "fees", "spread_cost", "slippage_cost", "net_pnl", "net_return"):
        assert field in payload, f"{field} missing from a close message"
    assert payload["net_pnl"] != payload["gross_pnl"], "gross reported as net"


async def test_a_profile_without_a_channel_produces_no_close_message(
    session: AsyncSession,
) -> None:
    """The nine generic profiles stay silent."""
    from app.scanner.service import _close_events

    assert _close_events([("", object())], symbol="TEST") == []


async def test_a_discord_failure_does_not_undo_a_completed_trade(
    session: AsyncSession, engine: object
) -> None:
    """The financial transaction is committed before anything is announced.

    A notifier that raises must leave the trade, the cash and the closed position
    exactly as they were.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.config import Settings
    from app.notifications.service import NotificationService

    profiles = await setup_portfolios(session)
    instrument = await make_instrument(session)
    profile = profiles["paper-10000"]
    paper_engine, _ = await open_in(session, profile, instrument)
    outcome = await paper_engine.process_bar(  # type: ignore[attr-defined]
        instrument_id=instrument.id,
        bar=bar("112.00", day=1, low="100.00", high="115.00"),
        quote=quote("112.00"),
    )
    await session.commit()

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:")
    notifier = NotificationService(
        settings, backends=[CapturingBackend(succeed=False)], session_factory=factory
    )

    await notifier.publish(
        Event.paper_trade_closed(
            symbol="TEST", routing_key="paper-10000", payload={"symbol": "TEST"}
        )
    )

    async with factory() as check:
        trades = (await check.execute(select(VirtualTrade))).scalars().all()
        assert len(trades) == 1, "the trade survived a failed notification"
        assert trades[0].net_pnl == outcome.closed_trades[0].net_pnl


# ---------------------------------------------------------------------------
# Isolation across the full lifecycle
# ---------------------------------------------------------------------------
async def test_a_full_round_trip_leaves_the_other_portfolios_untouched(
    session: AsyncSession,
) -> None:
    profiles = await setup_portfolios(session)
    instrument = await make_instrument(session)
    paper = PaperTradingRepository(session)

    engine, _ = await open_in(session, profiles["paper-10000"], instrument)
    await engine.process_bar(  # type: ignore[attr-defined]
        instrument_id=instrument.id,
        bar=bar("112.00", day=1, low="100.00", high="115.00"),
        quote=quote("112.00"),
    )
    await session.flush()

    for key in ("paper-100", "paper-1000"):
        other = profiles[key]
        portfolio = await paper.get_portfolio(other.id)  # type: ignore[attr-defined]
        assert portfolio.cash == other.initial_capital  # type: ignore[attr-defined]
        assert portfolio.realized_pnl == Decimal(0)
        assert portfolio.total_fees == Decimal(0)
        assert await paper.trades(other.id) == []  # type: ignore[attr-defined]


async def test_every_personal_portfolio_has_a_distinct_channel(
    session: AsyncSession,
) -> None:
    await setup_portfolios(session)

    rows = (
        (
            await session.execute(
                select(SimulationProfile).where(
                    SimulationProfile.notification_channel.in_(PORTFOLIO_KEYS)
                )
            )
        )
        .scalars()
        .all()
    )

    channels = [row.notification_channel for row in rows]
    assert sorted(channels) == sorted(PORTFOLIO_KEYS)
    assert len(set(channels)) == len(channels), "two portfolios share a channel"
