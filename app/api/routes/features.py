"""Feature endpoints."""

from __future__ import annotations

import math
from datetime import datetime

from fastapi import APIRouter, Path, Query

from app.api.deps import FeatureServiceDep
from app.api.schemas.feature import (
    FeatureDefinition,
    FeatureRow,
    FeatureSeriesResponse,
    FeatureSnapshotResponse,
)
from app.domain.enums import PriceSeriesAdjustment, Timeframe
from app.features.engine import FeatureEngine
from app.features.frame import CLOSE, TIMESTAMP
from app.features.service import DEFAULT_ADJUSTMENT

router = APIRouter(prefix="/instruments", tags=["features"])


def _definitions(timeframe: Timeframe) -> list[FeatureDefinition]:
    """Definitions of every feature in the baseline set for ``timeframe``."""
    engine = FeatureEngine.for_timeframe(timeframe)
    return [
        FeatureDefinition(
            name=spec.name,
            description=spec.description,
            warmup_bars=spec.warmup_bars,
            tags=sorted(spec.tags),
        )
        for spec in engine.feature_set
    ]


@router.get(
    "/{symbol}/features",
    response_model=FeatureSeriesResponse,
    summary="Computed feature series",
    responses={
        404: {"description": "Unknown symbol"},
        422: {"description": "Not enough history to warm up the features"},
    },
)
async def get_features(
    service: FeatureServiceDep,
    symbol: str = Path(description="Ticker, case-insensitive.", min_length=1, max_length=32),
    timeframe: Timeframe = Query(default=Timeframe.D1),
    bars: int = Query(default=60, ge=1, le=2000, description="Bars of output to return."),
    as_of: datetime | None = Query(
        default=None,
        description=(
            "Compute as the world looked at this instant. Bars at or after it are "
            "excluded at query level, so the result contains no look-ahead."
        ),
    ),
    adjustment: PriceSeriesAdjustment = Query(
        default=DEFAULT_ADJUSTMENT,
        description=(
            "Which price series to compute on. SPLIT_ADJUSTED (default) removes "
            "split discontinuities that would otherwise read as real returns. "
            "RAW is what actually traded. TOTAL_RETURN is not implemented."
        ),
    ),
) -> FeatureSeriesResponse:
    """Feature values per bar.

    Warm-up bars are fetched on top of ``bars``, so the caller does not need to
    know each feature's history requirement. A ``null`` value means the feature
    had not warmed up at that bar -- it does not mean zero.
    """
    result = await service.compute(
        symbol=symbol, timeframe=timeframe, bars=bars, as_of=as_of, adjustment=adjustment
    )

    # Return only the most recent `bars` rows; the rest were warm-up.
    tail = result.frame.tail(bars)
    rows = [
        FeatureRow(
            timestamp=record[TIMESTAMP],
            close=float(record[CLOSE]),
            values={name: _clean(record[name]) for name in result.feature_names},
        )
        for record in tail.iter_rows(named=True)
    ]
    return FeatureSeriesResponse(
        symbol=result.instrument.symbol,
        timeframe=timeframe,
        count=len(rows),
        definitions=_definitions(timeframe),
        rows=rows,
    )


@router.get(
    "/{symbol}/features/latest",
    response_model=FeatureSnapshotResponse,
    summary="Feature values at the most recent bar",
    responses={
        404: {"description": "Unknown symbol"},
        422: {"description": "Not enough history to warm up the features"},
    },
)
async def get_latest_features(
    service: FeatureServiceDep,
    symbol: str = Path(description="Ticker, case-insensitive.", min_length=1, max_length=32),
    timeframe: Timeframe = Query(default=Timeframe.D1),
    as_of: datetime | None = Query(default=None, description="Evaluate as of this instant."),
    adjustment: PriceSeriesAdjustment = Query(
        default=DEFAULT_ADJUSTMENT,
        description=(
            "Which price series to compute on. SPLIT_ADJUSTED (default) removes "
            "split discontinuities that would otherwise read as real returns. "
            "RAW is what actually traded. TOTAL_RETURN is not implemented."
        ),
    ),
) -> FeatureSnapshotResponse:
    """A single fully warmed-up feature row."""
    instrument, snapshot = await service.snapshot(
        symbol=symbol, timeframe=timeframe, as_of=as_of, adjustment=adjustment
    )
    return FeatureSnapshotResponse(
        symbol=instrument.symbol,
        timeframe=timeframe,
        timestamp=snapshot.timestamp,
        close=snapshot.close,
        bars_used=snapshot.bars_used,
        definitions=_definitions(timeframe),
        values=dict(snapshot.values),
    )


def _clean(value: object) -> float | None:
    """Polars cell to JSON-safe float. NaN/inf become null, not invalid JSON."""
    if value is None:
        return None
    number = float(value)  # type: ignore[arg-type]
    if math.isnan(number) or math.isinf(number):
        return None
    return number
