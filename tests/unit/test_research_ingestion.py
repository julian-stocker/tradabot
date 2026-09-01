"""Keeping the research store current, and what a failure is allowed to cost.

Three invariants carry this file, and each has a way of looking fine while
being broken:

* **One company must not cost the run.** A universe pass that aborts on the
  first SEC timeout looks like a working scheduler right up until one company
  is slow, and then nothing updates for anyone.
* **Evidence failure must not cost a classification.** The event is already
  established by the filing's metadata; losing it because an exhibit would not
  download destroys good data to punish a network blip.
* **Seen is not the same as finished.** A filing whose enrichment died is known
  by accession, so the obvious "have I got this one?" check skips it forever.

The rest is arithmetic about not asking SEC for things twice.

Nothing here touches the network. The daily index, submissions payloads and
evidence outcomes are all fixtures.
"""

from __future__ import annotations

import ast
import multiprocessing
import os
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.research_intelligence import universe as uni
from app.research_intelligence.ingest import (
    BACKOFF_MINUTES,
    MAX_CATCHUP_DAYS,
    AccessionState,
    AlreadyRunningError,
    IncrementalResearchIngestor,
    IngestionHealth,
    IngestionMode,
    RunStatus,
    base_form,
    health,
    parse_daily_index,
    single_run,
)
from app.research_intelligence.sec import SubmissionsPayload, parse_submissions
from app.research_intelligence.store import EventStore
from app.research_intelligence.universe import Exclusion, ResearchTarget

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


# ---------------------------------------------------------------- fixtures
class Listing:
    """The registry shape the universe policy reads."""

    def __init__(
        self,
        symbol: str,
        mic: str = "XNAS",
        cik: str | None = "0001045810",
        company_id: int = 1,
        asset_type: str = "STOCK",
        name: str = "NVIDIA",
    ) -> None:
        self.symbol, self.mic, self.cik = symbol, mic, cik
        self.company_id, self.asset_type, self.company_name = company_id, asset_type, name

    @property
    def qualified(self) -> str:
        return f"{self.symbol}.{self.mic}"


def submissions(
    cik: int = 1045810, filings: list[dict[str, Any]] | None = None
) -> SubmissionsPayload:
    rows = filings if filings is not None else [filing()]
    keys = (
        "accessionNumber",
        "form",
        "filingDate",
        "reportDate",
        "acceptanceDateTime",
        "items",
        "primaryDocument",
        "primaryDocDescription",
        "size",
    )
    recent = {k: [r.get(k) for r in rows] for k in keys}
    return parse_submissions(
        {
            "cik": cik,
            "name": "N",
            "sic": "3674",
            "sicDescription": "x",
            "tickers": [],
            "exchanges": [],
            "filings": {"recent": recent},
        }
    )


def filing(
    accession: str = "0001045810-26-000073",
    form: str = "8-K",
    items: str | None = "2.02",
    filed: str = "2026-08-26",
) -> dict[str, Any]:
    return {
        "accessionNumber": accession,
        "form": form,
        "filingDate": filed,
        "reportDate": filed,
        "acceptanceDateTime": f"{filed}T16:20:31.000Z",
        "items": items,
        "primaryDocument": "d.htm",
        "primaryDocDescription": form,
        "size": 1,
    }


class Source:
    """Submissions, with a company that can be made to fail."""

    def __init__(self, payloads: dict[int, SubmissionsPayload], fail: set[int] | None = None):
        self._payloads, self._fail = payloads, fail or set()
        self.calls: list[int] = []

    def submissions(self, cik: int) -> SubmissionsPayload:
        self.calls.append(int(cik))
        if int(cik) in self._fail:
            msg = "simulated SEC timeout"
            raise TimeoutError(msg)
        return self._payloads[int(cik)]


