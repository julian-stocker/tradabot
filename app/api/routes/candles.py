"""Candle and quote endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Path, Query

from app.api.deps import MarketDataServiceDep
from app.api.schemas.candle import CandleResponse, CandleSeriesResponse, QuoteResponse
from app.core.time import utc_now
from app.domain.enums import Timeframe

router = APIRouter(prefix="/instruments", tags=["market-data"])


@router.get(
    "/{symbol}/candles",
    response_model=CandleSeriesResponse,
    summary="Stored OHLCV candles",
    responses={404: {"description": "Unknown symbol"}},
)
async def get_candles(
    service: MarketDataServiceDep,
    symbol: str = Path(description="Ticker, case-insensitive.", min_length=1, max_length=32),
    timeframe: Timeframe = Query(default=Timeframe.D1),
    start: datetime | None = Query(
        default=None, description="Inclusive window start (UTC, ISO-8601)."
    ),
    end: datetime | None = Query(default=None, description="Exclusive window end (UTC, ISO-8601)."),
    limit: int = Query(default=1000, ge=1, le=50_000),
) -> CandleSeriesResponse:
    """Candles for ``[start, end)``, ascending.

    The half-open window means consecutive requests tile without duplicating the
    boundary bar. Timestamps are bar *open* times.
    """
    instrument, rows = await service.get_candles(
        symbol=symbol, timeframe=timeframe, start=start, end=end, limit=limit
    )
    candles = [CandleResponse.model_validate(row) for row in rows]
    return CandleSeriesResponse(
        symbol=instrument.symbol,
        timeframe=timeframe,
        start=candles[0].timestamp if candles else (start or utc_now()),
        end=candles[-1].timestamp if candles else (end or utc_now()),
        count=len(candles),
        candles=candles,
    )


@router.get(
    "/{symbol}/quote",
    response_model=QuoteResponse,
    summary="Latest quote with spread metrics",
    responses={404: {"description": "Unknown symbol"}, 502: {"description": "Provider error"}},
)
async def get_quote(
    service: MarketDataServiceDep,
    symbol: str = Path(description="Ticker, case-insensitive.", min_length=1, max_length=32),
) -> QuoteResponse:
    """Live top-of-book quote and the spread metrics derived from it."""
    instrument, quote = await service.get_latest_quote(symbol)
    return QuoteResponse(
        symbol=instrument.symbol,
        timestamp=quote.timestamp,
        bid=quote.bid,
        ask=quote.ask,
        bid_size=quote.bid_size,
        ask_size=quote.ask_size,
        mid_price=quote.mid_price,
        spread_absolute=quote.spread_absolute,
        spread_percent=quote.spread_percent,
        spread_bps=quote.spread_bps,
    )
