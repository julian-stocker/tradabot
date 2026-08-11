"""Candle persistence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.time import ensure_utc, utc_now
from app.db.models import Candle
from app.db.upsert import build_upsert, table_of
from app.domain.enums import Timeframe
from app.market_data.provider import CandleData

logger = get_logger(__name__)

_UPSERT_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trade_count",
    "vwap",
    "provider",
    "ingested_at",
)

MAX_CANDLE_LIMIT = 50_000
"""Hard ceiling on rows returned in one call. Prevents an unbounded API query
from pulling a decade of 1-minute bars into memory."""


class CandleRepository:
    """Data access for :class:`~app.db.models.Candle`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_range(
        self,
        *,
        instrument_id: int,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        limit: int = MAX_CANDLE_LIMIT,
    ) -> Sequence[Candle]:
        """Candles in ``[start, end)``, ascending.

        The window is half-open so consecutive requests tile without duplicating
        the boundary bar -- the same convention the provider abstraction uses.

        This is the query the composite primary key
        ``(instrument_id, timeframe, timestamp)`` was designed for: it resolves to
        a single index range scan.
        """
        stmt = (
            select(Candle)
            .where(
                Candle.instrument_id == instrument_id,
                Candle.timeframe == timeframe,
                Candle.timestamp >= ensure_utc(start),
                Candle.timestamp < ensure_utc(end),
            )
            .order_by(Candle.timestamp)
            .limit(min(limit, MAX_CANDLE_LIMIT))
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def get_latest(
        self,
        *,
        instrument_id: int,
        timeframe: Timeframe,
        limit: int,
        as_of: datetime | None = None,
    ) -> Sequence[Candle]:
        """The most recent ``limit`` candles, returned in **ascending** order.

        Args:
            as_of: if given, only bars strictly before this time are considered.

        ``as_of`` is not a convenience -- it is the mechanism that makes
        historical analysis honest. Recomputing a signal "as it would have looked
        on 3 March" requires the query itself to refuse later bars; filtering them
        out afterwards is the kind of step that gets forgotten and silently
        reintroduces look-ahead bias.
        """
        if limit < 1:
            msg = f"limit must be >= 1, got {limit}"
            raise ValueError(msg)

        stmt = select(Candle).where(
            Candle.instrument_id == instrument_id,
            Candle.timeframe == timeframe,
        )
        if as_of is not None:
            stmt = stmt.where(Candle.timestamp < ensure_utc(as_of))

        stmt = stmt.order_by(Candle.timestamp.desc()).limit(min(limit, MAX_CANDLE_LIMIT))
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(reversed(rows))

    async def latest_timestamp(
        self, *, instrument_id: int, timeframe: Timeframe
    ) -> datetime | None:
        """Timestamp of the newest stored bar, used to resume incremental ingest."""
        stmt = (
            select(Candle.timestamp)
            .where(Candle.instrument_id == instrument_id, Candle.timeframe == timeframe)
            .order_by(Candle.timestamp.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def count(self, *, instrument_id: int, timeframe: Timeframe) -> int:
        stmt = select(Candle.timestamp).where(
            Candle.instrument_id == instrument_id, Candle.timeframe == timeframe
        )
        return len((await self._session.execute(stmt)).scalars().all())

    async def upsert_many(
        self,
        *,
        instrument_id: int,
        timeframe: Timeframe,
        candles: Sequence[CandleData],
        provider: str | None = None,
        ingested_at: datetime | None = None,
    ) -> int:
        """Insert or replace candles. Idempotent on the natural key.

        ``provider`` and ``ingested_at`` record where each bar came from. Written
        per bar rather than per instrument because a backfill and a later
        incremental sync can come from different sources, and "where did this
        number come from" must stay answerable for the individual bar.
        """
        if not candles:
            return 0

        stamped_at = ingested_at or utc_now()
        rows = [
            {
                "instrument_id": instrument_id,
                "timeframe": timeframe,
                "timestamp": candle.timestamp,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
                "trade_count": candle.trade_count,
                "vwap": candle.vwap,
                "provider": provider,
                "ingested_at": stamped_at,
            }
            for candle in candles
        ]
        stmt = build_upsert(
            self._session,
            table_of(Candle),
            rows,
            index_elements=["instrument_id", "timeframe", "timestamp"],
            update_columns=_UPSERT_COLUMNS,
        )
        await self._session.execute(stmt)
        logger.info(
            "upserted candles",
            instrument_id=instrument_id,
            timeframe=timeframe.value,
            count=len(rows),
        )
        return len(rows)