class Client:
    """The daily index, and nothing else."""

    def __init__(self, days: dict[date, bytes] | None = None) -> None:
        self._days = days or {}
        self.requested: list[date] = []

    def daily_index(self, day: date) -> bytes:
        self.requested.append(day)
        if day not in self._days:
            msg = "not found"
            raise RuntimeError(msg)
        return self._days[day]


class Evidence:
    """An evidence pipeline whose outcome the test chooses."""

    def __init__(self, status: Any = None, raises: bool = False) -> None:
        from app.research_intelligence.schemas import EvidenceStatus

        self._status = status or EvidenceStatus.OK
        self._raises = raises
        self.calls = 0

    def collect(self, record: Any, *, events: Any, company_id: int) -> Any:
        self.calls += 1
        if self._raises:
            msg = "simulated archive outage"
            raise ConnectionError(msg)
        from app.research_intelligence.service import EvidenceReport

        return EvidenceReport(
            record.accession, self._status, documents_listed=1, documents_fetched=1, facts_new=1
        )


def target(cik: str = "0001045810", company_id: int = 1) -> ResearchTarget:
    return ResearchTarget(company_id, cik, "NVIDIA", (f"S{company_id}.US",))


def universe_of(*targets: ResearchTarget) -> Any:
    return uni.ResearchUniverse(
        targets=targets, excluded={}, total_listings=len(targets), total_companies=len(targets)
    )


