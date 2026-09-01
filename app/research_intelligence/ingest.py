"""Keeping the research store current without anyone running anything.

Discovery is the whole design problem
-------------------------------------
The obvious recurring job asks each company "anything new?" -- 989 submissions
requests per pass, whether or not a single filing was made, roughly two minutes
of SEC's rate budget to usually learn nothing. Run hourly that is 23,700
requests a day to discover perhaps forty filings.

EDGAR publishes a documented alternative: a **daily dissemination index**, one
file per trading day listing every filing accepted that day with its CIK, form
and accession. Measured across five trading days it is ~540 KB and ~6,100 rows,
of which ~40 belong to companies in this universe with forms worth ingesting.

So discovery costs **one request per day**, and the work after it scales with
what was actually filed rather than with how many companies exist -- about 43
requests a day in steady state against 23,700. The index is used only to learn
*which* companies filed; every fact about a filing still comes from the
documented submissions API, so identity, item codes and acceptance timestamps
are unchanged and point-in-time semantics are untouched.

Two modes, for two different jobs
---------------------------------
``INDEX`` is the recurring mode above. ``SWEEP`` polls companies directly and
exists for two cases the index cannot serve: the first run, when there is no
history to be incremental against, and an operator asking about one company.

What a failure may and may not cost
-----------------------------------
One company must never cost the run, and one failed exhibit must never cost a
filing's classification. Those are separate guarantees and they are kept
separately: a company failure is caught per company and backs that company off
alone, while evidence failure leaves the already-stored ResearchEvent alone and
marks the accession retryable. A checkpoint advances only after everything that
company owed has been written.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from app.core.logging import get_logger
from app.research_intelligence import extraction
from app.research_intelligence.freshness import is_current
from app.research_intelligence.identity import CompanyResolver
from app.research_intelligence.schemas import EvidenceStatus, ResearchEvent
from app.research_intelligence.universe import ResearchTarget, ResearchUniverse

logger = get_logger(__name__)

INGEST_FORMS: Final[frozenset[str]] = frozenset({"8-K", "6-K", "20-F", "40-F", "10-K", "10-Q"})
"""Base forms worth a submissions fetch. The same set Phase 15.0 ingests, so
the index filter cannot quietly widen or narrow what gets classified."""

MAX_CATCHUP_DAYS: Final = 10
"""Index days one run will process. A laptop that was closed for a fortnight
catches up over several runs instead of firing a two-week burst at SEC in one."""

MAX_RETRY_COMPANIES: Final = 25
"""Companies per run pulled in solely to finish unfinished evidence. Bounded so
a backlog drains steadily rather than turning a quiet run into a long one."""

BACKOFF_MINUTES: Final[tuple[int, ...]] = (15, 60, 240, 720, 1440)
"""Per-company backoff by consecutive failure count, capped at a day. A company
SEC cannot answer for is asked less often, never abandoned."""

LOCK_PATH: Final = Path("data/research_ingest.lock")


class IngestionMode(StrEnum):
    INDEX = "INDEX"
    """Daily-index discovery. The recurring mode."""
    SWEEP = "SWEEP"
    """Poll companies directly. Bootstrap, and operator-targeted runs."""


class RunStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    """Completed, with at least one company failing. Not an alert on its own."""
    FAILED = "FAILED"
    SKIPPED_ALREADY_RUNNING = "SKIPPED_ALREADY_RUNNING"
    DRY_RUN = "DRY_RUN"


class AccessionState(StrEnum):
    """How far deterministic processing of one filing has got.

    The distinction that matters is between *seen* and *finished*. A naive
    "have I got this accession?" check treats a filing whose evidence fetch died
    as done, and it is never revisited -- so the states below separate
    classification from enrichment, and only ``COMPLETE`` and
    ``PERMANENT_REFUSAL`` stop a filing being picked up again.
    """

    DISCOVERED = "DISCOVERED"
    EVENTS_STORED = "EVENTS_STORED"
    EVIDENCE_PARTIAL = "EVIDENCE_PARTIAL"
    COMPLETE = "COMPLETE"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_REFUSAL = "PERMANENT_REFUSAL"
    """Content this pipeline cannot read -- a PDF exhibit. Refetching it every
    night would spend SEC's budget re-learning the same answer."""


