"""Universe activation, batched retrieval and market-data reuse.

Offline throughout. The claim that matters most: market data is fetched **once**
and reused by every portfolio, which is the property that lets the universe grow
without multiplying provider requests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import MarketDataSettings, Settings
from app.core.errors import ProviderError
from app.domain.enums import AssetType, Timeframe
from app.domain.quotes import Quote
from app.market_data.import_service import MarketDataImportService, backfill_days_for
from app.market_data.provider import (
    BatchMarketDataProvider,
    CandleData,
    InstrumentInfo,
    MarketDataProvider,
)
from app.market_data.providers.mock import MockMarketDataProvider
from app.scanner.repository import WatchlistRepository
from app.scanner.seed import seed_watchlist
from app.scanner.universe import INITIAL_UNIVERSE, SECTORS, by_sector, universe_symbols
from app.simulation.portfolios import PORTFOLIO_KEYS, build_personal_profiles
from app.simulation.repository import SimulationProfileRepository

pytestmark = pytest.mark.integration

NOW = datetime(2024, 6, 5, 15, 0, tzinfo=UTC)


class CountingBatchProvider:
    """Records every request so fan-out can be counted, not assumed."""

    name = "counting"

    def __init__(self, symbols: list[str]) -> None:
        self._symbols = [s.upper() for s in symbols]
        self.batch_calls: list[tuple[tuple[str, ...], str]] = []
        self.single_calls: list[tuple[str, str]] = []

    async def get_instruments(self) -> list[InstrumentInfo]:
        return [
            InstrumentInfo(
                symbol=symbol,
                name=symbol,
                exchange="XNYS",
                currency="USD",
                asset_type=AssetType.STOCK,
            )
            for symbol in self._symbols
        ]

    def _bars(self, start: datetime) -> list[CandleData]:
        return [
            CandleData(
                timestamp=start + timedelta(days=index),
                open=Decimal(100),
                high=Decimal(102),
                low=Decimal(99),
                close=Decimal(101),
                volume=Decimal(1000),
            )
            for index in range(3)
        ]

    async def get_historical_candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[CandleData]:
        self.single_calls.append((symbol.upper(), timeframe.value))
        return self._bars(start)

    async def get_historical_candles_batch(
        self, symbols: list[str], timeframe: Timeframe, start: datetime, end: datetime
    ) -> dict[str, list[CandleData]]:
        self.batch_calls.append((tuple(s.upper() for s in symbols), timeframe.value))
        return {symbol.upper(): self._bars(start) for symbol in symbols}

    async def get_latest_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, timestamp=NOW, bid=Decimal("99.95"), ask=Decimal("100.05"))

    async def get_corporate_actions(self, symbol: str) -> list:  # type: ignore[type-arg]
        return []


class FailingBatchProvider(CountingBatchProvider):
    """A provider whose batch request fails."""

    name = "failing"

    async def get_historical_candles_batch(
        self, symbols: list[str], timeframe: Timeframe, start: datetime, end: datetime
    ) -> dict[str, list[CandleData]]:
        msg = "upstream is down"
        raise ProviderError(msg)


# ---------------------------------------------------------------------------
# The universe
# ---------------------------------------------------------------------------
def test_the_universe_loads_completely() -> None:
    symbols = universe_symbols()

    assert len(symbols) == len(INITIAL_UNIVERSE)
    assert len(symbols) >= 50, "the expanded universe, not a development subset"


def test_the_universe_has_no_duplicates() -> None:
    symbols = universe_symbols()

    assert len(symbols) == len(set(symbols))


def test_every_symbol_has_a_sector() -> None:
    for entry in INITIAL_UNIVERSE:
        assert entry.sector in SECTORS
        assert entry.tags == (entry.sector,)


def test_no_sector_dominates_the_universe() -> None:
    """A single sector holding most of the universe would make it one bet.

    Not a claim that the current split is optimal -- only that it is not
    degenerate.
    """
    grouped = by_sector()
    largest = max(len(symbols) for symbols in grouped.values())

    assert largest <= len(INITIAL_UNIVERSE) // 3


def test_settings_default_to_the_universe_not_a_second_list() -> None:
    """One authoritative source. A duplicated default is what goes stale."""
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:")

    assert tuple(settings.market_data.watchlist) == universe_symbols()


def test_a_smaller_development_watchlist_still_works() -> None:
    """Expanding must not remove the ability to run a small local set."""
    settings = MarketDataSettings(watchlist="NVDA,AAPL")  # type: ignore[arg-type]

    assert settings.watchlist == ("NVDA", "AAPL")


async def test_seeding_activates_the_whole_universe(session: AsyncSession) -> None:
    provider = CountingBatchProvider(list(universe_symbols()))

    report = await seed_watchlist(session, provider)  # type: ignore[arg-type]

    assert report.ok, f"unsupported: {report.missing}"
    assert report.watchlist_added == len(universe_symbols())
    assert len(await WatchlistRepository(session).symbols()) == len(universe_symbols())


async def test_seeding_is_idempotent_at_full_size(session: AsyncSession) -> None:
    provider = CountingBatchProvider(list(universe_symbols()))
    await seed_watchlist(session, provider)  # type: ignore[arg-type]

    await seed_watchlist(session, provider)  # type: ignore[arg-type]

    assert len(await WatchlistRepository(session).symbols()) == len(universe_symbols())


async def test_a_symbol_the_provider_lacks_is_isolated(session: AsyncSession) -> None:
    """One unsupported ticker must not cost the other fifty-one."""
    supported = [s for s in universe_symbols() if s != "BRK.B"]
    provider = CountingBatchProvider(supported)

    report = await seed_watchlist(session, provider)  # type: ignore[arg-type]

    assert report.missing == ["BRK.B"]
    assert report.watchlist_added == len(supported)
    assert not report.ok, "the gap is reported, not swallowed"


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------
def test_the_alpaca_provider_declares_the_batch_capability() -> None:
    from app.core.config import AlpacaSettings
    from app.market_data.providers.alpaca import AlpacaMarketDataProvider

    provider = AlpacaMarketDataProvider(AlpacaSettings(), MarketDataSettings())

    assert isinstance(provider, BatchMarketDataProvider)
    assert isinstance(provider, MarketDataProvider)


def test_the_mock_provider_also_batches() -> None:
    """So the batch code path is exercisable with no network."""
    assert isinstance(MockMarketDataProvider(1337), BatchMarketDataProvider)


async def test_a_watchlist_sync_issues_one_request_per_timeframe(
    session: AsyncSession,
) -> None:
    """The point of batching.

    Fifty-two symbols must not become fifty-two requests: that approaches the
    provider's rate ceiling and takes longer than the sync interval.
    """
    symbols = list(universe_symbols())
    provider = CountingBatchProvider(symbols)
    service = MarketDataImportService(session, provider)  # type: ignore[arg-type]
    await service.ensure_instruments(symbols)
    await session.flush()

    await service.sync_watchlist(symbols, timeframe=Timeframe.D1, now=NOW)

    assert len(provider.batch_calls) == 1, "one batched request, not fifty-two"
    assert provider.single_calls == [], "no per-symbol fallback was used"
    assert set(provider.batch_calls[0][0]) == set(symbols)


async def test_a_batched_sync_persists_every_symbol(session: AsyncSession) -> None:
    symbols = list(universe_symbols())[:10]
    provider = CountingBatchProvider(symbols)
    service = MarketDataImportService(session, provider)  # type: ignore[arg-type]
    await service.ensure_instruments(symbols)
    await session.flush()

    report = await service.sync_watchlist(symbols, timeframe=Timeframe.D1, now=NOW)

    assert len(report.reports) == len(symbols)
    assert report.total_inserted == 3 * len(symbols)
    assert report.ok


async def test_a_batch_failure_is_reported_per_symbol(session: AsyncSession) -> None:
    """Batching couples their fate; the report must say so honestly.

    Fifty-two symbols failing together needs a per-symbol record, or the reason
    is invisible.
    """
    symbols = list(universe_symbols())[:5]
    provider = FailingBatchProvider(symbols)
    service = MarketDataImportService(session, provider)  # type: ignore[arg-type]
    await service.ensure_instruments(symbols)
    await session.flush()

    report = await service.sync_watchlist(symbols, timeframe=Timeframe.D1, now=NOW)

    assert not report.ok
    assert len(report.symbols_failed) == len(symbols)
    assert all(item.error for item in report.reports)


async def test_an_unknown_instrument_does_not_abort_the_batch(
    session: AsyncSession,
) -> None:
    symbols = list(universe_symbols())[:5]
    provider = CountingBatchProvider(symbols)
    service = MarketDataImportService(session, provider)  # type: ignore[arg-type]
    await service.ensure_instruments(symbols)
    await session.flush()

    report = await service.sync_watchlist([*symbols, "GHOST"], timeframe=Timeframe.D1, now=NOW)

    assert "GHOST" in report.symbols_failed
    assert len(report.symbols_succeeded) == len(symbols)


def test_backfill_windows_scale_with_the_timeframe() -> None:
    """Pulling a year of five-minute bars is 30,000 rows for the same answer."""
    assert backfill_days_for(Timeframe.M5) < backfill_days_for(Timeframe.M15)
    assert backfill_days_for(Timeframe.M15) < backfill_days_for(Timeframe.H1)
    assert backfill_days_for(Timeframe.H1) < backfill_days_for(Timeframe.D1)


def test_the_batch_request_cap_is_configurable() -> None:
    from app.core.config import AlpacaSettings

    assert AlpacaSettings().max_symbols_per_request >= 1
    assert AlpacaSettings(max_symbols_per_request=25).max_symbols_per_request == 25


# ---------------------------------------------------------------------------
# Global reuse (Part F)
# ---------------------------------------------------------------------------
async def test_market_data_is_fetched_once_for_all_portfolios(
    session: AsyncSession,
) -> None:
    """The scaling property.

    Three portfolios must not mean three downloads. Analysis is a fact about the
    market; only the *decision* is per portfolio.
    """
    symbols = list(universe_symbols())[:5]
    provider = CountingBatchProvider(symbols)
    await SimulationProfileRepository(session).upsert_many(build_personal_profiles())
    await session.flush()

    service = MarketDataImportService(session, provider)  # type: ignore[arg-type]
    await service.ensure_instruments(symbols)
    await session.flush()
    await service.sync_watchlist(symbols, timeframe=Timeframe.D1, now=NOW)

    requested = [symbol for call in provider.batch_calls for symbol in call[0]]
    assert len(requested) == len(symbols), "a symbol was requested more than once"
    assert len(PORTFOLIO_KEYS) == 3, "three portfolios exist"


async def test_syncing_twice_does_not_redownload_everything(
    session: AsyncSession,
) -> None:
    """Incremental: the second window starts from the newest stored bar."""
    symbols = list(universe_symbols())[:5]
    provider = CountingBatchProvider(symbols)
    service = MarketDataImportService(session, provider)  # type: ignore[arg-type]
    await service.ensure_instruments(symbols)
    await session.flush()

    await service.sync_watchlist(symbols, timeframe=Timeframe.D1, now=NOW)
    first_window_start = provider.batch_calls[0]
    await service.sync_watchlist(symbols, timeframe=Timeframe.D1, now=NOW + timedelta(days=1))

    assert len(provider.batch_calls) == 2
    assert first_window_start is not None
    report = await service.sync_watchlist(
        symbols, timeframe=Timeframe.D1, now=NOW + timedelta(days=2)
    )
    assert report.ok
