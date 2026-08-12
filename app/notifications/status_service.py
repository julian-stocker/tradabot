"""Publishing the #status dashboard from operational state.

Reads :func:`~app.ops.check.operational_status` -- the same function ``ops
status`` prints -- so the CLI and Discord cannot disagree about whether the
system is up. Two health models would drift, and they would drift precisely
during the incident when both are being consulted.

Its own scheduled job, for one reason the trends job does not have: **the
dashboard must publish while the market is closed.** A heartbeat that stops on
Friday evening cannot distinguish "quiet weekend" from "the laptop died", which
is the single question #status exists to answer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Final

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.events import Event, EventType
from app.core.logging import get_logger
from app.core.redaction import safe_message
from app.core.time import utc_now
from app.db.models import Candle
from app.db.session import session_scope
from app.notifications.backends.discord import DiscordWebhookNotifier
from app.notifications.dashboard import (
    LIVENESS_NOTE,
    NOT_AVAILABLE,
    DashboardState,
    build_fields,
    fingerprint,
    should_publish,
)
from app.notifications.feeds import STATUS_ROUTING_KEY
from app.notifications.formatters import format_event
from app.notifications.repository import NotificationRepository
from app.ops.check import operational_status
from app.ops.launchd import ScheduledJob, scheduled_jobs

logger = get_logger(__name__)

TITLE: Final = "🖥️ TRADABOT STATUS"


@dataclass(frozen=True, slots=True)
class StatusRun:
    """What one dashboard tick did."""

    fields: dict[str, str]
    reason: str
    published: bool = False
    created: bool = False
    message_id: str | None = None
    error: str | None = None


class StatusService:
    """Renders and publishes the persistent dashboard."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        notifier: DiscordWebhookNotifier | None = None,
    ) -> None:
        self._factory = session_factory
        self._settings = settings
        self._notifier = notifier

    async def render(self, *, now: datetime | None = None) -> dict[str, str]:
        """The current dashboard grid. **Reads only** -- sends nothing.

        This is what ``--preview`` prints and what the scheduler publishes, from
        one code path: a preview that rendered separately would be reassuring
        about a message it did not produce.
        """
        moment = now or utc_now()
        async with session_scope(self._factory) as session:
            status = await operational_status(session, self._settings, now=moment)
            revision, candles = await _database_facts(session)

        return build_fields(
            status,
            environment=self._settings.env.value,
            provider=self._settings.market_data_provider,
            feed=self._settings.alpaca.feed,
            revision=revision,
            db_bytes=_database_bytes(self._settings),
            candles=candles,
            discord_destinations=len(self._settings.discord.configured_categories),
            jobs=tuple(job.name for job in _jobs(self._settings)),
            now=moment,
        )

    async def publish(self, *, now: datetime | None = None, force: bool = False) -> StatusRun:
        """Refresh the dashboard if anything changed, or on the heartbeat.

        **Never raises.** A dashboard that crashed its own job would be the one
        failure mode guaranteed to go unreported.
        """
        moment = now or utc_now()
        try:
            fields = await self.render(now=moment)
        except Exception as exc:
            error = safe_message(exc)
            logger.warning("status render failed", error=error)
            return StatusRun(fields={}, reason="render failed", error=error)

        async with session_scope(self._factory) as session:
            state = await NotificationRepository(session).dashboard_state()

        publish, reason = should_publish(state, fields, now=moment)
        if not publish and not force:
            logger.debug("status unchanged; not republished", reason=reason)
            return StatusRun(fields=fields, reason=reason, message_id=state.message_id)

        message_id = await self._deliver(fields, state=state, now=moment)
        if message_id is None:
            # Delivery failed. The stored state is left untouched so the next tick
            # tries again rather than believing it already published.
            logger.warning("status dashboard not delivered", reason=reason)
            return StatusRun(fields=fields, reason=reason, error="not delivered")

        await self._remember(
            replace(
                state,
                message_id=message_id,
                published_at=moment,
                fingerprint=fingerprint(fields),
            )
        )
        logger.info(
            "status dashboard published",
            reason=reason,
            created=message_id != state.message_id,
        )
        return StatusRun(
            fields=fields,
            reason=reason,
            published=True,
            created=message_id != state.message_id,
            message_id=message_id,
        )

    # -- Internals ---------------------------------------------------------

    async def _deliver(
        self, fields: dict[str, str], *, state: DashboardState, now: datetime
    ) -> str | None:
        """Edit the existing message, or create one. Returns the id in play."""
        if self._notifier is None:
            return None

        message = format_event(
            Event(
                type=EventType.OPERATIONAL_STATUS,
                occurred_at=now,
                payload={"title": TITLE, "fields": fields, "note": LIVENESS_NOTE},
                key="status:dashboard",
                # Dedicated destination, no fallback. #status is edited in place,
                # and editing a message in a shared channel would silently rewrite
                # something an operator was reading.
                routing_key=STATUS_ROUTING_KEY,
            )
        )
        try:
            return await self._notifier.publish_dashboard(message, message_id=state.message_id)
        # Scheduled work: a delivery fault is reported, never raised.
        except Exception as exc:
            logger.warning("status dashboard delivery raised", error=safe_message(exc))
            return None

    async def _remember(self, state: DashboardState) -> None:
        try:
            async with session_scope(self._factory) as session:
                await NotificationRepository(session).save_dashboard_state(state)
        # Losing the id costs one duplicate message next tick, not a broken run.
        except Exception as exc:
            logger.warning("could not persist dashboard state", error=safe_message(exc))


async def _database_facts(session: AsyncSession) -> tuple[str, int | None]:
    """Schema revision and candle count, or honest unknowns."""
    try:
        row = (await session.execute(text("SELECT version_num FROM alembic_version"))).first()
        revision = str(row[0]) if row else NOT_AVAILABLE
    except Exception:
        revision = NOT_AVAILABLE

    try:
        candles = (await session.execute(select(func.count()).select_from(Candle))).scalar_one()
    except Exception:
        return revision, None
    return revision, int(candles)


def _database_bytes(settings: Settings) -> int | None:
    """On-disk size, when the database is a local file.

    Returns ``None`` for anything else rather than guessing -- a PostgreSQL URL
    has no file to stat, and inventing a number would be worse than N/A.
    """
    if not settings.is_sqlite or "///" not in settings.database_url:
        return None
    path = Path(settings.database_url.rsplit("///", maxsplit=1)[-1])
    try:
        return path.stat().st_size if path.exists() else None
    # An unreadable path is a missing number, not a failed dashboard.
    except OSError:
        return None


def _jobs(settings: Settings) -> tuple[ScheduledJob, ...]:
    return scheduled_jobs(
        scan_minutes=settings.scanner.scan_interval_minutes,
        sync_minutes=settings.scanner.market_sync_interval_minutes,
        overview_minutes=settings.scanner.overview_interval_minutes,
        trends_minutes=settings.scanner.trends_interval_minutes,
        status_minutes=settings.scanner.status_interval_minutes,
    )
