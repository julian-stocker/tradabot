"""Signal endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Path, Query

from app.api.deps import SignalServiceDep
from app.api.schemas.signal import (
    ComponentScoreResponse,
    NetEdgeResponse,
    ReasonResponse,
    SignalResponse,
)
from app.domain.enums import Horizon, PriceSeriesAdjustment, Timeframe
from app.features.service import DEFAULT_ADJUSTMENT
from app.signals.models import SignalResult

router = APIRouter(prefix="/instruments", tags=["signals"])


@router.get(
    "/{symbol}/signal",
    response_model=SignalResponse,
    summary="Explainable rule-based signal",
    responses={
        404: {"description": "Unknown symbol"},
        422: {"description": "Not enough history to warm up the features"},
    },
)
async def get_signal(
    service: SignalServiceDep,
    symbol: str = Path(description="Ticker, case-insensitive.", min_length=1, max_length=32),
    timeframe: Timeframe = Query(default=Timeframe.D1, description="Candle interval to analyse."),
    horizon: Horizon = Query(default=Horizon.D5, description="Forecast horizon."),
    as_of: datetime | None = Query(
        default=None,
        description=(
            "Evaluate as the world looked at this instant. Later bars are excluded "
            "at query level, and the configured default spread is used instead of a "
            "live quote -- today's spread was not knowable then."
        ),
    ),
    adjustment: PriceSeriesAdjustment = Query(
        default=DEFAULT_ADJUSTMENT,
        description=(
            "Price series the features are computed from. SPLIT_ADJUSTED (default) "
            "prevents a split being scored as a real price move."
        ),
    ),
) -> SignalResponse:
    """Score one instrument, with the evidence behind the score.

    The score is a heuristic blend in ``[-100, 100]``, not a return forecast or a
    probability. Check ``net_edge.is_actionable`` before treating a directional
    reading as an opportunity: a bullish signal whose expected move is smaller
    than its round-trip cost is not one.
    """
    result = await service.evaluate(
        symbol=symbol, timeframe=timeframe, horizon=horizon, as_of=as_of, adjustment=adjustment
    )
    return _to_response(result, adjustment)


def _to_response(result: SignalResult, adjustment: PriceSeriesAdjustment) -> SignalResponse:
    """Map the domain result onto the wire schema."""
    return SignalResponse(
        symbol=result.symbol,
        timestamp=result.timestamp,
        generated_at=result.generated_at,
        timeframe=result.timeframe,
        horizon=result.horizon,
        horizon_bucket=result.horizon.bucket,
        price_adjustment=adjustment,
        score=result.score,
        classification=result.classification,
        confidence=result.confidence,
        is_actionable=result.is_actionable,
        reasons=[ReasonResponse.model_validate(r) for r in result.reasons],
        risks=[ReasonResponse.model_validate(r) for r in result.risks],
        components=[
            ComponentScoreResponse(
                name=c.name,
                kind=c.kind,
                score=c.score,
                weight=c.weight,
                configured_weight=c.configured_weight,
                contribution=c.contribution,
                available=c.available,
                reasons=[ReasonResponse.model_validate(r) for r in c.reasons],
            )
            for c in result.components
        ],
        reference_price=result.reference_price,
        spread_bps=result.spread_bps,
        net_edge=NetEdgeResponse(
            expected_move_bps=result.net_edge.expected_move_bps,
            cost_bps=result.net_edge.cost_bps,
            net_edge_bps=result.net_edge.net_edge_bps,
            is_actionable=result.net_edge.is_actionable,
            cost_coverage_ratio=result.net_edge.cost_coverage_ratio,
        ),
        feature_snapshot=result.feature_snapshot,
        bars_used=result.bars_used,
        engine_version=result.engine_version,
    )
