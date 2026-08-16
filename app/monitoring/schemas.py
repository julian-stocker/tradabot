"""What a monitored change is.

The question this layer answers
------------------------------
Not "what is the state of the world" -- the Advisor and Portfolio Fit already
answer that -- but "what *changed*, and is the change worth a human's attention".
Those are different questions, and conflating them produces the failure mode
this package exists to avoid: a channel that reports the same true facts every
day until nobody reads it.

So every event here is a **transition**. It carries the previous state and the
current state, and it exists only because they differ by more than a declared
threshold. A level is never an event.

Relationship to :mod:`app.core.events`
--------------------------------------
That module's :class:`~app.core.events.Event` is a *transport* object: a thing
that happened, with a payload, ready for a notification backend. This one is an
*analytical* object: it carries the before, the after, the measurements that
separate them, the threshold it had to clear and where every number came from.

They are not merged. A transport event with all of this in a free-form payload
would push the materiality decision into the formatter, which is where it would
quietly stop being auditable. Translation happens when a transport is attached,
which is deliberately outside this phase.

Nothing here recommends
-----------------------
No event expresses a buy, a sell, a rotation, a target weight or an expected
return. "Semiconductor exposure moved from 31% to 44%" is a fact about a
portfolio; what to do about it is not something any validated evidence in this
repository can answer. A test asserts that vocabulary never appears.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class EventKind(StrEnum):
    """The canonical change vocabulary.

    Grouped by what the change is *about*, because that is what decides who
    cares: a market regime change matters to everyone, a portfolio weight change
    matters only to the account that holds it.
    """

    # -- market ------------------------------------------------------------
    MARKET_REGIME_CHANGE = "MARKET_REGIME_CHANGE"
    SECTOR_MOVE = "SECTOR_MOVE"
    UNUSUAL_VOLUME = "UNUSUAL_VOLUME"
    UNUSUAL_VOLATILITY = "UNUSUAL_VOLATILITY"
    RELATIVE_STRENGTH_CHANGE = "RELATIVE_STRENGTH_CHANGE"

    # -- company -----------------------------------------------------------
    NEW_SEC_FILING = "NEW_SEC_FILING"
    FUNDAMENTAL_CHANGE = "FUNDAMENTAL_CHANGE"
    VALUATION_STATE_CHANGE = "VALUATION_STATE_CHANGE"
    COMPANY_CONFIDENCE_CHANGE = "COMPANY_CONFIDENCE_CHANGE"

    # -- portfolio ---------------------------------------------------------
    PORTFOLIO_WEIGHT_CHANGE = "PORTFOLIO_WEIGHT_CHANGE"
    PORTFOLIO_CONCENTRATION_CHANGE = "PORTFOLIO_CONCENTRATION_CHANGE"
    SECTOR_CONCENTRATION_CHANGE = "SECTOR_CONCENTRATION_CHANGE"
    CORRELATION_CLUSTER_CHANGE = "CORRELATION_CLUSTER_CHANGE"
    CASH_LEVEL_CHANGE = "CASH_LEVEL_CHANGE"
    POSITION_ADDED = "POSITION_ADDED"
    POSITION_REMOVED = "POSITION_REMOVED"

    # -- system ------------------------------------------------------------
    DATA_HEALTH_CHANGE = "DATA_HEALTH_CHANGE"


MARKET_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.MARKET_REGIME_CHANGE,
        EventKind.SECTOR_MOVE,
        EventKind.UNUSUAL_VOLUME,
        EventKind.UNUSUAL_VOLATILITY,
        EventKind.RELATIVE_STRENGTH_CHANGE,
    }
)
COMPANY_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.NEW_SEC_FILING,
        EventKind.FUNDAMENTAL_CHANGE,
        EventKind.VALUATION_STATE_CHANGE,
        EventKind.COMPANY_CONFIDENCE_CHANGE,
    }
)
PORTFOLIO_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.PORTFOLIO_WEIGHT_CHANGE,
        EventKind.PORTFOLIO_CONCENTRATION_CHANGE,
        EventKind.SECTOR_CONCENTRATION_CHANGE,
        EventKind.CORRELATION_CLUSTER_CHANGE,
        EventKind.CASH_LEVEL_CHANGE,
        EventKind.POSITION_ADDED,
        EventKind.POSITION_REMOVED,
    }
)


class Materiality(StrEnum):
    """How much the change deserves attention.

    ``ROUTINE`` exists so that a detected-but-unremarkable change has somewhere
    to go other than the outbox. Such events are still produced and counted --
    the suppression is a reporting decision, and a change that was observed and
    judged unimportant is a different thing from one that was never observed.
    """

    ROUTINE = "ROUTINE"
    NOTABLE = "NOTABLE"
    SIGNIFICANT = "SIGNIFICANT"
    CRITICAL = "CRITICAL"


MATERIALITY_ORDER: dict[str, int] = {
    "ROUTINE": 0,
    "NOTABLE": 1,
    "SIGNIFICANT": 2,
    "CRITICAL": 3,
}

REPORTABLE_FROM: Materiality = Materiality.NOTABLE
"""The floor for reporting. ``ROUTINE`` changes are recorded and never announced."""


class EventConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT = "INSUFFICIENT"


_CONFIDENCE_ORDER: dict[str, int] = {
    "INSUFFICIENT": 0,
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
}


def weakest(*levels: EventConfidence) -> EventConfidence:
    """Confidence is the minimum of its inputs, never the mean.

    An event resting on a solid price series and a shaky sector label is only as
    trustworthy as the sector label. Averaging would let one strong input hide a
    weak one, which is precisely the case where the reader most needs to know.
    """
    present = [x for x in levels if x is not None]
    if not present:
        return EventConfidence.INSUFFICIENT
    return min(present, key=lambda c: _CONFIDENCE_ORDER[str(c)])


class ScopeKind(StrEnum):
    MARKET = "market"
    SECTOR = "sector"
    COMPANY = "company"
    PORTFOLIO = "portfolio"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class Scope:
    """Who a change is about.

    ``account`` is set only for portfolio events. A weight change in PAPER_3K is
    not a fact about PAPER_1K, and an event that did not say which account it
    belonged to could be routed to the wrong one.
    """

    kind: ScopeKind
    account: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": str(self.kind), "account": self.account}


@dataclass(frozen=True, slots=True)
class Evidence:
    """One measurement behind an event, and the bar it had to clear.

    Carrying the threshold alongside the value is what makes materiality
    auditable after the fact. "Volume was 3.4x average" invites the question
    "and what counts as unusual?"; the answer travels with the claim.
    """

    measure: str
    previous: float | str | None
    current: float | str | None
    change: float | None = None
    unit: str = ""
    threshold: float | None = None
    comparison: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "measure": self.measure,
            "previous": self.previous,
            "current": self.current,
            "change": self.change,
            "unit": self.unit,
            "threshold": self.threshold,
            "comparison": self.comparison,
        }


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where the numbers came from, and as of when."""

    source: str
    as_of: str
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "as_of": self.as_of, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """One observed transition.

    ``dedup_key`` identifies the *subject and the transition*, not the moment.
    Two runs that observe the same unchanged condition produce the same key, so
    a cooldown can recognise a repeat without comparing payloads -- the same
    principle the notification policy already uses for signals.
    """

    kind: EventKind
    occurred_at: datetime
    subject: str
    previous_state: str | None
    current_state: str
    materiality: Materiality
    summary: str
    evidence: tuple[Evidence, ...] = ()
    confidence: EventConfidence = EventConfidence.INSUFFICIENT
    provenance: tuple[Provenance, ...] = ()
    scope: Scope = field(default_factory=lambda: Scope(ScopeKind.MARKET))
    dedup_key: str = ""

    @property
    def reportable(self) -> bool:
        """Whether this clears the reporting floor on materiality alone.

        Cooldown and deduplication are applied separately, by the engine: an
        event can be material and still not worth repeating this hour.
        """
        return (
            MATERIALITY_ORDER[str(self.materiality)]
            >= MATERIALITY_ORDER[str(REPORTABLE_FROM)]
        )

    def key(self) -> str:
        """Deduplication identity, derived when one was not supplied."""
        if self.dedup_key:
            return self.dedup_key
        account = self.scope.account or "-"
        return f"{self.kind}:{account}:{self.subject}:{self.current_state}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "occurred_at": self.occurred_at.isoformat(),
            "subject": self.subject,
            "previous_state": self.previous_state,
            "current_state": self.current_state,
            "materiality": str(self.materiality),
            "summary": self.summary,
            "evidence": [e.as_dict() for e in self.evidence],
            "confidence": str(self.confidence),
            "provenance": [p.as_dict() for p in self.provenance],
            "scope": self.scope.as_dict(),
            "dedup_key": self.key(),
            "reportable": self.reportable,
        }


@dataclass(frozen=True, slots=True)
class MonitoringRun:
    """Everything one pass observed, including what it chose not to report."""

    as_of: str
    started_at: datetime
    events: tuple[ChangeEvent, ...] = ()
    suppressed_routine: int = 0
    suppressed_cooldown: int = 0
    suppressed_duplicate: int = 0
    subjects_examined: int = 0
    notes: tuple[str, ...] = ()

    @property
    def reported(self) -> tuple[ChangeEvent, ...]:
        return self.events

    @property
    def quiet(self) -> bool:
        """True when nothing survived. **The answer the engine exists to give.**"""
        return not self.events

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "started_at": self.started_at.isoformat(),
            "quiet": self.quiet,
            "reported": len(self.events),
            "suppressed_routine": self.suppressed_routine,
            "suppressed_cooldown": self.suppressed_cooldown,
            "suppressed_duplicate": self.suppressed_duplicate,
            "subjects_examined": self.subjects_examined,
            "events": [e.as_dict() for e in self.events],
            "notes": list(self.notes),
        }
