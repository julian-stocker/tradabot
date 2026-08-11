"""Paper-trading persistence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import (
    PortfolioSnapshot,
    VirtualOrder,
    VirtualPortfolio,
    VirtualPosition,
    VirtualTrade,
)
from app.domain.enums import PositionStatus
from app.paper.portfolio import PortfolioValuation
from app.simulation.models import SimulationProfileConfig

ZERO = Decimal(0)


class PaperTradingRepository:
    """Data access for portfolios, orders, positions, trades and snapshots."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        """The session this repository writes through.

        Exposed so the engine can hand the same session to the broker: order,
        cash and position mutations must land in one transaction, which is only
        possible if they share a session.
        """
        return self._session

    # -- Portfolios --------------------------------------------------------

    async def ensure_portfolio(self, profile: SimulationProfileConfig) -> VirtualPortfolio:
        """Fetch a profile's portfolio, creating it at initial capital if absent.

        Idempotent, and the reason restart recovery needs no special case: a
        process that starts with an existing portfolio simply finds it.
        """
        if profile.id is None:
            msg = f"profile {profile.name!r} must be persisted before it can trade"
            raise ValueError(msg)

        stmt = select(VirtualPortfolio).where(VirtualPortfolio.simulation_profile_id == profile.id)
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return existing

        portfolio = VirtualPortfolio(
            simulation_profile_id=profile.id,
            currency=profile.currency,
            initial_capital=profile.initial_capital,
            cash=profile.initial_capital,
            realized_pnl=ZERO,
            total_fees=ZERO,
            total_spread_cost=ZERO,
            total_slippage_cost=ZERO,
            peak_equity=profile.initial_capital,
            max_drawdown=ZERO,
        )
        self._session.add(portfolio)
        await self._session.flush()
        return portfolio

    async def get_portfolio(self, simulation_profile_id: int) -> VirtualPortfolio:
        stmt = select(VirtualPortfolio).where(
            VirtualPortfolio.simulation_profile_id == simulation_profile_id
        )
        portfolio = (await self._session.execute(stmt)).scalar_one_or_none()
        if portfolio is None:
            raise NotFoundError("virtual_portfolio", simulation_profile_id)
        return portfolio

    async def list_portfolios(self) -> Sequence[VirtualPortfolio]:
        stmt = select(VirtualPortfolio).order_by(VirtualPortfolio.simulation_profile_id)
        return (await self._session.execute(stmt)).scalars().all()

    # -- Positions ---------------------------------------------------------

    async def open_positions(
        self, simulation_profile_id: int, *, instrument_id: int | None = None
    ) -> Sequence[VirtualPosition]:
        stmt = select(VirtualPosition).where(
            VirtualPosition.simulation_profile_id == simulation_profile_id,
            VirtualPosition.status == PositionStatus.OPEN,
        )
        if instrument_id is not None:
            stmt = stmt.where(VirtualPosition.instrument_id == instrument_id)
        return (await self._session.execute(stmt.order_by(VirtualPosition.id))).scalars().all()

    async def all_open_positions(self) -> Sequence[VirtualPosition]:
        """Every open position across every profile, for bar-driven monitoring."""
        stmt = (
            select(VirtualPosition)
            .where(VirtualPosition.status == PositionStatus.OPEN)
            .order_by(VirtualPosition.simulation_profile_id, VirtualPosition.id)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def positions(
        self,
        simulation_profile_id: int,
        *,
        status: PositionStatus | None = None,
        limit: int = 200,
    ) -> Sequence[VirtualPosition]:
        stmt = (
            select(VirtualPosition)
            .where(VirtualPosition.simulation_profile_id == simulation_profile_id)
            .order_by(VirtualPosition.entry_timestamp.desc())
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(VirtualPosition.status == status)
        return (await self._session.execute(stmt)).scalars().all()

    async def get_position(self, position_id: int) -> VirtualPosition | None:
        return await self._session.get(VirtualPosition, position_id)

    # -- Orders ------------------------------------------------------------

    async def orders(
        self, simulation_profile_id: int, *, limit: int = 200
    ) -> Sequence[VirtualOrder]:
        stmt = (
            select(VirtualOrder)
            .where(VirtualOrder.simulation_profile_id == simulation_profile_id)
            .order_by(VirtualOrder.requested_at.desc(), VirtualOrder.id.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def count_orders(self, simulation_profile_id: int) -> int:
        stmt = select(VirtualOrder.id).where(
            VirtualOrder.simulation_profile_id == simulation_profile_id
        )
        return len((await self._session.execute(stmt)).scalars().all())

    # -- Trades ------------------------------------------------------------

    async def trades(
        self, simulation_profile_id: int, *, limit: int = 200
    ) -> Sequence[VirtualTrade]:
        stmt = (
            select(VirtualTrade)
            .where(VirtualTrade.simulation_profile_id == simulation_profile_id)
            .order_by(VirtualTrade.exit_timestamp.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def record_trade(self, trade: VirtualTrade) -> VirtualTrade:
        """Persist a closed trade, replacing any prior record for the position.

        Keyed on ``position_id`` so a replayed close cannot append a duplicate
        trade to the performance history.
        """
        stmt = select(VirtualTrade).where(VirtualTrade.position_id == trade.position_id)
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return existing
        self._session.add(trade)
        await self._session.flush()
        return trade

    # -- Snapshots ---------------------------------------------------------

    async def record_snapshot(
        self, *, simulation_profile_id: int, valuation: PortfolioValuation
    ) -> PortfolioSnapshot:
        """Append (or overwrite) one point on the equity curve.

        Overwriting on a repeated timestamp keeps candle replay idempotent: the
        equity curve must not gain duplicate points because a bar was processed
        twice.
        """
        stmt = select(PortfolioSnapshot).where(
            PortfolioSnapshot.simulation_profile_id == simulation_profile_id,
            PortfolioSnapshot.timestamp == valuation.timestamp,
        )
        snapshot = (await self._session.execute(stmt)).scalar_one_or_none()
        if snapshot is None:
            snapshot = PortfolioSnapshot(
                simulation_profile_id=simulation_profile_id, timestamp=valuation.timestamp
            )
            self._session.add(snapshot)

        snapshot.cash = valuation.cash
        snapshot.positions_value = valuation.positions_value
        snapshot.equity = valuation.equity
        snapshot.realized_pnl = valuation.realized_pnl
        snapshot.unrealized_pnl = valuation.unrealized_pnl
        snapshot.open_position_count = valuation.open_position_count
        snapshot.gross_exposure = valuation.gross_exposure
        snapshot.drawdown = valuation.drawdown
        await self._session.flush()
        return snapshot

    async def snapshots(
        self, simulation_profile_id: int, *, since: datetime | None = None, limit: int = 1000
    ) -> Sequence[PortfolioSnapshot]:
        stmt = (
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.simulation_profile_id == simulation_profile_id)
            .order_by(PortfolioSnapshot.timestamp)
            .limit(limit)
        )
        if since is not None:
            stmt = stmt.where(PortfolioSnapshot.timestamp >= since)
        return (await self._session.execute(stmt)).scalars().all()

    async def orders_for_position(self, position_id: int) -> Sequence[VirtualOrder]:
        """Every order that opened or closed one position.

        The audit trail behind a trade's cost breakdown.
        """
        stmt = (
            select(VirtualOrder)
            .where(VirtualOrder.position_id == position_id)
            .order_by(VirtualOrder.requested_at, VirtualOrder.id)
        )
        return (await self._session.execute(stmt)).scalars().all()
