"""Corporate-action ingestion must be complete, repeatable and containable.

Phase 9A left this path with two defects that tests did not catch because no
test asked. The provider call was unbounded, so it silently returned about a
month of actions and reported success; and nothing re-fetched when an instrument
was added later, so SMH carried an unadjusted 2-for-1 through an entire phase.

Both were coverage failures rather than logic failures, which is why the tests
here are about *what gets asked for* as much as what comes back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.errors import InstrumentNotFoundError, ProviderError
from app.corporate_actions.models import CorporateAction
from app.corporate_actions.repository import CorporateActionRepository
from app.domain.enums import CorporateActionType, Timeframe
from app.instruments.repository import InstrumentRepository
from app.market_data.ingest import IngestionService
from app.market_data.integrity import DiscontinuityKind, scan_price_series
from app.market_data.provider import InstrumentInfo

WINDOW_START = datetime(2020, 1, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 12, 31, tzinfo=UTC)


class RecordingProvider:
    """A provider that remembers the window it was asked for.

    The bug this exists to catch is invisible to a mock that ignores its
    arguments: an unbounded request *succeeds*, it just returns almost nothing.
    Only asserting on what was requested can detect it.
    """

    name = "recording"

    def __init__(self, actions: list[CorporateAction] | None = None) -> None:
        self.actions = actions or []
        self.calls: list[tuple[str, datetime | None, datetime | None]] = []

    async def get_corporate_actions(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[CorporateAction]:
        self.calls.append((symbol, start, end))
        if start is None or end is None:
            return []
        return [a for a in self.actions if start <= a.effective_at <= end]


class FailingProvider(RecordingProvider):
    """Raises on one symbol, succeeds on the rest."""

    def __init__(self, bad_symbol: str, actions: list[CorporateAction] | None = None) -> None:
        super().__init__(actions)
        self._bad = bad_symbol

    async def get_corporate_actions(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[CorporateAction]:
        if symbol == self._bad:
            msg = f"provider exploded for {symbol}"
            raise ProviderError(msg)
        return await super().get_corporate_actions(symbol, start=start, end=end)


def action(symbol: str, when: datetime, from_shares: int, to_shares: int) -> CorporateAction:
    return CorporateAction(
        symbol=symbol,
        action_type=CorporateActionType.SPLIT,
        effective_at=when,
        from_shares=Decimal(from_shares),
        to_shares=Decimal(to_shares),
        external_id=f"{symbol}-{when:%Y%m%d}",
    )


async def seed_instrument(session, symbol: str = "TEST") -> int:
    instruments = InstrumentRepository(session)
    await instruments.upsert_many(
        [InstrumentInfo(symbol=symbol, name=f"{symbol} Inc.", exchange="XNAS", currency="USD")]
    )
    await session.flush()
    instrument = await instruments.get_by_symbol(symbol)
    assert instrument is not None
    return instrument.id


# ---------------------------------------------------------------------------
# 11-12: idempotency and duplicates
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingestion_is_idempotent(session) -> None:
    """Re-running must not multiply rows. Actions upsert on their natural key."""
    instrument_id = await seed_instrument(session)
    split = action("TEST", datetime(2023, 5, 5, tzinfo=UTC), 1, 2)
    provider = RecordingProvider([split])
    service = IngestionService(session, provider)  # type: ignore[arg-type]

    for _ in range(3):
        await service.sync_corporate_actions("TEST", start=WINDOW_START, end=WINDOW_END)
    await session.flush()

    assert await CorporateActionRepository(session).count_for_instrument(instrument_id) == 1


@pytest.mark.asyncio
async def test_no_duplicates_across_overlapping_windows(session) -> None:
    """Two fetches whose windows overlap must still leave one row per event."""
    instrument_id = await seed_instrument(session)
    split = action("TEST", datetime(2023, 5, 5, tzinfo=UTC), 1, 2)
    service = IngestionService(session, RecordingProvider([split]))  # type: ignore[arg-type]

    await service.sync_corporate_actions(
        "TEST", start=WINDOW_START, end=datetime(2024, 1, 1, tzinfo=UTC)
    )
    await service.sync_corporate_actions(
        "TEST", start=datetime(2022, 1, 1, tzinfo=UTC), end=WINDOW_END
    )
    await session.flush()

    stored = await CorporateActionRepository(session).list_for_instrument(
        instrument_id=instrument_id, symbol="TEST"
    )
    assert len(stored) == 1
    assert stored[0].effective_at == split.effective_at


@pytest.mark.asyncio
async def test_provenance_survives_a_refetch(session) -> None:
    """A disputed action must stay traceable to the source that reported it."""
    instrument_id = await seed_instrument(session)
    service = IngestionService(
        session, RecordingProvider([action("TEST", datetime(2023, 5, 5, tzinfo=UTC), 1, 2)])
    )  # type: ignore[arg-type]

    await service.sync_corporate_actions("TEST", start=WINDOW_START, end=WINDOW_END)
    await service.sync_corporate_actions("TEST", start=WINDOW_START, end=WINDOW_END)
    await session.flush()

    stored = await CorporateActionRepository(session).list_for_instrument(
        instrument_id=instrument_id, symbol="TEST"
    )
    assert stored[0].external_id == "TEST-20230505"


# ---------------------------------------------------------------------------
# The window: the defect that produced zero splits
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_full_history_window_is_requested(session) -> None:
    """**The gate.** An unbounded call returns nothing and reports success."""
    await seed_instrument(session)
    provider = RecordingProvider([])
    service = IngestionService(session, provider)  # type: ignore[arg-type]

    await service.sync_corporate_actions("TEST", start=WINDOW_START, end=WINDOW_END)

    assert len(provider.calls) == 1
    _, start, end = provider.calls[0]
    assert start == WINDOW_START
    assert end == WINDOW_END


@pytest.mark.asyncio
async def test_a_historical_split_outside_the_default_window_is_retrieved(session) -> None:
    """Six years back, which the provider's own default would have excluded."""
    instrument_id = await seed_instrument(session)
    old = action("TEST", datetime(2020, 8, 31, tzinfo=UTC), 1, 4)
    service = IngestionService(session, RecordingProvider([old]))  # type: ignore[arg-type]

    await service.sync_corporate_actions("TEST", start=WINDOW_START, end=WINDOW_END)
    await session.flush()

    stored = await CorporateActionRepository(session).list_for_instrument(
        instrument_id=instrument_id, symbol="TEST"
    )
    assert len(stored) == 1
    assert stored[0].split_ratio == Decimal(4)


