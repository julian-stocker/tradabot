"""The event store: append-only, point-in-time, idempotent.

Its own SQLite file under ``data/``, deliberately not a table in
``tradabot.db``. Three reasons: the trading database is four gigabytes and
carries the paper-trading and market-data schema under Alembic control, so a
research table there inherits migration risk it does not need; the store is
rebuildable from SEC at any time, which is a different durability class from
trade records; and keeping it separate means a corrupt or discarded event store
cannot take the trading database with it.

Point-in-time is the load-bearing property
------------------------------------------
Every read takes an ``as_of`` and filters on ``published_at <= as_of``. An
event that became public after the moment being asked about is not returned,
which is the same discipline ``FactStore._known`` applies with ``filed <=
as_of``. Without it, a question asked about last quarter would be answered with
this quarter's filings and the answer would look entirely normal.

Nothing is ever deleted
-----------------------
An amendment does not overwrite the filing it amends, and a superseded event
stays with ``superseded_at`` set. The store's job is to make historical state
reconstructable, and a row removed because a newer one arrived destroys exactly
that.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.research_intelligence.schemas import (
    Confidence,
    EventKind,
    EventScope,
    EvidenceReference,
    HistoricalEvidence,
    Materiality,
    MaterialityContext,
    QuarantinedFiling,
    ResearchEvent,
    SourceQuality,
    SourceType,
)

logger = get_logger(__name__)

DEFAULT_PATH = Path("data/research_events.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_events (
    event_id             TEXT PRIMARY KEY,
    company_id           INTEGER NOT NULL,
    company_key          TEXT NOT NULL,
    cik                  TEXT NOT NULL,
    scope                TEXT NOT NULL,
    event_kind           TEXT NOT NULL,
    occurred_at          TEXT,
    published_at         TEXT NOT NULL,
    fetched_at           TEXT NOT NULL,
    source_type          TEXT NOT NULL,
    source_quality       TEXT NOT NULL,
    source_url           TEXT NOT NULL,
    source_document_id   TEXT NOT NULL,
    source_hash          TEXT NOT NULL,
    form                 TEXT NOT NULL,
    accession            TEXT NOT NULL,
    item_codes           TEXT NOT NULL,
    classifying_item     TEXT,
    title                TEXT,
    fact_summary         TEXT NOT NULL,
    evidence             TEXT NOT NULL,
    materiality          TEXT NOT NULL,
    materiality_context  TEXT NOT NULL,
    materiality_detail   TEXT,
    source_confidence    TEXT NOT NULL,
    extraction_confidence TEXT NOT NULL,
    historical_evidence  TEXT NOT NULL,
    supersedes_event_id  TEXT,
    amends_accession     TEXT,
    superseded_at        TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_company ON research_events (company_id, published_at);
CREATE INDEX IF NOT EXISTS ix_events_kind    ON research_events (event_kind, published_at);
CREATE INDEX IF NOT EXISTS ix_events_pub     ON research_events (published_at);
CREATE INDEX IF NOT EXISTS ix_events_acc     ON research_events (accession);

CREATE TABLE IF NOT EXISTS quarantined_filings (
    cik        TEXT NOT NULL,
    accession  TEXT NOT NULL,
    form       TEXT NOT NULL,
    reason     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (cik, accession)
);
"""

_COLUMNS = (
    "event_id",
    "company_id",
    "company_key",
    "cik",
    "scope",
    "event_kind",
    "occurred_at",
    "published_at",
    "fetched_at",
    "source_type",
    "source_quality",
    "source_url",
    "source_document_id",
    "source_hash",
    "form",
    "accession",
    "item_codes",
    "classifying_item",
    "title",
    "fact_summary",
    "evidence",
    "materiality",
    "materiality_context",
    "materiality_detail",
    "source_confidence",
    "extraction_confidence",
    "historical_evidence",
    "supersedes_event_id",
    "amends_accession",
)


def _row(event: ResearchEvent) -> tuple[Any, ...]:
    return (
        event.event_id,
        event.company_id,
        event.company_key,
        event.cik,
        str(event.scope),
        str(event.event_kind),
        event.occurred_at,
        event.published_at,
        event.fetched_at,
        str(event.source_type),
        str(event.source_quality),
        event.source_url,
        event.source_document_id,
        event.source_hash,
        event.form,
        event.accession,
        json.dumps(list(event.item_codes)),
        event.classifying_item,
        event.title,
        event.fact_summary,
        json.dumps([e.as_dict() for e in event.evidence]),
        str(event.materiality),
        str(event.materiality_context),
        event.materiality_detail,
        str(event.source_confidence),
        str(event.extraction_confidence),
        str(event.historical_evidence),
        event.supersedes_event_id,
        event.amends_accession,
    )


