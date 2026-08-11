"""Corporate-action persistence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.time import ensure_utc
from app.corporate_actions.models import CorporateAction
from app.db.models import CorporateActionRow
from app.db.upsert import build_upsert, table_of
from app.domain.enums import CorporateActionType

logger = get_logger(__name__)

_UPSERT_COLUMNS = (
    "payment_at",
    "from_shares",
    "to_shares",
    "cash_amount",
    "currency",
    "source",
    "external_id",
)


class CorporateActionRepository:
    """Data access for :class:`~app.db.models.CorporateActionRow`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_instrument(
        self,
        *,
        instrument_id: int,
        symbol: str,
        known_as_of: datetime | None = None,
        action_types: Sequence[CorporateActionType] | None = None,
    ) -> list[CorporateAction]:
        """Actions for one instrument, ascending by effective time.

        Args:
            known_as_of: restrict to actions already effective at this instant.
                This is the point-in-time switch: a backtest reconstructing what
                was knowable in March 2021 must not adjust prices using a split
                that happened in July.

                Note this uses *effective* time as a proxy for *known* time. In
                reality a split is announced weeks before it takes effect, so
                this is conservative -- it hides an action slightly longer than
                the market did. Modelling announcement dates is a later refinement
                (docs/data-adjustments.md).
            action_types: restrict to these kinds. Omit for all.
        """
        stmt = (
            select(CorporateActionRow)
            .where(CorporateActionRow.instrument_id == instrument_id)
            .order_by(CorporateActionRow.effective_at)
        )
        if known_as_of is not None:
            stmt = stmt.where(CorporateActionRow.effective_at <= ensure_utc(known_as_of))
        if action_types:
            stmt = stmt.where(CorporateActionRow.action_type.in_(list(action_types)))

        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(row, symbol) for row in rows]

    async def count_for_instrument(self, instrument_id: int) -> int:
        stmt = select(CorporateActionRow.id).where(
            CorporateActionRow.instrument_id == instrument_id
        )
        return len((await self._session.execute(stmt)).scalars().all())

    async def upsert_many(self, *, instrument_id: int, actions: Sequence[CorporateAction]) -> int:
        """Insert or update actions on ``(instrument, type, effective_at)``.

        Idempotent, so re-ingesting a provider's full action history is safe.
        """
        if not actions:
            return 0

        rows = [
            {
                "instrument_id": instrument_id,
                "action_type": action.action_type,
                "effective_at": action.effective_at,
                "payment_at": action.payment_at,
                "from_shares": action.from_shares,
                "to_shares": action.to_shares,
                "cash_amount": action.cash_amount,
                "currency": action.currency,
                "source": action.source,
                "external_id": action.external_id,
            }
            for action in actions
        ]
        stmt = build_upsert(
            self._session,
            table_of(CorporateActionRow),
            rows,
            index_elements=["instrument_id", "action_type", "effective_at"],
            update_columns=_UPSERT_COLUMNS,
        )
        await self._session.execute(stmt)
        logger.info("upserted corporate actions", instrument_id=instrument_id, count=len(rows))
        return len(rows)


def _to_domain(row: CorporateActionRow, symbol: str) -> CorporateAction:
    """ORM row to validated domain model.

    ``symbol`` is passed in rather than joined: callers already know which
    instrument they asked about, and a join per action would be wasteful.
    """
    return CorporateAction(
        symbol=symbol,
        action_type=row.action_type,
        effective_at=row.effective_at,
        payment_at=row.payment_at,
        from_shares=row.from_shares,
        to_shares=row.to_shares,
        cash_amount=row.cash_amount,
        currency=row.currency,
        source=row.source,
        external_id=row.external_id,
    )
