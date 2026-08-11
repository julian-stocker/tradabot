"""Persistence for notification audit and policy state."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.db.models import NotificationAttempt, NotificationState
from app.notifications.models import DeliveryResult, NotificationMessage
from app.notifications.policy import HealthState, SignalPhase, SignalState

SCOPE_SIGNAL = "signal"
SCOPE_HEALTH = "health"

STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


class NotificationRepository:
    """Reads and writes notification state and delivery attempts."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- Audit -------------------------------------------------------------

    async def record_attempt(
        self, message: NotificationMessage, result: DeliveryResult
    ) -> NotificationAttempt:
        """Persist what was attempted and what came of it.

        The message *content* is not stored -- only its identity and outcome.
        Keeping the rendered text would duplicate data that already exists in the
        signal and trade tables, and would grow without bound for no operational
        benefit.
        """
        now = utc_now()
        status = STATUS_DELIVERED if result.delivered else STATUS_FAILED
        if result.attempts == 0:
            status = STATUS_SKIPPED

        attempt = NotificationAttempt(
            event_type=message.event_type.value,
            category=message.category.value,
            event_key=message.key,
            backend=result.backend,
            status=status,
            attempt_count=result.attempts,
            last_status_code=result.status_code,
            last_error=result.error,
            created_at=message.occurred_at,
            attempted_at=now,
            delivered_at=now if result.delivered else None,
        )
        self._session.add(attempt)
        await self._session.flush()
        return attempt

    async def recent_attempts(self, limit: int = 50) -> Sequence[NotificationAttempt]:
        stmt = (
            select(NotificationAttempt)
            .order_by(NotificationAttempt.attempted_at.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def counts_by_status(self) -> dict[str, int]:
        """How many attempts landed in each status. For the health endpoint."""
        rows = (await self._session.execute(select(NotificationAttempt.status))).scalars().all()
        counts: dict[str, int] = {}
        for status in rows:
            counts[status] = counts.get(status, 0) + 1
        return counts

    async def last_outcome(self) -> tuple[datetime | None, datetime | None]:
        """Timestamps of the last success and the last failure.

        Both, not just the most recent: "last delivered an hour ago" and "failing
        continuously since" are different situations that a single timestamp
        cannot distinguish.
        """
        success = (
            await self._session.execute(
                select(NotificationAttempt.delivered_at)
                .where(NotificationAttempt.status == STATUS_DELIVERED)
                .order_by(NotificationAttempt.attempted_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        failure = (
            await self._session.execute(
                select(NotificationAttempt.attempted_at)
                .where(NotificationAttempt.status == STATUS_FAILED)
                .order_by(NotificationAttempt.attempted_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return success, failure

    # -- Policy state ------------------------------------------------------

    async def signal_state(self, key: str) -> SignalState:
        """What was last announced about a subject, or a clean slate."""
        row = await self._row(SCOPE_SIGNAL, key)
        if row is None:
            return SignalState()
        return SignalState(
            phase=SignalPhase(row.phase), score=row.score, notified_at=row.notified_at
        )

    async def save_signal_state(self, key: str, state: SignalState) -> None:
        row = await self._row(SCOPE_SIGNAL, key)
        now = utc_now()
        if row is None:
            row = NotificationState(
                scope=SCOPE_SIGNAL, key=key, phase=state.phase.value, updated_at=now
            )
            self._session.add(row)
        if row.phase != state.phase.value:
            row.changed_at = now
        row.phase = state.phase.value
        row.score = state.score
        row.notified_at = state.notified_at
        row.updated_at = now
        await self._session.flush()

    async def health_state(self, key: str) -> HealthState:
        """Whether a component was last seen healthy.

        An unknown component is assumed **healthy**. The alternative -- assuming
        unhealthy -- would fire a recovery alert for every component the first
        time it is ever checked, on every fresh install.
        """
        row = await self._row(SCOPE_HEALTH, key)
        if row is None:
            return HealthState()
        return HealthState(healthy=row.phase == "healthy", since=row.changed_at)

    async def save_health_state(self, key: str, state: HealthState) -> None:
        row = await self._row(SCOPE_HEALTH, key)
        now = utc_now()
        phase = "healthy" if state.healthy else "unhealthy"
        if row is None:
            row = NotificationState(scope=SCOPE_HEALTH, key=key, phase=phase, updated_at=now)
            self._session.add(row)
        if row.phase != phase:
            row.changed_at = state.since or now
        row.phase = phase
        row.updated_at = now
        await self._session.flush()

    async def _row(self, scope: str, key: str) -> NotificationState | None:
        stmt = select(NotificationState).where(
            NotificationState.scope == scope, NotificationState.key == key
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
