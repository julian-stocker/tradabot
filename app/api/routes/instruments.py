"""Instrument endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Path, Query

from app.api.deps import InstrumentServiceDep
from app.api.schemas.common import Page, PageMeta
from app.api.schemas.instrument import InstrumentResponse
from app.domain.enums import AssetType

router = APIRouter(prefix="/instruments", tags=["instruments"])


@router.get(
    "",
    response_model=Page[InstrumentResponse],
    summary="List instruments",
)
async def list_instruments(
    service: InstrumentServiceDep,
    exchange: str | None = Query(default=None, description="Filter by venue, e.g. XNAS."),
    asset_type: AssetType | None = Query(default=None, description="Filter by asset type."),
    include_inactive: bool = Query(
        default=False,
        description=(
            "Include delisted/inactive instruments. Required for unbiased "
            "historical studies -- excluding them causes survivorship bias."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Page[InstrumentResponse]:
    """Paginated instrument universe."""
    rows = await service.list_instruments(
        exchange=exchange,
        asset_type=asset_type,
        active_only=not include_inactive,
        limit=limit,
        offset=offset,
    )
    items = [InstrumentResponse.model_validate(row) for row in rows]
    return Page(items=items, meta=PageMeta(limit=limit, offset=offset, count=len(items)))


@router.get(
    "/{symbol}",
    response_model=InstrumentResponse,
    summary="Get one instrument",
    responses={404: {"description": "Unknown symbol"}},
)
async def get_instrument(
    service: InstrumentServiceDep,
    symbol: str = Path(description="Ticker, case-insensitive.", min_length=1, max_length=32),
) -> InstrumentResponse:
    """Instrument metadata for one symbol."""
    return InstrumentResponse.model_validate(await service.get_instrument(symbol))
