"""Backtest and research endpoints.

**Read-only, and deliberately so.** There is no endpoint that starts a backtest.
A 52-symbol replay reads tens of thousands of candles and writes thousands of
rows; exposing it over unauthenticated HTTP would be a way to saturate the
database from a browser tab, and it would race the scheduler for the same write
lock. Backtests are started from the CLI, where the operator is present.

**No credentials of any kind appear in these responses.**
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import SessionDep
from app.core.logging import get_logger
from app.domain.enums import Horizon
from app.research.analytics import (
    SCORE_BANDS,
    THRESHOLD_BANDS,
    by_score_band,
    load_observations,
)
from app.research.repository import BacktestRunRepository, OutcomeRepository

router = APIRouter(tags=["research"])
logger = get_logger(__name__)

MAX_LIMIT = 50


class BacktestRunSummary(BaseModel):
    """One historical run. Configuration and versions, no secrets."""

    model_config = ConfigDict(extra="forbid")

    id: int
    run_key: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    from_timestamp: datetime
    to_timestamp: datetime
    primary_timeframe: str
    regular_session_only: bool
    observation_count: int
    symbols_processed: int
    duration_seconds: float | None


class BacktestRunDetail(BacktestRunSummary):
    """A run plus its universe, versions and metrics."""

    universe_definition: dict[str, Any]
    versions: dict[str, str]
    metrics: dict[str, Any]
    error: str | None


class ScoreBandRow(BaseModel):
    """One score band's outcome statistics."""

    model_config = ConfigDict(extra="forbid")

    label: str
    n: int
    mean_return: float | None
    median_return: float | None
    positive_rate: float | None
    mean_mfe: float | None
    mean_mae: float | None
    reportable: bool = Field(
        description="False when the sample is too small to quote without a caveat."
    )


class ScoreCalibrationResponse(BaseModel):
    """Outcome quality by score band.

    Descriptive only. This endpoint exists to *measure* whether outcome quality
    rises with score; it is not a basis for moving the 75/85 thresholds, and
    tuning them against this data would make any later validation circular.
    """

    model_config = ConfigDict(extra="forbid")

    horizon: str
    observations: int
    bands: list[ScoreBandRow]
    caveat: str = Field(
        default=(
            "Descriptive statistics over historical observations. Not a claim of "
            "statistical significance, and not a basis for changing thresholds."
        )
    )


class OutcomeStatusResponse(BaseModel):
    """How much of the dataset is actually labelled."""

    model_config = ConfigDict(extra="forbid")

    total: int
    by_status: dict[str, int]
    complete_by_horizon: dict[str, int]
    pending: int


@router.get("/backtests", response_model=list[BacktestRunSummary])
async def list_backtests(
    session: SessionDep, limit: int = Query(default=20, ge=1, le=MAX_LIMIT)
) -> list[BacktestRunSummary]:
    runs = await BacktestRunRepository(session).list_recent(limit)
    return [_summary(run) for run in runs]


@router.get("/backtests/{run_id}", response_model=BacktestRunDetail)
async def get_backtest(session: SessionDep, run_id: int) -> BacktestRunDetail:
    run = await BacktestRunRepository(session).get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="backtest run not found")
    return BacktestRunDetail(
        **_summary(run).model_dump(),
        universe_definition=run.universe_definition,
        versions={
            "engine": run.engine_version,
            "feature_set": run.feature_set_version,
            "signal_model": run.signal_model_version,
            "scanner_policy": run.scanner_policy_version,
            "cost_model": run.cost_model_version,
            "label_policy": run.label_policy_version,
        },
        metrics=run.metrics,
        error=run.error,
    )


@router.get("/research/score-calibration", response_model=ScoreCalibrationResponse)
async def score_calibration(
    session: SessionDep,
    horizon: Horizon = Query(default=Horizon.D1),
    run_id: int | None = Query(default=None),
    around_threshold: bool = Query(
        default=False, description="Use the finer bands around the 75 cutoff."
    ),
) -> ScoreCalibrationResponse:
    rows = await load_observations(session, horizon=horizon, backtest_run_id=run_id)
    bands = THRESHOLD_BANDS if around_threshold else SCORE_BANDS
    groups = by_score_band(rows, bands=bands)
    return ScoreCalibrationResponse(
        horizon=horizon.value,
        observations=len(rows),
        bands=[ScoreBandRow(**_band(group)) for group in groups],
    )


@router.get("/research/outcomes", response_model=OutcomeStatusResponse)
async def outcome_status(session: SessionDep) -> OutcomeStatusResponse:
    repo = OutcomeRepository(session)
    return OutcomeStatusResponse(
        total=await repo.total_count(),
        by_status=await repo.status_counts(),
        complete_by_horizon=await repo.horizon_counts(),
        pending=await repo.pending_count(),
    )


def _summary(run: Any) -> BacktestRunSummary:
    return BacktestRunSummary(
        id=run.id,
        run_key=run.run_key,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        from_timestamp=run.from_timestamp,
        to_timestamp=run.to_timestamp,
        primary_timeframe=run.primary_timeframe,
        regular_session_only=run.regular_session_only,
        observation_count=run.observation_count,
        symbols_processed=run.symbols_processed,
        duration_seconds=run.duration_seconds,
    )


def _band(group: Any) -> dict[str, Any]:
    payload: dict[str, Any] = dict(group.as_dict())
    payload.pop("stdev_return", None)
    return payload
