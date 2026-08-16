"""Proof that this machine is still alive, sent somewhere else.

The problem no internal check can solve
---------------------------------------
Every health field Tradabot publishes is produced by Tradabot. A stopped
process, a crashed interpreter, a stalled scheduler and an unplugged laptop all
produce the same observable: nothing new appears. And "nothing new appeared" is
indistinguishable from "the market was quiet", which is the normal case this
whole product is designed around.

So liveness cannot be asserted from the inside. It can only be *inferred from
the outside*, by something that notices the absence of an expected signal.

What lives here, and what does not
----------------------------------
Here: the emitter. A small, bounded, never-raising ping to an external endpoint,
plus the state machine that defines what UP, LATE, DOWN and RECOVERED mean, so
those definitions are testable and have one owner.

Not here: **the watcher**. It must run somewhere this machine cannot affect --
see ``.github/workflows/watchdog.yml``. A watchdog hosted on the box it watches
reports "everything is fine" right up until it stops reporting at all, and then
says nothing, forever.

The emitter is deliberately dumb
--------------------------------
It sends a timestamp and nothing else. No metrics, no status summary, no
payload that could fail to serialise. The single fact it conveys -- *this
process reached this line at this time* -- is the only one that cannot be
reconstructed after the fact.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from app.core.logging import get_logger
from app.core.redaction import safe_message

logger = get_logger(__name__)

HEARTBEAT_ENV: Final = "TRADABOT_HEARTBEAT_URL"
"""Where to ping. A ping URL is a bearer credential -- anyone holding it can
mark this host alive -- so it is read from configuration, never logged, and
never written to a report."""

INTERVAL_SECONDS: Final = 5 * 60
"""How often the heartbeat is sent. Declared here so the emitter and the
watcher agree on one number."""

GRACE_SECONDS: Final = 15 * 60
"""How long silence is tolerated before the host is called DOWN. Three missed
beats: one missed heartbeat is a network blip, three is a pattern.

An operational threshold, not a market one. It answers "is the machine on?",
never "is anything happening?"."""

TIMEOUT_SECONDS: Final = 10.0
MAX_ATTEMPTS: Final = 2


class Liveness(StrEnum):
    """What the *watcher* concludes from the age of the last heartbeat."""

    UP = "UP"
    LATE = "LATE"
    """Past the interval but inside the grace period. Reported, never alerted:
    alerting here would fire on every slow network moment."""
    DOWN = "DOWN"
    RECOVERED = "RECOVERED"
    UNKNOWN = "UNKNOWN"
    """No heartbeat has ever been seen. Not the same as DOWN -- a watchdog that
    was only just configured has learned nothing yet."""


@dataclass(frozen=True, slots=True)
class HeartbeatResult:
    """What one emission attempt did. Carries no URL."""

    sent: bool
    attempts: int = 0
    error: str | None = None
    configured: bool = True

    @property
    def ok(self) -> bool:
        return self.sent


def evaluate(
    last_seen: datetime | None,
    now: datetime,
    *,
    previous: Liveness | None = None,
    interval_seconds: int = INTERVAL_SECONDS,
    grace_seconds: int = GRACE_SECONDS,
) -> Liveness:
    """The liveness a watcher should conclude, and whether it changed.

    ``previous`` is what the watcher last concluded. It is what turns a return
    to health into ``RECOVERED`` rather than a silent slide back to ``UP``, and
    what lets a watcher suppress a repeated ``DOWN``.

    Args:
        last_seen: timestamp of the most recent heartbeat, if any.
        now: evaluation time.
        previous: the previously reported state.
        interval_seconds: expected gap between heartbeats.
        grace_seconds: silence tolerated before DOWN.
    """
    if last_seen is None:
        return Liveness.UNKNOWN
    age = now - last_seen
    if age > timedelta(seconds=grace_seconds):
        return Liveness.DOWN
    if previous in (Liveness.DOWN, Liveness.UNKNOWN):
        return Liveness.RECOVERED
    if age > timedelta(seconds=interval_seconds):
        return Liveness.LATE
    return Liveness.UP


def should_alert(state: Liveness, previous: Liveness | None) -> bool:
    """Whether this transition is worth waking someone for.

    Only the edges. A host that has been down for a day is still down, and
    repeating that every fifteen minutes is how an operator learns to filter the
    channel that exists to tell them the machine is off.
    """
    if state is Liveness.DOWN:
        return previous is not Liveness.DOWN
    return state is Liveness.RECOVERED


def emit(url: str | None, *, now: datetime, opener: object = None) -> HeartbeatResult:
    """Ping the external watchdog. **Never raises.**

    A heartbeat that could throw would take down the process it exists to prove
    is healthy, which is a genuinely absurd failure mode and an easy one to
    write by accident.
    """
    if not url:
        return HeartbeatResult(sent=False, configured=False, error="no heartbeat URL configured")

    payload = now.isoformat().encode("utf-8")
    last_error = "unknown"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(
                url, data=payload, headers={"User-Agent": "tradabot-heartbeat"}
            )
            if opener is not None:
                response = opener(request, timeout=TIMEOUT_SECONDS)  # type: ignore[operator]
            else:
                response = urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)
            with response:
                pass
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except Exception as exc:
            # Transport errors routinely echo the request URL back. The ping URL
            # is a bearer credential, so it is stripped explicitly rather than
            # trusting a generic redactor to recognise an arbitrary endpoint.
            last_error = safe_message(exc).replace(url, "<heartbeat-endpoint>")
        else:
            return HeartbeatResult(sent=True, attempts=attempt)
    logger.warning("heartbeat not delivered", attempts=MAX_ATTEMPTS, error=last_error)
    return HeartbeatResult(sent=False, attempts=MAX_ATTEMPTS, error=last_error)


def declared_policy() -> dict[str, Any]:
    """The thresholds, for documentation and for the watcher to match."""
    return {
        "heartbeat_interval_seconds": INTERVAL_SECONDS,
        "grace_seconds": GRACE_SECONDS,
        "missed_beats_before_down": GRACE_SECONDS // INTERVAL_SECONDS,
        "states": [str(s) for s in Liveness],
        "alerting_transitions": ["-> DOWN (once)", "-> RECOVERED"],
        "watcher_location": "must be off-host; see .github/workflows/watchdog.yml",
    }
