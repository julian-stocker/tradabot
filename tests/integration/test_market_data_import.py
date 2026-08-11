"""Import, incremental sync, provenance and split adjustment against a database.

Offline throughout. The provider is a scripted stub whose responses are fixed, so
these tests describe *our* behaviour rather than a vendor's uptime.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ProviderError
from app.core.events import EventType, RecordingEventPublisher
from app.corporate_actions.models import CorporateAction
from app.corporate_actions.repository import CorporateActionRepository
from app.db.models import Candle, Instrument, VirtualPosition
from app.domain.enums import (
    AssetType,
    CorporateActionType,
    PositionStatus,
    Side,
    Timeframe,
)
from app.domain.quotes import Quote
from app.instruments.repository import InstrumentRepository
from app.market_data.import_service import MarketDataImportService
from app.market_data.provider import CandleData, InstrumentInfo
from app.paper.corporate_actions import PositionCorporateActionService, apply_split_to_position
from app.simulation.defaults import build_default_profiles
from app.simulation.repository import SimulationProfileRepository

pytestmark = pytest.mark.integration

SYMBOL = "NVDA"
START = datetime(2024, 6, 3, tzinfo=UTC)
END = datetime(2024, 6, 15, tzinfo=UTC)
# Six consecutive NYSE sessions: Mon 3 June through Mon 10 June.
SESSION_DAYS = [3, 4, 5, 6, 7, 10]


def make_candles(days: list[int], *, base: Decimal = Decimal(100)) -> list[CandleData]:
    return [
        CandleData(
            timestamp=datetime(2024, 6, day, 20, 0, tzinfo=UTC),
            open=base,
            high=base + Decimal(2),
            low=base - Decimal(1),
            close=base + Decimal(1),
            volume=Decimal(1_000),
        )
        for day in days
    ]


class ScriptedProvider:
    """A provider that returns exactly what a test tells it to.

    Records every candle request so tests can assert on *what was asked for* --
    which is the whole point of incremental sync.
    """

    name = "scripted"

    def __init__(
        self,
        candles: list[CandleData] | None = None,
        *,
        actions: list[CorporateAction] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._candles = candles if candles is not None else make_candles(SESSION_DAYS)
        self._actions = actions or []
        self._error = error
        self.requests: list[tuple[datetime, datetime]] = []

    async def get_instruments(self) -> list[InstrumentInfo]:
        return [
            InstrumentInfo(
                symbol=SYMBOL,
                name="NVIDIA",
                exchange="XNYS",
                currency="USD",
                asset_type=AssetType.STOCK,
            )
        ]

    async def get_historical_candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[CandleData]:
        self.requests.append((start, end))
        if self._error is not None:
            raise self._error
        return [c for c in self._candles if start <= c.timestamp < end]

    async def get_latest_quote(self, symbol: str) -> Quote:
        return Quote(
            symbol=symbol,
            timestamp=END,
            bid=Decimal("99.95"),
            ask=Decimal("100.05"),
        )

    async def get_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        return list(self._actions)


async def stored_candles(session: AsyncSession, instrument_id: int) -> list[Candle]:
    result = await session.execute(
        select(Candle).where(Candle.instrument_id == instrument_id).order_by(Candle.timestamp)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
async def test_import_stores_bars_and_reports_what_happened(session: AsyncSession) -> None:
    provider = ScriptedProvider()
    service = MarketDataImportService(session, provider)
    await service.ensure_instruments([SYMBOL])

    report = await service.import_symbol(symbol=SYMBOL, start=START, end=END)

    assert report.ok
    assert report.received_bars == len(SESSION_DAYS)
    assert report.inserted_bars == len(SESSION_DAYS)
    assert report.existing_bars == 0
    assert report.expected_bars is not None, "a daily window has a knowable session count"


async def test_reimporting_the_same_window_inserts_nothing_new(
    session: AsyncSession,
) -> None:
    """Idempotency. Re-running an import is routine; duplicating bars is not."""
    provider = ScriptedProvider()
    service = MarketDataImportService(session, provider)
    await service.ensure_instruments([SYMBOL])

    first = await service.import_symbol(symbol=SYMBOL, start=START, end=END)
    second = await service.import_symbol(symbol=SYMBOL, start=START, end=END)
    await session.flush()

    instrument = await InstrumentRepository(session).get_by_symbol(SYMBOL)
    assert instrument is not None

    assert second.existing_bars == first.inserted_bars
    assert len(await stored_candles(session, instrument.id)) == len(SESSION_DAYS)


async def test_an_unknown_instrument_is_reported_not_invented(
    session: AsyncSession,
) -> None:
    """A candle request must never create an instrument row as a side effect."""
    service = MarketDataImportService(session, ScriptedProvider())

    report = await service.import_symbol(symbol="GHOST", start=START, end=END)

    assert not report.ok
    assert report.error is not None
    assert "instrument table" in report.error


async def test_an_inverted_window_fails_before_requesting_anything(
    session: AsyncSession,
) -> None:
    provider = ScriptedProvider()
    service = MarketDataImportService(session, provider)
    await service.ensure_instruments([SYMBOL])

    report = await service.import_symbol(symbol=SYMBOL, start=END, end=START)

    assert not report.ok
    assert provider.requests == []


async def test_a_provider_failure_becomes_a_report_not_an_exception(
    session: AsyncSession,
) -> None:
    """One bad symbol must not abort a watchlist sync."""
    provider = ScriptedProvider(error=ProviderError("upstream is down"))
    service = MarketDataImportService(session, provider)
    await service.ensure_instruments([SYMBOL])

    report = await service.import_symbol(symbol=SYMBOL, start=START, end=END)

    assert not report.ok
    assert report.error is not None


async def test_a_failed_sync_emits_an_event(session: AsyncSession) -> None:
    """The hook a future Discord transport attaches to."""
    events = RecordingEventPublisher()
    provider = ScriptedProvider(error=ProviderError("upstream is down"))
    service = MarketDataImportService(session, provider, events=events)
    await service.ensure_instruments([SYMBOL])

    await service.import_symbol(symbol=SYMBOL, start=START, end=END)

    assert events.of_type(EventType.MARKET_DATA_SYNC_FAILED)


async def test_an_event_payload_carries_no_credential(session: AsyncSession) -> None:
    events = RecordingEventPublisher()
    provider = ScriptedProvider(error=ProviderError("bad key: api_key=PKLIVEFAKE1234567890"))
    service = MarketDataImportService(session, provider, events=events)
    await service.ensure_instruments([SYMBOL])
    await service.import_symbol(symbol=SYMBOL, start=START, end=END)

    payload = events.of_type(EventType.MARKET_DATA_SYNC_FAILED)[0].redacted_payload()

    assert "api_key" not in {key.lower() for key in payload}


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
async def test_stored_data_records_where_it_came_from(session: AsyncSession) -> None:
    """Without provenance, a suspicious bar has no one to ask about it."""
    provider = ScriptedProvider()
    service = MarketDataImportService(session, provider)
    await service.ensure_instruments([SYMBOL])
    await service.import_symbol(symbol=SYMBOL, start=START, end=END)
    await session.flush()

    instrument = (
        await session.execute(select(Instrument).where(Instrument.symbol == SYMBOL))
    ).scalar_one()
    assert instrument.provider == provider.name

    for candle in await stored_candles(session, instrument.id):
        assert candle.provider == provider.name
        assert candle.ingested_at is not None


# ---------------------------------------------------------------------------
# Incremental sync
# ---------------------------------------------------------------------------
async def test_sync_requests_only_what_is_missing(session: AsyncSession) -> None:
    """A full re-download every run wastes quota and rate-limits the next symbol."""
    provider = ScriptedProvider()
    service = MarketDataImportService(session, provider)
    await service.ensure_instruments([SYMBOL])
    await service.import_symbol(symbol=SYMBOL, start=START, end=END)
    await session.flush()
    provider.requests.clear()

    await service.sync_symbol(symbol=SYMBOL, now=END)

    assert len(provider.requests) == 1
    requested_start, _ = provider.requests[0]
    assert requested_start > START, "the sync re-requested history it already held"


async def test_sync_overlaps_the_newest_stored_bars(session: AsyncSession) -> None:
    """Providers revise recent bars; the upsert makes re-fetching them free."""
    provider = ScriptedProvider()
    service = MarketDataImportService(session, provider)
    await service.ensure_instruments([SYMBOL])
    await service.import_symbol(symbol=SYMBOL, start=START, end=END)
    await session.flush()
    provider.requests.clear()

    await service.sync_symbol(symbol=SYMBOL, now=END)

    newest_stored = datetime(2024, 6, 10, 20, 0, tzinfo=UTC)
    requested_start, _ = provider.requests[0]
    assert requested_start < newest_stored, "no overlap means revisions are never picked up"


async def test_sync_backfills_a_symbol_with_no_history(session: AsyncSession) -> None:
    provider = ScriptedProvider()
    service = MarketDataImportService(session, provider)
    await service.ensure_instruments([SYMBOL])

    report = await service.sync_symbol(symbol=SYMBOL, now=END)

    assert report.ok
    assert report.inserted_bars > 0


async def test_syncing_a_watchlist_continues_past_a_failure(session: AsyncSession) -> None:
    provider = ScriptedProvider()
    service = MarketDataImportService(session, provider)
    await service.ensure_instruments([SYMBOL])

    report = await service.sync_watchlist([SYMBOL, "GHOST"], now=END)

    assert SYMBOL in report.symbols_succeeded
    assert "GHOST" in report.symbols_failed
    assert not report.ok


# ---------------------------------------------------------------------------
# Corporate actions on open positions
# ---------------------------------------------------------------------------
async def open_position(
    session: AsyncSession, *, entry: datetime, quantity: Decimal, price: Decimal
) -> VirtualPosition:
    """A minimal open position belonging to the first default profile."""
    profiles = SimulationProfileRepository(session)
    await profiles.upsert_many(build_default_profiles())
    await session.flush()
    profile = (await profiles.list_profiles(enabled_only=True))[0]
    assert profile.id is not None

    instrument = await InstrumentRepository(session).get_by_symbol(SYMBOL)
    assert instrument is not None

    position = VirtualPosition(
        simulation_profile_id=profile.id,
        instrument_id=instrument.id,
        side=Side.LONG,
        status=PositionStatus.OPEN,
        quantity=quantity,
        average_entry_price=price,
        entry_timestamp=entry,
        entry_bar_index=0,
        stop_loss=price * Decimal("0.96"),
        take_profit=price * Decimal("1.08"),
        unrealized_pnl=Decimal(0),
    )
    session.add(position)
    await session.flush()
    return position


def split(effective_at: datetime, *, from_shares: int, to_shares: int) -> CorporateAction:
    return CorporateAction(
        symbol=SYMBOL,
        action_type=CorporateActionType.SPLIT,
        effective_at=effective_at,
        from_shares=Decimal(from_shares),
        to_shares=Decimal(to_shares),
        source="test",
    )


def test_a_split_preserves_a_positions_economic_value() -> None:
    """10 @ 100 becomes 20 @ 50. Nothing happened economically, and the maths says so."""
    position = VirtualPosition(
        simulation_profile_id=1,
        instrument_id=1,
        side=Side.LONG,
        status=PositionStatus.OPEN,
        quantity=Decimal(10),
        average_entry_price=Decimal(100),
        entry_timestamp=START,
        entry_bar_index=0,
        stop_loss=Decimal(96),
        take_profit=Decimal(108),
        unrealized_pnl=Decimal(0),
    )

    adjustment = apply_split_to_position(position, Decimal(2))

    assert position.quantity == Decimal(20)
    assert position.average_entry_price == Decimal(50)
    assert adjustment.value_after == adjustment.value_before
    assert position.stop_loss == Decimal(48), "a 4% stop stays a 4% stop"
    assert position.take_profit == Decimal(54)


def test_a_reverse_split_uses_the_same_arithmetic() -> None:
    position = VirtualPosition(
        simulation_profile_id=1,
        instrument_id=1,
        side=Side.LONG,
        status=PositionStatus.OPEN,
        quantity=Decimal(100),
        average_entry_price=Decimal(2),
        entry_timestamp=START,
        entry_bar_index=0,
        unrealized_pnl=Decimal(0),
    )

    adjustment = apply_split_to_position(position, Decimal("0.1"))

    assert position.quantity == Decimal(10)
    assert position.average_entry_price == Decimal(20)
    assert adjustment.value_after == adjustment.value_before


def test_a_non_positive_ratio_is_refused() -> None:
    position = VirtualPosition(
        simulation_profile_id=1,
        instrument_id=1,
        side=Side.LONG,
        status=PositionStatus.OPEN,
        quantity=Decimal(10),
        average_entry_price=Decimal(100),
        entry_timestamp=START,
        entry_bar_index=0,
        unrealized_pnl=Decimal(0),
    )

    with pytest.raises(ValueError, match="positive"):
        apply_split_to_position(position, Decimal(0))


async def test_a_split_adjusts_a_position_opened_before_it(session: AsyncSession) -> None:
    service_provider = ScriptedProvider()
    importer = MarketDataImportService(session, service_provider)
    await importer.ensure_instruments([SYMBOL])
    await session.flush()

    position = await open_position(session, entry=START, quantity=Decimal(10), price=Decimal(100))
    instrument_id = position.instrument_id

    await PositionCorporateActionService(session).apply_actions(
        instrument_id=instrument_id,
        actions=[split(datetime(2024, 6, 5, tzinfo=UTC), from_shares=1, to_shares=2)],
        as_of=END,
    )

    assert position.quantity == Decimal(20)
    assert position.average_entry_price == Decimal(50)


async def test_a_position_opened_after_a_split_is_left_alone(
    session: AsyncSession,
) -> None:
    """It was already bought at post-split prices; halving it invents a loss."""
    importer = MarketDataImportService(session, ScriptedProvider())
    await importer.ensure_instruments([SYMBOL])
    await session.flush()

    position = await open_position(
        session,
        entry=datetime(2024, 6, 6, tzinfo=UTC),
        quantity=Decimal(10),
        price=Decimal(50),
    )

    await PositionCorporateActionService(session).apply_actions(
        instrument_id=position.instrument_id,
        actions=[split(datetime(2024, 6, 5, tzinfo=UTC), from_shares=1, to_shares=2)],
        as_of=END,
    )

    assert position.quantity == Decimal(10)
    assert position.average_entry_price == Decimal(50)


async def test_applying_the_same_split_twice_adjusts_once(session: AsyncSession) -> None:
    """Re-importing an action history is routine; halving a holding twice is not."""
    importer = MarketDataImportService(session, ScriptedProvider())
    await importer.ensure_instruments([SYMBOL])
    await session.flush()

    position = await open_position(session, entry=START, quantity=Decimal(10), price=Decimal(100))
    actions = [split(datetime(2024, 6, 5, tzinfo=UTC), from_shares=1, to_shares=2)]
    service = PositionCorporateActionService(session)

    await service.apply_actions(instrument_id=position.instrument_id, actions=actions, as_of=END)
    second = await service.apply_actions(
        instrument_id=position.instrument_id, actions=actions, as_of=END
    )

    assert second == []
    assert position.quantity == Decimal(20)


async def test_a_cash_dividend_does_not_change_a_position(session: AsyncSession) -> None:
    """Dividend income is out of scope; a half-implementation overstates returns."""
    importer = MarketDataImportService(session, ScriptedProvider())
    await importer.ensure_instruments([SYMBOL])
    await session.flush()

    position = await open_position(session, entry=START, quantity=Decimal(10), price=Decimal(100))
    dividend = CorporateAction(
        symbol=SYMBOL,
        action_type=CorporateActionType.CASH_DIVIDEND,
        effective_at=datetime(2024, 6, 5, tzinfo=UTC),
        cash_amount=Decimal("0.50"),
        currency="USD",
        source="test",
    )

    adjustments = await PositionCorporateActionService(session).apply_actions(
        instrument_id=position.instrument_id, actions=[dividend], as_of=END
    )

    assert adjustments == []
    assert position.quantity == Decimal(10)


async def test_importing_a_split_adjusts_open_positions(session: AsyncSession) -> None:
    """The Part L wiring: ingestion is where a newly-known split reaches a position."""
    provider = ScriptedProvider(
        actions=[split(datetime(2024, 6, 5, tzinfo=UTC), from_shares=1, to_shares=2)]
    )
    service = MarketDataImportService(session, provider)
    await service.ensure_instruments([SYMBOL])
    await session.flush()

    position = await open_position(session, entry=START, quantity=Decimal(10), price=Decimal(100))

    report = await service.import_symbol(symbol=SYMBOL, start=START, end=END)

    assert report.corporate_actions == 1
    assert report.positions_adjusted == 1
    assert position.quantity == Decimal(20)


async def test_a_provider_without_corporate_actions_still_imports_candles(
    session: AsyncSession,
) -> None:
    provider = ScriptedProvider(actions=[])
    service = MarketDataImportService(session, provider)
    await service.ensure_instruments([SYMBOL])

    report = await service.import_symbol(symbol=SYMBOL, start=START, end=END)

    assert report.ok
    assert report.corporate_actions == 0
    assert report.inserted_bars == len(SESSION_DAYS)


async def test_corporate_actions_are_stored_for_later_price_adjustment(
    session: AsyncSession,
) -> None:
    action = split(datetime(2024, 6, 5, tzinfo=UTC), from_shares=1, to_shares=2)
    service = MarketDataImportService(session, ScriptedProvider(actions=[action]))
    await service.ensure_instruments([SYMBOL])
    await service.import_symbol(symbol=SYMBOL, start=START, end=END)
    await session.flush()

    instrument = await InstrumentRepository(session).get_by_symbol(SYMBOL)
    assert instrument is not None
    stored = await CorporateActionRepository(session).list_for_instrument(
        instrument_id=instrument.id, symbol=SYMBOL
    )

    assert len(stored) == 1
    assert stored[0].split_ratio == Decimal(2)


# ---------------------------------------------------------------------------
# Determinism of the mock provider
# ---------------------------------------------------------------------------
async def test_the_mock_provider_stays_deterministic(session: AsyncSession) -> None:
    """The mock must not be removed or made random: it is the offline baseline.

    Every test in this repository depends on the same seed producing the same
    candles. If this fails, the whole suite silently becomes flaky.
    """
    from app.market_data.providers.mock import MockMarketDataProvider

    window = (START - timedelta(days=90), END)
    first = await MockMarketDataProvider(1337).get_historical_candles("AAPL", Timeframe.D1, *window)
    second = await MockMarketDataProvider(1337).get_historical_candles(
        "AAPL", Timeframe.D1, *window
    )
    other_seed = await MockMarketDataProvider(99).get_historical_candles(
        "AAPL", Timeframe.D1, *window
    )

    assert first == second
    assert first, "the mock must still return data"
    assert first != other_seed, "a different seed must produce a different series"
