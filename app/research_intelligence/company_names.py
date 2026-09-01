"""Recovering issuer display names from the payload ingestion already fetches.

The gap
-------
988 CIK-backed companies carry ticker-shaped display names -- ``'NVDA'`` where
``'NVIDIA CORP'`` belongs. The authoritative name was never unavailable: the SEC
submissions document has always carried ``name``, and
:meth:`~app.fundamentals.client.EdgarClient.profile` fetched it and returned it
alongside the SIC that *was* kept. The name was simply dropped on the floor.

Phase 13.7 showed what that costs. ``/check CNR`` offers a choice between a
field labelled ``CNI`` and one labelled ``CNR`` -- two tickers, no company
names, on the exact card whose purpose is to stop the reader picking the wrong
company.

Why this module plans rather than writes
----------------------------------------
Event ingestion already holds the authoritative payload, so recovering the name
costs no extra request. Applying it to 988 rows is still a bulk mutation of
shared state, so the two halves are separated: :func:`plan` is pure and returns
what *would* change, and :func:`apply` performs the write only when a caller
asks. A plan can be read and argued with; a migration that ran on import cannot.

The rules, all of them refusals
-------------------------------
* **CIK is the only key.** Never the ticker -- that is what mis-resolves --
  and never the name, which would invite fuzzy matching.
* **No merging.** This changes a display string on an existing row. It never
  joins two company rows, however alike their names.
* **Curated names win.** A row whose current name is not ticker-shaped was set
  deliberately (``Deutsche Telekom AG``, ``SPDR S&P 500 ETF Trust``) and is
  left alone.
* **No CIK, no change.** ``AEP`` and ``BRK.B`` carry no CIK, so there is no
  authoritative source and they stay as they are rather than being guessed.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

TICKER_SHAPED = re.compile(r"^[A-Z0-9.\-]{1,6}$")
"""What a display name looks like when it is really a ticker. Deliberately the
same shape the symbol validator accepts, so nothing longer or lower-cased --
that is, nothing a human wrote -- can match."""


@dataclass(frozen=True, slots=True)
class NameChange:
    """One proposed rename, with the evidence for it."""

    company_id: int
    cik: str
    current: str
    proposed: str
    source: str = "SEC submissions entityName"

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "cik": self.cik,
            "current": self.current,
            "proposed": self.proposed,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class NamePlan:
    """What a backfill would do. Nothing has been written."""

    changes: tuple[NameChange, ...]
    kept_curated: tuple[str, ...]
    no_authority: tuple[str, ...]
    """Companies with no CIK, so no authoritative name exists. Left alone."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "changes": [c.as_dict() for c in self.changes],
            "kept_curated": list(self.kept_curated),
            "no_authority": list(self.no_authority),
        }


def is_ticker_shaped(name: str | None) -> bool:
    return bool(name) and bool(TICKER_SHAPED.fullmatch(name or ""))


def plan(
    rows: Sequence[tuple[int, str | None, str | None]],
    entity_names: dict[str, str],
) -> NamePlan:
    """What would change, given company rows and SEC entity names by CIK.

    Pure. ``rows`` are ``(company_id, cik, current_name)``; ``entity_names``
    maps a zero-padded CIK to the name SEC holds for that registrant.
    """
    changes: list[NameChange] = []
    curated: list[str] = []
    unknown: list[str] = []
    for company_id, cik, current in rows:
        if not cik:
            unknown.append(current or str(company_id))
            continue
        if not is_ticker_shaped(current):
            curated.append(current or "")
            continue
        proposed = entity_names.get(str(cik).zfill(10), "").strip()
        if not proposed or proposed == current:
            continue
        changes.append(
            NameChange(
                company_id=company_id,
                cik=str(cik).zfill(10),
                current=current or "",
                proposed=proposed,
            )
        )
    return NamePlan(
        changes=tuple(changes),
        kept_curated=tuple(curated),
        no_authority=tuple(unknown),
    )


def read_rows(database: str) -> list[tuple[int, str | None, str | None]]:
    """``(company_id, cik, name)`` for every company. Read-only connection."""
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return [
            (int(r[0]), r[1], r[2])
            for r in connection.execute("SELECT id, cik, name FROM companies")
        ]
    finally:
        connection.close()


def apply(database: str, plan_: NamePlan) -> int:
    """Write a plan. Returns rows changed.

    Separated from :func:`plan` so that producing the proposal and mutating
    shared state are two decisions rather than one.
    """
    if not plan_.changes:
        return 0
    connection = sqlite3.connect(database)
    try:
        connection.executemany(
            "UPDATE companies SET name = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND cik = ?",
            [(c.proposed, c.company_id, c.cik) for c in plan_.changes],
        )
        connection.commit()
    finally:
        connection.close()
    logger.info("company names backfilled", changed=len(plan_.changes))
    return len(plan_.changes)