def _event(row: sqlite3.Row) -> ResearchEvent:
    return ResearchEvent(
        event_id=row["event_id"],
        company_id=row["company_id"],
        company_key=row["company_key"],
        cik=row["cik"],
        scope=EventScope(row["scope"]),
        event_kind=EventKind(row["event_kind"]),
        occurred_at=row["occurred_at"],
        published_at=row["published_at"],
        fetched_at=row["fetched_at"],
        source_type=SourceType(row["source_type"]),
        source_quality=SourceQuality(row["source_quality"]),
        source_url=row["source_url"],
        source_document_id=row["source_document_id"],
        source_hash=row["source_hash"],
        form=row["form"],
        accession=row["accession"],
        item_codes=tuple(json.loads(row["item_codes"])),
        classifying_item=row["classifying_item"],
        title=row["title"],
        fact_summary=row["fact_summary"],
        evidence=tuple(EvidenceReference(**e) for e in json.loads(row["evidence"])),
        materiality=Materiality(row["materiality"]),
        materiality_context=MaterialityContext(row["materiality_context"]),
        materiality_detail=row["materiality_detail"],
        source_confidence=Confidence(row["source_confidence"]),
        extraction_confidence=Confidence(row["extraction_confidence"]),
        historical_evidence=HistoricalEvidence(row["historical_evidence"]),
        supersedes_event_id=row["supersedes_event_id"],
        amends_accession=row["amends_accession"],
    )


class EventStore:
    """Persistent, point-in-time research events.

    Args:
        path: SQLite file. ``:memory:`` is accepted for tests.
    """

    def __init__(self, path: str | Path = DEFAULT_PATH) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._memory = sqlite3.connect(":memory:") if self._path == ":memory:" else None
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> Any:
        if self._memory is not None:
            # An in-memory database vanishes when its connection closes, so the
            # test store keeps one open and hands out a no-op context manager.
            return _Keep(self._memory)
        return closing(sqlite3.connect(self._path))

    # ------------------------------------------------------------------ writes
    def upsert(self, events: Sequence[ResearchEvent]) -> int:
        """Store events, returning how many were **new**.

        ``INSERT OR IGNORE`` on the deterministic primary key is what makes
        re-ingestion idempotent: the second write of the same filing matches
        every existing identity and inserts nothing.
        """
        if not events:
            return 0
        placeholders = ",".join("?" * len(_COLUMNS))
        sql = (
            f"INSERT OR IGNORE INTO research_events ({','.join(_COLUMNS)}) VALUES ({placeholders})"
        )
        with self._connect() as connection:
            before = connection.execute("SELECT COUNT(*) FROM research_events").fetchone()[0]
            connection.executemany(sql, [_row(e) for e in events])
            connection.commit()
            after = connection.execute("SELECT COUNT(*) FROM research_events").fetchone()[0]
        return int(after - before)

    def quarantine(self, filings: Sequence[QuarantinedFiling]) -> int:
        if not filings:
            return 0
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO quarantined_filings "
                "(cik, accession, form, reason, fetched_at) VALUES (?,?,?,?,?)",
                [(f.cik, f.accession, f.form, f.reason, f.fetched_at) for f in filings],
            )
            connection.commit()
        return len(filings)

    def mark_superseded(self, event_id: str, *, by: str, when: str) -> None:
        """Record that an event was superseded. **The row stays.**"""
        with self._connect() as connection:
            connection.execute(
                "UPDATE research_events SET superseded_at = ? WHERE event_id = ?",
                (when, event_id),
            )
            connection.execute(
                "UPDATE research_events SET supersedes_event_id = ? WHERE event_id = ?",
                (event_id, by),
            )
            connection.commit()

    # ------------------------------------------------------------------- reads
    def _query(self, where: str, params: Sequence[Any]) -> list[ResearchEvent]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT * FROM research_events WHERE {where} ORDER BY published_at DESC, event_id",
                tuple(params),
            ).fetchall()
        return [_event(r) for r in rows]

    def events_for_company(
        self, company_id: int, *, as_of: str, limit: int | None = None
    ) -> list[ResearchEvent]:
        """Everything known about a company **as of** a moment.

        ``published_at <= as_of`` is not an optimisation. It is what makes a
        question about the past answerable with what was public then.
        """
        events = self._query("company_id = ? AND published_at <= ?", (company_id, as_of))
        return events[:limit] if limit else events

    def events_by_kind(
        self, kind: EventKind, *, as_of: str, limit: int | None = None
    ) -> list[ResearchEvent]:
        events = self._query("event_kind = ? AND published_at <= ?", (str(kind), as_of))
        return events[:limit] if limit else events

    def recent_events(self, company_id: int, *, as_of: str, since: str) -> list[ResearchEvent]:
        return self._query(
            "company_id = ? AND published_at <= ? AND published_at >= ?",
            (company_id, as_of, since),
        )

    def events_for_accession(self, accession: str) -> list[ResearchEvent]:
        return self._query("accession = ?", (accession,))

    def get(self, event_id: str) -> ResearchEvent | None:
        found = self._query("event_id = ?", (event_id,))
        return found[0] if found else None

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM research_events").fetchone()[0])

    def quarantined(self) -> list[QuarantinedFiling]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM quarantined_filings").fetchall()
        return [
            QuarantinedFiling(
                cik=r["cik"],
                accession=r["accession"],
                form=r["form"],
                reason=r["reason"],
                fetched_at=r["fetched_at"],
            )
            for r in rows
        ]

    def size_bytes(self) -> int:
        if self._path == ":memory:":
            return 0
        path = Path(self._path)
        return path.stat().st_size if path.exists() else 0


class _Keep:
    """Context manager that yields a connection without closing it."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self._connection

    def __exit__(self, *_exc: object) -> None:
        return None
