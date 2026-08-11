"""Scanner status, candidates and signal history.

Read-only. Nothing here starts a scan: a scan is a scheduled operation with a
database lease, and an HTTP request that could trigger one would be an
unauthenticated way to exhaust provider quota.

**No credentials of any kind appear in these responses.**
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import SessionDep, SettingsDep
from app.core.logging import get_logger
from app.core.time import utc_now
from app.instruments.repository import InstrumentRepository
from app.market_data.calendars import get_trading_calendar
from app.scanner.repository import (
    ScanRunRepository,
    SignalEvaluationRepository,
    TrackedSignalRepository,
    WatchlistRepository,
    evaluation_payload,
)
from app.scanner.service import to_ranked
from app.scanner.sessions import next_session_open, session_phase

router = APIRouter(tags=["scanner"])
logger = get_logger(__name__)

MAX_LIMIT = 50


class ScannerStatusResponse(BaseModel):
    """Configuration and last-run state."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    watchlist_size: int

    session_phase: str
    accepts_new_signals: bool = Field(
        description="Whether the current session phase permits a new qualification."
    )
    next_session_open: datetime | None = None

    scan_interval_minutes: int
    market_sync_interval_minutes: int

    last_scan_started: datetime | None = None
    last_scan_completed: datetime | None = None
    last_scan_duration_seconds: float | None = None
    last_scan_status: str | None = None
    last_success: datetime | None = None
    last_error: str | None = Field(default=None, description="Redacted before storage.")

    current_qualified_signals: int = 0
    current_strong_signals: int = 0
    evaluations_stored: int = 0

    signal_threshold: float = Field(
        description="Operational heuristic controlling notification volume. Not a probability."
    )
    strong_signal_threshold: float

    checked_at: datetime


class CandidateResponse(BaseModel):
    """One ranked candidate, with the arithmetic behind its rank."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    evaluation_id: int | None
    tracked_signal_id: int | None
    score: float
    confidence: float
    agreement: float
    direction: int
    net_edge_bps: float | None
    spread_bps: float | None
    rank_score: float
    contributions: dict[str, float] = Field(
        description="Each component's weighted contribution, so the ordering is auditable."
    )


class CandidatesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[CandidateResponse]
    total_qualified: int
    threshold: float
    generated_at: datetime
    note: str = Field(
        default=(
            "Ranking orders candidates that already cleared a threshold. It is not "
            "a probability and does not claim the top candidate is more likely to work."
        )
    )


class TrackedSignalResponse(BaseModel):
    """A continuing setup's identity and lifecycle."""

    model_config = ConfigDict(extra="forbid")

    id: int
    symbol: str
    direction: str
    primary_timeframe: str
    horizon: str
    setup: str
    lifecycle: str

    current_score: float
    peak_score: float
    current_confidence: float | None
    evaluation_count: int

    discovered_at: datetime
    last_evaluated_at: datetime
    qualified_at: datetime | None
    strong_at: datetime | None
    weakened_at: datetime | None
    invalidated_at: datetime | None
    expired_at: datetime | None


@router.get("/scanner/status", response_model=ScannerStatusResponse, summary="Scanner status")
async def scanner_status(session: SessionDep, settings: SettingsDep) -> ScannerStatusResponse:
    """Configuration, session phase and last-run state."""
    now = utc_now()
    calendar = get_trading_calendar(settings.market_data.default_exchange)
    phase = session_phase(calendar, now)

    runs = ScanRunRepository(session)
    latest = await runs.latest()
    success = await runs.latest_successful()
    signals = TrackedSignalRepository(session)
    qualified = await signals.qualified_signals(limit=500)

    return ScannerStatusResponse(
        enabled=settings.scanner.enabled,
        watchlist_size=await WatchlistRepository(session).count(),
        session_phase=phase.value,
        accepts_new_signals=phase.is_tradable or not settings.scanner.require_regular_session,
        next_session_open=next_session_open(calendar, now),
        scan_interval_minutes=settings.scanner.scan_interval_minutes,
        market_sync_interval_minutes=settings.scanner.market_sync_interval_minutes,
        last_scan_started=latest.started_at if latest else None,
        last_scan_completed=latest.completed_at if latest else None,
        last_scan_duration_seconds=latest.duration_seconds if latest else None,
        last_scan_status=latest.status if latest else None,
        last_success=success.started_at if success else None,
        last_error=latest.error if latest else None,
        current_qualified_signals=sum(1 for s in qualified if s.lifecycle == "QUALIFIED"),
        current_strong_signals=sum(1 for s in qualified if s.lifecycle == "STRONG"),
        evaluations_stored=await SignalEvaluationRepository(session).count(),
        signal_threshold=settings.notifications.signal_threshold,
        strong_signal_threshold=settings.notifications.strong_signal_threshold,
        checked_at=now,
    )