_TERMINAL: Final[frozenset[AccessionState]] = frozenset(
    {AccessionState.COMPLETE, AccessionState.PERMANENT_REFUSAL}
)

_EVIDENCE_OUTCOMES: Final[dict[EvidenceStatus, AccessionState]] = {
    EvidenceStatus.OK: AccessionState.COMPLETE,
    EvidenceStatus.NO_RELEVANT_EXHIBIT: AccessionState.COMPLETE,
    EvidenceStatus.AMBIGUOUS_DOCUMENT: AccessionState.COMPLETE,
    EvidenceStatus.CHANGED_SOURCE_CONTENT: AccessionState.COMPLETE,
    EvidenceStatus.UNSUPPORTED_CONTENT_TYPE: AccessionState.PERMANENT_REFUSAL,
    EvidenceStatus.CONTENT_FETCH_FAILED: AccessionState.RETRYABLE_FAILURE,
}
"""Evidence outcome to filing state. Three of these are *finished* rather than
successful: a filing with no citable exhibit, one whose exhibit could not be
chosen safely, and one whose archived bytes changed have each had every
deterministic thing done to them that this pipeline will ever do."""


# --------------------------------------------------------------- daily index
_INDEX_ROW = re.compile(r"^(\d+)\|([^|]*)\|([^|]+)\|(\d{8})\|(.+)$")


@dataclass(frozen=True, slots=True)
class IndexEntry:
    cik: str
    form: str
    accession: str
    filed: str


def parse_daily_index(raw: bytes) -> list[IndexEntry]:
    """Rows from one day's dissemination index. Pure, and treats it as data.

    The file is pipe-delimited text SEC generates, and it is parsed with a fixed
    pattern rather than trusted: a row that does not match is skipped, and the
    accession it yields is only ever compared against accessions this system
    already knows or passed to the documented submissions API.
    """
    entries: list[IndexEntry] = []
    for line in raw.decode("latin-1").splitlines():
        match = _INDEX_ROW.match(line.strip())
        if match is None:
            continue
        cik, _name, form, filed, path = match.groups()
        accession = Path(path).stem
        if len(accession) != 20 or accession.count("-") != 2:  # noqa: PLR2004
            continue
        entries.append(
            IndexEntry(cik=cik.zfill(10), form=form.strip(), accession=accession, filed=filed)
        )
    return entries


def base_form(form: str) -> str:
    return form[:-2] if form.endswith("/A") else form


# ------------------------------------------------------------------- results
@dataclass(frozen=True, slots=True)
class CompanyResult:
    """What one company's ingestion did, or why it did not."""

    company_id: int
    cik: str
    ok: bool
    accessions_new: int = 0
    events_new: int = 0
    documents_new: int = 0
    facts_new: int = 0
    unchanged: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class IngestionRun:
    """One run, in enough detail to explain stale research coverage later."""

    run_id: str
    started_at: str
    mode: IngestionMode
    status: RunStatus
    finished_at: str | None = None
    companies_attempted: int = 0
    companies_succeeded: int = 0
    companies_failed: int = 0
    companies_unchanged: int = 0
    accessions_discovered: int = 0
    events_created: int = 0
    documents_created: int = 0
    facts_created: int = 0
    network_requests: int = 0
    retries: int = 0
    duration_seconds: float = 0.0
    cursor_date: str | None = None
    detail: str | None = None
    failures: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "mode": str(self.mode),
            "status": str(self.status),
            "companies_attempted": self.companies_attempted,
            "companies_succeeded": self.companies_succeeded,
            "companies_failed": self.companies_failed,
            "companies_unchanged": self.companies_unchanged,
            "accessions_discovered": self.accessions_discovered,
            "events_created": self.events_created,
            "documents_created": self.documents_created,
            "facts_created": self.facts_created,
            "network_requests": self.network_requests,
            "retries": self.retries,
            "duration_seconds": round(self.duration_seconds, 1),
            "cursor_date": self.cursor_date,
            "detail": self.detail,
            "failures": list(self.failures),
        }

    def persistable(self) -> dict[str, Any]:
        """The columns ``ingestion_runs`` holds. ``failures`` is not one."""
        row = self.as_dict()
        row.pop("run_id")
        row.pop("started_at")
        row.pop("mode")
        row.pop("failures")
        return row


