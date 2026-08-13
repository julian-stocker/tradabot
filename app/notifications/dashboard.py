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
DASHBOARD_FINGERPRINT_KEY: Final = "status:fingerprint"
"""Where the last publication is remembered, in ``notification_state``.

Two rows rather than one packed string, and no migration: the existing table is
keyed on ``(scope, key)`` and already holds exactly this shape of fact. The
message id is **not a credential** -- it names a message, and without the webhook
URL it grants nothing. The URL itself is never written here or anywhere else on
disk.
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

CLOSED_TOLERANCE: Final = 4.0
"""How much the freshness windows widen while the market is closed.

Not switched *off*, widened. Both jobs are driven by launchd `StartInterval` and
fire regardless of session, so their lateness is still evidence -- but a laptop
that sleeps through a Saturday is the ordinary case, and marking it DEGRADED
every weekend would teach the operator to ignore the colour. A scheduler that has
genuinely been dead for hours still shows through, because the window widens
rather than disappearing.

The Friday-close problem this solves: on Sunday the newest daily bar is Friday's,
which is *correct*. Market-data recency and job recency are different questions,
and only the second one belongs in the health verdict.
"""

CLOSED_SESSIONS: Final[frozenset[str]] = frozenset({"CLOSED", "WEEKEND", "HOLIDAY"})

NOT_AVAILABLE: Final = "N/A"
"""Shown when a metric genuinely cannot be read.

Never a zero. "no scan recorded" and "scanned zero symbols" are different
situations, and a dashboard that renders the first as the second is lying at
exactly the moment someone is relying on it.
"""

LIVENESS_NOTE: Final = (
    "This dashboard is written *by* tradabot. A dead process cannot post 🔴 OFFLINE "
    "-- it simply stops updating, so read the heartbeat, not the colour."
)
"""**The limitation, stated in the message itself.**

