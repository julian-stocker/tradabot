"""Deciding whether an event is worth telling a human about.

**This module controls notification volume and nothing else.** Every signal is
still computed, scored and persisted regardless of what it decides; every trade
decision and outcome is still written. A suppressed notification is a message not
sent, never a row not stored -- see docs/notifications.md, and Part X of the
phase brief. The database is the future ML dataset, and it must not be shaped by
what happened to be interesting on a chat channel.

Two problems, one mechanism
---------------------------
**Signal spam.** A scanner re-evaluating a symbol every fifteen minutes produces
a stream of near-identical scores. Announcing each one makes the channel useless
within a day. So a signal notifies on a *transition* -- crossing into qualified,
strengthening materially, falling back out -- and not on a level.

**Alert spam.** A stale feed is stale on every check. Alerting each time buries
the moment it *became* stale, which is the only interesting instant. So health
alerts are also transitions: healthy → unhealthy notifies, unhealthy → unhealthy
does not, and unhealthy → healthy sends a recovery.

**Overview spam.** The hourly market overview published unconditionally --
including "No qualified opportunities." at 03:00 on a Sunday. Zero candidates is
the *normal* state: the phase-5.5 benchmark qualified 385 observations out of
116,844, so roughly 997 of every 1,000 scans have nothing to say. Announcing
that hourly is the purest form of the problem this module exists to solve, and it
trains the reader to ignore the one channel that should never be ignored.

Both are the same idea: notify on change, not on state. The difference is only
what counts as a change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.core.config import NotificationSettings
from app.core.events import EventType
from app.scanner.enums import SessionPhase


class SignalPhase(StrEnum):
    """Where a subject sits in the notification lifecycle.

    Not a trading concept. It exists purely so the policy can tell "already told
    you about this" from "this is new".
    """

    NONE = "none"
    """Never qualified, or invalidated since."""
    QUALIFIED = "qualified"
    STRONG = "strong"


@dataclass(frozen=True, slots=True)
class SignalState:
    """What was last announced about one subject."""

    phase: SignalPhase = SignalPhase.NONE
    score: float | None = None
    notified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Whether to notify, what about, and what to remember.

    ``reason`` is recorded even when suppressing. "Why did Discord go quiet?" is
    a question that gets asked, and a decision that cannot explain itself is
    indistinguishable from a bug.
    """

    notify: bool
    event_type: EventType | None
    next_state: SignalState
    reason: str


def evaluate_signal(
    *,
    settings: NotificationSettings,
    state: SignalState,
    score: float,
    now: datetime,
) -> PolicyDecision:
    """Decide whether a freshly scored signal is worth announcing.

    The lifecycle, with the default thresholds (75 / 85, 60 minutes, 5 points)::

        64  ->  nothing        (below the notification threshold)
        76  ->  QUALIFIED      (crossed into range)
        77  ->  nothing        (same phase, +1 is not material)
        86  ->  STRENGTHENED   (crossed the strong threshold)
        85  ->  nothing        (still strong)
        61  ->  INVALIDATED    (dropped back out)

    Cooldown applies to *repeat* notifications within a phase. It deliberately
    does **not** gate a phase change: a signal collapsing out of range twenty
    minutes after it qualified is exactly the thing worth interrupting someone
    for, and silencing it to respect a timer would be the wrong trade.
    """
    phase = _phase_for(settings, score)

    if phase is SignalPhase.NONE:
        if state.phase is SignalPhase.NONE:
            return PolicyDecision(
                notify=False,
                event_type=None,
                next_state=SignalState(SignalPhase.NONE, score, state.notified_at),
                reason=f"score {score:.1f} below threshold {settings.signal_threshold:.1f}",
            )
        # Was announced, no longer qualifies. Closing the loop matters: a reader
        # who acted on the original message needs to know it lapsed.
        return PolicyDecision(
            notify=True,
            event_type=EventType.MARKET_SIGNAL_INVALIDATED,
            next_state=SignalState(SignalPhase.NONE, score, now),
            reason=f"fell from {state.phase.value} to {score:.1f}",
        )

    if phase is SignalPhase.STRONG and state.phase is not SignalPhase.STRONG:
        return PolicyDecision(
            notify=True,
            event_type=EventType.MARKET_SIGNAL_STRENGTHENED
            if state.phase is SignalPhase.QUALIFIED
            else EventType.MARKET_SIGNAL_QUALIFIED,
            next_state=SignalState(SignalPhase.STRONG, score, now),
            reason=f"entered strong range at {score:.1f}",
        )

    if state.phase is SignalPhase.NONE:
        return PolicyDecision(
            notify=True,
            event_type=EventType.MARKET_SIGNAL_QUALIFIED,
            next_state=SignalState(phase, score, now),
            reason=f"qualified at {score:.1f}",
        )

    return _evaluate_repeat(settings=settings, state=state, phase=phase, score=score, now=now)


