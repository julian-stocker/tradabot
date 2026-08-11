"""Paper-trading endpoints.

**Read-only by design.** The simulation is driven by the engine and the CLI; HTTP
observes it. There is no endpoint that opens a position, moves cash, or edits a
trade, because those would let a caller bypass the invariants the engine exists
to maintain -- and an editable P&L history is not a record of anything.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Path, Query

from app.api.deps import PaperTradingRepositoryDep, SimulationProfileRepositoryDep
from app.api.schemas.paper import (
    OrderResponse,
    OverviewResponse,
    OverviewRow,
    PerformanceResponse,
    PortfolioResponse,
    PositionResponse,
    TradeResponse,
)
from app.core.time import utc_now
from app.db.models import VirtualPortfolio
from app.domain.enums import PositionStatus
from app.paper.performance import summarise
from app.paper.portfolio import PortfolioValuation, value_portfolio
from app.paper.repository import PaperTradingRepository
from app.simulation.models import SimulationProfileConfig

router = APIRouter(prefix="/simulation/profiles", tags=["paper-trading"])
overview_router = APIRouter(prefix="/simulation", tags=["paper-trading"])

ZERO = Decimal(0)


async def _valued(
    repository: PaperTradingRepository, portfolio: VirtualPortfolio
) -> PortfolioValuation:
    """Mark a portfolio at its last known prices.

    No live quotes are fetched: an HTTP read must not depend on a market-data
    round trip, and positions already carry the mark from the last processed bar.
    Equity here is therefore as of the last bar, not as of this instant.
    """
    positions = await repository.open_positions(portfolio.simulation_profile_id)
    marks = {
        p.instrument_id: p.current_mark_price for p in positions if p.current_mark_price is not None
    }
    return value_portfolio(
        portfolio=portfolio,
        positions=positions,
        quotes={},
        marks=marks,
        timestamp=portfolio.last_valued_at or utc_now(),
    )


@router.get(
    "/{name}/portfolio",
    response_model=PortfolioResponse,
    summary="Virtual portfolio state",
    responses={404: {"description": "Unknown profile or no portfolio yet"}},
)
async def get_portfolio(
    profiles: SimulationProfileRepositoryDep,
    repository: PaperTradingRepositoryDep,
    name: str = Path(description="Profile name, e.g. 5000eur-balanced."),
) -> PortfolioResponse:
    """Cash, equity, exposure and drawdown for one virtual portfolio."""
    profile = await profiles.get_profile(name)
    portfolio = await repository.get_portfolio(_require_id(profile))
    valuation = await _valued(repository, portfolio)
    return _portfolio_response(profile, portfolio, valuation)


@router.get(
    "/{name}/positions",
    response_model=list[PositionResponse],
    summary="Virtual positions",
    responses={404: {"description": "Unknown profile"}},
)
async def get_positions(
    profiles: SimulationProfileRepositoryDep,
    repository: PaperTradingRepositoryDep,
    name: str = Path(description="Profile name."),
    status: PositionStatus | None = Query(default=None, description="Filter by status."),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[PositionResponse]:
    """Positions for one profile, newest first."""
    profile = await profiles.get_profile(name)
    rows = await repository.positions(_require_id(profile), status=status, limit=limit)
    return [PositionResponse.model_validate(r) for r in rows]


@router.get(
    "/{name}/orders",
    response_model=list[OrderResponse],
    summary="Virtual orders, including rejections",
    responses={404: {"description": "Unknown profile"}},
)
async def get_orders(
    profiles: SimulationProfileRepositoryDep,
    repository: PaperTradingRepositoryDep,
    name: str = Path(description="Profile name."),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[OrderResponse]:
    """Order history.

    Rejections are included: "what did this portfolio try to do, and why could it
    not" is a property of the strategy worth being able to query.
    """
    profile = await profiles.get_profile(name)
    rows = await repository.orders(_require_id(profile), limit=limit)
    return [OrderResponse.model_validate(r) for r in rows]


@router.get(
    "/{name}/trades",
    response_model=list[TradeResponse],
    summary="Completed round trips",
    responses={404: {"description": "Unknown profile"}},
)
async def get_trades(
    profiles: SimulationProfileRepositoryDep,
    repository: PaperTradingRepositoryDep,
    name: str = Path(description="Profile name."),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[TradeResponse]:
    """Closed trades with their full cost breakdown."""
    profile = await profiles.get_profile(name)
    rows = await repository.trades(_require_id(profile), limit=limit)
    return [TradeResponse.model_validate(r) for r in rows]


@router.get(
    "/{name}/performance",
    response_model=PerformanceResponse,
    summary="Derived performance summary",
    responses={404: {"description": "Unknown profile"}},
)
async def get_performance(
    profiles: SimulationProfileRepositoryDep,
    repository: PaperTradingRepositoryDep,
    name: str = Path(description="Profile name."),
) -> PerformanceResponse:
    """Performance for one portfolio.

    On synthetic data this measures accounting correctness, not profitability.
    """
    profile = await profiles.get_profile(name)
    portfolio = await repository.get_portfolio(_require_id(profile))
    valuation = await _valued(repository, portfolio)
    trades = await repository.trades(_require_id(profile), limit=500)
    snapshots = await repository.snapshots(_require_id(profile))

    summary = summarise(
        profile_name=profile.name,
        portfolio=portfolio,
        trades=trades,
        snapshots=snapshots,
        open_position_count=valuation.open_position_count,
        unrealized_pnl=valuation.unrealized_pnl,
    )
    return PerformanceResponse(
        profile_name=summary.profile_name,
        currency=summary.currency,
        starting_capital=summary.starting_capital,
        ending_equity=summary.ending_equity,
        net_pnl=summary.net_pnl,
        return_pct=summary.return_pct,
        realized_pnl=summary.realized_pnl,
        unrealized_pnl=summary.unrealized_pnl,
        trade_count=summary.trade_count,
        winning_trades=summary.winning_trades,
        losing_trades=summary.losing_trades,
        breakeven_trades=summary.breakeven_trades,
        win_rate=summary.win_rate,
        profit_factor=summary.profit_factor,
        average_winner=summary.average_winner,
        average_loser=summary.average_loser,
        total_fees=summary.total_fees,
        total_spread_cost=summary.total_spread_cost,
        total_slippage_cost=summary.total_slippage_cost,
        total_costs=summary.total_costs,
        cost_drag_pct=summary.cost_drag_pct,
        max_drawdown=summary.max_drawdown,
        peak_equity=summary.peak_equity,
        open_position_count=summary.open_position_count,
        bars_processed=summary.bars_processed,
        halted_reason=summary.halted_reason,
    )


@overview_router.get(
    "/overview",
    response_model=OverviewResponse,
    summary="All virtual portfolios side by side",
)
async def get_overview(
    profiles: SimulationProfileRepositoryDep,
    repository: PaperTradingRepositoryDep,
) -> OverviewResponse:
    """Every portfolio's headline numbers.

    The most useful view of the whole system: the same signals, nine outcomes.
    """
    configured = {p.id: p for p in await profiles.list_profiles(enabled_only=False)}
    rows: list[OverviewRow] = []

    for portfolio in await repository.list_portfolios():
        profile = configured.get(portfolio.simulation_profile_id)
        if profile is None:
            continue
        valuation = await _valued(repository, portfolio)
        equity = valuation.equity
        rows.append(
            OverviewRow(
                profile_name=profile.name,
                currency=portfolio.currency,
                initial_capital=portfolio.initial_capital,
                equity=equity,
                return_pct=(
                    float((equity - portfolio.initial_capital) / portfolio.initial_capital) * 100.0
                    if portfolio.initial_capital > 0
                    else 0.0
                ),
                realized_pnl=portfolio.realized_pnl,
                open_position_count=valuation.open_position_count,
                trade_count=portfolio.trade_count,
                total_costs=(
                    portfolio.total_fees
                    + portfolio.total_spread_cost
                    + portfolio.total_slippage_cost
                ),
                max_drawdown=portfolio.max_drawdown,
                halted_reason=portfolio.halted_reason,
            )
        )

    return OverviewResponse(count=len(rows), portfolios=rows)


def _portfolio_response(
    profile: SimulationProfileConfig,
    portfolio: VirtualPortfolio,
    valuation: PortfolioValuation,
) -> PortfolioResponse:
    return PortfolioResponse(
        simulation_profile_id=portfolio.simulation_profile_id,
        profile_name=profile.name,
        currency=portfolio.currency,
        initial_capital=portfolio.initial_capital,
        cash=portfolio.cash,
        positions_value=valuation.positions_value,
        equity=valuation.equity,
        realized_pnl=portfolio.realized_pnl,
        unrealized_pnl=valuation.unrealized_pnl,
        open_position_count=valuation.open_position_count,
        gross_exposure=valuation.gross_exposure,
        net_exposure=valuation.net_exposure,
        peak_equity=portfolio.peak_equity,
        drawdown=valuation.drawdown,
        max_drawdown=portfolio.max_drawdown,
        total_fees=portfolio.total_fees,
        total_spread_cost=portfolio.total_spread_cost,
        total_slippage_cost=portfolio.total_slippage_cost,
        trade_count=portfolio.trade_count,
        bars_processed=portfolio.bars_processed,
        halted_reason=portfolio.halted_reason,
    )


def _require_id(profile: SimulationProfileConfig) -> int:
    if profile.id is None:
        msg = f"profile {profile.name!r} is not persisted"
        raise ValueError(msg)
    return profile.id
