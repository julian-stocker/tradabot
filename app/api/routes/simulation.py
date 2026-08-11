"""Simulation-profile endpoints."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import SimulationProfileRepositoryDep
from app.simulation.models import SimulationProfileConfig

router = APIRouter(prefix="/simulation", tags=["simulation"])


class RiskConfigResponse(BaseModel):
    """A named risk appetite, shared across portfolio sizes."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    risk_per_trade: Decimal
    max_position_percent: Decimal
    max_total_exposure: Decimal
    max_open_positions: int
    max_daily_loss: Decimal
    max_drawdown: Decimal
    min_signal_score: Decimal
    min_confidence: Decimal
    require_positive_net_edge: bool
    allow_short: bool

    # Execution policy (phase 3).
    stop_loss_atr_multiple: Decimal | None
    take_profit_r_multiple: Decimal | None
    max_holding_bars: int | None
    require_stop_loss: bool
    allow_pyramiding: bool
    max_quote_age_seconds: int


class BrokerCostConfigResponse(BaseModel):
    """Named broker cost assumptions. Illustrative, not calibrated."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    order_fee: Decimal
    variable_fee_rate: Decimal
    slippage_spread_multiple: Decimal
    default_spread_bps: Decimal
    min_order_notional: Decimal


class SimulationProfileResponse(BaseModel):
    """One virtual portfolio."""

    model_config = ConfigDict(extra="forbid")

    id: int | None
    name: str
    description: str
    initial_capital: Decimal
    currency: str
    enabled: bool
    risk: RiskConfigResponse
    costs: BrokerCostConfigResponse
    max_position_notional: Decimal = Field(
        description="initial_capital x max_position_percent -- where capital meets risk."
    )
    risk_budget: Decimal = Field(description="Currency at risk on a single trade.")


class SimulationProfileListResponse(BaseModel):
    """Configured portfolios.

    ``distinct_risk_profiles`` is reported alongside the count to make the
    normalisation visible: nine portfolios typically share three risk profiles,
    not nine copies of one.
    """

    model_config = ConfigDict(extra="forbid")

    count: int
    distinct_risk_profiles: int
    profiles: list[SimulationProfileResponse]


@router.get(
    "/profiles",
    response_model=SimulationProfileListResponse,
    summary="List simulation profiles",
)
async def list_profiles(
    repository: SimulationProfileRepositoryDep,
    include_disabled: bool = Query(default=False),
) -> SimulationProfileListResponse:
    """Every configured virtual portfolio with its risk and cost configuration."""
    profiles = await repository.list_profiles(enabled_only=not include_disabled)
    return SimulationProfileListResponse(
        count=len(profiles),
        distinct_risk_profiles=len({p.risk.name for p in profiles}),
        profiles=[_to_response(p) for p in profiles],
    )


@router.get(
    "/profiles/{name}",
    response_model=SimulationProfileResponse,
    summary="Get one simulation profile",
    responses={404: {"description": "Unknown profile"}},
)
async def get_profile(
    repository: SimulationProfileRepositoryDep,
    name: str = Path(description="Profile name, e.g. 500eur-balanced."),
) -> SimulationProfileResponse:
    """One portfolio's full configuration."""
    return _to_response(await repository.get_profile(name))


def _to_response(profile: SimulationProfileConfig) -> SimulationProfileResponse:
    return SimulationProfileResponse(
        id=profile.id,
        name=profile.name,
        description=profile.description,
        initial_capital=profile.initial_capital,
        currency=profile.currency,
        enabled=profile.enabled,
        risk=RiskConfigResponse(**profile.risk.model_dump(exclude={"id"})),
        costs=BrokerCostConfigResponse(**profile.costs.model_dump(exclude={"id"})),
        max_position_notional=profile.max_position_notional,
        risk_budget=profile.risk_budget,
    )
