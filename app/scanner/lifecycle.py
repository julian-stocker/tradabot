"""Signal identity and lifecycle transitions.

Pure functions. Given what a signal was and what the scanner just observed, they
say what it becomes -- with no database and no clock of their own, so every rule
here is testable by calling it.

Identity
--------
A continuing setup must keep one identity across scans, or "how long has this
been true?" becomes unanswerable and every fifteen-minute cycle invents a fresh
discovery. Two observations belong to the same signal when **all** of these hold:

* same instrument;
* same direction -- a long turning into a short is a different idea, not the
  same one continuing;
* same primary timeframe and horizon -- a 1h setup and a 1d setup about the same
  instrument are genuinely different claims;
* same **setup premise** (the structural state) -- a breakout that becomes a
  breakdown has had its premise falsified, and calling that "the same signal,
  weakened" would hide the falsification;
* the existing signal is still active, and was seen recently enough.

Anything else starts a new signal. The rules are deliberately strict: merging two
distinct setups loses information irrecoverably, whereas splitting one setup into
two is visible and can be reasoned about later.

Lifecycle
---------
::

    DISCOVERED ──► QUALIFIED ──► STRONG
         │             │  ▲         │
         │             ▼  └─────────┘
         │         WEAKENED
         │             │
         ▼             ▼
      EXPIRED     INVALIDATED

``WEAKENED`` exists so a setup that dips below the threshold and recovers is not
recorded as two separate discoveries. ``EXPIRED`` is distinct from
``INVALIDATED`` on purpose: one means the scanner stopped looking, the other
means the market said no. A future model that conflated them would learn from
labels that partly describe tradabot's uptime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.config import NotificationSettings, ScannerSettings
from app.scanner.enums import SignalLifecycle, StructureState

SETUP_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SignalIdentity:
    """What makes two observations the same setup."""

    instrument_id: int
    direction: str
    primary_timeframe: str
    horizon: str
    setup: str

    def matches(self, other: SignalIdentity) -> bool:
        return self == other


def setup_for(structure: StructureState) -> str:
    """The premise a setup rests on.

    ``RANGING`` and ``UNKNOWN`` collapse to ``UNKNOWN``: neither is a premise
    that could be falsified, so treating them as distinct setups would create
    signal churn every time price wandered between them.
    """
    if structure in (
        StructureState.BREAKOUT,
        StructureState.BREAKDOWN,
        StructureState.CONSOLIDATION,
    ):
        return structure.value
    return SETUP_UNKNOWN


def direction_label(direction: int) -> str:
    """Signals are tracked as LONG or SHORT.

    A direction of 0 is not a signal to track -- there is no setup to have an
    opinion about -- and callers filter it before reaching here.
    """
    return "LONG" if direction >= 0 else "SHORT"


@dataclass(frozen=True, slots=True)
class LifecycleTransition:
    """What a signal becomes, and whether that is worth announcing."""

    lifecycle: SignalLifecycle
    changed: bool
    """Whether the state differs from the previous one."""
    reason: str

    @property
    def newly_qualified(self) -> bool:
        return self.changed and self.lifecycle is SignalLifecycle.QUALIFIED

    @property
    def newly_strong(self) -> bool:
        return self.changed and self.lifecycle is SignalLifecycle.STRONG

    @property
    def newly_invalidated(self) -> bool:
        return self.changed and self.lifecycle is SignalLifecycle.INVALIDATED


def evaluate_lifecycle(
    *,
    current: SignalLifecycle | None,
    score: float,
    settings: NotificationSettings,
    actionable: bool = True,
) -> LifecycleTransition:
    """The state a signal takes given a fresh score.

    Args:
        current: the existing lifecycle, or None for a first sighting.
        score: the new score.
        settings: qualification and strength thresholds.
        actionable: whether the data was good enough to act on. Non-actionable
            observations can **downgrade** a signal but never promote one -- a
            setup that only looks qualified on stale data is not qualified, and
            promoting on bad input is how a feed outage becomes a trade.

    Thresholds are operational heuristics for controlling notification volume.
    A score of 85 is not an 85% chance of anything.
    """
    qualifies = score >= settings.signal_threshold
    strong = score >= settings.strong_signal_threshold

    if current is None:
        return _first_sighting(
            score=score, qualifies=qualifies, strong=strong, actionable=actionable
        )

    if current.is_terminal:
        # Terminal is terminal. A recovering score starts a *new* signal rather
        # than resurrecting a dead one, so the record of what was invalidated,
        # and when, stays intact.
        return LifecycleTransition(current, changed=False, reason="already terminal")

    if not qualifies:
        return _fell_below(current=current, score=score, threshold=settings.signal_threshold)

    if not actionable:
        return LifecycleTransition(
            current, changed=False, reason="data not actionable; no promotion"
        )

    return _still_qualifying(current=current, score=score, strong=strong)


def _fell_below(*, current: SignalLifecycle, score: float, threshold: float) -> LifecycleTransition:
    """The score no longer clears the qualification threshold.

    A signal that had qualified is **invalidated**; one that never did simply
    stays DISCOVERED. The distinction matters downstream: only the first is worth
    telling anyone about, because only the first contradicts something already
    said.
    """
    if current in (SignalLifecycle.QUALIFIED, SignalLifecycle.STRONG, SignalLifecycle.WEAKENED):
        # Always a change: terminal states are handled by the caller, so
        # `current` here is one of the three live qualified states.
        return LifecycleTransition(
            SignalLifecycle.INVALIDATED,
            changed=True,
            reason=f"fell to {score:.1f}, below {threshold:.1f}",
        )
    return LifecycleTransition(
        SignalLifecycle.DISCOVERED, changed=False, reason=f"still below threshold at {score:.1f}"
    )


def _still_qualifying(
    *, current: SignalLifecycle, score: float, strong: bool
) -> LifecycleTransition:
    """The score clears the threshold on usable data."""
    if strong:
        return LifecycleTransition(
            SignalLifecycle.STRONG,
            changed=current is not SignalLifecycle.STRONG,
            reason=f"strong at {score:.1f}",
        )

    if current is SignalLifecycle.STRONG:
        # Dropped out of strong but still qualified. WEAKENED rather than
        # QUALIFIED so the decline is visible, and so a later recovery does not
        # read as a brand-new qualification.
        return LifecycleTransition(
            SignalLifecycle.WEAKENED, changed=True, reason=f"eased from strong to {score:.1f}"
        )

    return LifecycleTransition(
        SignalLifecycle.QUALIFIED,
        changed=current is SignalLifecycle.DISCOVERED,
        reason=f"qualified at {score:.1f}",
    )


def _first_sighting(
    *, score: float, qualifies: bool, strong: bool, actionable: bool
) -> LifecycleTransition:
    """A setup nobody has seen before.

    Split from the transition logic because the questions differ: this one asks
    "what is this?", the other asks "what has it become?". A first sighting on
    unusable data is recorded as DISCOVERED and nothing more -- the observation
    is worth storing, the promotion is not earned.
    """
    if not actionable:
        return LifecycleTransition(
            SignalLifecycle.DISCOVERED, changed=True, reason="first sighting on unusable data"
        )
    if strong:
        return LifecycleTransition(
            SignalLifecycle.STRONG, changed=True, reason=f"discovered strong at {score:.1f}"
        )
    if qualifies:
        return LifecycleTransition(
            SignalLifecycle.QUALIFIED, changed=True, reason=f"discovered at {score:.1f}"
        )
    return LifecycleTransition(
        SignalLifecycle.DISCOVERED, changed=True, reason=f"discovered at {score:.1f}"
    )


def has_expired(*, last_evaluated_at: datetime, now: datetime, settings: ScannerSettings) -> bool:
    """Whether an active signal has gone unseen long enough to expire.

    Expiry is about the *scanner*, not the market: a symbol removed from the
    watchlist, a delisting, a weekend of downtime. It is why the state exists
    separately from invalidation.
    """
    return now - last_evaluated_at >= timedelta(hours=settings.signal_expiry_hours)
