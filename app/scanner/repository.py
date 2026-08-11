"""Persistence for the watchlist, signal lifecycle, evaluations and scan leases."""

from __future__ import annotations

import os
import socket
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.time import utc_now
from app.db.models import (
    Instrument,
    ScanRun,
    SignalEvaluation,
    TrackedSignal,
    WatchlistEntry,
)
from app.scanner.enums import SignalLifecycle
from app.scanner.lifecycle import SignalIdentity

logger = get_logger(__name__)

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

SCOPE_SCAN = "scan"
SCOPE_SYNC = "sync"


class WatchlistRepository:
    """The instruments the scanner evaluates."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_entries(
        self, *, enabled_only: bool = True
    ) -> Sequence[tuple[WatchlistEntry, Instrument]]:
        """Watchlist rows with their instruments, highest priority first.

        Joined rather than lazily loaded: the scanner needs the symbol for every
        entry, and N+1 queries across fifty instruments is fifty round trips for
        data one join already has.
        """
        stmt = (
            select(WatchlistEntry, Instrument)
            .join(Instrument, Instrument.id == WatchlistEntry.instrument_id)
            .order_by(WatchlistEntry.priority.desc(), Instrument.symbol)
        )
        if enabled_only:
            stmt = stmt.where(WatchlistEntry.enabled.is_(True))
        rows = await self._session.execute(stmt)
        return [(entry, instrument) for entry, instrument in rows.all()]

    async def symbols(self, *, enabled_only: bool = True) -> list[str]:
        return [i.symbol for _, i in await self.list_entries(enabled_only=enabled_only)]

    async def get_entry(self, symbol: str) -> WatchlistEntry | None:
        stmt = (
            select(WatchlistEntry)
            .join(Instrument, Instrument.id == WatchlistEntry.instrument_id)
            .where(Instrument.symbol == symbol.upper())
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def add(
        self, instrument_id: int, *, priority: int = 0, tags: Sequence[str] = ()
    ) -> WatchlistEntry:
        """Add an instrument, or re-enable and update it if already present.

        Idempotent: seeding the universe twice must not fail, and re-adding a
        disabled symbol is the natural way to say "watch this again".
        """
        existing = (
            await self._session.execute(
                select(WatchlistEntry).where(WatchlistEntry.instrument_id == instrument_id)
            )
        ).scalar_one_or_none()

        now = utc_now()
        if existing is not None:
            existing.enabled = True
            existing.priority = priority
            existing.tags = list(tags)
            existing.updated_at = now
            await self._session.flush()
            return existing

        entry = WatchlistEntry(
            instrument_id=instrument_id,
            enabled=True,
            priority=priority,
            tags=list(tags),
            created_at=now,
            updated_at=now,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def set_enabled(self, symbol: str, *, enabled: bool) -> bool:
        """Enable or disable one symbol. Returns whether it was found.

        Disabling rather than deleting: a removed row loses the record that the
        instrument was ever watched, and every evaluation already stored for it
        becomes harder to interpret.
        """
        entry = await self.get_entry(symbol)
        if entry is None:
            return False
        entry.enabled = enabled
        entry.updated_at = utc_now()
        await self._session.flush()
        return True

    async def count(self, *, enabled_only: bool = True) -> int:
        stmt = select(WatchlistEntry.id)
        if enabled_only:
            stmt = stmt.where(WatchlistEntry.enabled.is_(True))
        return len((await self._session.execute(stmt)).scalars().all())


class TrackedSignalRepository:
    """Signal lifecycle rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_active(self, identity: SignalIdentity) -> TrackedSignal | None:
        """The active signal matching this identity, if one exists.

        All five identity fields must match. A change in any of them -- direction
        flipping, the structural premise breaking -- means this is a different
        idea and belongs to a different signal.
        """
        stmt = (
            select(TrackedSignal)
            .where(
                TrackedSignal.instrument_id == identity.instrument_id,
                TrackedSignal.direction == identity.direction,
                TrackedSignal.primary_timeframe == identity.primary_timeframe,
                TrackedSignal.horizon == identity.horizon,
                TrackedSignal.setup == identity.setup,
                TrackedSignal.lifecycle.notin_(
                    [SignalLifecycle.INVALIDATED.value, SignalLifecycle.EXPIRED.value]
                ),
            )
            .order_by(TrackedSignal.last_evaluated_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def create(
        self,
        *,
        identity: SignalIdentity,
        lifecycle: SignalLifecycle,
        score: float,
        confidence: float | None,
        now: datetime,
    ) -> TrackedSignal:
        signal = TrackedSignal(
            instrument_id=identity.instrument_id,
            direction=identity.direction,
            primary_timeframe=identity.primary_timeframe,
            horizon=identity.horizon,
            setup=identity.setup,
            lifecycle=lifecycle.value,
            current_score=score,
            peak_score=score,
            current_confidence=confidence,
            evaluation_count=1,
            discovered_at=now,
            last_evaluated_at=now,
            qualified_at=now if lifecycle is SignalLifecycle.QUALIFIED else None,
            strong_at=now if lifecycle is SignalLifecycle.STRONG else None,
        )
        self._session.add(signal)
        await self._session.flush()
        return signal

    async def apply_transition(
        self,
        signal: TrackedSignal,
        *,
        lifecycle: SignalLifecycle,
        score: float,
        confidence: float | None,
        now: datetime,
    ) -> TrackedSignal:
        """Advance a signal, stamping the first time it reached each state.

        Timestamps are written **once**: ``qualified_at`` records when the setup
        first qualified, not the last time it happened to be qualified. A signal
        oscillating around the threshold otherwise loses its own history.
        """
        signal.lifecycle = lifecycle.value
        signal.current_score = score
        signal.peak_score = max(signal.peak_score, score)
        signal.current_confidence = confidence
        signal.evaluation_count += 1
        signal.last_evaluated_at = now

        if lifecycle is SignalLifecycle.QUALIFIED and signal.qualified_at is None:
            signal.qualified_at = now
        elif lifecycle is SignalLifecycle.STRONG:
            if signal.qualified_at is None:
                signal.qualified_at = now
            if signal.strong_at is None:
                signal.strong_at = now
        elif lifecycle is SignalLifecycle.WEAKENED:
            signal.weakened_at = now
        elif lifecycle is SignalLifecycle.INVALIDATED and signal.invalidated_at is None:
            signal.invalidated_at = now

        await self._session.flush()
        return signal

    async def active_signals(self, *, limit: int = 100) -> Sequence[TrackedSignal]:
        stmt = (
            select(TrackedSignal)
            .where(
                TrackedSignal.lifecycle.notin_(
                    [SignalLifecycle.INVALIDATED.value, SignalLifecycle.EXPIRED.value]
                )
            )
            .order_by(TrackedSignal.current_score.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def qualified_signals(self, *, limit: int = 100) -> Sequence[TrackedSignal]:
        """Signals currently at or above qualification.

        WEAKENED is excluded: it is still active and still tracked, but it is by
        definition no longer what was announced, and listing it as a current
        opportunity would overstate what the scanner is claiming.
        """
        stmt = (
            select(TrackedSignal)
            .where(
                TrackedSignal.lifecycle.in_(
                    [SignalLifecycle.QUALIFIED.value, SignalLifecycle.STRONG.value]
                )
            )
            .order_by(TrackedSignal.current_score.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def get(self, signal_id: int) -> TrackedSignal | None:
        return await self._session.get(TrackedSignal, signal_id)

    async def expire_stale(self, *, older_than: datetime) -> int:
        """Expire active signals not evaluated since ``older_than``.

        EXPIRED, never INVALIDATED: the market did not reject these, the scanner
        stopped looking at them. Recording that distinction is what keeps a
        future label from partly describing tradabot's uptime.
        """
        stmt = (
            update(TrackedSignal)
            .where(
                TrackedSignal.last_evaluated_at < older_than,
                TrackedSignal.lifecycle.notin_(
                    [SignalLifecycle.INVALIDATED.value, SignalLifecycle.EXPIRED.value]
                ),
            )
            .values(lifecycle=SignalLifecycle.EXPIRED.value, expired_at=utc_now())
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        # CursorResult exposes rowcount; the base Result protocol does not.
        return int(getattr(result, "rowcount", 0) or 0)


class SignalEvaluationRepository:
    """Every observation the scanner made. **This is the ML dataset.**"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, evaluation: SignalEvaluation) -> SignalEvaluation:
        """Persist one evaluation.

        Called for *every* evaluated candidate -- qualified or not, notified or
        not. Recording only the interesting ones would leave a future model with
        no negatives, and a dataset of winners teaches survivorship.
        """
        self._session.add(evaluation)
        await self._session.flush()
        return evaluation

    async def latest_for_instrument(
        self, instrument_id: int, *, limit: int = 20
    ) -> Sequence[SignalEvaluation]:
        stmt = (
            select(SignalEvaluation)
            .where(SignalEvaluation.instrument_id == instrument_id)
            .order_by(SignalEvaluation.evaluated_at.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def for_signal(
        self, tracked_signal_id: int, *, limit: int = 100
    ) -> Sequence[SignalEvaluation]:
        stmt = (
            select(SignalEvaluation)
            .where(SignalEvaluation.tracked_signal_id == tracked_signal_id)
            .order_by(SignalEvaluation.evaluated_at.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def latest_per_instrument(
        self, *, qualified_only: bool = False
    ) -> Sequence[SignalEvaluation]:
        """The most recent evaluation for each instrument.

        Loads a bounded recent window and reduces in Python rather than using a
        window function: SQLite's support differs from PostgreSQL's, and the
        result set here is one row per watchlist entry, not a table scan.
        """
        stmt = select(SignalEvaluation).order_by(SignalEvaluation.evaluated_at.desc()).limit(2000)
        if qualified_only:
            stmt = stmt.where(SignalEvaluation.qualified.is_(True))
        rows = (await self._session.execute(stmt)).scalars().all()

        seen: dict[int, SignalEvaluation] = {}
        for row in rows:
            seen.setdefault(row.instrument_id, row)
        return list(seen.values())

    async def count(self) -> int:
        return len((await self._session.execute(select(SignalEvaluation.id))).scalars().all())

    async def count_since(self, moment: datetime) -> int:
        stmt = select(SignalEvaluation.id).where(SignalEvaluation.evaluated_at >= moment)
        return len((await self._session.execute(stmt)).scalars().all())


class ScanRunRepository:
    """Scan cycles and the leases that keep them from overlapping."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire_lease(
        self, *, scope: str = SCOPE_SCAN, lease_seconds: int, now: datetime | None = None
    ) -> ScanRun | None:
        """Take the lease for ``scope``, or return None if someone else holds it.

        Database-backed rather than in-process, so a second cron invocation, a
        restarted process or a second machine all contend for the same lease.

        An **expired** lease is taken over. That matters more than it looks: a
        process killed mid-cycle leaves a `running` row behind, and without
        expiry the scanner would be locked out until someone noticed and cleared
        it by hand -- which is exactly the moment nobody is watching.
        """
        now = now or utc_now()

        held = (
            await self._session.execute(
                select(ScanRun)
                .where(
                    ScanRun.scope == scope,
                    ScanRun.status == STATUS_RUNNING,
                    ScanRun.lease_expires_at > now,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if held is not None:
            logger.info(
                "scan lease already held", scope=scope, owner=held.lease_owner, run_id=held.id
            )
            return None

        # Reclaim anything expired, so a crashed cycle is marked failed rather
        # than left looking like it is still running.
        await self._session.execute(
            update(ScanRun)
            .where(
                ScanRun.scope == scope,
                ScanRun.status == STATUS_RUNNING,
                ScanRun.lease_expires_at <= now,
            )
            .values(status=STATUS_FAILED, error="lease expired; presumed crashed")
        )

        run = ScanRun(
            scope=scope,
            status=STATUS_RUNNING,
            started_at=now,
            lease_owner=lease_owner(),
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
        self._session.add(run)
        try:
            await self._session.flush()
        except IntegrityError:  # pragma: no cover -- concurrent insert
            await self._session.rollback()
            return None
        return run

    async def complete(
        self, run: ScanRun, *, metrics: dict[str, int], error: str | None = None
    ) -> ScanRun:
        now = utc_now()
        run.status = STATUS_FAILED if error else STATUS_COMPLETED
        run.completed_at = now
        run.duration_seconds = (now - run.started_at).total_seconds()
        run.error = error
        for field_name, value in metrics.items():
            if hasattr(run, field_name):
                setattr(run, field_name, value)
        await self._session.flush()
        return run

    async def latest(self, *, scope: str = SCOPE_SCAN) -> ScanRun | None:
        stmt = (
            select(ScanRun)
            .where(ScanRun.scope == scope)
            .order_by(ScanRun.started_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def latest_successful(self, *, scope: str = SCOPE_SCAN) -> ScanRun | None:
        stmt = (
            select(ScanRun)
            .where(ScanRun.scope == scope, ScanRun.status == STATUS_COMPLETED)
            .order_by(ScanRun.started_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def recent(self, *, limit: int = 20) -> Sequence[ScanRun]:
        stmt = select(ScanRun).order_by(ScanRun.started_at.desc()).limit(limit)
        return (await self._session.execute(stmt)).scalars().all()


def lease_owner() -> str:
    """``host:pid``. Identifies the holder well enough to diagnose a stuck lease."""
    return f"{socket.gethostname()}:{os.getpid()}"


def evaluation_payload(evaluation: SignalEvaluation) -> dict[str, Any]:
    """A read-only view of one evaluation, for the API and the CLI."""
    return {
        "id": evaluation.id,
        "instrument_id": evaluation.instrument_id,
        "tracked_signal_id": evaluation.tracked_signal_id,
        "evaluated_at": evaluation.evaluated_at,
        "market_data_timestamp": evaluation.market_data_timestamp,
        "score": evaluation.score,
        "confidence": evaluation.confidence,
        "classification": evaluation.classification,
        "direction": evaluation.direction,
        "qualified": evaluation.qualified,
        "agreement": evaluation.agreement,
        "aligned": evaluation.aligned,
        "net_edge_bps": evaluation.net_edge_bps,
        "spread_bps": evaluation.spread_bps,
        "data_quality": evaluation.data_quality,
        "session_phase": evaluation.session_phase,
        "timeframe_states": evaluation.timeframe_states,
        "reason_codes": evaluation.reason_codes,
        "risk_codes": evaluation.risk_codes,
        "feature_set_version": evaluation.feature_set_version,
        "signal_model_version": evaluation.signal_model_version,
        "scanner_policy_version": evaluation.scanner_policy_version,
    }
