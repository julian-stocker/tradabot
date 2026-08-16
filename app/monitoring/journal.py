"""An append-only record of what was reported.

Why events are kept as well as state
------------------------------------
The baseline in :mod:`app.monitoring.state` answers "what is true now". It
cannot answer "what were the five biggest things that happened this week",
because each run overwrites the previous snapshot and the intermediate
transitions are gone.

So reported events are appended here, one JSON object per line, partitioned by
month. A weekly digest reads a date range; nothing rewrites history.

Append-only, and only what was reported
---------------------------------------
Suppressed events are counted in the run summary but not journalled. Writing
every routine non-change would make the file enormous and would blur the one
distinction the journal exists to preserve: this is the record of what was
considered worth saying.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.monitoring.schemas import ChangeEvent

logger = get_logger(__name__)

DEFAULT_JOURNAL_DIR: Path = Path("data/monitor_events")


class EventJournal:
    """Reported events on disk, partitioned by month.

    Args:
        directory: where the monthly files live.
    """

    def __init__(self, directory: Path = DEFAULT_JOURNAL_DIR) -> None:
        self._dir = directory

    def _path(self, when: date) -> Path:
        return self._dir / f"{when.year:04d}-{when.month:02d}.jsonl"

    def append(self, events: Iterable[ChangeEvent]) -> int:
        """Record events. Returns how many were written."""
        by_month: dict[Path, list[str]] = {}
        count = 0
        for event in events:
            path = self._path(event.occurred_at.date())
            by_month.setdefault(path, []).append(json.dumps(event.as_dict()))
            count += 1
        if not count:
            return 0
        self._dir.mkdir(parents=True, exist_ok=True)
        for path, lines in by_month.items():
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        return count

    def read(self, *, since: date | None = None, until: date | None = None
             ) -> list[dict[str, Any]]:
        """Every journalled event in a date range, oldest first.

        A malformed line is skipped rather than aborting the read: a digest that
        refuses to render because one historical line is damaged is less useful
        than one that renders the rest.
        """
        rows: list[dict[str, Any]] = []
        for path in sorted(self._dir.glob("*.jsonl")) if self._dir.exists() else []:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    when = datetime.fromisoformat(row["occurred_at"]).date()
                except (json.JSONDecodeError, KeyError, ValueError):
                    logger.warning("skipping unreadable journal line", file=path.name)
                    continue
                if since is not None and when < since:
                    continue
                if until is not None and when > until:
                    continue
                rows.append(row)
        rows.sort(key=lambda r: str(r.get("occurred_at")))
        return rows

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.read())
