"""Pilot accounting: what was asked for, what it cost, and what came back.

Its own SQLite file, for the reason the research store has one. This records
money and model output; the trading database records positions and fills.
Putting a provider's raw response in the same file as an order table creates
exactly one useful property -- a single backup -- and several bad ones, the
worst being that a table named ``synthesis_raw`` sits one join away from
something executable.

Three tables, three jobs:

``synthesis_calls``
    Every attempt, billed or not, including the ones refused before dispatch.
    A budget that only records successes cannot answer "why did this month cost
    that much", because the expensive month is the one full of retries.

``synthesis_cache``
    Validated syntheses only. See :mod:`app.synthesis.cache`.

``synthesis_raw``
    What the provider actually returned, kept for manual scoring, marked with
    its verdict. Rejected output is retained deliberately -- a pilot that
    discards its failures cannot report a failure taxonomy -- and is stored
    under a status that no read path treats as a synthesis.

Nothing here holds a credential. The adapter never passes one in, and a test
asserts that no column of any row matches a key-shaped string.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_PATH: Final = Path("data/synthesis_ledger.db")

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS synthesis_calls (
    call_id                  TEXT PRIMARY KEY,
    requested_at             TEXT NOT NULL,
    month                    TEXT NOT NULL,
    provider                 TEXT NOT NULL,
    model                    TEXT NOT NULL,
    company_key              TEXT NOT NULL,
    as_of                    TEXT NOT NULL,
    packet_hash              TEXT NOT NULL,
    template_version         TEXT NOT NULL,
    schema_version           TEXT NOT NULL,
    estimated_input_tokens   INTEGER NOT NULL,
    max_output_tokens        INTEGER NOT NULL,
    estimated_usd            TEXT NOT NULL,
    actual_input_tokens      INTEGER,
    actual_output_tokens     INTEGER,
    actual_usd               TEXT,
    billed_usd               TEXT NOT NULL,
    status                   TEXT NOT NULL,
    failure                  TEXT,
    cache_hit                INTEGER NOT NULL DEFAULT 0,
    latency_ms               INTEGER
);
CREATE INDEX IF NOT EXISTS idx_calls_month ON synthesis_calls(month);

CREATE TABLE IF NOT EXISTS synthesis_cache (
    cache_key        TEXT PRIMARY KEY,
    company_key      TEXT NOT NULL,
    listing          TEXT,
    as_of            TEXT NOT NULL,
    packet_hash      TEXT NOT NULL,
    provider         TEXT NOT NULL,
    model            TEXT NOT NULL,
    schema_version   TEXT NOT NULL,
    template_version TEXT NOT NULL,
    config_hash      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    validated_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS synthesis_raw (
    raw_id        TEXT PRIMARY KEY,
    call_id       TEXT NOT NULL,
    stored_at     TEXT NOT NULL,
    company_key   TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    packet_hash   TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    failure       TEXT,
    raw_response  TEXT NOT NULL,
    candidate     TEXT,
    validated     TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_call ON synthesis_raw(call_id);
"""

VERDICT_VALID: Final = "VALID"
VERDICT_INVALID: Final = "INVALID"
"""The only two verdicts a stored raw response carries. ``INVALID`` is never
served: :meth:`SynthesisLedger.validated_from_cache` reads the cache table, and
the cache table only ever receives validated output."""

STATUS_DISPATCHED: Final = "DISPATCHED"
STATUS_OK: Final = "OK"
STATUS_VALIDATOR_REJECTED: Final = "VALIDATOR_REJECTED"
STATUS_PROVIDER_FAILED: Final = "PROVIDER_FAILED"
STATUS_REFUSED_BUDGET: Final = "REFUSED_BUDGET"
STATUS_CACHE_HIT: Final = "CACHE_HIT"
STATUS_NOT_APPLICABLE: Final = "NOT_APPLICABLE"
"""The status vocabulary, defined here because the ledger decides what is
charged and the service must write the strings the ledger sums. They were once
two lists that agreed by eye; they did not agree, and a month of rejected
responses reported zero spend."""

