"""Remembering what things looked like last time.

Why there is state at all
-------------------------
A change cannot be detected from one observation. "NVDA's valuation is high" is
a level; "NVDA's valuation moved from normal to high" is an event, and the
second requires knowing what was true on the previous run. Without persistence,
a restart would either re-announce everything it had ever seen or announce
nothing until it had run twice -- and a crash loop would be indistinguishable
from a market that suddenly got interesting.

Why it is not in the trading database
-------------------------------------
This layer is read-only with respect to everything it observes. Giving it a
write path into the database that holds instruments, candles and paper decisions
would mean the guarantee rested on nobody misusing an open session. Keeping its
memory in its own directory means the monitoring package has no database write
path to misuse, and a test asserts it imports no session, engine or model.

The cost is that monitoring state is not transactional with trading state. That
is the right trade: these snapshots are a reporting convenience, and losing them
costs one quiet run while the engine re-learns the baseline, not a lost trade.

Writes are atomic
-----------------
A run interrupted midway through saving must not leave a half-written baseline
that the next run would read as a genuine change in every subject at once.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_STATE_DIR: Path = Path("data/monitor_state")


@dataclass(frozen=True, slots=True)
class StoredState:
    """What one subject looked like when it was last observed."""

    state: dict[str, Any] = field(default_factory=dict)
    observed_at: str | None = None
    changed_at: str | None = None
    notified_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "observed_at": self.observed_at,
            "changed_at": self.changed_at,
            "notified_at": self.notified_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StoredState:
        return cls(
            state=dict(raw.get("state") or {}),
            observed_at=raw.get("observed_at"),
            changed_at=raw.get("changed_at"),
            notified_at=raw.get("notified_at"),
        )


@runtime_checkable
class MonitorStateStore(Protocol):
    """Read and write the monitoring baseline. No other capability."""

    def get(self, scope: str, key: str) -> StoredState | None:
        ...

    def put(self, scope: str, key: str, state: dict[str, Any], *, observed_at: str) -> None:
        ...

    def mark_notified(self, scope: str, key: str, *, at: str) -> None:
        ...

    def flush(self) -> None:
        ...


class InMemoryStateStore:
    """A baseline that lives only as long as the process.

    Used by tests and by the historical replay, where writing a real baseline
    would contaminate the live one with events from a simulated past.
    """

    def __init__(self, initial: dict[str, dict[str, StoredState]] | None = None) -> None:
        self._rows: dict[str, dict[str, StoredState]] = initial or {}

    def get(self, scope: str, key: str) -> StoredState | None:
        return self._rows.get(scope, {}).get(key)

    def put(self, scope: str, key: str, state: dict[str, Any], *, observed_at: str) -> None:
        held = self._rows.setdefault(scope, {}).get(key)
        changed = observed_at if held is None or held.state != state else held.changed_at
        self._rows[scope][key] = StoredState(
            state=state,
            observed_at=observed_at,
            changed_at=changed,
            notified_at=held.notified_at if held else None,
        )

    def mark_notified(self, scope: str, key: str, *, at: str) -> None:
        held = self._rows.setdefault(scope, {}).get(key) or StoredState()
        self._rows[scope][key] = StoredState(
            state=held.state,
            observed_at=held.observed_at,
            changed_at=held.changed_at,
            notified_at=at,
        )

    def flush(self) -> None:
        return None

    def snapshot(self) -> dict[str, dict[str, StoredState]]:
        return self._rows


class JsonStateStore:
    """The durable baseline, one file per scope under ``data/monitor_state``.

    Held in memory for the duration of a run and written once at the end, so a
    pass over a thousand subjects does not become a thousand file writes.

    Args:
        directory: where the scope files live.
    """

    def __init__(self, directory: Path = DEFAULT_STATE_DIR) -> None:
        self._dir = directory
        self._loaded: dict[str, dict[str, StoredState]] = {}
        self._dirty: set[str] = set()

    def _scope(self, scope: str) -> dict[str, StoredState]:
        if scope in self._loaded:
            return self._loaded[scope]
        path = self._dir / f"{scope}.json"
        rows: dict[str, StoredState] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                rows = {k: StoredState.from_dict(v) for k, v in raw.items()}
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                # A damaged baseline is discarded rather than half-read. The
                # cost is one quiet run; trusting it would report every subject
                # as changed at once, which is the worst possible false alarm.
                logger.warning(
                    "discarding unreadable monitor baseline",
                    scope=scope,
                    reason=type(exc).__name__,
                )
                rows = {}
        self._loaded[scope] = rows
        return rows

    def get(self, scope: str, key: str) -> StoredState | None:
        return self._scope(scope).get(key)

    def put(self, scope: str, key: str, state: dict[str, Any], *, observed_at: str) -> None:
        rows = self._scope(scope)
        held = rows.get(key)
        changed = observed_at if held is None or held.state != state else held.changed_at
        rows[key] = StoredState(
            state=state,
            observed_at=observed_at,
            changed_at=changed,
            notified_at=held.notified_at if held else None,
        )
        self._dirty.add(scope)

    def mark_notified(self, scope: str, key: str, *, at: str) -> None:
        rows = self._scope(scope)
        held = rows.get(key) or StoredState()
        rows[key] = StoredState(
            state=held.state,
            observed_at=held.observed_at,
            changed_at=held.changed_at,
            notified_at=at,
        )
        self._dirty.add(scope)

    def flush(self) -> None:
        """Write every touched scope atomically."""
        for scope in sorted(self._dirty):
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"{scope}.json"
            payload = {k: v.as_dict() for k, v in sorted(self._loaded[scope].items())}
            with tempfile.NamedTemporaryFile(
                "w", dir=self._dir, delete=False, encoding="utf-8", suffix=".partial"
            ) as handle:
                json.dump(payload, handle, indent=1, sort_keys=True)
                temporary = Path(handle.name)
            temporary.replace(path)
        self._dirty.clear()