class IngestionHealth(StrEnum):
    """Whether ingestion itself is current -- a different question from whether
    a company has filed anything."""

    CURRENT = "CURRENT"
    STALE = "STALE"
    DEGRADED = "DEGRADED"
    """Running, and failing often enough that coverage is drifting."""
    NEVER_RUN = "NEVER_RUN"


STALE_AFTER_HOURS: Final = 36
"""Hours since the last successful run before coverage reads as stale.

Longer than any sensible cadence and shorter than a weekend: EDGAR publishes no
index on Saturday or Sunday, so a Friday-evening run is still the freshest
possible state on Sunday afternoon and must not be reported as stale."""

DEGRADED_FAILURE_RATE: Final = 0.25


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: IngestionHealth
    last_success_at: str | None = None
    age_hours: float | None = None
    detail: str | None = None

    @property
    def current(self) -> bool:
        return self.status is IngestionHealth.CURRENT

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "last_success_at": self.last_success_at,
            "age_hours": round(self.age_hours, 1) if self.age_hours is not None else None,
            "detail": self.detail,
        }


def health(store: Any, *, now: datetime | None = None) -> HealthReport:
    """Whether the research store is being kept current. **Never raises.**

    Deliberately answered from run history rather than from event recency: a
    universe with no filings this week is not stale, and a scheduler that has
    been failing for three days is -- and those look identical from the events
    alone.
    """
    moment = now or datetime.now(UTC)
    try:
        last = store.last_run(status=str(RunStatus.OK)) or store.last_run(
            status=str(RunStatus.DEGRADED)
        )
    except Exception:
        return HealthReport(IngestionHealth.NEVER_RUN, detail="run history unreadable")
    if last is None or not last.get("finished_at"):
        return HealthReport(IngestionHealth.NEVER_RUN, detail="ingestion has never completed")
    finished = _moment(str(last["finished_at"]))
    if finished is None:
        return HealthReport(IngestionHealth.NEVER_RUN, detail="run history unreadable")
    age = (moment - finished).total_seconds() / 3600
    if age > STALE_AFTER_HOURS:
        return HealthReport(
            IngestionHealth.STALE,
            str(last["finished_at"]),
            age,
            detail=f"last successful ingestion {age / 24:.1f} days ago",
        )
    attempted = int(last.get("companies_attempted") or 0)
    failed = int(last.get("companies_failed") or 0)
    if attempted and failed / attempted > DEGRADED_FAILURE_RATE:
        return HealthReport(
            IngestionHealth.DEGRADED,
            str(last["finished_at"]),
            age,
            detail=f"{failed} of {attempted} companies failed on the last run",
        )
    return HealthReport(IngestionHealth.CURRENT, str(last["finished_at"]), age)