@router.get("/scanner/candidates", response_model=CandidatesResponse, summary="Ranked candidates")
async def scanner_candidates(
    session: SessionDep,
    settings: SettingsDep,
    limit: int = Query(default=5, ge=1, le=MAX_LIMIT),
) -> CandidatesResponse:
    """Currently qualified candidates, ranked.

    An empty list is a valid and common answer. It means nothing currently meets
    the configured threshold -- not that the scanner is broken.
    """
    evaluations = await SignalEvaluationRepository(session).latest_per_instrument(
        qualified_only=True
    )
    instruments = InstrumentRepository(session)

    ranked = []
    for evaluation in evaluations:
        instrument = await instruments.get_by_id(evaluation.instrument_id)
        if instrument is None:  # pragma: no cover -- FK guarantees this
            continue
        ranked.append(to_ranked(evaluation, instrument.symbol))

    ranked.sort(key=lambda c: (-c.rank_score, c.symbol))
    return CandidatesResponse(
        candidates=[CandidateResponse(**_candidate_fields(c)) for c in ranked[:limit]],
        total_qualified=len(ranked),
        threshold=settings.notifications.signal_threshold,
        generated_at=utc_now(),
    )


@router.get(
    "/signals/active",
    response_model=list[TrackedSignalResponse],
    summary="Active tracked signals",
)
async def active_signals(
    session: SessionDep, limit: int = Query(default=25, ge=1, le=MAX_LIMIT * 4)
) -> list[TrackedSignalResponse]:
    """Setups still being tracked, highest score first."""
    signals = await TrackedSignalRepository(session).active_signals(limit=limit)
    instruments = InstrumentRepository(session)
    return [await _signal_response(s, instruments) for s in signals]


@router.get(
    "/signals/{signal_id}",
    response_model=TrackedSignalResponse,
    summary="One tracked signal",
)
async def tracked_signal(session: SessionDep, signal_id: int) -> TrackedSignalResponse:
    signal = await TrackedSignalRepository(session).get(signal_id)
    if signal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"signal {signal_id} not found"
        )
    return await _signal_response(signal, InstrumentRepository(session))


@router.get(
    "/signals/{signal_id}/evaluations",
    response_model=list[dict[str, Any]],
    summary="A signal's evaluation history",
)
async def signal_evaluations(
    session: SessionDep, signal_id: int, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict[str, Any]]:
    """Every observation recorded for this setup, newest first.

    This is the X history: what tradabot knew at each point. Outcome labels are
    phase 5's and live in a separate table -- nothing returned here is derived
    from the future.
    """
    signal = await TrackedSignalRepository(session).get(signal_id)
    if signal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"signal {signal_id} not found"
        )
    evaluations = await SignalEvaluationRepository(session).for_signal(signal_id, limit=limit)
    return [evaluation_payload(e) for e in evaluations]


def _candidate_fields(candidate: Any) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "evaluation_id": candidate.evaluation_id,
        "tracked_signal_id": candidate.tracked_signal_id,
        "score": candidate.score,
        "confidence": candidate.confidence,
        "agreement": candidate.agreement,
        "direction": candidate.direction,
        "net_edge_bps": candidate.net_edge_bps,
        "spread_bps": candidate.spread_bps,
        "rank_score": candidate.rank_score,
        "contributions": candidate.contributions,
    }


async def _signal_response(signal: Any, instruments: InstrumentRepository) -> TrackedSignalResponse:
    instrument = await instruments.get_by_id(signal.instrument_id)
    return TrackedSignalResponse(
        id=signal.id,
        symbol=instrument.symbol if instrument else "?",
        direction=signal.direction,
        primary_timeframe=signal.primary_timeframe,
        horizon=signal.horizon,
        setup=signal.setup,
        lifecycle=signal.lifecycle,
        current_score=signal.current_score,
        peak_score=signal.peak_score,
        current_confidence=signal.current_confidence,
        evaluation_count=signal.evaluation_count,
        discovered_at=signal.discovered_at,
        last_evaluated_at=signal.last_evaluated_at,
        qualified_at=signal.qualified_at,
        strong_at=signal.strong_at,
        weakened_at=signal.weakened_at,
        invalidated_at=signal.invalidated_at,
        expired_at=signal.expired_at,
    )
