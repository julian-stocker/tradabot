"""Notification audit and policy state.

Two tables, two jobs.

``notification_attempts`` answers "did that alert actually get delivered?". When
tradabot runs unattended, the difference between "nothing happened" and "something
happened and the message never arrived" is invisible without a record, and it is
exactly the difference an operator needs at 08:00. **No webhook URL is stored** --
only the category, which is what identifies a destination without being a
credential.

``notification_state`` is the memory behind deduplication and health transitions.
It lives in the database rather than in a process so that a restart does not
re-announce every open signal and re-alert every ongoing outage. State that only
survives while the process does would make a crash loop indistinguishable from a
market event.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime


class NotificationAttempt(Base):
    """One attempt to deliver one message.

    A row per attempt sequence, updated in place on retry, so a flapping channel
    produces a visible ``attempt_count`` rather than a thousand rows.
    """

    __tablename__ = "notification_attempts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc="Routing category. Identifies the destination channel; never the URL.",
    )
    event_key: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        doc="The event's dedup key, e.g. 'NVDA:1d:5d'. Ties an attempt to its subject.",
    )
    backend: Mapped[str] = mapped_column(String(32), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        doc="'delivered', 'failed' or 'skipped'. Skipped means no backend was configured.",
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Redacted failure text. Never contains a webhook URL or a credential.",
    )

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        Index("ix_notification_attempts_created_at", "created_at"),
        Index("ix_notification_attempts_status", "status"),
        Index("ix_notification_attempts_event_key", "event_key"),
    )

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid
        return f"<NotificationAttempt {self.event_type} {self.status}>"


class NotificationState(Base):
    """What has already been announced about one subject.

    Keyed on ``(scope, key)`` so signal lifecycle and component health share one
    table without colliding: ``('signal', 'NVDA:1d:5d')`` and
    ``('health', 'provider:alpaca')`` are different rows with different meanings
    and the same mechanics.
    """

    __tablename__ = "notification_state"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    scope: Mapped[str] = mapped_column(String(32), nullable=False, doc="'signal' or 'health'.")
    key: Mapped[str] = mapped_column(
        String(128), nullable=False, doc="Subject identity within the scope."
    )

    phase: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc="Signal phase ('none'/'qualified'/'strong') or health ('healthy'/'unhealthy').",
    )
    score: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        doc=(
            "Last announced score. A notification threshold, not a trading one -- "
            "Float rather than Money because it is a dimensionless 0..100 figure."
        ),
    )
    changed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, doc="When the phase last changed. Measures downtime."
    )
    notified_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, doc="When a message was last sent. Drives cooldown."
    )
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (UniqueConstraint("scope", "key", name="uq_notification_state_scope_key"),)

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid
        return f"<NotificationState {self.scope}:{self.key} {self.phase}>"
