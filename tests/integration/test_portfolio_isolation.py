"""Portfolio isolation, ownership, and the provider-connection boundary.

The central claim: a trade or a loss in one portfolio must never alter another.
Asserted by actually trading in one and checking the others, rather than by
reading the code that is supposed to guarantee it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ExternalAccountConnection, SimulationProfile, TradabotUser
from app.db.models.ownership import (
    LOCAL_OWNER_IDENTITY,
    ConnectionPurpose,
    IdentityType,
)
from app.domain.enums import PriceSeriesAdjustment
from app.domain.quotes import Quote
from app.ownership.service import (
    ALPACA_CREDENTIAL_REFERENCE,
    OwnershipService,
    ensure_local_ownership,
)
from app.paper.exits import BarPrices
from app.paper.repository import PaperTradingRepository
from app.paper.service import PaperTradingService
from app.signals.repository import SignalRepository
from app.simulation.defaults import build_default_profiles
from app.simulation.portfolios import PORTFOLIO_KEYS, build_personal_profiles
from app.simulation.repository import SimulationProfileRepository, TradeDecisionRepository
from tests.integration.test_paper_lifecycle import make_instrument, make_signal

pytestmark = pytest.mark.integration

T0 = datetime(2024, 3, 1, tzinfo=UTC)
EXEC_AT = T0 + timedelta(days=1)


async def seed_portfolios(session: AsyncSession) -> SimulationProfileRepository:
    repository = SimulationProfileRepository(session)
    await repository.upsert_many(build_personal_profiles())
    await session.flush()
    return repository


def quote(price: str = "100.00") -> Quote:
    mid = Decimal(price)
    return Quote(
        symbol="TEST", timestamp=EXEC_AT, bid=mid - Decimal("0.05"), ask=mid + Decimal("0.05")
    )


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------
async def test_the_three_portfolios_are_persisted_independently(
    session: AsyncSession,
) -> None:
    repository = await seed_portfolios(session)

    profiles = await repository.list_profiles(enabled_only=True)
    personal = {p.name: p for p in profiles if p.name in PORTFOLIO_KEYS}

    assert set(personal) == set(PORTFOLIO_KEYS)
    capitals = [float(personal[key].initial_capital) for key in PORTFOLIO_KEYS]
    assert capitals == [100.0, 1000.0, 10000.0]


async def test_each_portfolio_has_its_own_cash_and_equity(session: AsyncSession) -> None:
    repository = await seed_portfolios(session)
    paper = PaperTradingRepository(session)

    cash: dict[str, Decimal] = {}
    for profile in await repository.list_profiles(enabled_only=True):
        if profile.name not in PORTFOLIO_KEYS:
            continue
        portfolio = await paper.ensure_portfolio(profile)
        cash[profile.name] = portfolio.cash

    assert cash["paper-100"] == Decimal(100)
    assert cash["paper-1000"] == Decimal(1000)
    assert cash["paper-10000"] == Decimal(10000)


async def test_a_trade_in_one_portfolio_does_not_touch_the_others(
    session: AsyncSession,
) -> None:
    """The core isolation guarantee, asserted by trading.

    One portfolio opens a position; the other two must be byte-for-byte
    unchanged -- same cash, no positions, no orders, no trades.
    """
    repository = await seed_portfolios(session)
    instrument = await make_instrument(session)
    paper = PaperTradingRepository(session)

    profiles = {
        p.name: p
        for p in await repository.list_profiles(enabled_only=True)
        if p.name in PORTFOLIO_KEYS
    }
    for profile in profiles.values():
        await paper.ensure_portfolio(profile)
    await session.flush()

    target = profiles["paper-10000"]
    engine = await PaperTradingService(
        repository=paper,
        profiles=repository,
        signals=SignalRepository(session),
        decisions=TradeDecisionRepository(session),
    ).engine_for(target)

    outcome = await engine.open_from_decision(
        instrument=instrument,
        trade_decision_id=1,
        signal_id=None,
        signal_bar_timestamp=T0,
        execution_timestamp=EXEC_AT,
        execution_price=Decimal("100.00"),
        quote=quote(),
        atr=Decimal("2.00"),
    )
    await session.flush()
    assert outcome.accepted, "the large portfolio should take this trade"

    for name in ("paper-100", "paper-1000"):
        untouched = profiles[name]
        assert untouched.id is not None
        portfolio = await paper.get_portfolio(untouched.id)
        assert portfolio.cash == untouched.initial_capital, f"{name} cash moved"
        assert await paper.open_positions(untouched.id) == []
        assert await paper.trades(untouched.id) == []


async def test_a_loss_in_one_portfolio_does_not_reach_the_others(
    session: AsyncSession,
) -> None:
    """A realised loss must be contained to the portfolio that took it."""
    repository = await seed_portfolios(session)
    instrument = await make_instrument(session)
    paper = PaperTradingRepository(session)

    profiles = {
        p.name: p
        for p in await repository.list_profiles(enabled_only=True)
        if p.name in PORTFOLIO_KEYS
    }
    for profile in profiles.values():
        await paper.ensure_portfolio(profile)
    await session.flush()

    service = PaperTradingService(
        repository=paper,
        profiles=repository,
        signals=SignalRepository(session),
        decisions=TradeDecisionRepository(session),
    )
    target = profiles["paper-10000"]
    engine = await service.engine_for(target)
    await engine.open_from_decision(
        instrument=instrument,
        trade_decision_id=1,
        signal_id=None,
        signal_bar_timestamp=T0,
        execution_timestamp=EXEC_AT,
        execution_price=Decimal("100.00"),
        quote=quote(),
        atr=Decimal("2.00"),
    )
    await session.flush()

    # Collapse the price so the position closes at a loss.
    crash = EXEC_AT + timedelta(days=1)
    await engine.process_bar(
        instrument_id=instrument.id,
        bar=BarPrices(
            timestamp=crash,
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("50"),
            close=Decimal("55"),
        ),
        quote=quote("55.00"),
    )
    await session.flush()

    assert target.id is not None
    hurt = await paper.get_portfolio(target.id)
    assert hurt.realized_pnl < 0, "the trading portfolio took a loss"

    for name in ("paper-100", "paper-1000"):
        spared = profiles[name]
        assert spared.id is not None
        portfolio = await paper.get_portfolio(spared.id)
        assert portfolio.realized_pnl == Decimal(0), f"{name} absorbed another portfolio's loss"
        assert portfolio.cash == spared.initial_capital


async def test_costs_are_tracked_per_portfolio(session: AsyncSession) -> None:
    repository = await seed_portfolios(session)
    instrument = await make_instrument(session)
    paper = PaperTradingRepository(session)

    profiles = {
        p.name: p
        for p in await repository.list_profiles(enabled_only=True)
        if p.name in PORTFOLIO_KEYS
    }
    for profile in profiles.values():
        await paper.ensure_portfolio(profile)
    await session.flush()

    target = profiles["paper-10000"]
    service = PaperTradingService(
        repository=paper,
        profiles=repository,
        signals=SignalRepository(session),
        decisions=TradeDecisionRepository(session),
    )
    engine = await service.engine_for(target)
    await engine.open_from_decision(
        instrument=instrument,
        trade_decision_id=1,
        signal_id=None,
        signal_bar_timestamp=T0,
        execution_timestamp=EXEC_AT,
        execution_price=Decimal("100.00"),
        quote=quote(),
        atr=Decimal("2.00"),
    )
    await session.flush()

    assert target.id is not None
    traded = await paper.get_portfolio(target.id)
    assert traded.total_fees > 0

    quiet = profiles["paper-100"]
    assert quiet.id is not None
    assert (await paper.get_portfolio(quiet.id)).total_fees == Decimal(0)


async def test_a_small_portfolio_declines_what_a_large_one_takes(
    session: AsyncSession,
) -> None:
    """The reason there are three sizes.

    A fixed per-order fee is a large fraction of a 100 EUR round trip and a
    negligible one of a 10,000 EUR trade, so the same signal produces different
    decisions. If all three ever agreed on everything, two of them would be
    redundant.
    """
    repository = await seed_portfolios(session)
    instrument = await make_instrument(session)
    service = PaperTradingService(
        repository=PaperTradingRepository(session),
        profiles=repository,
        signals=SignalRepository(session),
        decisions=TradeDecisionRepository(session),
    )

    result = await service.run_signal(
        signal=make_signal(score=88.0),
        instrument=instrument,
        adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
        execution_timestamp=EXEC_AT,
        execution_price=Decimal("100.00"),
        quote=quote(),
        atr=Decimal("2.00"),
        now=EXEC_AT,
    )

    decisions = {d.profile_name: d.is_trade for d in result.decisions}
    assert decisions.get("paper-10000") is True
    assert decisions.get("paper-100") is False, "a 100 EUR portfolio cannot absorb the fee"


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------
async def test_seeding_creates_exactly_one_local_owner(
    session: AsyncSession, settings: Settings
) -> None:
    await seed_portfolios(session)

    first = await ensure_local_ownership(session, settings)
    second = await ensure_local_ownership(session, settings)

    assert first.owner_id == second.owner_id
    assert first.owner_created
    assert not second.owner_created
    owners = (await session.execute(select(TradabotUser))).scalars().all()
    assert len(owners) == 1
    assert owners[0].external_identity_type == IdentityType.LOCAL.value
    assert owners[0].external_identity_id == LOCAL_OWNER_IDENTITY


async def test_every_portfolio_belongs_to_the_owner(
    session: AsyncSession, settings: Settings
) -> None:
    await seed_portfolios(session)

    report = await ensure_local_ownership(session, settings)

    owned = await OwnershipService(session).profiles_for(report.owner_id)
    assert {p.name for p in owned} >= set(PORTFOLIO_KEYS)


async def test_ownership_does_not_reassign_another_owners_portfolio(
    session: AsyncSession, settings: Settings
) -> None:
    """Idempotent seeding must not steal portfolios once a second user exists."""
    await seed_portfolios(session)
    await ensure_local_ownership(session, settings)

    other = TradabotUser(
        external_identity_type=IdentityType.DISCORD.value,
        external_identity_id="999",
        display_name="Someone else",
        enabled=True,
        created_at=T0,
    )
    session.add(other)
    await session.flush()

    profile = (
        await session.execute(
            select(SimulationProfile).where(SimulationProfile.name == "paper-100")
        )
    ).scalar_one()
    profile.owner_id = other.id
    await session.flush()

    await ensure_local_ownership(session, settings)

    await session.refresh(profile)
    assert profile.owner_id == other.id, "seeding reassigned another owner's portfolio"


async def test_legacy_profiles_gain_an_owner_without_changing_behaviour(
    session: AsyncSession, settings: Settings
) -> None:
    """The nine phase-3 profiles keep working and simply acquire an owner."""
    repository = SimulationProfileRepository(session)
    await repository.upsert_many(build_default_profiles())
    await session.flush()

    report = await ensure_local_ownership(session, settings)

    owned = await OwnershipService(session).profiles_for(report.owner_id)
    assert len(owned) == 9
    assert all(p.notification_channel is None for p in owned), "legacy profiles have no channel"


# ---------------------------------------------------------------------------
# Provider connections
# ---------------------------------------------------------------------------
async def test_the_market_data_connection_is_recorded(
    session: AsyncSession, settings: Settings
) -> None:
    report = await ensure_local_ownership(session, settings)

    connections = await OwnershipService(session).connections(report.owner_id)

    assert len(connections) == 1
    assert connections[0].purpose == ConnectionPurpose.MARKET_DATA.value
    assert connections[0].provider == "ALPACA"


async def test_a_connection_never_stores_a_raw_secret(
    session: AsyncSession, settings: Settings
) -> None:
    """Part K, asserted structurally and by value.

    A raw secret column would be one database backup away from a leak.
    """
    await ensure_local_ownership(session, settings)

    columns = {c.name for c in ExternalAccountConnection.__table__.columns}
    forbidden = {"api_secret", "api_key", "secret", "token", "password", "access_token"}
    assert not (columns & forbidden)

    connection = (await session.execute(select(ExternalAccountConnection))).scalars().one()
    assert connection.credential_reference == ALPACA_CREDENTIAL_REFERENCE
    assert connection.credential_reference is not None
    assert "env:" in connection.credential_reference, "a pointer, not a value"


async def test_no_trading_connection_is_created(session: AsyncSession, settings: Settings) -> None:
    """tradabot places no orders, so it records no trading connection."""
    report = await ensure_local_ownership(session, settings)

    purposes = {c.purpose for c in await OwnershipService(session).connections(report.owner_id)}

    assert ConnectionPurpose.LIVE_TRADING.value not in purposes
    assert ConnectionPurpose.PAPER_TRADING.value not in purposes


async def test_the_connection_table_does_not_supply_credentials(
    session: AsyncSession, settings: Settings
) -> None:
    """The registry builds the provider from settings and never reads this table.

    If it did, a database row could change which credentials the system uses --
    a far larger surface than a configuration file.
    """
    import inspect

    from app.market_data import registry

    source = inspect.getsource(registry)

    assert "ExternalAccountConnection" not in source
    assert "credential_reference" not in source
