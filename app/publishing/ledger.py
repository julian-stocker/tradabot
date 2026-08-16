"""What has already been delivered, and what failed trying.

The problem
-----------
A publisher restarted mid-run, or run twice by a scheduler that fired late, must
not post the same alert again. Discord has no idempotency key, so the identity
has to be ours.

Identity is the *observation*, not the moment
---------------------------------------------
An event's ``occurred_at`` is the wall clock when the pass ran, so re-running the
same session five minutes later would produce a different timestamp and a
different id — which is precisely the duplicate this ledger exists to stop. So
the identity is the deduplication key plus the **session being observed**, both
of which the monitoring engine already computes and neither of which moves when
the publisher is restarted.

A failure is not an unseen event
--------------------------------
When delivery fails the event is recorded as ``DELIVERY_FAILED``, not left
absent. Leaving it absent would make it eligible again on the next pass, and a
transport outage lasting a day would then discharge a day of accumulated alerts
the moment Discord came back. Recovery is a bounded, deliberate act — see
:mod:`app.publishing.publisher` — not a side effect of retrying.

Not in the trading tables
-------------------------
This is publisher state and lives beside the monitoring baseline. Nothing about
whether a message was delivered belongs in a table that records decisions or
orders.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.monitoring.schemas import ChangeEvent

logger = get_logger(__name__)

DEFAULT_LEDGER_DIR: Path = Path("data/monitor_delivery")


class DeliveryStatus(StrEnum):
    DELIVERED = "DELIVERED"
    DELIVERY_FAILED = "DELIVERY_FAILED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    BASELINE = "BASELINE_NOT_SENT"
    """Recorded on the first publishing run so an established monitoring
    baseline does not open the channel with a burst of current conditions."""
    SUPPRESSED = "SUPPRESSED_ALREADY_DELIVERED"


def observation_date(event: ChangeEvent) -> str:
    """The session an event describes, not the moment it was noticed."""
    for source in event.provenance:
        if source.as_of:
            return str(source.as_of)[:10]
    return event.occurred_at.date().isoformat()


def event_id(event: ChangeEvent) -> str:
    """Stable delivery identity for one event.

    Built from the kind, the monitoring deduplication key and the observed
    session, so the same finding re-derived by a later run maps to the same id.
    """
    raw = f"{event.kind}|{event.key()}|{observation_date(event)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    event_id: str
    destination: str
    status: DeliveryStatus
    first_seen: str
    updated_at: str
    attempts: int = 0
    error: str | None = None
    subject: str | None = None
    kind: str | None = None

    @property
    def delivered(self) -> bool:
        return self.status is DeliveryStatus.DELIVERED

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "destination": self.destination,
            "status": str(self.status),
            "first_seen": self.first_seen,
            "updated_at": self.updated_at,
            "attempts": self.attempts,
            "error": self.error,
            "subject": self.subject,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DeliveryRecord:
        return cls(
            event_id=str(raw["event_id"]),
            destination=str(raw["destination"]),
            status=DeliveryStatus(str(raw["status"])),
            first_seen=str(raw.get("first_seen") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            attempts=int(raw.get("attempts") or 0),
            error=raw.get("error"),
            subject=raw.get("subject"),
            kind=raw.get("kind"),
        )


class DeliveryLedger:
    """Delivery status per (event, destination), persisted between runs.

    Args:
        directory: where the ledger file lives.
        retain: how many records to keep. Old delivered records are pruned so
            the file cannot grow without bound; failures are kept preferentially
            because they are the ones an operator still needs.
    """

    def __init__(self, directory: Path = DEFAULT_LEDGER_DIR, *, retain: int = 20_000) -> None:
        self._dir = directory
        self._path = directory / "deliveries.json"
        self._retain = retain
        self._rows: dict[tuple[str, str], DeliveryRecord] | None = None
        self._dirty = False

    # ------------------------------------------------------------------ io
    def _load(self) -> dict[tuple[str, str], DeliveryRecord]:
        if self._rows is not None:
            return self._rows
        rows: dict[tuple[str, str], DeliveryRecord] = {}
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                for entry in raw:
                    record = DeliveryRecord.from_dict(entry)
                    rows[(record.event_id, record.destination)] = record
            except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
                # An unreadable ledger is treated as empty. That risks one
                # duplicate message; trusting a half-parsed file risks silently
                # suppressing real alerts, which is the worse failure.
                logger.warning(
                    "discarding unreadable delivery ledger", reason=type(exc).__name__
                )
                rows = {}
        self._rows = rows
        return rows

    def flush(self) -> None:
        if not self._dirty:
            return
        rows = list(self._load().values())
        if len(rows) > self._retain:
            # Keep every failure, then the most recent deliveries.
            failures = [r for r in rows if not r.delivered]
            delivered = sorted(
                (r for r in rows if r.delivered), key=lambda r: r.updated_at, reverse=True
            )
            rows = failures + delivered[: max(0, self._retain - len(failures))]
        self._dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=self._dir, delete=False, encoding="utf-8", suffix=".partial"
        ) as handle:
            json.dump([r.as_dict() for r in rows], handle, indent=1)
            temporary = Path(handle.name)
        temporary.replace(self._path)
        self._dirty = False

    # -------------------------------------------------------------- queries
    def status_of(self, event_id_: str, destination: str) -> DeliveryRecord | None:
        return self._load().get((event_id_, destination))

    def already_delivered(self, event_id_: str, destination: str) -> bool:
        record = self.status_of(event_id_, destination)
        return record is not None and record.delivered

    def should_skip(self, event_id_: str, destination: str) -> bool:
        """Whether this event is finished with, for this destination.

        Delivered and baselined are both terminal: the first was sent, the
        second was deliberately not sent and must not become eligible later.
        A failure is *not* terminal, so the next pass may legitimately retry it.
        """
        record = self.status_of(event_id_, destination)
        return record is not None and record.status in (
            DeliveryStatus.DELIVERED,
            DeliveryStatus.BASELINE,
        )

    def is_empty(self, destination: str | None = None) -> bool:
        """Whether anything has been recorded, optionally for one destination.

        Scoped by destination because the first-run baseline has to be per
        channel. Publishing a portfolio update first would otherwise leave the
        ledger non-empty and strip the market channel of its baseline, so the
        very next events pass would open #market-signals with whatever
        conditions happened to be true that day.
        """
        rows = self._load()
        if destination is None:
            return not rows
        return not any(key[1] == destination for key in rows)

    def pending_failures(self) -> list[DeliveryRecord]:
        """Everything that failed and has not since succeeded."""
        return sorted(
            (r for r in self._load().values() if r.status is DeliveryStatus.DELIVERY_FAILED),
            key=lambda r: r.updated_at,
        )

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for record in self._load().values():
            out[str(record.status)] = out.get(str(record.status), 0) + 1
        return out

    def last_delivery(self) -> str | None:
        delivered = [r.updated_at for r in self._load().values() if r.delivered]
        return max(delivered) if delivered else None

    # --------------------------------------------------------------- writes
    def record(
        self,
        event_id_: str,
        destination: str,
        status: DeliveryStatus,
        *,
        now: datetime,
        attempts: int = 0,
        error: str | None = None,
        subject: str | None = None,
        kind: str | None = None,
    ) -> DeliveryRecord:
        rows = self._load()
        key = (event_id_, destination)
        held = rows.get(key)
        stamp = now.isoformat()
        record = DeliveryRecord(
            event_id=event_id_,
            destination=destination,
            status=status,
            first_seen=held.first_seen if held else stamp,
            updated_at=stamp,
            attempts=(held.attempts if held else 0) + attempts,
            error=error,
            subject=subject or (held.subject if held else None),
            kind=kind or (held.kind if held else None),
        )
        rows[key] = record
        self._dirty = True
        return record

    def forget(self, records: Iterable[DeliveryRecord]) -> None:
        """Drop records entirely. Used only when a recovery notice replaces them."""
        rows = self._load()
        for record in records:
            rows.pop((record.event_id, record.destination), None)
        self._dirty = True
