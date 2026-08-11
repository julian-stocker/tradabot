"""Point-in-time correctness at the query layer. **Part D.**

The bug these exist to prevent was real and shipped: ``as_of`` filtered on a
bar's *start* stamp, so at 14:20 the hourly bar stamped 14:00 -- which does not
finish until 15:00 -- was visible, close price and all. Forty minutes of future
information, in the one place the whole project relies on to exclude it.

Bars are stamped when they **open**, so "before ``as_of``" and "finished by
``as_of``" are different questions, and only the second is safe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Candle, Instrument
from app.domain.enums import AssetType, Timeframe
from app.market_data.repository import CandleRepository

pytestmark = pytest.mark.integration

DAY = datetime(2024, 6, 3, tzinfo=UTC)


async def _instrument(session: AsyncSession) -> Instrument:
    instrument = Instrument(
        symbol="PIT",
        name="Point In Time",
        exchange="XNAS",
        currency="USD",
        asset_type=AssetType.STOCK,
        is_active=True,
    )
    session.add(instrument)
    await session.flush()
    return instrument


async def _hourly_bars(session: AsyncSession, instrument: Instrument) -> None:
    """Hourly bars 13:00-16:00, each with a distinctive close."""
    for index, hour in enumerate((13, 14, 15, 16)):
        session.add(
            Candle(
                instrument_id=instrument.id,
                timeframe=Timeframe.H1,
                timestamp=DAY.replace(hour=hour),
                open=Decimal(100 + index),
                high=Decimal(101 + index),
                low=Decimal(99 + index),
                close=Decimal(100 + index) + Decimal("0.5"),
                volume=Decimal(1_000),
            )
        )
    await session.flush()


async def test_a_bar_that_has_not_closed_is_invisible(session: AsyncSession) -> None:
    """**The regression test for the leak.**

    At 14:20 the 14:00 hourly bar is still forming. Its close is not information
    that existed yet, and a signal scored from it would be reading the future.
    """
    instrument = await _instrument(session)
    await _hourly_bars(session, instrument)

    rows = await CandleRepository(session).get_latest(
        instrument_id=instrument.id,
        timeframe=Timeframe.H1,
        limit=10,
        as_of=DAY.replace(hour=14, minute=20),
    )

    stamps = [row.timestamp for row in rows]
    assert DAY.replace(hour=14) not in stamps, "an unfinished bar was visible"
    assert stamps == [DAY.replace(hour=13)]


async def test_a_bar_becomes_visible_exactly_when_it_closes(session: AsyncSession) -> None:
    """The boundary is inclusive: at 15:00 the 14:00-15:00 bar is complete."""
    instrument = await _instrument(session)
    await _hourly_bars(session, instrument)
    repository = CandleRepository(session)

    just_before = await repository.get_latest(
        instrument_id=instrument.id,
        timeframe=Timeframe.H1,
        limit=10,
        as_of=DAY.replace(hour=14, minute=59, second=59),
    )
    exactly_at = await repository.get_latest(
        instrument_id=instrument.id, timeframe=Timeframe.H1, limit=10, as_of=DAY.replace(hour=15)
    )

    assert DAY.replace(hour=14) not in [row.timestamp for row in just_before]
    assert DAY.replace(hour=14) in [row.timestamp for row in exactly_at]


async def test_walk_forward_replay_ordering_is_unchanged(session: AsyncSession) -> None:
    """Passing a bar's own timestamp still yields every strictly earlier bar.

    This is what makes the fix safe: ``app.paper.replay`` scores at
    ``bar[i].timestamp``, and the previous bar closes exactly then, so the
    visible set is identical to before. Only the live scanner's exposure to a
    partially formed bar changed.
    """
    instrument = await _instrument(session)
    await _hourly_bars(session, instrument)

    rows = await CandleRepository(session).get_latest(
        instrument_id=instrument.id,
        timeframe=Timeframe.H1,
        limit=10,
        as_of=DAY.replace(hour=15),
    )

    assert [row.timestamp for row in rows] == [DAY.replace(hour=13), DAY.replace(hour=14)]


async def test_a_higher_timeframe_bar_mid_formation_is_excluded(
    session: AsyncSession,
) -> None:
    """Part D's example, exactly: at 10:20 an hourly bar ending 11:00 is invisible.

    The multi-timeframe context is where this matters most. A 5-minute evaluation
    that could see a partially formed daily candle would be reading a whole day
    of future information into its macro view.
    """
    instrument = await _instrument(session)
    session.add(
        Candle(
            instrument_id=instrument.id,
            timeframe=Timeframe.H1,
            timestamp=DAY.replace(hour=10),
            open=Decimal(100),
            high=Decimal(110),
            low=Decimal(90),
            close=Decimal(108),
            volume=Decimal(1_000),
        )
    )
    await session.flush()

    rows = await CandleRepository(session).get_latest(
        instrument_id=instrument.id,
        timeframe=Timeframe.H1,
        limit=5,
        as_of=DAY.replace(hour=10, minute=20),
    )

    assert rows == []


async def test_the_daily_bar_of_the_current_session_is_not_visible_intraday(
    session: AsyncSession,
) -> None:
    """A daily candle stamped today does not exist until tomorrow's stamp."""
    instrument = await _instrument(session)
    session.add(
        Candle(
            instrument_id=instrument.id,
            timeframe=Timeframe.D1,
            timestamp=DAY.replace(hour=4),
            open=Decimal(100),
            high=Decimal(120),
            low=Decimal(95),
            close=Decimal(118),
            volume=Decimal(5_000),
        )
    )
    await session.flush()

    intraday = await CandleRepository(session).get_latest(
        instrument_id=instrument.id,
        timeframe=Timeframe.D1,
        limit=5,
        as_of=DAY.replace(hour=18),
    )
    next_day = await CandleRepository(session).get_latest(
        instrument_id=instrument.id,
        timeframe=Timeframe.D1,
        limit=5,
        as_of=DAY.replace(hour=4) + timedelta(days=1),
    )

    assert intraday == [], "the still-forming daily bar leaked into an intraday view"
    assert len(next_day) == 1


async def test_no_as_of_still_returns_the_newest_bar(session: AsyncSession) -> None:
    """Unbounded reads are unaffected -- ingestion and status still see everything."""
    instrument = await _instrument(session)
    await _hourly_bars(session, instrument)

    rows = await CandleRepository(session).get_latest(
        instrument_id=instrument.id, timeframe=Timeframe.H1, limit=10
    )

    assert len(rows) == 4
    assert rows[-1].timestamp == DAY.replace(hour=16)
