"""Repository and ingestion behaviour against a real database.

Runs on in-memory SQLite so no container is needed. The custom ``Money`` and
``UTCDateTime`` column types are exercised here -- they are the pieces most
likely to behave differently across dialects, so they get direct round-trip
coverage rather than being assumed correct.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import InstrumentNotFoundError
from app.db.models import Candle, Instrument
from app.domain.enums import AssetType, Timeframe
from app.instruments.repository import InstrumentRepository
from app.market_data.ingest import IngestionService
from app.market_data.provider import CandleData, InstrumentInfo
from app.market_data.repository import CandleRepository

START = datetime(2024, 1, 1, tzinfo=UTC)


def candle(day: int, close: str = "100.123456", volume: str = "1000") -> CandleData:
    price = Decimal(close)
    return CandleData(
        timestamp=START + timedelta(days=day),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(volume),
    )


async def make_instrument(session, symbol: str = "TEST") -> Instrument:
    repo = InstrumentRepository(session)
    await repo.upsert_many(
        [
            InstrumentInfo(
                symbol=symbol,
                name=f"{symbol} Corp",
                exchange="XNAS",
                currency="USD",
                asset_type=AssetType.STOCK,
            )
        ]
    )
    await session.flush()
    instrument = await repo.get_by_symbol(symbol)
    assert instrument is not None
    return instrument


class TestColumnTypes:
    async def test_decimal_survives_the_round_trip_exactly(self, session):
        """The whole point of the Money type: no binary float in the path."""
        instrument = await make_instrument(session)
        exact = Decimal("123.456789")

        await CandleRepository(session).upsert_many(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            candles=[candle(0, close=str(exact))],
        )
        await session.flush()
        session.expunge_all()

        stored = (await session.execute(select(Candle))).scalar_one()
        assert stored.close == exact
        assert isinstance(stored.close, Decimal)

    async def test_a_price_that_floats_cannot_represent(self, session):
        """0.1 + 0.2 != 0.3 in binary floating point. It must here."""
        instrument = await make_instrument(session)
        await CandleRepository(session).upsert_many(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            candles=[candle(0, close="0.300000")],
        )
        await session.flush()
        session.expunge_all()

        stored = (await session.execute(select(Candle))).scalar_one()
        assert stored.close == Decimal("0.1") + Decimal("0.2")

    async def test_timestamps_come_back_aware_and_utc(self, session):
        instrument = await make_instrument(session)
        await CandleRepository(session).upsert_many(
            instrument_id=instrument.id, timeframe=Timeframe.D1, candles=[candle(0)]
        )
        await session.flush()
        session.expunge_all()

        stored = (await session.execute(select(Candle))).scalar_one()
        assert stored.timestamp.tzinfo is not None
        assert stored.timestamp == START

    async def test_non_utc_timestamp_is_normalised_on_write(self, session):
        from datetime import timezone

        instrument = await make_instrument(session)
        berlin = timezone(timedelta(hours=2))
        stamped = datetime(2024, 3, 5, 14, 0, tzinfo=berlin)

        await CandleRepository(session).upsert_many(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            candles=[
                CandleData(
                    timestamp=stamped,
                    open=Decimal(1),
                    high=Decimal(1),
                    low=Decimal(1),
                    close=Decimal(1),
                    volume=Decimal(1),
                )
            ],
        )
        await session.flush()
        session.expunge_all()

        stored = (await session.execute(select(Candle))).scalar_one()
        assert stored.timestamp == datetime(2024, 3, 5, 12, 0, tzinfo=UTC)


class TestInstrumentRepository:
    async def test_upsert_is_idempotent(self, session, provider):
        repo = InstrumentRepository(session)
        infos = await provider.get_instruments()

        await repo.upsert_many(infos)
        await repo.upsert_many(infos)
        await session.flush()

        # active_only=False: the mock universe includes a delisted instrument,
        # which is retained on purpose (survivorship bias).
        assert await repo.count(active_only=False) == len(infos)
        assert await repo.count(active_only=True) < len(infos)

    async def test_upsert_updates_existing_rows(self, session):
        repo = InstrumentRepository(session)
        base = InstrumentInfo(symbol="ACME", name="Old Name", exchange="XNAS", currency="USD")
        await repo.upsert_many([base])
        await repo.upsert_many([base.model_copy(update={"name": "New Name"})])
        await session.flush()

        stored = await repo.get_by_symbol("ACME")
        assert stored is not None
        assert stored.name == "New Name"

    async def test_lookup_is_case_insensitive(self, session):
        await make_instrument(session, "NVDA")
        assert await InstrumentRepository(session).get_by_symbol("nvda") is not None

    async def test_unknown_symbol_returns_none(self, session):
        assert await InstrumentRepository(session).get_by_symbol("NOPE") is None

    async def test_inactive_instruments_are_retained_for_backtests(self, session):
        """Deleting delisted instruments is how survivorship bias gets in."""
        repo = InstrumentRepository(session)
        await repo.upsert_many(
            [
                InstrumentInfo(symbol="LIVE", name="Live", exchange="XNAS", currency="USD"),
                InstrumentInfo(
                    symbol="DEAD",
                    name="Delisted",
                    exchange="XNAS",
                    currency="USD",
                    is_active=False,
                ),
            ]
        )
        await session.flush()

        assert len(await repo.list_all(active_only=True)) == 1
        assert len(await repo.list_all(active_only=False)) == 2

    async def test_filters(self, session):
        repo = InstrumentRepository(session)
        await repo.upsert_many(
            [
                InstrumentInfo(symbol="A", name="A", exchange="XNAS", currency="USD"),
                InstrumentInfo(symbol="B", name="B", exchange="XETR", currency="EUR"),
                InstrumentInfo(
                    symbol="C",
                    name="C",
                    exchange="XETR",
                    currency="EUR",
                    asset_type=AssetType.ETF,
                ),
            ]
        )
        await session.flush()

        assert len(await repo.list_all(exchange="XETR")) == 2
        assert len(await repo.list_all(asset_type=AssetType.ETF)) == 1


class TestCandleRepository:
    async def test_range_query_is_half_open(self, session):
        """[start, end) so consecutive windows tile without duplicating a bar."""
        instrument = await make_instrument(session)
        repo = CandleRepository(session)
        await repo.upsert_many(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            candles=[candle(i) for i in range(10)],
        )
        await session.flush()

        rows = await repo.get_range(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            start=START + timedelta(days=2),
            end=START + timedelta(days=5),
        )
        assert [r.timestamp for r in rows] == [START + timedelta(days=i) for i in (2, 3, 4)]

    async def test_results_are_ascending(self, session):
        instrument = await make_instrument(session)
        repo = CandleRepository(session)
        await repo.upsert_many(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            candles=[candle(i) for i in reversed(range(10))],
        )
        await session.flush()

        rows = await repo.get_range(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            start=START,
            end=START + timedelta(days=20),
        )
        stamps = [r.timestamp for r in rows]
        assert stamps == sorted(stamps)

    async def test_get_latest_returns_ascending_tail(self, session):
        instrument = await make_instrument(session)
        repo = CandleRepository(session)
        await repo.upsert_many(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            candles=[candle(i) for i in range(20)],
        )
        await session.flush()

        rows = await repo.get_latest(instrument_id=instrument.id, timeframe=Timeframe.D1, limit=5)
        assert len(rows) == 5
        assert [r.timestamp for r in rows] == [START + timedelta(days=i) for i in range(15, 20)]

    async def test_as_of_excludes_later_bars(self, session):
        """The query-level guard against look-ahead in historical analysis."""
        instrument = await make_instrument(session)
        repo = CandleRepository(session)
        await repo.upsert_many(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            candles=[candle(i) for i in range(20)],
        )
        await session.flush()

        rows = await repo.get_latest(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            limit=100,
            as_of=START + timedelta(days=10),
        )
        assert all(r.timestamp < START + timedelta(days=10) for r in rows)
        assert len(rows) == 10

    async def test_timeframes_are_isolated(self, session):
        instrument = await make_instrument(session)
        repo = CandleRepository(session)
        await repo.upsert_many(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            candles=[candle(i) for i in range(5)],
        )
        await repo.upsert_many(
            instrument_id=instrument.id,
            timeframe=Timeframe.H1,
            candles=[candle(i) for i in range(3)],
        )
        await session.flush()

        assert await repo.count(instrument_id=instrument.id, timeframe=Timeframe.D1) == 5
        assert await repo.count(instrument_id=instrument.id, timeframe=Timeframe.H1) == 3

    async def test_upsert_overwrites_on_the_natural_key(self, session):
        """Re-ingesting a revised bar must update, not duplicate or fail."""
        instrument = await make_instrument(session)
        repo = CandleRepository(session)

        await repo.upsert_many(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            candles=[candle(0, close="100.000000")],
        )
        await repo.upsert_many(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            candles=[candle(0, close="105.000000")],
        )
        await session.flush()
        session.expunge_all()

        rows = (await session.execute(select(Candle))).scalars().all()
        assert len(rows) == 1
        assert rows[0].close == Decimal("105.000000")

    async def test_latest_timestamp(self, session):
        instrument = await make_instrument(session)
        repo = CandleRepository(session)
        await repo.upsert_many(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            candles=[candle(i) for i in range(7)],
        )
        await session.flush()

        latest = await repo.latest_timestamp(instrument_id=instrument.id, timeframe=Timeframe.D1)
        assert latest == START + timedelta(days=6)

    async def test_latest_timestamp_is_none_when_empty(self, session):
        instrument = await make_instrument(session)
        assert (
            await CandleRepository(session).latest_timestamp(
                instrument_id=instrument.id, timeframe=Timeframe.D1
            )
            is None
        )

    async def test_empty_upsert_is_a_no_op(self, session):
        instrument = await make_instrument(session)
        written = await CandleRepository(session).upsert_many(
            instrument_id=instrument.id, timeframe=Timeframe.D1, candles=[]
        )
        assert written == 0


class TestIngestion:
    async def test_sync_instruments_populates_the_universe(self, session, provider):
        service = IngestionService(session, provider)
        count = await service.sync_instruments()
        await session.flush()

        assert count == len(await provider.get_instruments())
        assert await InstrumentRepository(session).count(active_only=False) == count

    async def test_sync_candles_stores_bars(self, session, provider):
        service = IngestionService(session, provider)
        await service.sync_instruments()
        await session.flush()

        written = await service.sync_candles(
            symbol="NVDA",
            timeframe=Timeframe.D1,
            start=datetime(2023, 1, 1, tzinfo=UTC),
            end=datetime(2023, 6, 1, tzinfo=UTC),
        )
        assert written > 0

    async def test_sync_candles_is_idempotent(self, session, provider):
        service = IngestionService(session, provider)
        await service.sync_instruments()
        await session.flush()

        window = {
            "symbol": "NVDA",
            "timeframe": Timeframe.D1,
            "start": datetime(2023, 1, 1, tzinfo=UTC),
            "end": datetime(2023, 3, 1, tzinfo=UTC),
        }
        await service.sync_candles(**window)
        await session.flush()

        instrument = await InstrumentRepository(session).get_by_symbol("NVDA")
        assert instrument is not None
        repo = CandleRepository(session)
        first = await repo.count(instrument_id=instrument.id, timeframe=Timeframe.D1)

        await service.sync_candles(**window)
        await session.flush()
        second = await repo.count(instrument_id=instrument.id, timeframe=Timeframe.D1)

        assert first == second, "re-ingesting the same window must not duplicate bars"

    async def test_unknown_symbol_raises(self, session, provider):
        service = IngestionService(session, provider)
        await service.sync_instruments()
        await session.flush()

        with pytest.raises(InstrumentNotFoundError):
            await service.sync_candles(symbol="NOPE", timeframe=Timeframe.D1)

    async def test_sync_all_reports_failures_without_aborting(self, session, provider):
        """One bad ticker must not abort a universe-wide sync, or hide itself."""
        service = IngestionService(session, provider)
        report = await service.sync_all(
            timeframe=Timeframe.D1,
            symbols=["NVDA", "NOT_A_REAL_SYMBOL"],
            start=datetime(2023, 1, 1, tzinfo=UTC),
            end=datetime(2023, 3, 1, tzinfo=UTC),
        )
        await session.flush()

        assert "NVDA" in report.symbols_succeeded
        assert len(report.symbols_failed) == 1
        assert report.symbols_failed[0][0] == "NOT_A_REAL_SYMBOL"
        assert not report.ok
        assert report.candles_written > 0

    async def test_incremental_sync_resumes_from_stored_data(self, session, provider):
        service = IngestionService(session, provider)
        await service.sync_instruments()
        await session.flush()

        await service.sync_candles(
            symbol="NVDA",
            timeframe=Timeframe.D1,
            start=datetime(2023, 1, 1, tzinfo=UTC),
            end=datetime(2023, 6, 1, tzinfo=UTC),
        )
        await session.flush()

        instrument = await InstrumentRepository(session).get_by_symbol("NVDA")
        assert instrument is not None
        repo = CandleRepository(session)

        written = await service.sync_candles(
            symbol="NVDA", timeframe=Timeframe.D1, end=datetime(2023, 9, 1, tzinfo=UTC)
        )
        await session.flush()

        assert written > 0
        latest = await repo.latest_timestamp(instrument_id=instrument.id, timeframe=Timeframe.D1)
        assert latest is not None
        assert latest > datetime(2023, 8, 1, tzinfo=UTC)
