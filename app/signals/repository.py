"""Signal persistence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import SignalRow
from app.domain.enums import PriceSeriesAdjustment, Timeframe
from app.signals.models import SignalResult

logger = get_logger(__name__)


class SignalRepository:
    """Data access for :class:`~app.db.models.SignalRow`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        result: SignalResult,
        instrument_id: int,
        adjustment: PriceSeriesAdjustment,
    ) -> int:
        """Persist a signal, replacing any identical prior computation.

        The natural key includes ``engine_version`` and ``price_adjustment``, so
        re-running the same engine over the same bar updates one row, while a
        scoring change or a different price series produces a distinct record.
        Both behaviours are wanted: the first keeps the table clean, the second
        keeps old results comparable to the code that produced them.

        Returns:
            The signal row id, for attaching trade decisions.
        """
        stmt = select(SignalRow).where(
            SignalRow.instrument_id == instrument_id,
            SignalRow.timeframe == result.timeframe,
            SignalRow.horizon == result.horizon,
            SignalRow.bar_timestamp == result.timestamp,
            SignalRow.engine_version == result.engine_version,
            SignalRow.price_adjustment == adjustment,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = SignalRow(
                instrument_id=instrument_id,
                timeframe=result.timeframe,
                horizon=result.horizon,
                bar_timestamp=result.timestamp,
                engine_version=result.engine_version,
                price_adjustment=adjustment,
            )
            self._session.add(row)

        row.generated_at = result.generated_at
        row.score = result.score
        row.classification = result.classification
        row.confidence = result.confidence
        row.reference_price = result.reference_price
        row.spread_bps = result.spread_bps
        row.expected_move_bps = result.net_edge.expected_move_bps
        row.cost_bps = result.net_edge.cost_bps
        row.net_edge_bps = result.net_edge.net_edge_bps
        row.bars_used = result.bars_used
        row.feature_snapshot = dict(result.feature_snapshot)
        row.components = [
            {
                "name": component.name,
                "kind": component.kind.value,
                "score": component.score,
                "weight": component.weight,
                "configured_weight": component.configured_weight,
                "available": component.available,
                "reasons": [
                    {
                        "kind": reason.kind.value,
                        "code": reason.code,
                        "message": reason.message,
                        "feature": reason.feature,
                        "value": reason.value,
                    }
                    for reason in component.reasons
                ],
            }
            for component in result.components
        ]
        await self._session.flush()
        logger.debug("recorded signal", symbol=result.symbol, signal_id=row.id)
        return row.id

    async def get(self, signal_id: int) -> SignalRow | None:
        return await self._session.get(SignalRow, signal_id)

    async def list_for_instrument(
        self,
        *,
        instrument_id: int,
        timeframe: Timeframe | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> Sequence[SignalRow]:
        """Stored signals for one instrument, newest bar first."""
        stmt = (
            select(SignalRow)
            .where(SignalRow.instrument_id == instrument_id)
            .order_by(SignalRow.bar_timestamp.desc())
            .limit(limit)
        )
        if timeframe is not None:
            stmt = stmt.where(SignalRow.timeframe == timeframe)
        if since is not None:
            stmt = stmt.where(SignalRow.bar_timestamp >= since)
        return (await self._session.execute(stmt)).scalars().all()

    async def count(self) -> int:
        return len((await self._session.execute(select(SignalRow.id))).scalars().all())
