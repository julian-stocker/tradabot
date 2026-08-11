"""Shared test fixtures.

Every fixture is deterministic and offline. No network, no wall-clock dependence,
no sleeps. A test that fails intermittently teaches you nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import CostSettings, Environment, Settings, SignalSettings
from app.db.base import Base
from app.domain.enums import Timeframe
from app.main import create_app
from app.market_data.provider import CandleData
from app.market_data.providers.mock import MockMarketDataProvider

TEST_SEED = 1337
FIXED_NOW = datetime(2024, 6, 3, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Test settings: in-memory SQLite and the deterministic mock provider."""
    return Settings(
        env=Environment.TEST,
        database_url="sqlite+aiosqlite:///:memory:",
        market_data_provider="mock",
        mock_seed=TEST_SEED,
        log_level="WARNING",
        costs=CostSettings(),
        signals=SignalSettings(),
    )


@pytest.fixture
def cost_settings() -> CostSettings:
    return CostSettings()


@pytest.fixture
def signal_settings() -> SignalSettings:
    return SignalSettings()


@pytest.fixture
def fixed_clock() -> object:
    """A frozen clock, so ``generated_at`` is reproducible."""
    return lambda: FIXED_NOW


@pytest.fixture
def provider() -> MockMarketDataProvider:
    return MockMarketDataProvider(seed=TEST_SEED)


@pytest_asyncio.fixture
async def daily_candles(provider: MockMarketDataProvider) -> list[CandleData]:
    """Roughly two years of deterministic daily bars for NVDA."""
    return await provider.get_historical_candles(
        "NVDA",
        Timeframe.D1,
        datetime(2022, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
    )


@pytest_asyncio.fixture
async def engine(settings: Settings) -> AsyncIterator[object]:
    """In-memory SQLite engine with the schema created.

    ``StaticPool`` keeps every connection pointed at the same in-memory database;
    without it each connection would get its own empty one.
    """
    from sqlalchemy.pool import StaticPool

    test_engine = create_async_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield test_engine
    await test_engine.dispose()


@pytest_asyncio.fixture
async def session(engine: object) -> AsyncIterator[AsyncSession]:
    """A session that rolls back after each test."""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    async with factory() as test_session:
        yield test_session
        await test_session.rollback()


@pytest_asyncio.fixture
async def seeded_session(session: AsyncSession, provider: MockMarketDataProvider) -> AsyncSession:
    """A session pre-loaded with instruments and ~2 years of NVDA/AAPL daily bars."""
    from app.market_data.ingest import IngestionService

    service = IngestionService(session, provider)
    await service.sync_instruments()
    for symbol in ("NVDA", "AAPL"):
        await service.sync_corporate_actions(symbol)
        await service.sync_candles(
            symbol=symbol,
            timeframe=Timeframe.D1,
            start=datetime(2022, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 1, tzinfo=UTC),
        )
    await session.commit()
    return session


@pytest_asyncio.fixture
async def client(
    settings: Settings, engine: object, provider: MockMarketDataProvider
) -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the ASGI app, with test resources injected.

    The app's lifespan is bypassed: ``app.state`` is populated directly so the
    test database and mock provider are used instead of the configured ones.
    """
    app = create_app(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    app.state.provider = provider

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest_asyncio.fixture
async def seeded_client(
    client: AsyncClient, engine: object, provider: MockMarketDataProvider
) -> AsyncClient:
    """A client whose database already contains instruments and candles."""
    from app.market_data.ingest import IngestionService

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    async with factory() as setup_session:
        service = IngestionService(setup_session, provider)
        await service.sync_instruments()
        for symbol in ("NVDA", "AAPL", "OLDCO", "LATE"):
            await service.sync_corporate_actions(symbol)
            await service.sync_candles(
                symbol=symbol,
                timeframe=Timeframe.D1,
                start=datetime(2022, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 1, tzinfo=UTC),
            )
        await setup_session.commit()
    return client


def make_candle(
    timestamp: datetime,
    close: str,
    *,
    open_: str | None = None,
    high: str | None = None,
    low: str | None = None,
    volume: str = "1000",
) -> CandleData:
    """Build a candle from strings, keeping every price an exact Decimal.

    Defaults derive a valid OHLC around ``close`` so tests that only care about
    the close do not have to restate the invariants each time.
    """
    close_value = Decimal(close)
    open_value = Decimal(open_) if open_ is not None else close_value
    high_value = Decimal(high) if high is not None else max(open_value, close_value)
    low_value = Decimal(low) if low is not None else min(open_value, close_value)
    return CandleData(
        timestamp=timestamp,
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=Decimal(volume),
    )
