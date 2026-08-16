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

import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
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
from app.market_data.volatility import MODEL_VERSION as VOLATILITY_MODEL
from app.market_data.volatility import VolatilityRegime
from app.market_data.volatility_service import VolatilityService
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
from app.scanner.repository import WatchlistRepository

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
            volatility = await _volatility_health(session, now=moment)

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
            volatility=volatility,
            monitor=_monitor_health(),
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


def _monitor_health() -> dict[str, str]:
    """Publisher and monitoring health, read from their own state directories.

    Read-only and failure-tolerant: the status dashboard must still render when
    the monitoring layer has never run, so every absence becomes a value rather
    than an exception.
    """
    from app.monitoring.journal import EventJournal  # noqa: PLC0415
    from app.ops.heartbeat import HEARTBEAT_ENV, declared_policy  # noqa: PLC0415
    from app.publishing.ledger import DeliveryLedger  # noqa: PLC0415

    def stamp(path: Path) -> str:
        if not path.exists():
            return "never"
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        )

    out: dict[str, str] = {}
    try:
        # Liveness first, and deliberately blunt. Every other field here is
        # produced by this machine, so none of them can tell "quiet" from "off".
        # Stating that stops "last sync 1m ago" from being read as "the server is
        # up right now" -- which it never was.
        beats = int(declared_policy()["heartbeat_interval_seconds"]) // 60
        out["Server heartbeat"] = (
            f"emitted every {beats}m; liveness judged off-host"
            if _configured(HEARTBEAT_ENV)
            else "NOT CONFIGURED — this dashboard cannot detect a stopped server"
        )
        ledger = DeliveryLedger()
        pending = ledger.pending_failures()
        out["Application"] = "HEALTHY" if not ledger.is_empty() else "NO RUNS RECORDED"
        out["Discord delivery"] = "DEGRADED" if pending else "HEALTHY"
        out["Pending delivery failures"] = str(len(pending))
        last = ledger.last_delivery()
        out["Last successful Discord delivery"] = (
            str(last)[:16].replace("T", " ") + " UTC" if last else "never"
        )
        events = EventJournal().read()
        out["Last market event"] = (
            str(events[-1].get("occurred_at"))[:16].replace("T", " ") + " UTC"
            if events
            else "none recorded"
        )
        out["Last monitor run"] = stamp(Path("data/monitor_state/market.json"))
        out["Last fundamentals sync"] = stamp(Path("data/sec_facts.parquet"))
    except Exception as exc:
        logger.warning("monitor health unavailable", reason=type(exc).__name__)
        out["Application"] = "UNKNOWN"
    return out


def _configured(name: str) -> bool:
    """Whether a setting has a value. **Presence only** -- never the value."""
    from app.core.webhooks import _dotenv_values  # noqa: PLC0415

    merged = dict(_dotenv_values(Path(".env")))
    merged.update(os.environ)
    return bool(merged.get(name, "").strip())


async def _volatility_health(session: AsyncSession, *, now: datetime) -> str | None:
    """One line describing the volatility engine's health, not the market.

    Reports coverage and how many symbols are elevated -- enough to see the
    engine ran and to notice a feed problem, without turning the health channel
    into a market feed. Never raises: a status dashboard that failed because a
    derived metric failed would be reporting on itself.
    """
    try:
        symbols = await WatchlistRepository(session).symbols()
        snapshot = await VolatilityService(session).for_symbols(symbols, now=now)
    except Exception as exc:
        logger.debug("volatility health unavailable", error=safe_message(exc))
        return None

    if not snapshot.estimates:
        return f"{VOLATILITY_MODEL} · no estimate"

    counts = snapshot.by_regime
    elevated = counts[VolatilityRegime.HIGH] + counts[VolatilityRegime.EXTREME]
    stale = len(snapshot.stale(now=now))
    detail = (
        f"{VOLATILITY_MODEL} · {len(snapshot.estimates)}/{snapshot.symbols_requested} "
        f"· {elevated} elevated"
    )
    return detail + (f" · {stale} stale" if stale else "")


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