def _evaluate_repeat(
    *,
    settings: NotificationSettings,
    state: SignalState,
    phase: SignalPhase,
    score: float,
    now: datetime,
) -> PolicyDecision:
    """The subject is in the same phase it was last announced in.

    Split out from the transition logic above because the questions are
    different: that one asks "did something change?", this one asks "has it
    changed *enough*, and has enough time passed?". Both gates apply -- either
    one alone still produces a stream of near-identical messages.
    """
    moved = abs(score - state.score) if state.score is not None else None

    if moved is None or moved < settings.minimum_score_change:
        return PolicyDecision(
            notify=False,
            event_type=None,
            next_state=SignalState(phase, score, state.notified_at),
            reason=f"moved {moved:.1f} < {settings.minimum_score_change:.1f}"
            if moved is not None
            else "no previous score",
        )

    if _within_cooldown(settings, state.notified_at, now):
        return PolicyDecision(
            notify=False,
            event_type=None,
            next_state=SignalState(phase, score, state.notified_at),
            reason=f"cooldown: last notified {state.notified_at}",
        )

    return PolicyDecision(
        notify=True,
        event_type=EventType.MARKET_SIGNAL_STRENGTHENED
        if score > (state.score or 0)
        else EventType.MARKET_SIGNAL_QUALIFIED,
        next_state=SignalState(phase, score, now),
        reason=f"moved {moved:.1f} after cooldown",
    )


@dataclass(frozen=True, slots=True)
class OverviewDecision:
    """Whether to publish a routine market overview, and why not."""

    should_publish: bool
    reason: str

    def __bool__(self) -> bool:
        return self.should_publish


def evaluate_overview(
    *,
    candidate_count: int,
    session: SessionPhase,
    require_regular_session: bool = True,
) -> OverviewDecision:
    """Decide whether an hourly market overview is worth sending.

    Three rules, in order:

    1. **A closed market has no opportunities to report.** Weekend, holiday and
       out-of-hours overviews are pure noise -- nothing has changed since the
       close and nothing can until the open. Closed-market status belongs in the
       daily summary, which is a report, not an alert.
    2. **Extended hours do not qualify signals either.** The scanner already
       refuses to promote setups on pre/post-market IEX prints (see
       `app.scanner.sessions`), so an overview then can only ever say zero.
       Announcing a foregone conclusion is still noise.
    3. **Zero candidates is not news.** It is the overwhelmingly common state.
       An overview is published when there is something *in* it.

    Nothing here suppresses a *transition*: a signal newly qualifying still
    notifies through :func:`evaluate_signal`. This governs only the periodic
    digest.
    """
    if session in _CLOSED_PHASES:
        return OverviewDecision(False, f"market {session.value.lower()}")
    if require_regular_session and session is not SessionPhase.REGULAR:
        return OverviewDecision(False, f"{session.value.lower()} cannot qualify signals")
    if candidate_count <= 0:
        return OverviewDecision(False, "no qualified opportunities")
    return OverviewDecision(True, f"{candidate_count} qualified")


_CLOSED_PHASES: frozenset[SessionPhase] = frozenset(
    {SessionPhase.CLOSED, SessionPhase.WEEKEND, SessionPhase.HOLIDAY}
)


def _phase_for(settings: NotificationSettings, score: float) -> SignalPhase:
    if score >= settings.strong_signal_threshold:
        return SignalPhase.STRONG
    return SignalPhase.QUALIFIED if score >= settings.signal_threshold else SignalPhase.NONE


def _within_cooldown(settings: NotificationSettings, last: datetime | None, now: datetime) -> bool:
    if last is None or settings.signal_cooldown_minutes <= 0:
        return False
    return now - last < timedelta(minutes=settings.signal_cooldown_minutes)


# ---------------------------------------------------------------------------
# Health transitions (Part M)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HealthState:
    """Whether a component was last seen healthy, and since when."""

    healthy: bool = True
    since: datetime | None = None


@dataclass(frozen=True, slots=True)
class HealthDecision:
    notify: bool
    recovered: bool
    next_state: HealthState
    downtime_seconds: float | None = None


def evaluate_health(*, state: HealthState, healthy: bool, now: datetime) -> HealthDecision:
    """Notify only when health *changes*.

    A provider that is down stays down. Alerting on every check turns the system
    channel into a scrolling wall that hides the next real incident, and trains
    whoever reads it to mute the channel -- which is worse than not alerting at
    all.
    """
    if healthy == state.healthy:
        return HealthDecision(
            notify=False, recovered=False, next_state=HealthState(healthy, state.since or now)
        )

    downtime = (now - state.since).total_seconds() if healthy and state.since is not None else None
    return HealthDecision(
        notify=True,
        recovered=healthy,
        next_state=HealthState(healthy, now),
        downtime_seconds=downtime,
    )
