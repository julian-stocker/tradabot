"""What a notification is, independently of where it goes.

A :class:`NotificationMessage` is a *rendered* message with no transport in it.
Formatters produce one; backends consume one. That split is what lets the console
backend and the Discord backend show the same content without either of them
owning the formatting, and what makes a future Telegram or email backend a new
file rather than a new set of format strings.

Nothing here knows about Discord's payload shape, its character limit or its
webhook protocol. Those live in the Discord backend, where they belong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.events import EventCategory, EventType, Severity

TRUNCATION_MARKER = "\n… (truncated)"


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    """A message ready to be delivered anywhere.

    ``title`` and ``body`` are plain text with light markdown. Keeping them
    transport-neutral means a backend may reformat, but never has to *re-derive*
    what happened.
    """

    category: EventCategory
    severity: Severity
    title: str
    body: str
    event_type: EventType
    occurred_at: datetime
    key: str | None = None
    """Carried through from the event, so a backend or an audit row can identify
    the subject without parsing the rendered text."""
    routing_key: str | None = None
    """Destination within the category, e.g. ``"paper-100"``. A backend resolves
    it to a channel; ``None`` means the category's default destination."""
    fields: dict[str, str] = field(default_factory=dict)
    """Structured detail, for backends that can render it natively. The body
    already contains this information -- these are for machines, not humans."""

    def rendered(self, limit: int) -> str:
        """Title and body as one string, truncated to ``limit`` characters.

        Truncation drops from the **end**, because formatters put the important
        material first: symbol, score, decision, result. A long tail of reasons
        is the part worth losing, and losing it is much better than failing
        delivery -- an alert that does not arrive because it was too detailed is
        the worst possible outcome.
        """
        text = f"{self.title}\n{self.body}" if self.body else self.title
        if len(text) <= limit:
            return text
        keep = max(0, limit - len(TRUNCATION_MARKER))
        return text[:keep].rstrip() + TRUNCATION_MARKER


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """What happened when a backend tried to deliver.

    A failure is **reported, never raised**. Notification delivery is secondary
    to everything that produces a notification, and a transport that could throw
    into a caller would eventually roll back a trade because Discord was down.
    """

    backend: str
    delivered: bool
    status_code: int | None = None
    error: str | None = None
    """Redacted before it gets here."""
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.delivered
