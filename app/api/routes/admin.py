"""Administrative endpoints.

Data ingestion is exposed over HTTP for convenience during local development.
There is no authentication, because tradabot is a local-first single-user tool.
**Do not expose this service to a network you do not control** -- see
docs/architecture.md#security.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import IngestionServiceDep
from app.domain.enums import Timeframe

router = APIRouter(prefix="/admin", tags=["admin"])


class SyncResponse(BaseModel):
    """Result of an ingestion run."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = Field(description="False if any symbol failed.")
    instruments_synced: int
    corporate_actions_written: int = Field(
        description=(
            "Zero means the provider supplies no corporate-action data, which is "
            "NOT the same as the universe having had none -- adjusted series would "
            "then silently equal raw ones."
        )
    )
    candles_written: int
    symbols_succeeded: list[str]
    symbols_failed: list[dict[str, str]] = Field(
        description="Per-symbol failures as {symbol, error}; failures are reported, not hidden."
    )


@router.post(
    "/sync",
    response_model=SyncResponse,
    summary="Ingest instruments and candles from the active provider",
)
async def sync(
    service: IngestionServiceDep,
    timeframe: Timeframe = Query(default=Timeframe.D1),
    symbols: str | None = Query(
        default=None, description="Comma-separated tickers. Omit to sync the whole universe."
    ),
    start: datetime | None = Query(default=None, description="Window start (UTC)."),
    end: datetime | None = Query(default=None, description="Window end (UTC)."),
) -> SyncResponse:
    """Pull data from the configured provider into the database.

    Idempotent: candles are upserted on ``(instrument, timeframe, timestamp)``, so
    re-running over an overlapping window is safe.
    """
    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else None

    report = await service.sync_all(timeframe=timeframe, symbols=symbol_list, start=start, end=end)
    return SyncResponse(
        ok=report.ok,
        instruments_synced=report.instruments_synced,
        corporate_actions_written=report.corporate_actions_written,
        candles_written=report.candles_written,
        symbols_succeeded=list(report.symbols_succeeded),
        symbols_failed=[{"symbol": s, "error": e} for s, e in report.symbols_failed],
    )
