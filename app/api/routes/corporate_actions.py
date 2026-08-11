"""Corporate-action and universe endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Path, Query

from app.api.deps import (
    CorporateActionRepositoryDep,
    InstrumentServiceDep,
    UniverseServiceDep,
)
from app.api.schemas.corporate_action import (
    CorporateActionListResponse,
    CorporateActionResponse,
    UniverseMemberResponse,
    UniverseResponse,
)
from app.core.time import utc_now
from app.corporate_actions.models import CorporateAction
from app.domain.enums import AssetType

router = APIRouter(tags=["corporate-actions"])


@router.get(
    "/instruments/{symbol}/corporate-actions",
    response_model=CorporateActionListResponse,
    summary="Corporate actions for an instrument",
    responses={404: {"description": "Unknown symbol"}},
)
async def get_corporate_actions(
    instruments: InstrumentServiceDep,
    actions: CorporateActionRepositoryDep,
    symbol: str = Path(description="Ticker, case-insensitive.", min_length=1, max_length=32),
    known_as_of: datetime | None = Query(
        default=None,
        description=(
            "Restrict to actions already effective at this instant, for "
            "point-in-time reconstruction."
        ),
    ),
) -> CorporateActionListResponse:
    """Splits, dividends and other actions, ascending by effective time."""
    instrument = await instruments.get_instrument(symbol)
    rows = await actions.list_for_instrument(
        instrument_id=instrument.id, symbol=instrument.symbol, known_as_of=known_as_of
    )
    return CorporateActionListResponse(
        symbol=instrument.symbol,
        count=len(rows),
        actions=[_to_response(a) for a in rows],
    )


@router.get(
    "/universe",
    response_model=UniverseResponse,
    summary="Instruments tradable at a point in time",
)
async def get_universe(
    service: UniverseServiceDep,
    as_of: datetime | None = Query(
        default=None, description="Instruments tradable at this instant."
    ),
    start: datetime | None = Query(
        default=None, description="Window start; use with `end` for an overlap query."
    ),
    end: datetime | None = Query(default=None, description="Window end (exclusive)."),
    exchange: str | None = Query(default=None, description="Filter by venue."),
    asset_type: AssetType | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=1000),
) -> UniverseResponse:
    """The universe as it was, not as it is.

    Supply either ``as_of`` for a single instant, or ``start`` and ``end`` for
    every instrument tradable during any part of a window. Supplying nothing
    defaults to *now*, which is the only case where "the current universe" is the
    right answer.

    Backtests must use this rather than the instrument list: a strategy evaluated
    on today's survivors has been given ten years of hindsight.
    """
    if start is not None and end is not None:
        rows = await service.active_between(start, end, exchange=exchange, limit=limit)
        return UniverseResponse(
            start=start,
            end=end,
            count=len(rows),
            instruments=[UniverseMemberResponse.model_validate(r) for r in rows],
        )

    moment = as_of or utc_now()
    rows = await service.tradable_at(moment, exchange=exchange, asset_type=asset_type, limit=limit)
    return UniverseResponse(
        as_of=moment,
        count=len(rows),
        instruments=[UniverseMemberResponse.model_validate(r) for r in rows],
    )


def _to_response(action: CorporateAction) -> CorporateActionResponse:
    is_split = action.from_shares is not None and action.to_shares is not None
    return CorporateActionResponse(
        action_type=action.action_type,
        effective_at=action.effective_at,
        payment_at=action.payment_at,
        from_shares=action.from_shares,
        to_shares=action.to_shares,
        split_ratio=action.split_ratio if is_split else None,
        cash_amount=action.cash_amount,
        currency=action.currency,
        source=action.source,
        description=action.describe(),
    )
