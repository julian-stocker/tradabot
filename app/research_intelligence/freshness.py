"""Whether an event is still *current*, which is not whether it is still true.

A research event's historical truth never expires: NVIDIA filed an 8-K under
Item 2.02 on 26 August 2026, and that stays a fact for ever. Whether it belongs
under a heading called CURRENT DEVELOPMENTS is an entirely separate question,
and conflating the two produces the failure ``app/notifications/trends.py``
already names -- a persisting condition re-announced until nobody reads it.

So nothing here deletes. :func:`is_current` is a read-time predicate over a
store that keeps everything.

Windows are per kind, because "current" differs
-----------------------------------------------
An earnings release is current until the next one is due, not for ninety days.
A restatement stays current far longer than a debt drawdown, because it changes
how every other figure should be read. The windows follow the event's own
nature, and none is fitted against price outcomes -- ``docs/filing-events.md``
records that no stable post-filing effect survived pre-registration, so there
is nothing to fit to.

Defined and tested here, wired to no consumer in this phase.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from app.research_intelligence.schemas import EventKind, ResearchEvent

FRESHNESS_DAYS: Final[dict[EventKind, int]] = {
    # Slightly over a quarter, so a release stays current until its successor
    # is due rather than expiring in the gap between them.
    EventKind.EARNINGS_RELEASE: 100,
    EventKind.MANAGEMENT_CHANGE: 90,
    EventKind.M_AND_A: 365,
    EventKind.MATERIAL_AGREEMENT: 180,
    EventKind.DEBT_EVENT: 180,
    # A restatement changes how every other figure should be read, so it stays
    # current for as long as the restated periods are still being compared to.
    EventKind.ACCOUNTING_RESTATEMENT: 730,
    EventKind.AUDITOR_CHANGE: 365,
    EventKind.BANKRUPTCY_OR_RECEIVERSHIP: 730,
    EventKind.IMPAIRMENT: 365,
    EventKind.EXIT_OR_DISPOSAL_COSTS: 365,
    EventKind.LISTING_RULE_MATTER: 365,
    EventKind.CONTROL_CHANGE: 365,
    EventKind.CYBERSECURITY_INCIDENT: 365,
    EventKind.UNREGISTERED_EQUITY_SALE: 180,
    EventKind.UNCLASSIFIED_SEC_FILING: 90,
}

DEFAULT_DAYS: Final = 90


def window_days(kind: EventKind) -> int:
    """How long an event of this kind reads as current."""
    return FRESHNESS_DAYS.get(kind, DEFAULT_DAYS)


def is_current(event: ResearchEvent, *, as_of: str) -> bool:
    """Whether the event should be presented as a current development.

    Anchored on ``published_at`` rather than ``occurred_at``: an event becomes
    current when it becomes public, and a filing reporting something from three
    weeks earlier is news on the day it is filed.
    """
    published = _moment(event.published_at)
    asked = _moment(as_of)
    if published is None or asked is None:
        return False
    if published > asked:
        # Not yet public at the moment asked about. The store already excludes
        # this; repeating it keeps the predicate safe used on its own.
        return False
    return asked - published <= timedelta(days=window_days(event.event_kind))


def _moment(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