# ---------------------------------------------------------------------------
# 13: provider failure containment
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_one_symbols_provider_failure_does_not_abandon_the_rest(session) -> None:
    """A single bad ticker must not leave the remaining universe unfetched."""
    await seed_instrument(session, "GOOD")
    await seed_instrument(session, "BAD")
    provider = FailingProvider("BAD", [action("GOOD", datetime(2023, 5, 5, tzinfo=UTC), 1, 2)])
    service = IngestionService(session, provider)  # type: ignore[arg-type]

    written = 0
    failures = 0
    for symbol in ("BAD", "GOOD"):
        try:
            written += await service.sync_corporate_actions(
                symbol, start=WINDOW_START, end=WINDOW_END
            )
        except ProviderError:
            failures += 1

    assert failures == 1
    assert written == 1


@pytest.mark.asyncio
async def test_an_unknown_symbol_raises_rather_than_inventing_an_instrument(session) -> None:
    service = IngestionService(session, RecordingProvider([]))  # type: ignore[arg-type]
    with pytest.raises(InstrumentNotFoundError):
        await service.sync_corporate_actions("NOPE", start=WINDOW_START, end=WINDOW_END)


# ---------------------------------------------------------------------------
# The scan, end to end against a real session
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_scan_finds_a_split_with_no_stored_action(session) -> None:
    """**The SMH regression.** Registered late, never fetched, silently wrong."""
    from app.db.models import Candle

    instrument_id = await seed_instrument(session)
    start = datetime(2023, 5, 1, tzinfo=UTC)
    for index in range(20):
        price = 200.0 + index if index < 10 else (200.0 + index) / 2
        session.add(
            Candle(
                instrument_id=instrument_id,
                timeframe=Timeframe.D1,
                timestamp=start + timedelta(days=index),
                open=Decimal(str(price)),
                high=Decimal(str(price)),
                low=Decimal(str(price)),
                close=Decimal(str(price)),
                volume=Decimal(1000),
            )
        )
    await session.flush()

    report = await scan_price_series(session, timeframes=(Timeframe.D1,))

    unexplained = report.of(DiscontinuityKind.UNEXPLAINED)
    assert len(unexplained) == 1
    assert unexplained[0].symbol == "TEST"
    assert not report.healthy


@pytest.mark.asyncio
async def test_a_clean_series_scans_healthy(session) -> None:
    from app.db.models import Candle

    instrument_id = await seed_instrument(session)
    start = datetime(2023, 5, 1, tzinfo=UTC)
    for index in range(20):
        price = 200.0 + index * 0.5
        session.add(
            Candle(
                instrument_id=instrument_id,
                timeframe=Timeframe.D1,
                timestamp=start + timedelta(days=index),
                open=Decimal(str(price)),
                high=Decimal(str(price)),
                low=Decimal(str(price)),
                close=Decimal(str(price)),
                volume=Decimal(1000),
            )
        )
    await session.flush()

    report = await scan_price_series(session, timeframes=(Timeframe.D1,))
    assert report.healthy
    assert report.findings == []
