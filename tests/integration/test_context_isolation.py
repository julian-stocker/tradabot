"""Context instruments must stay out of every path that trades.

The unit tests assert the catalogue is well formed. These assert the *database*
behaves: that registering twelve ETFs alongside the watchlist leaves the scanner
universe, the paper-trading candidate set and the opportunity count untouched,
and that the one way an ETF could leak -- somebody watchlisting it -- is
detected rather than silently traded.

Written against a real session because the risk is not in the constants; it is
in a query somewhere selecting from ``instruments`` instead of from the enabled
watchlist.
"""

from __future__ import annotations

import pytest

from app.instruments.repository import InstrumentRepository
from app.market_data.benchmarks import (
    BENCHMARK_SYMBOLS,
    is_benchmark,
    register_benchmarks,
    watchlisted_benchmarks,
)
from app.market_data.provider import InstrumentInfo
from app.scanner.repository import WatchlistRepository

STOCKS = ("AAPL", "NVDA", "JPM")


async def seed_trade_universe(session) -> list[int]:
    """Three ordinary stocks, watchlisted and enabled."""
    instruments = InstrumentRepository(session)
    await instruments.upsert_many(
        [
            InstrumentInfo(symbol=s, name=f"{s} Inc.", exchange="XNAS", currency="USD")
            for s in STOCKS
        ]
    )
    await session.flush()

    watchlist = WatchlistRepository(session)
    ids = []
    for symbol in STOCKS:
        instrument = await instruments.get_by_symbol(symbol)
        assert instrument is not None
        await watchlist.add(instrument.id, tags=["technology"])
        ids.append(instrument.id)
    await session.flush()
    return ids


@pytest.mark.asyncio
async def test_registering_context_does_not_change_the_trade_universe(session) -> None:
    """**The gate.** Twelve ETFs in `instruments`, zero change to the scan set."""
    await seed_trade_universe(session)
    watchlist = WatchlistRepository(session)
    before = set(await watchlist.symbols())

    await register_benchmarks(session, provider="test")

    after = set(await watchlist.symbols())
    assert after == before == set(STOCKS)


@pytest.mark.asyncio
async def test_context_instruments_exist_but_are_not_watchlisted(session) -> None:
    await seed_trade_universe(session)
    await register_benchmarks(session, provider="test")

    instruments = InstrumentRepository(session)
    for symbol in BENCHMARK_SYMBOLS:
        assert await instruments.get_by_symbol(symbol) is not None

    assert list(await watchlisted_benchmarks(session)) == []


@pytest.mark.asyncio
async def test_the_enabled_watchlist_contains_no_etf(session) -> None:
    """What the scanner and the paper engine both iterate."""
    await seed_trade_universe(session)
    await register_benchmarks(session, provider="test")

    entries = await WatchlistRepository(session).list_entries(enabled_only=True)
    symbols = [instrument.symbol for _, instrument in entries]

    assert symbols
    assert not any(is_benchmark(symbol) for symbol in symbols)


@pytest.mark.asyncio
async def test_the_opportunity_count_is_unaffected(session) -> None:
    """`count` drives the "N opportunities" line; ETFs must not inflate it."""
    await seed_trade_universe(session)
    watchlist = WatchlistRepository(session)
    before = await watchlist.count(enabled_only=True)

    await register_benchmarks(session, provider="test")

    assert await watchlist.count(enabled_only=True) == before == len(STOCKS)


@pytest.mark.asyncio
async def test_registering_twice_is_idempotent(session) -> None:
    await seed_trade_universe(session)
    first = await register_benchmarks(session, provider="test")
    second = await register_benchmarks(session, provider="test")

    assert len(first.registered) == len(BENCHMARK_SYMBOLS)
    assert second.registered == ()
    assert len(second.already_present) == len(BENCHMARK_SYMBOLS)
    assert await WatchlistRepository(session).count(enabled_only=True) == len(STOCKS)


@pytest.mark.asyncio
async def test_a_leaked_benchmark_is_detected(session) -> None:
    """The failure mode this guard exists for, forced deliberately.

    ``WatchlistRepository.add`` enables unconditionally, so this is exactly what
    a careless `watchlist add SPY` would do. It must be visible, not silent.
    """
    await seed_trade_universe(session)
    await register_benchmarks(session, provider="test")

    spy = await InstrumentRepository(session).get_by_symbol("SPY")
    assert spy is not None
    await WatchlistRepository(session).add(spy.id)
    await session.flush()

    assert list(await watchlisted_benchmarks(session)) == ["SPY"]


@pytest.mark.asyncio
async def test_a_disabled_benchmark_is_not_reported_as_leaked(session) -> None:
    """Only the *enabled* watchlist is the trade universe."""
    await seed_trade_universe(session)
    await register_benchmarks(session, provider="test")

    spy = await InstrumentRepository(session).get_by_symbol("SPY")
    assert spy is not None
    watchlist = WatchlistRepository(session)
    await watchlist.add(spy.id)
    await watchlist.set_enabled("SPY", enabled=False)
    await session.flush()

    assert list(await watchlisted_benchmarks(session)) == []
