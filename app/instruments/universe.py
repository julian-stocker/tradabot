"""Point-in-time instrument universe.

Answers "which instruments were tradable *then*", which is a different question
from "which instruments exist now" -- and confusing the two is the definition of
survivorship bias.

The bias, concretely
--------------------
Backtesting a strategy on "the instruments in my database" silently selects for
companies that survived to the present. Every bankruptcy, delisting and takeover
that would have hurt is missing from the sample. The result is a backtest that
looks good and describes a portfolio nobody could have held, because choosing it
required knowing the future.

The defence here is a *type-level* one: this service has no method that returns
"all instruments". Every query requires a timestamp or a window. A caller cannot
accidentally get today's universe for a 2019 date, because there is no call that
would give it to them.

Current limitation
------------------
This is lifecycle filtering, not index-membership history. It answers "was this
listed?", not "was this in the DAX in March 2019?". Membership history needs a
separate, time-versioned table and is out of scope for phase 2 -- see
docs/roadmap.md phase 3.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc
from app.db.models import Instrument
from app.domain.enums import AssetType


class UniverseService:
    """Historical instrument-universe queries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def tradable_at(
        self,
        moment: datetime,
        *,
        exchange: str | None = None,
        asset_type: AssetType | None = None,
        limit: int = 1000,
    ) -> Sequence[Instrument]:
        """Instruments tradable at ``moment``.

        Applies the half-open rule ``[listed_at, delisted_at)`` in SQL, matching
        :meth:`~app.db.models.Instrument.is_tradable_at` exactly. NULL bounds are
        treated as unbounded, so instruments with unknown lifecycles are included
        rather than silently dropped -- absence of a listing date is missing
        metadata, not evidence the instrument did not exist.
        """
        moment = ensure_utc(moment)
        stmt = (
            select(Instrument)
            .where(
                or_(Instrument.listed_at.is_(None), Instrument.listed_at <= moment),
                or_(Instrument.delisted_at.is_(None), Instrument.delisted_at > moment),
            )
            .order_by(Instrument.symbol)
            .limit(limit)
        )
        if exchange is not None:
            stmt = stmt.where(Instrument.exchange == exchange.upper())
        if asset_type is not None:
            stmt = stmt.where(Instrument.asset_type == asset_type)
        return (await self._session.execute(stmt)).scalars().all()

    async def active_between(
        self,
        start: datetime,
        end: datetime,
        *,
        exchange: str | None = None,
        limit: int = 1000,
    ) -> Sequence[Instrument]:
        """Instruments tradable during **any part** of ``[start, end)``.

        Overlap, not containment. An instrument delisted halfway through the
        window belongs in a backtest covering that window -- it was tradable for
        part of it, and excluding it reintroduces exactly the bias this module
        exists to prevent.

        Raises:
            ValueError: if ``start`` is not before ``end``.
        """
        start, end = ensure_utc(start), ensure_utc(end)
        if start >= end:
            msg = f"start ({start.isoformat()}) must be before end ({end.isoformat()})"
            raise ValueError(msg)

        stmt = (
            select(Instrument)
            .where(
                # Listed before the window ends...
                or_(Instrument.listed_at.is_(None), Instrument.listed_at < end),
                # ...and not delisted before it starts.
                or_(Instrument.delisted_at.is_(None), Instrument.delisted_at > start),
            )
            .order_by(Instrument.symbol)
            .limit(limit)
        )
        if exchange is not None:
            stmt = stmt.where(Instrument.exchange == exchange.upper())
        return (await self._session.execute(stmt)).scalars().all()

    async def symbols_tradable_at(self, moment: datetime) -> tuple[str, ...]:
        """Just the tickers tradable at ``moment``, for scanner-style callers."""
        return tuple(i.symbol for i in await self.tradable_at(moment))

    async def was_tradable(self, symbol: str, moment: datetime) -> bool:
        """Whether one instrument was tradable at ``moment``.

        Returns False for an unknown symbol: from a universe perspective an
        instrument we have never heard of was not tradable.
        """
        stmt = select(Instrument).where(Instrument.symbol == symbol.upper())
        instrument = (await self._session.execute(stmt)).scalar_one_or_none()
        if instrument is None:
            return False
        return instrument.is_tradable_at(moment)
