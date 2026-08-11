"""End-to-end smoke test against a live provider.

Proves the whole path runs on real data: provider -> normalisation -> database ->
features -> signal. It asserts on *structure and consistency*, never on values --
a test that asserted NVDA's close would fail every day for the right reason and
teach nothing.

**This does not test the strategy.** It tests that real bytes from a real API
survive the trip into a signal without corruption. Whether the resulting signal
is any good is a phase 4 question.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.time import utc_now
from app.corporate_actions.repository import CorporateActionRepository
from app.db.base import Base
from app.domain.enums import Horizon, Timeframe
from app.features.service import FeatureService
from app.instruments.repository import InstrumentRepository
from app.instruments.service import InstrumentService
from app.market_data.import_service import MarketDataImportService
from app.market_data.quality import quote_age_seconds
from app.market_data.registry import build_provider
from app.market_data.repository import CandleRepository
from app.signals.service import SignalService

pytestmark = [pytest.mark.external, pytest.mark.asyncio]

SMOKE_SYMBOL = "AAPL"
# Long enough to warm up a 200-bar indicator window with room for holidays.
LOOKBACK_DAYS = 500
# Yesterday, not today: the current session's bar may be partial, and a provider
# is entitled to return nothing at all before the open.
END_OFFSET_DAYS = 1


@pytest_asyncio.fixture
async def smoke_session(live_settings: Settings) -> AsyncSession:
    """A throwaway SQLite database, so a smoke run never touches real data."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_real_data_reaches_a_signal(
    live_settings: Settings, smoke_session: AsyncSession
) -> None:
    """Import real bars, then score them."""
    provider = build_provider(live_settings)
    service = MarketDataImportService(smoke_session, provider)

    end = utc_now() - timedelta(days=END_OFFSET_DAYS)
    start = end - timedelta(days=LOOKBACK_DAYS)

    await service.ensure_instruments([SMOKE_SYMBOL])
    report = await service.import_symbol(
        symbol=SMOKE_SYMBOL, start=start, end=end, timeframe=Timeframe.D1
    )
    await smoke_session.flush()

    assert report.error is None, f"import failed: {report.error}"
    assert report.received_bars > 0, "provider returned no bars for a 500-day window"
    assert report.rejected_bars == 0, (
        f"provider returned {report.rejected_bars} bars that failed validation"
    )

    instruments = InstrumentRepository(smoke_session)
    instrument = await instruments.get_by_symbol(SMOKE_SYMBOL)
    assert instrument is not None
    assert instrument.provider == provider.name, "provenance was not recorded"

    candles = CandleRepository(smoke_session)
    stored = await candles.get_range(
        instrument_id=instrument.id, timeframe=Timeframe.D1, start=start, end=end
    )
    assert len(stored) == report.inserted_bars

    # Real bars must satisfy the OHLC invariants after normalisation, or something
    # in the mapping is wrong -- transposed fields being the classic failure.
    for candle in stored:
        assert candle.low <= candle.open <= candle.high
        assert candle.low <= candle.close <= candle.high
        assert candle.volume >= 0

    timestamps = [candle.timestamp for candle in stored]
    assert timestamps == sorted(timestamps), "stored bars are not chronological"
    assert len(set(timestamps)) == len(timestamps), "duplicate timestamps were stored"
    assert all(t.tzinfo is not None for t in timestamps), "a naive timestamp was stored"

    signals = SignalService(
        FeatureService(
            InstrumentService(instruments), candles, CorporateActionRepository(smoke_session)
        ),
        provider,
        live_settings,
    )
    signal = await signals.evaluate(
        symbol=SMOKE_SYMBOL, timeframe=Timeframe.D1, horizon=Horizon.D5, as_of=stored[-1].timestamp
    )

    # Structure, not direction. Any classification is a valid outcome here; the
    # claim is that the pipeline produced a coherent one.
    assert signal.symbol == SMOKE_SYMBOL
    assert signal.timestamp == stored[-1].timestamp
    assert -100.0 <= signal.score <= 100.0
    assert 0.0 <= signal.confidence <= 1.0
    assert signal.bars_used > 0
    assert signal.reference_price > 0


async def test_live_quote_is_fresh_and_coherent(live_settings: Settings) -> None:
    """A latest quote must be orderly and recent.

    Runs against whatever the market is doing: outside trading hours the last
    quote is legitimately hours old, so age is only asserted to be non-negative.
    A *negative* age would mean a timestamp from the future, which is a real bug.
    """
    provider = build_provider(live_settings)
    quote = await provider.get_latest_quote(SMOKE_SYMBOL)

    assert quote.symbol == SMOKE_SYMBOL
    assert quote.bid > 0
    assert quote.ask >= quote.bid, "crossed quote: ask below bid"
    assert quote.timestamp.tzinfo is not None
    assert quote_age_seconds(quote, now=utc_now()) >= 0, "quote is timestamped in the future"
