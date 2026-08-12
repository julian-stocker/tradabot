"""Historical backfill: chunking, resume, provenance and coexistence.

Entirely offline. A counting fake provider stands in for Alpaca so the tests can
assert *how many requests were made* and *with what windows* -- the properties
that decide whether a multi-year expansion finishes at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Candle, ScanRun, SignalEvaluation, VirtualPosition
from app.domain.enums import AssetType, Timeframe
from app.domain.quotes import Quote
from app.market_data.backfill import HistoricalBackfill
from app.market_data.provider import CandleData, InstrumentInfo
from app.market_data.repository import CandleRepository

pytestmark = pytest.mark.integration

SYMBOLS = ("NVDA", "AAPL")
START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 4, 1, tzinfo=UTC)


class CountingProvider:
    """Returns one daily bar per calendar day and records every request."""

    name = "alpaca"  # the backfill refuses anything that is not alpaca

    def __init__(self) -> None:
        self.batch_calls: list[tuple[tuple[str, ...], str, datetime, datetime]] = []
        self.fail_next = 0

    async def get_instruments(self) -> list[InstrumentInfo]:
        return [
            InstrumentInfo(
                symbol=symbol,
                name=symbol,
                exchange="XNYS",
                currency="USD",
                asset_type=AssetType.STOCK,
            )
            for symbol in SYMBOLS
        ]

    def _bars(self, start: datetime, end: datetime) -> list[CandleData]:
        out: list[CandleData] = []
        cursor = start
        while cursor < end:
            out.append(
                CandleData(
                    timestamp=cursor,
                    open=Decimal(100),
                    high=Decimal(102),
                    low=Decimal(99),
                    close=Decimal(101),
                    volume=Decimal(1000),
                )
            )
            cursor += timedelta(days=1)
        return out

    async def get_historical_candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[CandleData]:
        return self._bars(start, end)

    async def get_historical_candles_batch(
        self, symbols: list[str], timeframe: Timeframe, start: datetime, end: datetime
    ) -> dict[str, list[CandleData]]:
        self.batch_calls.append((tuple(symbols), timeframe.value, start, end))
        if self.fail_next > 0:
            self.fail_next -= 1
            from app.core.errors import ProviderError

            msg = "transient upstream failure"
            raise ProviderError(msg)
        return {symbol: self._bars(start, end) for symbol in symbols}

    async def get_latest_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, timestamp=START, bid=Decimal(100), ask=Decimal(101))

    async def get_corporate_actions(self, symbol: str) -> list:  # type: ignore[type-arg]
        return []


@pytest.fixture
def factory(engine: object) -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]


@pytest.fixture
async def ready(session: AsyncSession) -> CountingProvider:
    from app.market_data.ingest import IngestionService

    provider = CountingProvider()
    await IngestionService(session, provider).sync_instruments()  # type: ignore[arg-type]
    await session.commit()
    return provider


async def count_candles(factory: async_sessionmaker, timeframe: Timeframe) -> int:  # type: ignore[type-arg]
    async with factory() as check:
        stmt = select(func.count()).select_from(Candle).where(Candle.timeframe == timeframe)
        return int((await check.execute(stmt)).scalar_one())


# ---------------------------------------------------------------------------
# 5. Incremental backfill
# ---------------------------------------------------------------------------
async def test_a_backfill_stores_the_requested_range(
    ready: CountingProvider, factory: async_sessionmaker
) -> None:
    backfill = HistoricalBackfill(factory, ready)  # type: ignore[arg-type]

    report = await backfill.run(symbols=SYMBOLS, timeframes=[Timeframe.D1], start=START, end=END)

    assert report.ok, report.failed
    assert report.inserted > 0
    assert await count_candles(factory, Timeframe.D1) == report.inserted


async def test_one_request_covers_the_whole_universe(
    ready: CountingProvider, factory: async_sessionmaker
) -> None:
    """**Why a multi-year expansion is feasible.**

    Per symbol it would be thousands of round trips; batched it is dozens.
    """
    backfill = HistoricalBackfill(factory, ready)  # type: ignore[arg-type]

    await backfill.run(symbols=SYMBOLS, timeframes=[Timeframe.D1], start=START, end=END)

    assert len(ready.batch_calls) == 1, "one window, one request"
    assert set(ready.batch_calls[0][0]) == set(SYMBOLS)


async def test_windows_are_requested_oldest_first(
    ready: CountingProvider, factory: async_sessionmaker
) -> None:
    """An interrupted run must leave a contiguous frontier, not islands."""
    backfill = HistoricalBackfill(factory, ready)  # type: ignore[arg-type]

    await backfill.run(
        symbols=SYMBOLS,
        timeframes=[Timeframe.M5],
        start=START,
        end=START + timedelta(days=60),
    )

    starts = [call[2] for call in ready.batch_calls]
    assert starts == sorted(starts)
    assert len(starts) > 1, "a 60-day 5-minute range must be chunked"


# ---------------------------------------------------------------------------
# 6-7. Resume and idempotency
# ---------------------------------------------------------------------------
async def test_rerunning_downloads_nothing(
    ready: CountingProvider, factory: async_sessionmaker
) -> None:
    """Resume: the second run is nearly free."""
    backfill = HistoricalBackfill(factory, ready)  # type: ignore[arg-type]
    await backfill.run(symbols=SYMBOLS, timeframes=[Timeframe.D1], start=START, end=END)
    first_calls = len(ready.batch_calls)

    second = await backfill.run(symbols=SYMBOLS, timeframes=[Timeframe.D1], start=START, end=END)

    assert len(ready.batch_calls) == first_calls, "a covered window was re-requested"
    assert second.skipped_chunks > 0
    assert second.inserted == 0


async def test_rerunning_creates_no_duplicates(
    ready: CountingProvider, factory: async_sessionmaker
) -> None:
    backfill = HistoricalBackfill(factory, ready)  # type: ignore[arg-type]
    await backfill.run(symbols=SYMBOLS, timeframes=[Timeframe.D1], start=START, end=END)
    after_first = await count_candles(factory, Timeframe.D1)

    await backfill.run(
        symbols=SYMBOLS, timeframes=[Timeframe.D1], start=START, end=END, resume=False
    )

    assert await count_candles(factory, Timeframe.D1) == after_first


async def test_a_partially_filled_range_is_completed_not_skipped(
    ready: CountingProvider, factory: async_sessionmaker
) -> None:
    """**The regression test for the resume bug.**

    Filling only the first month once made a frontier-based check report the
    entire range as covered, leaving a hole that nothing would ever notice.
    Coverage is measured session by session precisely so this cannot recur.
    """
    backfill = HistoricalBackfill(factory, ready)  # type: ignore[arg-type]
    await backfill.run(
        symbols=SYMBOLS,
        timeframes=[Timeframe.D1],
        start=START,
        end=START + timedelta(days=30),
    )
    partial = await count_candles(factory, Timeframe.D1)

    await backfill.run(symbols=SYMBOLS, timeframes=[Timeframe.D1], start=START, end=END)

    assert await count_candles(factory, Timeframe.D1) > partial, "the hole was not filled"


# ---------------------------------------------------------------------------
# 8. Retry
# ---------------------------------------------------------------------------
async def test_a_transient_failure_is_retried_and_succeeds(
    ready: CountingProvider, factory: async_sessionmaker
) -> None:
    ready.fail_next = 1
    backfill = HistoricalBackfill(factory, ready)  # type: ignore[arg-type]

    report = await backfill.run(symbols=SYMBOLS, timeframes=[Timeframe.D1], start=START, end=END)

    assert report.ok, "a retryable failure ended the run"
    assert len(ready.batch_calls) >= 2, "the chunk was not retried"
    assert report.inserted > 0


async def test_a_persistent_failure_is_recorded_not_raised(
    ready: CountingProvider, factory: async_sessionmaker
) -> None:
    """One bad window must not cost the other 99% of a multi-hour run."""
    ready.fail_next = 99
    backfill = HistoricalBackfill(factory, ready)  # type: ignore[arg-type]

    report = await backfill.run(symbols=SYMBOLS, timeframes=[Timeframe.D1], start=START, end=END)

    assert not report.ok
    assert report.failed
    assert all(chunk.error for chunk in report.failed)


# ---------------------------------------------------------------------------
# 9-10. Provenance
# ---------------------------------------------------------------------------
async def test_every_backfilled_candle_records_its_provider(
    ready: CountingProvider, factory: async_sessionmaker
) -> None:
    backfill = HistoricalBackfill(factory, ready)  # type: ignore[arg-type]
    await backfill.run(symbols=SYMBOLS, timeframes=[Timeframe.D1], start=START, end=END)

    async with factory() as check:
        providers = (await check.execute(select(Candle.provider).distinct())).scalars().all()
        stamped = (
            await check.execute(select(func.count()).where(Candle.ingested_at.is_(None)))
        ).scalar_one()

    assert set(providers) == {"alpaca"}
    assert stamped == 0, "a candle was stored without an ingestion timestamp"


async def test_no_mock_data_reaches_the_historical_archive(
    ready: CountingProvider, factory: async_sessionmaker
) -> None:
    """The assertion phase 5.5 requires after every expansion."""
    backfill = HistoricalBackfill(factory, ready)  # type: ignore[arg-type]
    await backfill.run(symbols=SYMBOLS, timeframes=[Timeframe.D1], start=START, end=END)

    async with factory() as check:
        mock_rows = (
            await check.execute(
                select(func.count()).select_from(Candle).where(Candle.provider == "mock")
            )
        ).scalar_one()

    assert mock_rows == 0


# ---------------------------------------------------------------------------
# 17-18. Isolation and contention
# ---------------------------------------------------------------------------
async def test_a_backfill_touches_no_production_state(
    ready: CountingProvider, factory: async_sessionmaker
) -> None:
    """Market data is shared; scan runs, evaluations and positions are not."""
    backfill = HistoricalBackfill(factory, ready)  # type: ignore[arg-type]

    await backfill.run(symbols=SYMBOLS, timeframes=[Timeframe.D1], start=START, end=END)

    async with factory() as check:
        assert (await check.execute(select(ScanRun))).scalars().all() == []
        assert (await check.execute(select(SignalEvaluation))).scalars().all() == []
        assert (await check.execute(select(VirtualPosition))).scalars().all() == []


async def test_a_backfill_commits_per_chunk_not_once_at_the_end(
    ready: CountingProvider, factory: async_sessionmaker
) -> None:
    """**Why the live scheduler is not blocked.**

    Data is visible from another session while the run is still going, which is
    only true if each chunk committed independently. One transaction spanning the
    whole expansion would hold the write lock for hours.
    """
    seen: list[int] = []

    backfill = HistoricalBackfill(factory, ready)  # type: ignore[arg-type]
    await backfill.run(
        symbols=SYMBOLS,
        timeframes=[Timeframe.M5],
        start=START,
        end=START + timedelta(days=60),
        progress=lambda _result: seen.append(-1),
    )

    async with factory() as check:
        final = (
            await check.execute(
                select(func.count()).select_from(Candle).where(Candle.timeframe == Timeframe.M5)
            )
        ).scalar_one()
    assert final > 0, "nothing was committed"
    assert len(seen) > 1, "a multi-chunk run produced a single commit"


# ---------------------------------------------------------------------------
# 20. Reproducibility
# ---------------------------------------------------------------------------
async def test_the_same_range_produces_the_same_rows(
    ready: CountingProvider, factory: async_sessionmaker
) -> None:
    """Immutable inputs, identical stored result -- re-running is safe."""
    backfill = HistoricalBackfill(factory, ready)  # type: ignore[arg-type]
    await backfill.run(symbols=SYMBOLS, timeframes=[Timeframe.D1], start=START, end=END)

    async with factory() as first:
        rows = CandleRepository(first)
        instrument_ids = (await first.execute(select(Candle.instrument_id).distinct())).scalars()
        before = {
            (candle.instrument_id, candle.timestamp, candle.close)
            for ident in set(instrument_ids)
            for candle in await rows.get_range(
                instrument_id=ident, timeframe=Timeframe.D1, start=START, end=END
            )
        }

    await backfill.run(
        symbols=SYMBOLS, timeframes=[Timeframe.D1], start=START, end=END, resume=False
    )

    async with factory() as second:
        rows = CandleRepository(second)
        instrument_ids = (await second.execute(select(Candle.instrument_id).distinct())).scalars()
        after = {
            (candle.instrument_id, candle.timestamp, candle.close)
            for ident in set(instrument_ids)
            for candle in await rows.get_range(
                instrument_id=ident, timeframe=Timeframe.D1, start=START, end=END
            )
        }

    assert before == after
