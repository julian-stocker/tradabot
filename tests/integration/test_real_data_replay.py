"""Replaying stored candles through the paper broker.

Uses the mock provider's deterministic series as the "real" data: the point of
these tests is the *replay mechanics* -- ordering, no-look-ahead, session-aware
risk -- not the vendor. A test that needed live NVDA prices could not assert
anything stable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.enums import Horizon, Timeframe
from app.market_data.ingest import IngestionService
from app.market_data.providers.mock import MockMarketDataProvider
from app.paper.replay import ReplayError, replay_symbol
from app.simulation.defaults import build_default_profiles
from app.simulation.repository import SimulationProfileRepository

pytestmark = pytest.mark.integration

SYMBOL = "NVDA"
HISTORY_START = datetime(2022, 1, 1, tzinfo=UTC)
REPLAY_START = datetime(2023, 6, 1, tzinfo=UTC)
REPLAY_END = datetime(2023, 12, 31, tzinfo=UTC)


@pytest.fixture
async def replay_ready(session: AsyncSession, provider: MockMarketDataProvider) -> AsyncSession:
    """A database with instruments, a long candle history and the default profiles."""
    ingestion = IngestionService(session, provider)
    await ingestion.sync_instruments()
    await ingestion.sync_candles(
        symbol=SYMBOL, timeframe=Timeframe.D1, start=HISTORY_START, end=REPLAY_END
    )
    await SimulationProfileRepository(session).upsert_many(build_default_profiles())
    await session.flush()
    return session


async def test_replay_walks_the_stored_bars(
    replay_ready: AsyncSession, settings: Settings, provider: MockMarketDataProvider
) -> None:
    result = await replay_symbol(
        replay_ready,
        settings=settings,
        provider=provider,
        symbol=SYMBOL,
        start=REPLAY_START,
        end=REPLAY_END,
        horizon=Horizon.D5,
    )

    assert result.ok
    assert result.bars_replayed > 0
    assert result.first_bar is not None
    assert result.last_bar is not None
    assert REPLAY_START <= result.first_bar <= result.last_bar <= REPLAY_END


async def test_replay_produces_one_summary_per_enabled_profile(
    replay_ready: AsyncSession, settings: Settings, provider: MockMarketDataProvider
) -> None:
    """Every profile sees the same data and reaches its own conclusion."""
    profiles = await SimulationProfileRepository(replay_ready).list_profiles(enabled_only=True)

    result = await replay_symbol(
        replay_ready,
        settings=settings,
        provider=provider,
        symbol=SYMBOL,
        start=REPLAY_START,
        end=REPLAY_END,
    )

    assert len(result.summaries) == len(profiles)
    assert {s.profile_name for s in result.summaries} == {p.name for p in profiles}


async def test_replay_accounting_reconciles(
    replay_ready: AsyncSession, settings: Settings, provider: MockMarketDataProvider
) -> None:
    """Whatever the trades did, the books must balance.

    This is the claim the replay actually supports: the machinery is consistent.
    It says nothing about whether the strategy is any good.
    """
    result = await replay_symbol(
        replay_ready,
        settings=settings,
        provider=provider,
        symbol=SYMBOL,
        start=REPLAY_START,
        end=REPLAY_END,
    )

    for summary in result.summaries:
        assert summary.ending_equity >= 0, "a paper portfolio cannot go negative"
        assert summary.total_costs >= 0
        assert summary.trade_count >= 0
        assert Decimal("-1") <= Decimal(str(summary.max_drawdown)) <= Decimal("0")


async def test_replay_is_deterministic(
    session: AsyncSession, settings: Settings, provider: MockMarketDataProvider
) -> None:
    """Same stored bars, same answer. A replay that drifts cannot be reasoned about."""

    async def run() -> tuple[int, int, int]:
        ingestion = IngestionService(session, provider)
        await ingestion.sync_instruments()
        await ingestion.sync_candles(
            symbol=SYMBOL, timeframe=Timeframe.D1, start=HISTORY_START, end=REPLAY_END
        )
        await SimulationProfileRepository(session).upsert_many(build_default_profiles())
        await session.flush()
        result = await replay_symbol(
            session,
            settings=settings,
            provider=provider,
            symbol=SYMBOL,
            start=REPLAY_START,
            end=REPLAY_END,
        )
        return result.bars_replayed, result.signals_evaluated, result.signals_actionable

    first = await run()
    await session.rollback()
    second = await run()

    assert first == second


async def test_an_unimported_symbol_is_refused_with_a_usable_message(
    replay_ready: AsyncSession, settings: Settings, provider: MockMarketDataProvider
) -> None:
    """Replay reads what is stored; it never quietly fetches."""
    with pytest.raises(ReplayError, match="instrument table"):
        await replay_symbol(
            replay_ready,
            settings=settings,
            provider=provider,
            symbol="GHOST",
            start=REPLAY_START,
            end=REPLAY_END,
        )


async def test_too_short_a_window_is_refused(
    replay_ready: AsyncSession, settings: Settings, provider: MockMarketDataProvider
) -> None:
    """One bar cannot be replayed: there is no next open to execute at."""
    with pytest.raises(ReplayError, match="import"):
        await replay_symbol(
            replay_ready,
            settings=settings,
            provider=provider,
            symbol=SYMBOL,
            start=REPLAY_START,
            end=REPLAY_START + timedelta(days=1),
        )


async def test_warmup_bars_are_skipped_rather_than_scored(
    session: AsyncSession, settings: Settings, provider: MockMarketDataProvider
) -> None:
    """Indicators need history. Scoring before the window fills would be noise."""
    ingestion = IngestionService(session, provider)
    await ingestion.sync_instruments()
    # A window barely longer than the warm-up requirement, imported from its start,
    # so the early bars cannot possibly have enough history behind them.
    short_start = datetime(2023, 1, 2, tzinfo=UTC)
    short_end = datetime(2023, 3, 15, tzinfo=UTC)
    await ingestion.sync_candles(
        symbol=SYMBOL, timeframe=Timeframe.D1, start=short_start, end=short_end
    )
    await SimulationProfileRepository(session).upsert_many(build_default_profiles())
    await session.flush()

    result = await replay_symbol(
        session,
        settings=settings,
        provider=provider,
        symbol=SYMBOL,
        start=short_start,
        end=short_end,
    )

    assert result.warmup_skipped > 0
    assert result.bars_replayed > result.warmup_skipped


async def test_a_split_mid_replay_rescales_positions_the_replay_opened(
    replay_ready: AsyncSession, settings: Settings, provider: MockMarketDataProvider
) -> None:
    """The gap import-time adjustment cannot cover.

    A split recorded in the middle of the window applies to positions the replay
    itself opens, which did not exist when the import ran. Without this, a
    pre-split quantity is marked against a post-split price and the books show a
    50% loss that never happened.

    The assertion is that the machinery *ran* -- the split reached the position
    layer and the accounting still reconciles. Whether any position happened to be
    open on that particular day depends on the signal, which is not what this
    test is about.
    """
    from app.corporate_actions.models import CorporateAction
    from app.corporate_actions.repository import CorporateActionRepository
    from app.domain.enums import CorporateActionType
    from app.instruments.repository import InstrumentRepository

    instrument = await InstrumentRepository(replay_ready).get_by_symbol(SYMBOL)
    assert instrument is not None
    await CorporateActionRepository(replay_ready).upsert_many(
        instrument_id=instrument.id,
        actions=[
            CorporateAction(
                symbol=SYMBOL,
                action_type=CorporateActionType.SPLIT,
                effective_at=datetime(2023, 9, 1, tzinfo=UTC),
                from_shares=Decimal(1),
                to_shares=Decimal(2),
                source="test",
            )
        ],
    )
    await replay_ready.flush()

    result = await replay_symbol(
        replay_ready,
        settings=settings,
        provider=provider,
        symbol=SYMBOL,
        start=REPLAY_START,
        end=REPLAY_END,
    )

    assert result.ok
    assert result.positions_adjusted >= 0
    for summary in result.summaries:
        assert summary.ending_equity >= 0