OFFLINE is emitted only for the case a live process can observe: it is running
and nothing has ever run successfully. The failure mode that actually matters --
the whole installation stopped -- shows up as a `Checked` timestamp that stops
advancing, which is why the heartbeat exists and why it is never suppressed.
"""


@dataclass(frozen=True, slots=True)
class DashboardState:
    """What was last published, so an unchanged dashboard can stay quiet."""

    message_id: str | None = None
    published_at: datetime | None = None
    fingerprint: str | None = None


def delivery_failing(status: Any) -> bool:
    """Whether the most recent notification attempt failed.

    Compared rather than counted: a failure an hour ago followed by a success is
    a recovered blip, and reporting it as an ongoing fault would make the amber
    state permanent for anyone who has ever had a webhook hiccup.
    """
    ok = getattr(status, "last_notification_success", None)
    failed = getattr(status, "last_notification_failure", None)
    if failed is None:
        return False
    return ok is None or failed > ok


def _session_name(status: Any) -> str:
    """The session phase as a stable word.

    Prefers the enum and falls back to the rendered string only when there is no
    enum to read, so the value stays constant for as long as the session does.
    """
    session = getattr(status, "session", None)
    name = getattr(session, "value", None)
    return str(name) if name else str(getattr(status, "session_phase", NOT_AVAILABLE))


def _windows(session: Any) -> tuple[timedelta, timedelta]:
    """Freshness windows for this session, widened while the market is closed.

    Reads the session *enum*, never the rendered ``session_phase`` string. That
    string is prose -- "CLOSED at 18:12 UTC" -- and matching against it would
    work until someone improved the wording, then quietly stop widening the
    windows on exactly the weekends this exists for.
    """
    name = getattr(session, "value", session)
    if isinstance(name, str) and name.upper() in CLOSED_SESSIONS:
        return STALE_SYNC * CLOSED_TOLERANCE, STALE_SCAN * CLOSED_TOLERANCE
    return STALE_SYNC, STALE_SCAN


def server_state(status: Any, *, now: datetime | None = None) -> str:
    """ONLINE / DEGRADED / OFFLINE from how recently the jobs actually ran.

    Derived from evidence rather than from a process being alive: a scheduler
    that is running but whose jobs all fail is not online in any useful sense.

    OFFLINE is reachable only in the one shape a live process can honestly
    report -- it is running, and nothing has ever succeeded, or every job has
    been late for far longer than a sleeping laptop explains. For the real
    outage, see :data:`LIVENESS_NOTE`.
    """
    moment = now or utc_now()
    stale_sync, stale_scan = _windows(getattr(status, "session", None))

    if status.last_sync is None and status.last_scan is None:
        return OFFLINE
    if status.last_error:
        return DEGRADED
    if delivery_failing(status):
        # Delivery is broken. The dashboard itself arrived, so something narrower
        # than the process is wrong -- a rotated webhook, a rate limit.
        return DEGRADED

    sync_late = status.last_sync is None or (moment - status.last_sync) > stale_sync
    scan_late = status.last_scan is None or (moment - status.last_scan) > stale_scan

    if sync_late and scan_late:
        return OFFLINE
    if sync_late or scan_late:
        return DEGRADED
    return ONLINE


def _job_fields(status: Any, moment: datetime) -> dict[str, str]:
    """Sync and scan recency, with the detail an operator triages on.

    "never" rather than an omitted row: a fresh installation that has not synced
    yet and one whose sync stopped working look the same on a dashboard that
    simply leaves the line out.
    """
    fields: dict[str, str] = {}

    if status.last_sync is None:
        fields["Last sync"] = "never"
    else:
        detail = f"{_ago(status.last_sync, moment)} ago"
        if status.last_sync_duration:
            detail += f" · {status.last_sync_duration:.0f}s"
        if status.last_sync_symbols:
            detail += f" · {status.last_sync_symbols} symbols"
        if status.last_sync_failures:
            detail += f" · {status.last_sync_failures} failed"
        fields["Last sync"] = detail

    if status.last_scan is None:
        fields["Last scan"] = "never"
    else:
        detail = f"{_ago(status.last_scan, moment)} ago"
        if status.last_scan_duration:
            detail += f" · {status.last_scan_duration:.0f}s"
        fields["Last scan"] = detail
        fields["Scan result"] = (
            f"{status.last_scan_evaluated} evaluated · "
            f"{status.last_scan_qualified} qualified · "
            f"{status.last_scan_strong} strong"
        )
    return fields


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
    jobs: tuple[str, ...] = (),
    volatility: str | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """The dashboard grid.

    Grouped the way someone triaging reads it: is it up, is data arriving, is the
    scanner working, is the database sound, where do the portfolios stand.
    Absent values are omitted or marked :data:`NOT_AVAILABLE` rather than shown
    as zero -- "no scan recorded" and "scanned zero symbols" are different
    situations, and only one of them is a problem.
    """
    moment = now or utc_now()
    fields: dict[str, str] = {
        "Server": server_state(status, now=moment),
        "Environment": environment,
        # The *phase*, not the rendered prose. `session_phase` reads "REGULAR at
        # 14:05 UTC" -- it carries the clock, so putting it here would change the
        # fingerprint on every single tick and republish the dashboard forever.
        # The time is already on the `Checked` line, where it belongs.
        "Session": _session_name(status),
        "Provider": f"{provider} / {feed}",
        "Symbols": str(status.watchlist_size or status.universe_size or NOT_AVAILABLE),
    }

    fields.update(_job_fields(status, moment))

    fields["Database"] = f"rev {revision}" + (f" · {db_bytes / 1024**2:.0f} MB" if db_bytes else "")
    fields["Candles"] = f"{candles:,}" if candles else NOT_AVAILABLE
    fields["Evaluations"] = (
        f"{status.evaluations_stored:,}" if status.evaluations_stored else NOT_AVAILABLE
    )

    if volatility:
        # One line, deliberately. #status answers "is it working?"; turning it
        # into a market dashboard would bury the health signal it exists for.
        fields["Volatility"] = volatility

    if jobs:
        # Named rather than counted: "6 jobs" does not tell an operator that the
        # trends job is missing from their LaunchAgents directory.
        fields["Scheduler"] = " · ".join(jobs)

    # Two fields, deliberately. The *health* of delivery is stable and belongs in
    # the fingerprint; the *age* of the last delivery moves every minute and must
    # not, or the dashboard would republish on every tick. See VOLATILE_FIELDS.
    discord = f"{discord_destinations} destinations" if discord_destinations else "not configured"
    last_ok = getattr(status, "last_notification_success", None)
    if discord_destinations:
        discord += " · failing" if delivery_failing(status) else " · delivering"
    fields["Discord"] = discord
    fields["Last delivery"] = f"{_ago(last_ok, moment)} ago" if last_ok else "never"

    for portfolio in status.portfolios:
        fields[portfolio.key] = (
            f"{portfolio.equity:,.2f} · {portfolio.open_positions} open · "
            f"{portfolio.closed_trades} closed"
        )
    if not status.portfolios:
        fields["Portfolios"] = NOT_AVAILABLE

    if status.last_error:
        fields["Last error"] = str(status.last_error)[:200]

    fields["Checked"] = moment.strftime("%Y-%m-%d %H:%M UTC")
    return {name: value for name, value in fields.items() if value}


VOLATILE_FIELDS: Final[frozenset[str]] = frozenset(
    {"Checked", "Last sync", "Last scan", "Last delivery"}
)
"""Fields that change on every tick without the system changing.

They render *ages* -- "3m ago" becomes "4m ago" a minute later -- so including
them in the fingerprint would mark every run as a change and republish the
dashboard constantly, which is exactly the behaviour the fingerprint exists to
prevent. The underlying facts are still compared: `Scan result` carries the
counts, `Server` degrades when a job goes late, and `Discord` says whether
delivery is working -- so every *change* these fields could have signalled is
caught by a stable field beside them.
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