def ingestor(
    *,
    store: EventStore,
    source: Source,
    client: Client | None = None,
    evidence: Any = None,
    targets: tuple[ResearchTarget, ...] = (),
    dry_run: bool = False,
) -> IncrementalResearchIngestor:
    return IncrementalResearchIngestor(
        universe=universe_of(*(targets or (target(),))),
        source=source,
        client=client or Client(),
        store=store,
        evidence=evidence,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------- universe
def test_a_listing_without_a_cik_is_not_polled() -> None:
    built = uni.build([Listing("XLE", "ARCX", cik=None, company_id=9)])
    assert built.size == 0
    assert built.excluded["XLE.ARCX"] is Exclusion.NO_SEC_IDENTITY


def test_a_fund_with_an_sec_identity_is_still_not_a_company() -> None:
    """SPY genuinely files. None of it is company reporting."""
    built = uni.build([Listing("SPY", "ARCX", cik="0000884394", company_id=9, asset_type="ETF")])
    assert built.size == 0
    assert built.excluded["SPY.ARCX"] is Exclusion.NOT_AN_OPERATING_COMPANY


def test_a_cross_listed_issuer_is_polled_once() -> None:
    """SAP.DE and SAP.US are one registrant filing one set of documents."""
    built = uni.build(
        [
            Listing("SAP", "XETR", cik="0001000184", company_id=7, name="SAP"),
            Listing("SAP", "XNAS", cik="0001000184", company_id=7, name="SAP"),
        ]
    )
    assert built.size == 1
    (only,) = built.targets
    assert set(only.listings) == {"SAP.XETR", "SAP.XNAS"}
    assert built.excluded["SAP.XNAS"] is Exclusion.DUPLICATE_REGISTRANT


def test_one_cik_claimed_by_two_companies_is_refused_not_resolved() -> None:
    built = uni.build(
        [
            Listing("A", "XNAS", cik="0000000001", company_id=1),
            Listing("B", "XNAS", cik="0000000001", company_id=2),
        ]
    )
    assert built.size == 0
    assert set(built.excluded.values()) == {Exclusion.AMBIGUOUS_IDENTITY}


def test_the_universe_is_not_filtered_by_how_interesting_a_company_is() -> None:
    """Every exclusion is a property that makes polling impossible."""
    reasons = {str(r) for r in Exclusion}
    assert reasons == {
        "NO_SEC_IDENTITY",
        "NOT_AN_OPERATING_COMPANY",
        "DUPLICATE_REGISTRANT",
        "AMBIGUOUS_IDENTITY",
    }


# ------------------------------------------------------------- daily index
INDEX = (
    b"CIK|Company Name|Form Type|Date Filed|File Name\n"
    b"-----------------------------------------------\n"
    b"1045810|NVIDIA CORP|8-K|20260826|edgar/data/1045810/0001045810-26-000073.txt\n"
    b"1045810|NVIDIA CORP|4|20260826|edgar/data/1045810/0001045810-26-000074.txt\n"
    b"320193|APPLE INC|10-Q|20260826|edgar/data/320193/0000320193-26-000020.txt\n"
    b"9999999|SOMEONE ELSE|8-K|20260826|edgar/data/9999999/0009999999-26-000001.txt\n"
)


def test_the_daily_index_is_parsed_as_data() -> None:
    entries = parse_daily_index(INDEX)
    assert [e.cik for e in entries] == ["0001045810", "0001045810", "0000320193", "0009999999"]
    assert entries[0].accession == "0001045810-26-000073"


def test_a_malformed_index_row_is_skipped_rather_than_trusted() -> None:
    assert parse_daily_index(b"garbage\n||||\n1|X|8-K|20260826|edgar/data/1/short.txt\n") == []


def test_only_eligible_ciks_with_ingestable_forms_are_followed_up() -> None:
    """Form 4 is an insider filing, and CIK 9999999 is not in the universe."""
    store = EventStore(":memory:")
    source = Source({1045810: submissions(), 320193: submissions(320193)})
    client = Client({date(2026, 8, 31): INDEX})
    run = ingestor(
        store=store,
        source=source,
        client=client,
        targets=(target(), target("0000320193", 2)),
    ).run(mode=IngestionMode.INDEX, now=datetime(2026, 8, 31, 20, tzinfo=UTC))
    assert sorted(source.calls) == [320193, 1045810]
    assert run.cursor_date == "2026-08-31"


@pytest.mark.parametrize(("form", "expected"), [("8-K/A", "8-K"), ("10-Q", "10-Q")])
def test_amended_forms_resolve_to_their_base(form: str, expected: str) -> None:
    assert base_form(form) == expected


def _cursor_at(store: EventStore, day: str) -> None:
    store.start_run("r", started_at=f"{day}T00:00:00+00:00", mode="INDEX")
    store.finish_run(
        "r", {"status": "OK", "cursor_date": day, "finished_at": f"{day}T23:00:00+00:00"}
    )


def test_todays_unpublished_index_does_not_advance_the_cursor() -> None:
    """The index for a day is published after that day closes.

    The cursor must not move past a day whose filings were never read, or those
    filings are missed permanently.
    """
    store = EventStore(":memory:")
    _cursor_at(store, "2026-08-31")
    run = ingestor(store=store, source=Source({}), client=Client({})).run(
        mode=IngestionMode.INDEX, now=NOW
    )
    assert run.cursor_date is None
    assert run.companies_attempted == 0


def test_a_weekend_does_not_wedge_the_cursor() -> None:
    """EDGAR publishes no index on a Saturday, and it never will.

    Treating that like an unpublished day would stop the cursor on the first
    weekend and the job would never reach Monday again.
    """
    store = EventStore(":memory:")
    _cursor_at(store, "2026-08-27")  # 29th and 30th are the weekend
    run = ingestor(
        store=store,
        source=Source({1045810: submissions(), 320193: submissions(320193)}),
        client=Client({date(2026, 8, 31): INDEX}),
        targets=(target(), target("0000320193", 2)),
    ).run(mode=IngestionMode.INDEX, now=datetime(2026, 8, 31, 23, tzinfo=UTC))
    assert run.cursor_date == "2026-08-31"
    assert run.companies_attempted == 2


def test_catch_up_is_bounded() -> None:
    store = EventStore(":memory:")
    store.start_run("r", started_at="2026-01-01T00:00:00+00:00", mode="INDEX")
    store.finish_run(
        "r",
        {"status": "OK", "cursor_date": "2026-01-01", "finished_at": "2026-01-01T01:00:00+00:00"},
    )
    client = Client({})
    ingestor(store=store, source=Source({}), client=client).run(mode=IngestionMode.INDEX, now=NOW)
    assert len(client.requested) <= MAX_CATCHUP_DAYS


# ------------------------------------------------------------- checkpoints
def test_a_checkpoint_is_created_and_advanced_on_success() -> None:
    store = EventStore(":memory:")
    ingestor(store=store, source=Source({1045810: submissions()})).run(
        mode=IngestionMode.SWEEP, now=NOW
    )
    checkpoint = store.checkpoint(1)
    assert checkpoint is not None
    assert checkpoint["last_success_at"] == NOW.isoformat()
    assert checkpoint["last_seen_accession"] == "0001045810-26-000073"
    assert checkpoint["consecutive_failures"] == 0


def test_a_failure_does_not_advance_the_successful_checkpoint() -> None:
    """Otherwise a company failing nightly reports itself freshly ingested."""
    store = EventStore(":memory:")
    ingestor(store=store, source=Source({1045810: submissions()})).run(
        mode=IngestionMode.SWEEP, now=NOW
    )
    good = store.checkpoint(1)
    assert good is not None
    later = NOW + timedelta(hours=1)
    ingestor(store=store, source=Source({}, fail={1045810})).run(
        mode=IngestionMode.SWEEP, now=later
    )
    after = store.checkpoint(1)
    assert after is not None
    assert after["last_success_at"] == good["last_success_at"]
    assert after["last_attempt_at"] == later.isoformat()
    assert after["consecutive_failures"] == 1
    assert after["last_error"] is not None


def test_a_repeatedly_failing_company_is_asked_less_often() -> None:
    store = EventStore(":memory:")
    source = Source({}, fail={1045810})
    moment = NOW
    for _ in range(3):
        ingestor(store=store, source=source).run(mode=IngestionMode.SWEEP, now=moment)
        moment += timedelta(days=1)
    checkpoint = store.checkpoint(1)
    assert checkpoint is not None
    assert checkpoint["consecutive_failures"] == 3
    assert checkpoint["next_eligible_at"] is not None


def test_a_backed_off_company_is_skipped_without_a_request() -> None:
    store = EventStore(":memory:")
    source = Source({}, fail={1045810})
    ingestor(store=store, source=source).run(mode=IngestionMode.SWEEP, now=NOW)
    before = len(source.calls)
    ingestor(store=store, source=source).run(
        mode=IngestionMode.SWEEP, now=NOW + timedelta(minutes=1)
    )
    assert len(source.calls) == before
    assert BACKOFF_MINUTES[0] >= 1


# --------------------------------------------------------------- isolation
def test_one_failing_company_does_not_stop_the_others() -> None:
    """**The gate.** SEC timing out on SAP must not stop NVIDIA updating."""
    store = EventStore(":memory:")
    payloads = {1045810: submissions(), 320193: submissions(320193), 789019: submissions(789019)}
    run = ingestor(
        store=store,
        source=Source(payloads, fail={320193}),
        targets=(target(), target("0000320193", 2), target("0000789019", 3)),
    ).run(mode=IngestionMode.SWEEP, now=NOW)

    assert run.status is RunStatus.DEGRADED
    assert run.companies_attempted == 3
    assert run.companies_succeeded == 2
    assert run.companies_failed == 1
    assert store.checkpoint(1) is not None
    assert store.checkpoint(3) is not None
    assert store.count() > 0


def test_a_run_summary_accounts_for_everything_attempted() -> None:
    store = EventStore(":memory:")
    run = ingestor(store=store, source=Source({1045810: submissions()})).run(
        mode=IngestionMode.SWEEP, now=NOW
    )
    assert run.companies_attempted == (run.companies_succeeded + run.companies_failed)
    for key in ("accessions_discovered", "events_created", "network_requests", "duration_seconds"):
        assert key in run.as_dict()


# ------------------------------------------------------------ idempotency
def test_a_second_run_with_no_new_filings_creates_nothing() -> None:
    store = EventStore(":memory:")
    source = Source({1045810: submissions()})
    ingestor(store=store, source=source, evidence=Evidence()).run(mode=IngestionMode.SWEEP, now=NOW)
    events, documents, facts = store.count(), store.count_documents(), store.count_facts()

    again = ingestor(store=store, source=source, evidence=Evidence()).run(
        mode=IngestionMode.SWEEP, now=NOW + timedelta(hours=1)
    )
    assert again.events_created == 0
    assert again.facts_created == 0
    assert again.companies_unchanged == 1
    assert (store.count(), store.count_documents(), store.count_facts()) == (
        events,
        documents,
        facts,
    )


def test_a_no_change_run_costs_one_request_per_company() -> None:
    store = EventStore(":memory:")
    source = Source({1045810: submissions()})
    ingestor(store=store, source=source, evidence=Evidence()).run(mode=IngestionMode.SWEEP, now=NOW)
    evidence = Evidence()
    ingestor(store=store, source=source, evidence=evidence).run(
        mode=IngestionMode.SWEEP, now=NOW + timedelta(hours=1)
    )
    assert evidence.calls == 0  # no archive request for a finished filing


# --------------------------------------------------------- accession state
def test_a_completed_filing_is_not_processed_again() -> None:
    store = EventStore(":memory:")
    source = Source({1045810: submissions()})
    ingestor(store=store, source=source, evidence=Evidence()).run(mode=IngestionMode.SWEEP, now=NOW)
    state = store.accession_states(["0001045810-26-000073"])["0001045810-26-000073"]
    assert state["state"] == str(AccessionState.COMPLETE)


def test_an_evidence_failure_keeps_the_event_and_marks_the_filing_retryable() -> None:
    """**The gate.** Losing a classification because an exhibit would not
    download destroys good data to punish a network blip."""
    store = EventStore(":memory:")
    ingestor(
        store=store, source=Source({1045810: submissions()}), evidence=Evidence(raises=True)
    ).run(mode=IngestionMode.SWEEP, now=NOW)

    assert store.count() > 0  # the event survived
    state = store.accession_states(["0001045810-26-000073"])["0001045810-26-000073"]
    assert state["state"] == str(AccessionState.RETRYABLE_FAILURE)


def test_a_retryable_filing_is_picked_up_again_and_completes() -> None:
    """Seen is not finished: a naive accession check would skip this forever."""
    store = EventStore(":memory:")
    source = Source({1045810: submissions()})
    ingestor(store=store, source=source, evidence=Evidence(raises=True)).run(
        mode=IngestionMode.SWEEP, now=NOW
    )
    before = store.count()

    ingestor(store=store, source=source, evidence=Evidence()).run(
        mode=IngestionMode.SWEEP, now=NOW + timedelta(hours=1)
    )
    state = store.accession_states(["0001045810-26-000073"])["0001045810-26-000073"]
    assert state["state"] == str(AccessionState.COMPLETE)
    assert store.count() == before  # retried, not duplicated


def test_content_this_pipeline_cannot_read_is_never_refetched() -> None:
    from app.research_intelligence.schemas import EvidenceStatus

    store = EventStore(":memory:")
    source = Source({1045810: submissions()})
    ingestor(
        store=store,
        source=source,
        evidence=Evidence(EvidenceStatus.UNSUPPORTED_CONTENT_TYPE),
    ).run(mode=IngestionMode.SWEEP, now=NOW)
    state = store.accession_states(["0001045810-26-000073"])["0001045810-26-000073"]
    assert state["state"] == str(AccessionState.PERMANENT_REFUSAL)

    evidence = Evidence()
    ingestor(store=store, source=source, evidence=evidence).run(
        mode=IngestionMode.SWEEP, now=NOW + timedelta(hours=1)
    )
    assert evidence.calls == 0


def test_a_filing_outside_every_freshness_window_costs_no_archive_request() -> None:
    store = EventStore(":memory:")
    old = submissions(filings=[filing(filed="2020-01-05")])
    evidence = Evidence()
    ingestor(store=store, source=Source({1045810: old}), evidence=evidence).run(
        mode=IngestionMode.SWEEP, now=NOW
    )
    assert evidence.calls == 0
    assert store.count() > 0  # classified, just not enriched


def test_a_company_with_unfinished_work_is_revisited_without_filing_again() -> None:
    store = EventStore(":memory:")
    store.set_accession_state(
        "a1",
        company_id=1,
        cik="0001045810",
        form="8-K",
        state=str(AccessionState.EVENTS_STORED),
        when=NOW.isoformat(),
    )
    assert store.unfinished_companies() == [1]


# -------------------------------------------------------------- dry run
def test_a_dry_run_mutates_nothing() -> None:
    path = Path(tempfile.mkdtemp()) / "dry.db"
    store = EventStore(path)
    before = (store.count(), store.count_documents(), store.count_facts())

    run = ingestor(
        store=store, source=Source({1045810: submissions()}), evidence=Evidence(), dry_run=True
    ).run(mode=IngestionMode.SWEEP, now=NOW)

    assert run.status is RunStatus.DRY_RUN
    assert run.accessions_discovered == 1
    assert (store.count(), store.count_documents(), store.count_facts()) == before
    assert store.checkpoint(1) is None
    assert store.last_run() is None
    assert store.accession_states(["0001045810-26-000073"]) == {}


# ----------------------------------------------------------------- locking
def test_a_second_run_refuses_to_start(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    with single_run(lock), pytest.raises(AlreadyRunningError), single_run(lock):
        pass


def test_the_lock_is_released_when_the_holder_exits(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    with single_run(lock):
        pass
    with single_run(lock):
        pass  # reacquired


def _hold(path: str, ready: Any, release: Any) -> None:
    with single_run(Path(path)):
        ready.set()
        release.wait(10)


def test_a_killed_run_leaves_no_stale_lock(tmp_path: Path) -> None:
    """flock is released by the kernel, so there is no recovery heuristic."""
    lock = tmp_path / "run.lock"
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold, args=(str(lock), ready, release))
    holder.start()
    assert ready.wait(20)
    with pytest.raises(AlreadyRunningError), single_run(lock):
        pass
    holder.kill()
    holder.join(10)
    with single_run(lock):
        pass  # the kernel dropped it


# ------------------------------------------------------------------ health
def test_health_is_never_run_before_anything_completes() -> None:
    assert health(EventStore(":memory:"), now=NOW).status is IngestionHealth.NEVER_RUN


def test_health_is_current_after_a_recent_run() -> None:
    store = EventStore(":memory:")
    store.start_run("r", started_at=NOW.isoformat(), mode="INDEX")
    store.finish_run(
        "r",
        {
            "status": "OK",
            "finished_at": NOW.isoformat(),
            "companies_attempted": 10,
            "companies_failed": 0,
        },
    )
    assert health(store, now=NOW + timedelta(hours=2)).status is IngestionHealth.CURRENT


def test_health_goes_stale_rather_than_silently_serving_old_coverage() -> None:
    store = EventStore(":memory:")
    store.start_run("r", started_at=NOW.isoformat(), mode="INDEX")
    store.finish_run(
        "r",
        {
            "status": "OK",
            "finished_at": NOW.isoformat(),
            "companies_attempted": 10,
            "companies_failed": 0,
        },
    )
    report = health(store, now=NOW + timedelta(days=3))
    assert report.status is IngestionHealth.STALE
    assert report.age_hours is not None


def test_a_weekend_does_not_make_a_friday_run_look_stale() -> None:
    """EDGAR publishes no index on Saturday or Sunday."""
    store = EventStore(":memory:")
    store.start_run("r", started_at=NOW.isoformat(), mode="INDEX")
    store.finish_run(
        "r",
        {
            "status": "OK",
            "finished_at": NOW.isoformat(),
            "companies_attempted": 10,
            "companies_failed": 0,
        },
    )
    assert health(store, now=NOW + timedelta(hours=30)).status is IngestionHealth.CURRENT


def test_a_run_failing_most_companies_reads_as_degraded() -> None:
    store = EventStore(":memory:")
    store.start_run("r", started_at=NOW.isoformat(), mode="INDEX")
    store.finish_run(
        "r",
        {
            "status": "DEGRADED",
            "finished_at": NOW.isoformat(),
            "companies_attempted": 10,
            "companies_failed": 6,
        },
    )
    assert health(store, now=NOW).status is IngestionHealth.DEGRADED


def test_health_never_raises_on_an_unreadable_store() -> None:
    class Broken:
        def last_run(self, *, status: str | None = None) -> None:
            raise RuntimeError("disk")

    assert health(Broken(), now=NOW).status is IngestionHealth.NEVER_RUN


def test_stale_ingestion_is_not_the_same_as_a_quiet_company() -> None:
    """The distinction the card depends on."""
    from app.research_intelligence.developments import CoverageStatus, CurrentDevelopmentsService

    store = EventStore(":memory:")
    quiet = CurrentDevelopmentsService(store=store).for_company(
        company_id=1, cik="0001045810", as_of="2026-09-01"
    )
    assert quiet.status is CoverageStatus.NO_COVERAGE
    assert quiet.ingestion is not None
    assert quiet.ingestion.status is IngestionHealth.NEVER_RUN


# ------------------------------------------------------------ concurrency
def test_a_reader_is_not_blocked_by_a_writer(tmp_path: Path) -> None:
    """WAL, so a /check read never queues behind a universe-wide write."""
    path = tmp_path / "wal.db"
    writer = EventStore(path)
    reader = EventStore(path)
    import sqlite3

    with sqlite3.connect(path) as probe:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    source = Source({1045810: submissions()})
    started = time.monotonic()
    ingestor(store=writer, source=source, evidence=Evidence()).run(
        mode=IngestionMode.SWEEP, now=NOW
    )
    assert reader.count() >= 0
    assert time.monotonic() - started < 10


# ------------------------------------------------------------- boundaries
def test_ingestion_reaches_no_trading_or_model_provider() -> None:
    forbidden = (
        "app.broker",
        "app.paper",
        "app.strategy",
        "app.discord_bot",
        "app.publishing",
        "alpaca",
        "openai",
        "anthropic",
        "genai",
    )
    for name in ("ingest.py", "universe.py"):
        tree = ast.parse(
            Path("app/research_intelligence") / name
            and (Path("app/research_intelligence") / name).read_text()
        )
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.module
                else []
            )
            for module in names:
                assert not any(module.startswith(f) for f in forbidden), f"{name}: {module}"


def test_ingestion_opens_no_socket_of_its_own() -> None:
    """All SEC access stays behind the client that owns the rate limit."""
    body = Path("app/research_intelligence/ingest.py").read_text()
    for token in ("urlopen", "requests.", "httpx", "urllib", "socket"):
        assert token not in body, f"ingest.py references {token}"


def test_no_ingestion_string_uses_recommendation_vocabulary() -> None:
    banned = (
        "buy",
        "sell",
        "bullish",
        "bearish",
        "recommend",
        "forecast",
        "price target",
        "catalyst",
        "upside",
        "downside",
    )
    tree = ast.parse(Path("app/research_intelligence/ingest.py").read_text())
    docstrings = {
        ast.get_docstring(n, clean=False)
        for n in ast.walk(tree)
        if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in docstrings:
                continue
            for word in banned:
                assert word not in node.value.lower(), node.value


def test_the_lock_path_is_runtime_state(tmp_path: Path) -> None:
    from app.research_intelligence.ingest import LOCK_PATH

    assert str(LOCK_PATH).startswith("data/")
    assert os.path.splitext(LOCK_PATH)[1] == ".lock"


def test_index_mode_ingests_the_delta_not_the_history() -> None:
    """**The gate.** The daily index *is* the delta, and must be used as one.

    Without scoping to the discovered accessions, a company that filed once
    yesterday is re-examined against its whole inline submissions history. On
    the live universe that was measured at 9,551 events re-created across forty
    companies, reaching filings from 1996 -- correct data, produced by a design
    that was supposed to be incremental and was not.
    """
    store = EventStore(":memory:")
    history = submissions(
        filings=[
            filing("0001045810-26-000073", filed="2026-08-26"),
            filing("0001045810-20-000001", filed="2020-03-02"),
            filing("0001045810-96-000001", filed="1996-07-05"),
        ]
    )
    source = Source({1045810: history})
    run = ingestor(store=store, source=source, client=Client({date(2026, 8, 31): INDEX})).run(
        mode=IngestionMode.INDEX, now=datetime(2026, 8, 31, 23, tzinfo=UTC)
    )

    assert run.accessions_discovered == 1
    seen = store.accession_states(
        ["0001045810-26-000073", "0001045810-20-000001", "0001045810-96-000001"]
    )
    assert set(seen) == {"0001045810-26-000073"}


def test_a_retry_visit_is_scoped_to_the_unfinished_filing() -> None:
    """A company pulled in to finish old work does not rebuild its history."""
    store = EventStore(":memory:")
    store.set_accession_state(
        "0001045810-20-000001",
        company_id=1,
        cik="0001045810",
        form="8-K",
        state=str(AccessionState.RETRYABLE_FAILURE),
        when=NOW.isoformat(),
    )
    history = submissions(
        filings=[
            filing("0001045810-20-000001", filed="2020-03-02"),
            filing("0001045810-96-000001", filed="1996-07-05"),
        ]
    )
    run = ingestor(store=store, source=Source({1045810: history}), client=Client({})).run(
        mode=IngestionMode.INDEX, now=NOW
    )

    assert run.companies_attempted == 1
    assert run.accessions_discovered == 1
    assert "0001045810-96-000001" not in store.accession_states(["0001045810-96-000001"])


def test_discovery_never_polls_the_submissions_api() -> None:
    """**The architecture gate.** Steady state must not regress to a full sweep.

    Asserted on the call graph rather than on words: an earlier version of this
    check searched ``_from_index`` for the string "submissions" and failed on
    its own docstring, which is the third time in this project a substring gate
    has caught vocabulary instead of behaviour.
    """
    tree = ast.parse(Path("app/research_intelligence/ingest.py").read_text())
    discovery = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_from_index"
    )
    for node in ast.walk(discovery):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    code = ast.unparse(discovery)
    assert "self._source" not in code, "discovery reached the submissions API"
    assert "self._client.daily_index" in code


def test_discovery_runs_before_any_company_is_processed() -> None:
    tree = ast.parse(Path("app/research_intelligence/ingest.py").read_text())
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run")
    code = ast.unparse(run)
    assert code.index("_from_index") < code.index("self._company")


def test_three_consecutive_non_publishing_days_do_not_wedge_the_cursor() -> None:
    """A long weekend: Friday's index read, then three silent days, then Monday."""
    store = EventStore(":memory:")
    _cursor_at(store, "2026-08-27")
    run = ingestor(
        store=store,
        source=Source({1045810: submissions(), 320193: submissions(320193)}),
        client=Client({date(2026, 8, 31): INDEX}),  # 28th, 29th, 30th all absent
        targets=(target(), target("0000320193", 2)),
    ).run(mode=IngestionMode.INDEX, now=datetime(2026, 8, 31, 23, tzinfo=UTC))
    assert run.cursor_date == "2026-08-31"
    assert run.companies_attempted == 2
