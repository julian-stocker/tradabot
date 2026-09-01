"""Is the fact store present, intact, and recent enough to answer with?

Four states, kept distinct on purpose
------------------------------------
``DATA_NOT_SYNCED``
    There is no file. Nothing is wrong; nothing has been fetched yet.
``DATA_CORRUPT``
    A file exists but cannot be read as this schema. Answering from it would
    produce numbers whose meaning is unknown, which is worse than refusing.
``DATA_STALE``
    Readable and structurally sound, but the newest filing it knows about is old
    enough that recent quarters are probably missing.
``READY``
    Usable.

Collapsing these into one "unhealthy" would hide the only thing the operator
needs to know: whether to run a sync, investigate a bad file, or do nothing.

This never fetches
------------------
Checking health is a local file operation. An Advisor request that quietly
reached the network when its data looked old would turn a 30-millisecond local
call into an unbounded one, and would do it at exactly the moment a user was
waiting for an answer.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import polars as pl

from app.fundamentals.concepts import FACT_COLUMNS, SCHEMA_VERSION
from app.fundamentals.sync import DEFAULT_STORE

STALE_AFTER_DAYS: int = 30
"""Across a universe of hundreds of filers, some company files almost every
week. A newest-filing date a month old means the sync stopped, not that the
market went quiet."""

_REQUIRED: frozenset[str] = frozenset(
    {"symbol", "metric", "concept", "unit", "value", "filed", "accession", "period_end"}
)
"""Columns without which the store cannot answer a point-in-time question.
``accepted`` is deliberately absent: it is provenance detail, and a store
predating it is degraded, not corrupt."""


class FactStoreStatus(StrEnum):
    NOT_SYNCED = "DATA_NOT_SYNCED"
    CORRUPT = "DATA_CORRUPT"
    STALE = "DATA_STALE"
    READY = "READY"


@dataclass(frozen=True, slots=True)
class FactStoreHealth:
    """What is known about the store on disk. Counts and dates, never facts."""

    path: str
    status: FactStoreStatus
    present: bool
    readable: bool
    rows: int = 0
    symbols: int = 0
    metrics: int = 0
    newest_filed: str | None = None
    oldest_filed: str | None = None
    newest_accepted: str | None = None
    oldest_accepted: str | None = None
    acceptance_coverage: float | None = None
    schema_version: str = SCHEMA_VERSION
    schema_hash: str | None = None
    size_bytes: int | None = None
    age_days: int | None = None
    detail: str | None = None
    missing_columns: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status is FactStoreStatus.READY

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": str(self.status),
            "present": self.present,
            "readable": self.readable,
            "rows": self.rows,
            "symbols": self.symbols,
            "metrics": self.metrics,
            "newest_filed": self.newest_filed,
            "oldest_filed": self.oldest_filed,
            "newest_accepted": self.newest_accepted,
            "oldest_accepted": self.oldest_accepted,
            "acceptance_coverage": self.acceptance_coverage,
            "schema_version": self.schema_version,
            "schema_hash": self.schema_hash,
            "size_bytes": self.size_bytes,
            "age_days": self.age_days,
            "missing_columns": list(self.missing_columns),
            "detail": self.detail,
            "notes": list(self.notes),
        }


def schema_hash(columns: tuple[str, ...] = FACT_COLUMNS) -> str:
    """Short digest of the column contract, to spot a file from another shape."""
    joined = "|".join(columns).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:12]


def health(
    path: str | Path = DEFAULT_STORE,
    *,
    as_of: date | None = None,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> FactStoreHealth:
    """Inspect the fact store without loading it into an Advisor.

    Args:
        path: parquet file to inspect.
        as_of: date staleness is measured against. Defaults to today, UTC.
        stale_after_days: how old the newest filing may be before STALE.
    """
    target = Path(path)
    if not target.exists():
        return FactStoreHealth(
            path=str(target),
            status=FactStoreStatus.NOT_SYNCED,
            present=False,
            readable=False,
            detail="no fact store on disk; run `tradabot fundamentals sync`",
            schema_hash=schema_hash(),
        )

    size = target.stat().st_size
    try:
        frame = pl.read_parquet(target)
    except Exception as exc:
        return FactStoreHealth(
            path=str(target),
            status=FactStoreStatus.CORRUPT,
            present=True,
            readable=False,
            size_bytes=size,
            detail=f"unreadable parquet ({type(exc).__name__})",
            schema_hash=schema_hash(),
        )

    missing = tuple(sorted(_REQUIRED - set(frame.columns)))
    if missing or frame.height == 0:
        return FactStoreHealth(
            path=str(target),
            status=FactStoreStatus.CORRUPT,
            present=True,
            readable=True,
            rows=frame.height,
            size_bytes=size,
            missing_columns=missing,
            detail=(
                f"missing required columns: {', '.join(missing)}"
                if missing
                else "fact store is empty"
            ),
            schema_hash=schema_hash(tuple(frame.columns)),
        )

    notes: list[str] = []
    filed = frame["filed"].drop_nulls()
    newest_filed = str(filed.max()) if filed.len() else None
    oldest_filed = str(filed.min()) if filed.len() else None

    newest_accepted = oldest_accepted = None
    coverage: float | None = None
    if "accepted" in frame.columns:
        accepted = frame["accepted"].drop_nulls()
        coverage = accepted.len() / frame.height
        if accepted.len():
            newest_accepted, oldest_accepted = str(accepted.max()), str(accepted.min())
        else:
            notes.append("no acceptance timestamps recorded")
    else:
        notes.append(
            "store predates the acceptance-timestamp column; provenance is "
            "degraded but point-in-time filing dates are intact"
        )

    today = as_of or datetime.now(UTC).date()
    age: int | None = None
    if newest_filed:
        try:
            age = (today - date.fromisoformat(newest_filed[:10])).days
        except ValueError:
            notes.append(f"unparseable newest filing date {newest_filed!r}")

    status = FactStoreStatus.READY
    detail = None
    if age is not None and age > stale_after_days:
        status = FactStoreStatus.STALE
        detail = (
            f"newest filing is {age} days old (> {stale_after_days}); "
            "run `tradabot fundamentals sync`"
        )

    return FactStoreHealth(
        path=str(target),
        status=status,
        present=True,
        readable=True,
        rows=frame.height,
        symbols=int(frame["symbol"].n_unique()),
        metrics=int(frame["metric"].n_unique()),
        newest_filed=newest_filed,
        oldest_filed=oldest_filed,
        newest_accepted=newest_accepted,
        oldest_accepted=oldest_accepted,
        acceptance_coverage=coverage,
        schema_hash=schema_hash(tuple(frame.columns)),
        size_bytes=size,
        age_days=age,
        detail=detail,
        notes=tuple(notes),
    )


def latest_filings(
    path: str | Path = DEFAULT_STORE,
    *,
    symbols: Sequence[str] | None = None,
    as_of: str | None = None,
) -> dict[str, dict[str, Any]]:
    """The most recent filing known for each symbol, by filing date.

    Point-in-time: ``as_of`` bounds the result to filings visible on that date,
    so a historical replay cannot see a document that had not been filed yet.

    Returns:
        ``{symbol: {"accession", "form", "filed", "accepted"}}``. Symbols with no
        visible filing are absent rather than present with nulls.
    """
    target = Path(path)
    if not target.exists():
        return {}
    columns = ["symbol", "accession", "form", "filed"]
    frame = pl.read_parquet(target)
    if "accepted" in frame.columns:
        columns.append("accepted")
    frame = frame.select(columns).drop_nulls(["filed", "accession"])
    if symbols is not None:
        frame = frame.filter(pl.col("symbol").is_in(list(symbols)))
    if as_of is not None:
        frame = frame.filter(pl.col("filed") <= as_of)
    if frame.height == 0:
        return {}
    # Sorting by filing date then accession makes the winner deterministic when
    # a company files several documents on one day.
    newest = (
        frame.sort(["symbol", "filed", "accession"]).group_by("symbol", maintain_order=True).last()
    )
    return {
        str(row["symbol"]): {k: row.get(k) for k in columns if k != "symbol"}
        for row in newest.iter_rows(named=True)
    }
