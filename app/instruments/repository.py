"""Instrument persistence."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Instrument
from app.db.upsert import build_upsert, table_of
from app.domain.enums import AssetType
from app.market_data.provider import InstrumentInfo

logger = get_logger(__name__)

_UPSERT_COLUMNS = (
    "name",
    "exchange",
    "currency",
    "asset_type",
    "isin",
    "is_active",
    "listed_at",
    "delisted_at",
)


class InstrumentRepository:
    """Data access for :class:`~app.db.models.Instrument`.

    Takes a session rather than creating one (coding rule 4), so a caller can run
    several repositories inside a single transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_symbol(self, symbol: str) -> Instrument | None:
        """Look up one instrument by ticker. Case-insensitive."""
        stmt = select(Instrument).where(Instrument.symbol == symbol.upper())
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, instrument_id: int) -> Instrument | None:
        return await self._session.get(Instrument, instrument_id)

    async def list_all(
        self,
        *,
        exchange: str | None = None,
        asset_type: AssetType | None = None,
        active_only: bool = True,
        limit: int = 500,
        offset: int = 0,
    ) -> Sequence[Instrument]:
        """List instruments, newest filters applied, ordered by symbol.

        ``active_only`` defaults to True for display purposes. Backtests must pass
        ``active_only=False``: excluding delisted instruments from a historical
        study is precisely how survivorship bias enters.
        """
        stmt = select(Instrument).order_by(Instrument.symbol).limit(limit).offset(offset)
        if exchange is not None:
            stmt = stmt.where(Instrument.exchange == exchange.upper())
        if asset_type is not None:
            stmt = stmt.where(Instrument.asset_type == asset_type)
        if active_only:
            stmt = stmt.where(Instrument.is_active.is_(True))
        return (await self._session.execute(stmt)).scalars().all()

    async def count(self, *, active_only: bool = True) -> int:
        stmt = select(Instrument.id)
        if active_only:
            stmt = stmt.where(Instrument.is_active.is_(True))
        return len((await self._session.execute(stmt)).scalars().all())

    async def upsert_many(self, infos: Sequence[InstrumentInfo]) -> int:
        """Insert or update instruments by symbol. Returns the number processed."""
        if not infos:
            return 0

        rows = [
            {
                "symbol": info.symbol.upper(),
                "name": info.name,
                "exchange": info.exchange.upper(),
                "currency": info.currency.upper(),
                "asset_type": info.asset_type,
                "isin": info.isin,
                "is_active": info.is_active,
                "listed_at": info.listed_at,
                "delisted_at": info.delisted_at,
            }
            for info in infos
        ]
        stmt = build_upsert(
            self._session,
            table_of(Instrument),
            rows,
            index_elements=["symbol"],
            update_columns=_UPSERT_COLUMNS,
        )
        await self._session.execute(stmt)
        logger.info("upserted instruments", count=len(rows))
        return len(rows)