BILLABLE_STATUSES: Final[frozenset[str]] = frozenset(
    {STATUS_DISPATCHED, STATUS_OK, STATUS_VALIDATOR_REJECTED, STATUS_PROVIDER_FAILED}
)
"""Statuses that consumed provider capacity and are therefore charged.

``VALIDATOR_REJECTED`` is in the list. A response the validator threw away was
still generated and still billed, and a budget that only counts output it liked
understates a bad month exactly when it matters. ``DISPATCHED`` is in it too:
that row means a request left the machine and no response was ever recorded,
which is the crash case, and the conservative reading of a crash is that it was
billed. ``REFUSED_BUDGET`` and ``CACHE_HIT`` are absent because nothing was
sent.
"""


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One row of the ledger. Constructed before dispatch, updated after."""

    call_id: str
    requested_at: str
    month: str
    provider: str
    model: str
    company_key: str
    as_of: str
    packet_hash: str
    template_version: str
    schema_version: str
    estimated_input_tokens: int
    max_output_tokens: int
    estimated_usd: Decimal
    status: str
    billed_usd: Decimal
    actual_input_tokens: int | None = None
    actual_output_tokens: int | None = None
    actual_usd: Decimal | None = None
    failure: str | None = None
    cache_hit: bool = False
    latency_ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "requested_at": self.requested_at,
            "month": self.month,
            "provider": self.provider,
            "model": self.model,
            "company_key": self.company_key,
            "as_of": self.as_of,
            "packet_hash": self.packet_hash,
            "estimated_input_tokens": self.estimated_input_tokens,
            "actual_input_tokens": self.actual_input_tokens,
            "actual_output_tokens": self.actual_output_tokens,
            "estimated_usd": str(self.estimated_usd),
            "actual_usd": None if self.actual_usd is None else str(self.actual_usd),
            "billed_usd": str(self.billed_usd),
            "status": self.status,
            "failure": self.failure,
            "cache_hit": self.cache_hit,
            "latency_ms": self.latency_ms,
        }


class SynthesisLedger:
    """Append-mostly accounting for a pilot that must not overspend."""

    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self._path = Path(path)
        if self._path.parent != Path():
            self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A transaction on the ledger file.

        Public because :mod:`app.synthesis.cache` writes the cache table in
        the same file. One database, two modules, one way in -- rather than a
        second connection helper that could open it with different pragmas.
        """
        with self._connect() as conn:
            yield conn

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            with closing(conn), conn:
                yield conn
        finally:
            pass

    # -- calls ------------------------------------------------------------

    def record(self, call: CallRecord) -> None:
        """Write or overwrite one call row.

        Overwrite rather than insert-only because a call is recorded twice: once
        before dispatch with an estimate, once after with what the provider
        reported. A crash between the two leaves the estimate, which is the
        conservative direction.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO synthesis_calls (
                    call_id, requested_at, month, provider, model, company_key,
                    as_of, packet_hash, template_version, schema_version,
                    estimated_input_tokens, max_output_tokens, estimated_usd,
                    actual_input_tokens, actual_output_tokens, actual_usd,
                    billed_usd, status, failure, cache_hit, latency_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    call.call_id,
                    call.requested_at,
                    call.month,
                    call.provider,
                    call.model,
                    call.company_key,
                    call.as_of,
                    call.packet_hash,
                    call.template_version,
                    call.schema_version,
                    call.estimated_input_tokens,
                    call.max_output_tokens,
                    str(call.estimated_usd),
                    call.actual_input_tokens,
                    call.actual_output_tokens,
                    None if call.actual_usd is None else str(call.actual_usd),
                    str(call.billed_usd),
                    call.status,
                    call.failure,
                    int(call.cache_hit),
                    call.latency_ms,
                ),
            )

    def month_spend_usd(self, month: str) -> Decimal:
        """What has been charged in one calendar month, ``YYYY-MM``.

        Sums ``billed_usd``, which the caller sets to the provider's reported
        usage where it returned any and to the pre-call estimate where it did
        not. A timeout that may or may not have been billed counts as billed:
        the alternative is a cap that leaks under exactly the failure mode that
        produces the most calls.
        """
        # Placeholders only. The marks string is built from the length of a
        # frozen constant, never from a caller-supplied value.
        marks = ",".join("?" * len(BILLABLE_STATUSES))
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT billed_usd FROM synthesis_calls
                 WHERE month = ? AND cache_hit = 0 AND status IN ({marks})
                """,
                (month, *sorted(BILLABLE_STATUSES)),
            ).fetchall()
        return sum((Decimal(r["billed_usd"]) for r in row), Decimal(0))

    def calls(self, *, month: str | None = None) -> Sequence[CallRecord]:
        sql = "SELECT * FROM synthesis_calls"
        params: tuple[str, ...] = ()
        if month is not None:
            sql += " WHERE month = ?"
            params = (month,)
        sql += " ORDER BY requested_at, call_id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_call_from_row(r) for r in rows]

    # -- raw pilot output --------------------------------------------------

    def record_raw(
        self,
        *,
        raw_id: str,
        call_id: str,
        stored_at: str,
        company_key: str,
        as_of: str,
        packet_hash: str,
        verdict: str,
        raw_response: str,
        failure: str | None = None,
        candidate: dict[str, Any] | None = None,
        validated: dict[str, Any] | None = None,
    ) -> None:
        """Keep a provider response for manual scoring, marked with its verdict.

        ``INVALID`` rows exist so the pilot can report *why* things failed. They
        are written here and read only by the evaluation harness; no rendering
        path queries this table.
        """
        if verdict not in (VERDICT_VALID, VERDICT_INVALID):
            raise ValueError(f"verdict must be {VERDICT_VALID} or {VERDICT_INVALID}")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO synthesis_raw (
                    raw_id, call_id, stored_at, company_key, as_of, packet_hash,
                    verdict, failure, raw_response, candidate, validated
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    raw_id,
                    call_id,
                    stored_at,
                    company_key,
                    as_of,
                    packet_hash,
                    verdict,
                    failure,
                    raw_response,
                    None if candidate is None else json.dumps(candidate, sort_keys=True),
                    None if validated is None else json.dumps(validated, sort_keys=True),
                ),
            )

    def raw_responses(self, *, verdict: str | None = None) -> Sequence[dict[str, Any]]:
        sql = "SELECT * FROM synthesis_raw"
        params: tuple[str, ...] = ()
        if verdict is not None:
            sql += " WHERE verdict = ?"
            params = (verdict,)
        sql += " ORDER BY stored_at, raw_id"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _call_from_row(row: sqlite3.Row) -> CallRecord:
    return CallRecord(
        call_id=row["call_id"],
        requested_at=row["requested_at"],
        month=row["month"],
        provider=row["provider"],
        model=row["model"],
        company_key=row["company_key"],
        as_of=row["as_of"],
        packet_hash=row["packet_hash"],
        template_version=row["template_version"],
        schema_version=row["schema_version"],
        estimated_input_tokens=row["estimated_input_tokens"],
        max_output_tokens=row["max_output_tokens"],
        estimated_usd=Decimal(row["estimated_usd"]),
        actual_input_tokens=row["actual_input_tokens"],
        actual_output_tokens=row["actual_output_tokens"],
        actual_usd=None if row["actual_usd"] is None else Decimal(row["actual_usd"]),
        billed_usd=Decimal(row["billed_usd"]),
        status=row["status"],
        failure=row["failure"],
        cache_hit=bool(row["cache_hit"]),
        latency_ms=row["latency_ms"],
    )
