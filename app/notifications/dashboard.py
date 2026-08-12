"""A single status message that gets edited, not a stream of them.

#status answers "is it working?" at a glance. That question has one current
answer, so it deserves one message -- posting a fresh snapshot every fifteen
minutes turns a dashboard into a log and buries the one line that matters.

Discord webhooks support this: creating with ``?wait=true`` returns the message,
and ``PATCH /webhooks/{id}/{token}/messages/{message_id}`` edits it. The message
id is persisted; **the webhook URL never is** -- it stays in the environment where
it belongs.

Fallback
--------
If the id is missing or the edit is rejected (the message was deleted, the
webhook rotated), the next run posts a new message and stores the new id. So the
worst failure mode is one extra message, not a broken dashboard.

This renders the existing :class:`~app.ops.check.OperationalStatus`. There is one
health model, used by both ``ops status`` and this -- two would drift, and then
the CLI and Discord would disagree about whether the system is up.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from app.core.time import utc_now

DASHBOARD_SCOPE: Final = "dashboard"
DASHBOARD_KEY: Final = "status:message_id"
"""Where the Discord message id lives in ``notification_state``.

Reuses the existing table rather than adding a migration for one string. The id
is not a credential -- it identifies a message, and without the webhook URL it
grants nothing.
"""

HEARTBEAT: Final = timedelta(minutes=15)
"""How often the dashboard refreshes even when nothing changed.

A dashboard that stops updating is indistinguishable from a dead process, so the
timestamp itself is the liveness signal.
"""

ONLINE: Final = "🟢 ONLINE"
DEGRADED: Final = "🟡 DEGRADED"
OFFLINE: Final = "🔴 OFFLINE"

STALE_SYNC: Final = timedelta(minutes=20)
STALE_SCAN: Final = timedelta(minutes=45)
"""Beyond these the scheduled jobs are late.

Four times the configured interval: a single missed run is normal after a laptop
sleeps, a sustained gap is not.
"""


@dataclass(frozen=True, slots=True)
class DashboardState:
    """What was last published, so an unchanged dashboard can stay quiet."""

    message_id: str | None = None
    published_at: datetime | None = None
    fingerprint: str | None = None


def server_state(status: Any, *, now: datetime | None = None) -> str:
    """ONLINE / DEGRADED / OFFLINE from how recently the jobs actually ran.

    Derived from evidence rather than from a process being alive: a scheduler
    that is running but whose jobs all fail is not online in any useful sense.
    """
    moment = now or utc_now()

    if status.last_sync is None and status.last_scan is None:
        return OFFLINE
    if status.last_error:
        return DEGRADED

    sync_late = status.last_sync is None or (moment - status.last_sync) > STALE_SYNC
    scan_late = status.last_scan is None or (moment - status.last_scan) > STALE_SCAN

    if sync_late and scan_late:
        return OFFLINE
    if sync_late or scan_late:
        return DEGRADED
    return ONLINE


def build_fields(
    status: Any,
    *,
    environment: str,
    provider: str,
    feed: str,
    revision: str,
    db_bytes: int | None = None,
    candles: int | None = None,
    discord_destinations: int = 0,
    now: datetime | None = None,
) -> dict[str, str]:
    """The dashboard grid.

    Grouped the way someone triaging reads it: is it up, is data arriving, is the
    scanner working, is the database sound, where do the portfolios stand.
    Absent values are omitted rather than shown as zero -- "no scan recorded" and
    "scanned zero symbols" are different situations.
    """
    moment = now or utc_now()
    fields: dict[str, str] = {
        "Server": server_state(status, now=moment),
        "Environment": environment,
        "Session": status.session_phase,
        "Provider": f"{provider} / {feed}",
        "Symbols": str(status.watchlist_size or status.universe_size or ""),
    }

    if status.last_sync is not None:
        detail = f"{_ago(status.last_sync, moment)} ago"
        if status.last_sync_duration:
            detail += f" · {status.last_sync_duration:.0f}s"
        if status.last_sync_symbols:
            detail += f" · {status.last_sync_symbols} symbols"
        if status.last_sync_failures:
            detail += f" · {status.last_sync_failures} failed"
        fields["Last sync"] = detail

    if status.last_scan is not None:
        detail = f"{_ago(status.last_scan, moment)} ago"
        if status.last_scan_duration:
            detail += f" · {status.last_scan_duration:.0f}s"
        fields["Last scan"] = detail
        fields["Scan result"] = (
            f"{status.last_scan_evaluated} evaluated · "
            f"{status.last_scan_qualified} qualified · "
            f"{status.last_scan_strong} strong"
        )

    fields["Database"] = f"rev {revision}" + (f" · {db_bytes / 1024**2:.0f} MB" if db_bytes else "")
    if candles:
        fields["Candles"] = f"{candles:,}"
    if status.evaluations_stored:
        fields["Evaluations"] = f"{status.evaluations_stored:,}"

    fields["Discord"] = (
        f"{discord_destinations} destinations" if discord_destinations else "not configured"
    )

    for portfolio in status.portfolios:
        fields[portfolio.key] = (
            f"{portfolio.equity:,.2f} · {portfolio.open_positions} open · "
            f"{portfolio.closed_trades} closed"
        )

    if status.last_error:
        fields["Last error"] = str(status.last_error)[:200]

    fields["Checked"] = moment.strftime("%Y-%m-%d %H:%M UTC")
    return {name: value for name, value in fields.items() if value}


VOLATILE_FIELDS: Final[frozenset[str]] = frozenset({"Checked", "Last sync", "Last scan"})
"""Fields that change on every tick without the system changing.

They render *ages* -- "3m ago" becomes "4m ago" a minute later -- so including
them in the fingerprint would mark every run as a change and republish the
dashboard constantly, which is exactly the behaviour the fingerprint exists to
prevent. The underlying facts are still compared: `Scan result` carries the
counts and `Server` degrades when a job goes late.
"""


def fingerprint(fields: dict[str, str]) -> str:
    """Identity of the *content*, ignoring anything that moves with the clock.

    Lets a caller tell "nothing changed" from "something changed", so the
    heartbeat can be slow while real changes still publish promptly.
    """
    material = {k: v for k, v in fields.items() if k not in VOLATILE_FIELDS}
    blob = "|".join(f"{k}={v}" for k, v in sorted(material.items()))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def should_publish(
    state: DashboardState, fields: dict[str, str], *, now: datetime | None = None
) -> tuple[bool, str]:
    """Publish on a real change, or on the heartbeat -- never on every tick."""
    moment = now or utc_now()
    current = fingerprint(fields)

    if state.published_at is None:
        return True, "first publication"
    if current != state.fingerprint:
        return True, "status changed"
    if (moment - state.published_at) >= HEARTBEAT:
        return True, "heartbeat"
    return False, "unchanged"


def _ago(moment: datetime, now: datetime) -> str:
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 90:  # noqa: PLR2004
        return f"{seconds}s"
    if seconds < 5400:  # noqa: PLR2004
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"