def _moment(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------- lock
class AlreadyRunningError(RuntimeError):
    """Another ingestion run holds the lock."""


@contextmanager
def single_run(path: Path = LOCK_PATH) -> Iterator[None]:
    """Exclusive run lock, released by the OS if the process dies.

    ``flock`` rather than a lock file with a timestamp in it: the kernel drops
    the lock when the holder exits however it exits, so a killed run leaves
    nothing stale behind and there is no recovery heuristic to get wrong.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            msg = "another research ingestion run is in progress"
            raise AlreadyRunningError(msg) from exc
        os.write(handle, f"{os.getpid()}\n".encode())
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


# ----------------------------------------------------------------- ingestor
@dataclass
class _Tally:
    accessions: int = 0
    events: int = 0
    documents: int = 0
    facts: int = 0
    failures: list[str] = field(default_factory=list)


class IncrementalResearchIngestor:
    """Discovers new filings and hands them to the existing Phase 15 services.

    Classifies nothing and extracts nothing of its own: event classification,
    identity resolution, exhibit selection and fact extraction all stay where
    they are, and this decides only *what to feed them and when*.

    Args:
        universe: eligible registrants.
        source: documented submissions access.
        client: the shared :class:`~app.fundamentals.client.EdgarClient`, which
            owns the declared User-Agent and the rate limit. Not a second one.
        store: the research store.
        evidence: the Phase 15.1 evidence pipeline, or ``None`` to classify only.
        dry_run: resolve and count, write nothing.
    """

    def __init__(
        self,
        *,
        universe: ResearchUniverse,
        source: Any,
        client: Any,
        store: Any,
        evidence: Any = None,
        dry_run: bool = False,
    ) -> None:
        self._universe = universe
        self._source = source
        self._client = client
        self._store = store
        self._evidence = evidence
        self._dry = dry_run
        self.requests = 0

    # ------------------------------------------------------------------ run
    def run(
        self,
        *,
        mode: IngestionMode = IngestionMode.INDEX,
        companies: Sequence[ResearchTarget] | None = None,
        since: str | None = None,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> IngestionRun:
        """One ingestion pass. **Never raises for a per-company failure.**"""
        started = now or datetime.now(UTC)
        run_id = hashlib.sha256(f"{started.isoformat()}|{mode}".encode()).hexdigest()[:16]
        cursor: str | None = None
        scope: dict[int, set[str]] = {}

        if companies is not None:
            targets = list(companies)
        elif mode is IngestionMode.SWEEP:
            targets = list(self._universe.targets)
        else:
            targets, scope, cursor = self._from_index(started)
            unfinished = self._with_unfinished_work(targets)
            targets.extend(unfinished)
            # A company pulled in only to finish old work is scoped to exactly
            # that work, so a retry never becomes a history rebuild either.
            for target in unfinished:
                scope[target.company_id] = {
                    str(row["accession"])
                    for row in self._store.retryable_accessions(target.company_id)
                }
        if limit is not None:
            targets = targets[:limit]

        if not self._dry:
            self._store.start_run(run_id, started_at=started.isoformat(), mode=str(mode))

        tally = _Tally()
        succeeded = unchanged = 0
        for target in targets:
            result = self._company(
                target, since=since, only=scope.get(target.company_id), now=started
            )
            if result.ok:
                succeeded += 1
                unchanged += 1 if result.unchanged else 0
                tally.accessions += result.accessions_new
                tally.events += result.events_new
                tally.documents += result.documents_new
                tally.facts += result.facts_new
            else:
                tally.failures.append(f"{target.cik}: {result.error}")

        finished = datetime.now(UTC) if now is None else now
        failed = len(targets) - succeeded
        status = (
            RunStatus.DRY_RUN if self._dry else (RunStatus.DEGRADED if failed else RunStatus.OK)
        )
        run = IngestionRun(
            run_id=run_id,
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            mode=mode,
            status=status,
            companies_attempted=len(targets),
            companies_succeeded=succeeded,
            companies_failed=failed,
            companies_unchanged=unchanged,
            accessions_discovered=tally.accessions,
            events_created=tally.events,
            documents_created=tally.documents,
            facts_created=tally.facts,
            network_requests=self.requests,
            duration_seconds=(finished - started).total_seconds(),
            cursor_date=cursor,
            failures=tuple(tally.failures[:20]),
        )
        if not self._dry:
            self._store.finish_run(run_id, run.persistable())
        return run

    # -------------------------------------------------------------- discovery
    def _from_index(
        self, now: datetime
    ) -> tuple[list[ResearchTarget], dict[int, set[str]], str | None]:
        """Companies that filed since the cursor, and exactly what they filed.

        The accessions are carried out with the targets, not discarded. The
        index *is* the delta: without it, a company that filed once yesterday
        would be re-examined against its whole inline submissions history --
        measured at 9,551 events re-created across forty companies, reaching
        filings from 1996.
        """
        by_cik = self._universe.by_cik
        found: dict[str, ResearchTarget] = {}
        delta: dict[int, set[str]] = {}
        cursor: str | None = None
        for day in self._pending_days(now):
            self.requests += 1  # counted before the call: a 403 is still a request
            try:
                raw = self._client.daily_index(day)
            except Exception as exc:
                logger.info(
                    "daily index unavailable", day=day.isoformat(), reason=type(exc).__name__
                )
                if day >= now.date():
                    # Today's index is published after the day closes. Stopping
                    # *without* advancing is the point: the cursor must never
                    # move past a day whose filings were not read.
                    break
                # A past day with no index never had one -- a weekend or a
                # market holiday. Advancing past it is required, not optional:
                # stopping here would wedge the cursor on the first Saturday and
                # the job would never reach Monday again.
                cursor = day.isoformat()
                continue
            for entry in parse_daily_index(raw):
                target = by_cik.get(entry.cik)
                if target is not None and base_form(entry.form) in INGEST_FORMS:
                    found.setdefault(entry.cik, target)
                    delta.setdefault(target.company_id, set()).add(entry.accession)
            cursor = day.isoformat()
        return list(found.values()), delta, cursor

    def _pending_days(self, now: datetime) -> list[date]:
        """Index days not yet processed, oldest first and bounded."""
        last = None
        try:
            row = self._store.last_run(status=str(RunStatus.OK))
            last = (row or {}).get("cursor_date")
        except Exception:
            last = None
        start = (
            date.fromisoformat(str(last)) + timedelta(days=1)
            if last
            else now.date() - timedelta(days=1)
        )
        days: list[date] = []
        while start <= now.date() and len(days) < MAX_CATCHUP_DAYS:
            days.append(start)
            start += timedelta(days=1)
        return days

    def _with_unfinished_work(self, already: Sequence[ResearchTarget]) -> list[ResearchTarget]:
        """Companies pulled in only to finish evidence a previous run left."""
        seen = {t.company_id for t in already}
        try:
            pending = self._store.unfinished_companies(limit=MAX_RETRY_COMPANIES)
        except Exception:
            return []
        by_id = {t.company_id: t for t in self._universe.targets}
        return [by_id[c] for c in pending if c in by_id and c not in seen]

    # ---------------------------------------------------------------- company
    def _company(
        self,
        target: ResearchTarget,
        *,
        since: str | None,
        only: set[str] | None,
        now: datetime,
    ) -> CompanyResult:
        """One company, start to finish. **Catches everything.**

        The guarantee this method exists for: a failure here costs this company
        and nothing else. SEC timing out on SAP must not stop NVIDIA updating.
        """
        stamp = now.isoformat()
        if self._backed_off(target, now):
            return CompanyResult(target.company_id, target.cik, ok=True, unchanged=True)
        if not self._dry:
            self._store.record_attempt(target.company_id, target.cik, stamp)
        try:
            return self._ingest(target, since=since, only=only, stamp=stamp)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning("company ingestion failed", cik=target.cik, reason=reason[:120])
            if not self._dry:
                self._store.record_failure(
                    target.company_id,
                    reason=reason[:200],
                    next_eligible=self._next_eligible(target, now),
                )
            return CompanyResult(target.company_id, target.cik, ok=False, error=reason[:200])

    def _ingest(
        self,
        target: ResearchTarget,
        *,
        since: str | None,
        only: set[str] | None,
        stamp: str,
    ) -> CompanyResult:
        payload = self._source.submissions(target.cik_number)
        self.requests += 1
        records = [
            r
            for r in payload.filings
            if r.base_form in INGEST_FORMS
            and (only is None or r.accession in only)
            and (since is None or r.filing_date >= since)
        ]
        known = self._store.accession_states([r.accession for r in records])
        pending = [
            r
            for r in records
            if AccessionState(known.get(r.accession, {}).get("state", "DISCOVERED"))
            not in _TERMINAL
        ]
        if not pending:
            if not self._dry:
                self._store.record_success(
                    target.company_id, when=stamp, last_accession=None, last_published=None
                )
            return CompanyResult(target.company_id, target.cik, ok=True, unchanged=True)
        if self._dry:
            return CompanyResult(
                target.company_id, target.cik, ok=True, accessions_new=len(pending)
            )

        key = CompanyResolver.key_for(target.cik_number)
        events_new = documents_new = facts_new = 0
        for record in pending:
            built = extraction.build(
                record, company_id=target.company_id, company_key=key, fetched_at=stamp
            )
            if not built:
                self._mark(record, target, AccessionState.COMPLETE, stamp, "no event")
                continue
            # Events first, and their state recorded before evidence is
            # attempted. If the process dies during retrieval the classification
            # survives, the accession reads EVENTS_STORED, and the next run
            # retries the enrichment without re-creating the event -- identity
            # is deterministic, so the second write inserts nothing.
            events_new += self._store.upsert(built)
            self._mark(record, target, AccessionState.EVENTS_STORED, stamp)
            documents_new, facts_new = self._enrich(
                record, built, target, stamp, documents_new, facts_new
            )

        newest = max(pending, key=lambda r: r.published_at)
        self._store.record_success(
            target.company_id,
            when=stamp,
            last_accession=newest.accession,
            last_published=newest.published_at,
        )
        return CompanyResult(
            target.company_id,
            target.cik,
            ok=True,
            accessions_new=len(pending),
            events_new=events_new,
            documents_new=documents_new,
            facts_new=facts_new,
        )

    def _enrich(
        self,
        record: Any,
        built: Sequence[ResearchEvent],
        target: ResearchTarget,
        stamp: str,
        documents: int,
        facts: int,
    ) -> tuple[int, int]:
        """Attach evidence, and never let its failure cost the event."""
        if self._evidence is None:
            self._mark(record, target, AccessionState.COMPLETE, stamp, "evidence not wired")
            return documents, facts
        if not any(is_current(e, as_of=stamp) for e in built):
            # A filing outside every freshness window can never appear as a
            # current development, and freshness only ever decreases -- so
            # retrieving its exhibits would spend SEC's budget on a document no
            # consumer can reach, now or later. Measured over a 365-day
            # bootstrap this is more than half the evidence-bearing filings.
            self._mark(
                record, target, AccessionState.COMPLETE, stamp, "outside every freshness window"
            )
            return documents, facts
        try:
            report = self._evidence.collect(
                record, events=list(built), company_id=target.company_id
            )
        except Exception as exc:
            self._mark(record, target, AccessionState.RETRYABLE_FAILURE, stamp, type(exc).__name__)
            return documents, facts
        self.requests += report.documents_fetched + (1 if report.documents_listed else 0)
        state = _EVIDENCE_OUTCOMES.get(report.status, AccessionState.EVIDENCE_PARTIAL)
        self._mark(record, target, state, stamp, str(report.status))
        return documents + report.documents_fetched, facts + report.facts_new

    def _mark(
        self,
        record: Any,
        target: ResearchTarget,
        state: AccessionState,
        stamp: str,
        detail: str | None = None,
    ) -> None:
        self._store.set_accession_state(
            record.accession,
            company_id=target.company_id,
            cik=target.cik,
            form=record.form,
            state=str(state),
            when=stamp,
            detail=detail,
        )

    # ---------------------------------------------------------------- backoff
    def _backed_off(self, target: ResearchTarget, now: datetime) -> bool:
        try:
            checkpoint = self._store.checkpoint(target.company_id)
        except Exception:
            return False
        until = (checkpoint or {}).get("next_eligible_at")
        if not until:
            return False
        moment = _moment(str(until))
        return moment is not None and moment > now

    def _next_eligible(self, target: ResearchTarget, now: datetime) -> str:
        try:
            failures = int(
                (self._store.checkpoint(target.company_id) or {}).get("consecutive_failures", 0)
            )
        except Exception:
            failures = 0
        minutes = BACKOFF_MINUTES[min(failures, len(BACKOFF_MINUTES) - 1)]
        return (now + timedelta(minutes=minutes)).isoformat()
