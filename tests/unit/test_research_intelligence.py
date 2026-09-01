"""What a filing establishes, and everything it does not.

The danger in this layer is not that it crashes. It is that it quietly says
more than the source does -- turning Item 1.01 into "won a major contract",
turning a 6-K into an earnings event because 6-Ks often carry one, or attaching
a filing to the nearest company when the CIK is unknown. None of those raise;
all of them read as findings.

So the assertions are mostly about restraint: the summary repeats the SEC item
title and stops, foreign filings stay unclassified, an unmapped CIK is
quarantined rather than guessed, materiality never carries a direction, and
historical evidence is NOT_ESTABLISHED on every event this phase can produce.

No test touches the network. Fixtures reproduce the shape of a real
``data.sec.gov/submissions`` payload, including the fields phase 14.0 measured:
``items``, ``acceptanceDateTime`` and ``reportDate``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.research_intelligence import (
    CompanyResolver,
    EventKind,
    EventScope,
    EventStore,
    HistoricalEvidence,
    Materiality,
    MaterialityContext,
    ResearchIngestionService,
    SecFilingSource,
    build,
    classify,
    event_id,
    is_current,
    parse_submissions,
    source_hash,
)
from app.research_intelligence.schemas import Confidence

NOW = datetime(2026, 9, 1, tzinfo=UTC)
AS_OF = "2026-09-01T00:00:00+00:00"


def submissions(
    cik: int = 1045810,
    name: str = "NVIDIA CORP",
    filings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A submissions payload in the shape data.sec.gov actually returns."""
    rows = filings if filings is not None else [_filing()]
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
    return {
        "cik": cik,
        "name": name,
        "sic": "3674",
        "sicDescription": "Semiconductors & Related Devices",
        "tickers": ["NVDA"],
        "exchanges": ["Nasdaq"],
        "filings": {"recent": {k: [r.get(k) for r in rows] for k in keys}, "files": []},
    }


def _filing(**kw: Any) -> dict[str, Any]:
    base = {
        "accessionNumber": "0001045810-26-000073",
        "form": "8-K",
        "filingDate": "2026-08-26",
        "reportDate": "2026-08-26",
        "acceptanceDateTime": "2026-08-26T20:21:19.000Z",
        "items": "2.02,9.01",
        "primaryDocument": "nvda-20260826.htm",
        "primaryDocDescription": "8-K",
        "size": 26457,
    }
    base.update(kw)
    return base


class _Client:
    """Stands in for EdgarClient. Records calls; never opens a socket."""

    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self._payload = payload
        self.calls = 0

    def submissions(self, cik: int) -> dict[str, Any]:
        self.calls += 1
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _service(
    payload: dict[str, Any] | Exception, *, candidates: list[Any] | None = None
) -> tuple[ResearchIngestionService, EventStore]:
    store = EventStore(":memory:")
    people = candidates if candidates is not None else [_candidate()]
    service = ResearchIngestionService(
        source=SecFilingSource(_Client(payload)),
        resolver=CompanyResolver(people),
        store=store,
    )
    return service, store


class _Candidate:
    def __init__(self, cik: str | None, company_id: int, name: str) -> None:
        self.cik = cik
        self.company_id = company_id
        self.company_name = name


def _candidate(cik: str = "0001045810", company_id: int = 42) -> Any:
    return _Candidate(cik, company_id, "NVDA")


class TestSubmissionsParsing:
    """The three timestamps SEC supplies are three different facts."""

    def test_the_measured_fields_are_all_parsed(self) -> None:
        payload = parse_submissions(submissions())

        assert payload.entity_name == "NVIDIA CORP"
        assert payload.cik == "0001045810"
        record = payload.filings[0]
        assert record.form == "8-K"
        assert record.items == ("2.02", "9.01")
        assert record.primary_document == "nvda-20260826.htm"

    def test_published_at_is_the_acceptance_instant_not_the_filing_day(self) -> None:
        """**The gate.** A filing date is a calendar day; acceptance is when it
        actually became public, to the second."""
        record = parse_submissions(submissions()).filings[0]

        assert record.published_at == "2026-08-26T20:21:19.000Z"
        assert record.published_at != record.filing_date

    def test_occurred_at_is_the_report_date_and_is_never_invented(self) -> None:
        with_date = parse_submissions(submissions()).filings[0]
        without = parse_submissions(submissions(filings=[_filing(reportDate=None)])).filings[0]

        assert with_date.report_date == "2026-08-26"
        assert without.report_date is None

    def test_an_amendment_is_recognised_from_its_form(self) -> None:
        record = parse_submissions(submissions(filings=[_filing(form="8-K/A")])).filings[0]

        assert record.is_amendment is True
        assert record.base_form == "8-K"

    def test_the_archive_url_uses_the_documented_path(self) -> None:
        record = parse_submissions(submissions()).filings[0]

        assert record.archive_url("nvda-20260826.htm") == (
            "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/nvda-20260826.htm"
        )


class TestClassification:
    """Item codes classify; text never does."""

    @pytest.mark.parametrize(
        ("items", "kind"),
        [
            ("2.02", EventKind.EARNINGS_RELEASE),
            ("5.02", EventKind.MANAGEMENT_CHANGE),
            ("2.01", EventKind.M_AND_A),
            ("1.01", EventKind.MATERIAL_AGREEMENT),
            ("1.02", EventKind.MATERIAL_AGREEMENT),
            ("2.03", EventKind.DEBT_EVENT),
            ("2.04", EventKind.DEBT_EVENT),
            ("4.02", EventKind.ACCOUNTING_RESTATEMENT),
            ("4.01", EventKind.AUDITOR_CHANGE),
            ("1.03", EventKind.BANKRUPTCY_OR_RECEIVERSHIP),
            ("2.06", EventKind.IMPAIRMENT),
        ],
    )
    def test_each_required_item_maps_to_its_kind(self, items: str, kind: EventKind) -> None:
        record = parse_submissions(submissions(filings=[_filing(items=items)])).filings[0]

        assert classify(record) == [(kind, items)]

    def test_an_administrative_item_alone_produces_no_event(self) -> None:
        """Item 9.01 records that exhibits are attached. That is bookkeeping
        about the filing, not an occurrence at the company."""
        record = parse_submissions(submissions(filings=[_filing(items="9.01")])).filings[0]

        assert classify(record) == []

    def test_an_administrative_item_does_not_suppress_a_real_one(self) -> None:
        record = parse_submissions(submissions(filings=[_filing(items="2.02,9.01")])).filings[0]

        assert classify(record) == [(EventKind.EARNINGS_RELEASE, "2.02")]

    def test_an_open_ended_item_is_recorded_unclassified_not_guessed(self) -> None:
        """Items 7.01 and 8.01 establish that something was disclosed without
        establishing what. Guessing is the whole failure mode."""
        record = parse_submissions(submissions(filings=[_filing(items="8.01")])).filings[0]

        assert classify(record) == [(EventKind.UNCLASSIFIED_SEC_FILING, "8.01")]

    def test_a_multi_item_filing_becomes_several_events(self) -> None:
        """**The gate.** A material agreement and a change of officers are two
        occurrences with different materiality and different lifetimes."""
        record = parse_submissions(submissions(filings=[_filing(items="1.01,5.02,9.01")])).filings[
            0
        ]

        events = build(record, company_id=42, company_key="CIK0001045810", fetched_at=AS_OF)

        kinds = {e.event_kind for e in events}
        assert kinds == {EventKind.MATERIAL_AGREEMENT, EventKind.MANAGEMENT_CHANGE}
        assert {e.accession for e in events} == {"0001045810-26-000073"}
        # The whole item list survives on every event, so the grouping is never lost.
        assert all(e.item_codes == ("1.01", "5.02", "9.01") for e in events)
        assert {e.classifying_item for e in events} == {"1.01", "5.02"}


class TestForeignFilerBoundary:
    """Phase 14.0 measured 1,322 foreign filings with zero item codes."""

    def test_a_six_k_is_unclassified_because_the_form_constrains_nothing(self) -> None:
        """**The gate.** A 6-K often contains earnings. 'Often' is not
        establishment, and no NLP is added to close the gap."""
        record = parse_submissions(submissions(filings=[_filing(form="6-K", items=None)])).filings[
            0
        ]

        events = build(record, company_id=7, company_key="CIK0001000184", fetched_at=AS_OF)

        assert len(events) == 1
        assert events[0].event_kind is EventKind.UNCLASSIFIED_SEC_FILING
        assert events[0].extraction_confidence is Confidence.LOW

    @pytest.mark.parametrize("form", ["20-F", "40-F", "10-K", "10-Q"])
    def test_a_periodic_report_is_classified_from_its_form_alone(self, form: str) -> None:
        """No item codes needed: the form *is* the statement that a periodic
        report was filed. Claiming less would hide a real distinction behind an
        honest-sounding label."""
        record = parse_submissions(submissions(filings=[_filing(form=form, items=None)])).filings[0]

        event = build(record, company_id=7, company_key="K", fetched_at=AS_OF)[0]

        assert event.event_kind is EventKind.PERIODIC_REPORT
        assert form in event.fact_summary

    def test_an_annual_report_outranks_a_quarterly_one(self) -> None:
        """The split app.monitoring.materiality already draws."""

        def band(form: str) -> Materiality:
            record = parse_submissions(
                submissions(filings=[_filing(form=form, items=None)])
            ).filings[0]
            return build(record, company_id=7, company_key="K", fetched_at=AS_OF)[0].materiality

        assert band("10-K") is Materiality.SIGNIFICANT
        assert band("10-Q") is Materiality.NOTABLE

    def test_the_unclassified_summary_claims_nothing(self) -> None:
        record = parse_submissions(submissions(filings=[_filing(form="6-K", items=None)])).filings[
            0
        ]

        summary = build(record, company_id=7, company_key="K", fetched_at=AS_OF)[0]

        assert "does not establish" in summary.fact_summary
        for word in ("earnings", "results", "profit", "revenue"):
            assert word not in summary.fact_summary.lower()


class TestFactSummary:
    """The summary repeats the SEC item title and stops."""

    def test_it_names_the_item_and_its_official_title(self) -> None:
        record = parse_submissions(submissions(filings=[_filing(items="1.01")])).filings[0]

        event = build(record, company_id=42, company_key="K", fetched_at=AS_OF)[0]

        assert "Item 1.01" in event.fact_summary
        assert "Entry into a Material Definitive Agreement" in event.fact_summary

    def test_a_broad_category_is_never_narrowed_to_a_business_claim(self) -> None:
        """**The gate.** Item 1.01 establishes a material definitive agreement.
        It does not establish a customer win."""
        record = parse_submissions(submissions(filings=[_filing(items="1.01")])).filings[0]

        summary = build(record, company_id=42, company_key="K", fetched_at=AS_OF)[
            0
        ].fact_summary.lower()

        for invented in (
            "contract win",
            "customer",
            "partnership",
            "supply",
            "major",
            "landmark",
            "significant deal",
        ):
            assert invented not in summary


class TestIdentity:
    """After phase 13, a wrong attachment is the expensive failure."""

    def test_a_known_cik_resolves_to_one_company(self) -> None:
        resolver = CompanyResolver([_candidate()])

        assert resolver.resolve("0001045810") == (42, None)
        assert resolver.resolve(1045810) == (42, None)

    def test_an_unknown_cik_is_refused_not_guessed(self) -> None:
        resolver = CompanyResolver([_candidate()])

        company_id, reason = resolver.resolve("0000000999")

        assert company_id is None
        assert "not a company Tradabot knows" in (reason or "")

    def test_an_ambiguous_cik_is_refused(self) -> None:
        resolver = CompanyResolver([_candidate(company_id=1), _candidate(company_id=2)])

        company_id, reason = resolver.resolve("0001045810")

        assert company_id is None
        assert "several companies" in (reason or "")

    def test_unresolved_filings_are_quarantined_and_produce_no_event(self) -> None:
        service, store = _service(submissions(cik=999), candidates=[_candidate()])

        report = service.ingest_company(999, now=NOW)

        assert report.company_id is None
        assert report.quarantined == 1
        assert store.count() == 0
        assert len(store.quarantined()) == 1

    def test_a_cross_listed_issuer_yields_one_company_event(self) -> None:
        """SAP.DE and SAP.US are one registrant. One filing, one event."""
        xetra = _Candidate("0001000184", 7, "SAP")
        nasdaq = _Candidate("0001000184", 7, "SAP")
        service, store = _service(
            submissions(cik=1000184, name="SAP SE"), candidates=[xetra, nasdaq]
        )

        service.ingest_company(1000184, now=NOW)

        events = store.events_for_company(7, as_of=AS_OF)
        assert len(events) == 1
        assert events[0].scope is EventScope.COMPANY

    def test_identity_never_consults_a_ticker(self) -> None:
        """A payload whose tickers point elsewhere still resolves by CIK."""
        resolver = CompanyResolver([_candidate()])
        payload = parse_submissions(submissions())

        assert payload.tickers == ("NVDA",)
        assert resolver.resolve(payload.cik)[0] == 42


class TestIdempotency:
    def test_the_same_filing_ingested_twice_creates_one_event(self) -> None:
        """**The gate.** Identity is derived from CIK, accession and item --
        nothing time-varying -- so re-ingestion is absorbed."""
        service, store = _service(submissions())

        first = service.ingest_company(1045810, now=NOW)
        second = service.ingest_company(1045810, now=NOW)

        assert first.events_new == 1
        assert second.events_new == 0
        assert second.events_duplicate == 1
        assert store.count() == 1

    def test_event_identity_is_stable_across_processes(self) -> None:
        a = event_id("0001045810", "0001045810-26-000073", EventKind.EARNINGS_RELEASE, "2.02")
        b = event_id("1045810", "0001045810-26-000073", EventKind.EARNINGS_RELEASE, "2.02")

        assert a == b

    def test_different_items_on_one_filing_have_different_identities(self) -> None:
        agreement = event_id("1", "acc", EventKind.MATERIAL_AGREEMENT, "1.01")
        officers = event_id("1", "acc", EventKind.MANAGEMENT_CHANGE, "5.02")

        assert agreement != officers

    def test_unrelated_filings_are_not_merged_by_similarity(self) -> None:
        """Two 8-Ks reporting the same item kind on different days are two
        events, not one deduplicated by resemblance."""
        rows = [
            _filing(accessionNumber="0001045810-26-000073", filingDate="2026-08-26"),
            _filing(
                accessionNumber="0001045810-26-000050",
                filingDate="2026-05-26",
                acceptanceDateTime="2026-05-26T20:00:00.000Z",
            ),
        ]
        service, store = _service(submissions(filings=rows))

        service.ingest_company(1045810, now=NOW)

        assert store.count() == 2

    def test_source_hash_covers_the_filing_metadata(self) -> None:
        original = parse_submissions(submissions()).filings[0]
        altered = parse_submissions(submissions(filings=[_filing(items="2.02,5.02,9.01")])).filings[
            0
        ]

        assert source_hash(original) != source_hash(altered)
        assert source_hash(original) == source_hash(original)


class TestPointInTime:
    """An as_of question must be answered with what was public then."""

    def _two_dated_events(self) -> EventStore:
        rows = [
            _filing(
                accessionNumber="acc-new",
                filingDate="2026-08-26",
                acceptanceDateTime="2026-08-26T20:21:19.000Z",
            ),
            _filing(
                accessionNumber="acc-old",
                filingDate="2025-08-26",
                acceptanceDateTime="2025-08-26T20:21:19.000Z",
            ),
        ]
        service, store = _service(submissions(filings=rows))
        service.ingest_company(1045810, now=NOW)
        return store

    def test_an_as_of_query_never_returns_a_later_event(self) -> None:
        """**The gate.** Without this, a question about last year is answered
        with this year's filings and the answer looks entirely normal."""
        store = self._two_dated_events()

        past = store.events_for_company(42, as_of="2026-01-01T00:00:00+00:00")

        assert len(past) == 1
        assert past[0].accession == "acc-old"
        assert all(e.published_at <= "2026-01-01T00:00:00+00:00" for e in past)

    def test_the_same_query_later_sees_both(self) -> None:
        store = self._two_dated_events()

        assert len(store.events_for_company(42, as_of=AS_OF)) == 2

    def test_kind_queries_are_point_in_time_too(self) -> None:
        store = self._two_dated_events()

        early = store.events_by_kind(EventKind.EARNINGS_RELEASE, as_of="2026-01-01T00:00:00+00:00")

        assert len(early) == 1

    def test_recent_events_respects_both_bounds(self) -> None:
        store = self._two_dated_events()

        window = store.recent_events(42, as_of=AS_OF, since="2026-01-01T00:00:00+00:00")

        assert [e.accession for e in window] == ["acc-new"]

    def test_fetched_at_is_not_confused_with_publication(self) -> None:
        store = self._two_dated_events()

        event = store.events_for_company(42, as_of=AS_OF)[0]

        assert event.fetched_at == NOW.isoformat()
        assert event.published_at != event.fetched_at


class TestAmendments:
    def test_an_amendment_never_overwrites_the_original(self) -> None:
        rows = [
            _filing(
                accessionNumber="acc-1",
                filingDate="2026-08-01",
                acceptanceDateTime="2026-08-01T20:00:00.000Z",
            ),
            _filing(
                accessionNumber="acc-2",
                form="8-K/A",
                filingDate="2026-08-10",
                acceptanceDateTime="2026-08-10T20:00:00.000Z",
            ),
        ]
        service, store = _service(submissions(filings=rows))

        service.ingest_company(1045810, now=NOW)

        assert store.count() == 2
        assert store.events_for_accession("acc-1")

    def test_the_amendment_is_marked_and_linked_where_unambiguous(self) -> None:
        rows = [
            _filing(
                accessionNumber="acc-1",
                filingDate="2026-08-01",
                acceptanceDateTime="2026-08-01T20:00:00.000Z",
            ),
            _filing(
                accessionNumber="acc-2",
                form="8-K/A",
                filingDate="2026-08-10",
                acceptanceDateTime="2026-08-10T20:00:00.000Z",
            ),
        ]
        service, store = _service(submissions(filings=rows))

        service.ingest_company(1045810, now=NOW)

        amendment = store.events_for_accession("acc-2")[0]
        assert amendment.amends_accession == "acc-2"
        assert amendment.supersedes_event_id == store.events_for_accession("acc-1")[0].event_id

    def test_an_ambiguous_amendment_links_to_nothing(self) -> None:
        """Two plausible originals means the relationship is unknown, and both
        events are kept with it unrecorded."""
        rows = [
            _filing(
                accessionNumber="acc-1",
                filingDate="2026-08-01",
                acceptanceDateTime="2026-08-01T20:00:00.000Z",
            ),
            _filing(
                accessionNumber="acc-1b",
                filingDate="2026-08-02",
                acceptanceDateTime="2026-08-02T20:00:00.000Z",
            ),
            _filing(
                accessionNumber="acc-2",
                form="8-K/A",
                filingDate="2026-08-10",
                acceptanceDateTime="2026-08-10T20:00:00.000Z",
            ),
        ]
        service, store = _service(submissions(filings=rows))

        service.ingest_company(1045810, now=NOW)

        assert store.count() == 3
        assert store.events_for_accession("acc-2")[0].supersedes_event_id is None


class TestMateriality:
    """Attention, never direction."""

    @pytest.mark.parametrize(
        ("items", "band"),
        [
            ("4.02", Materiality.CRITICAL),
            ("1.03", Materiality.CRITICAL),
            ("2.02", Materiality.SIGNIFICANT),
            ("5.02", Materiality.NOTABLE),
            ("1.01", Materiality.NOTABLE),
            ("2.03", Materiality.NOTABLE),
        ],
    )
    def test_form_semantics_set_the_band(self, items: str, band: Materiality) -> None:
        record = parse_submissions(submissions(filings=[_filing(items=items)])).filings[0]

        assert (
            build(record, company_id=42, company_key="K", fetched_at=AS_OF)[0].materiality is band
        )

    def test_no_direction_exists_anywhere_in_the_vocabulary(self) -> None:
        """**The gate.** There is no field a direction could be recorded in."""
        members = {m.value for m in Materiality}

        assert members == {"ROUTINE", "NOTABLE", "SIGNIFICANT", "CRITICAL"}
        for banned in ("GOOD", "BAD", "BULLISH", "BEARISH", "POSITIVE", "NEGATIVE"):
            assert banned not in members

    def test_magnitude_context_is_absent_and_says_why(self) -> None:
        """No amount is read from filing text, so nothing is put in proportion."""
        record = parse_submissions(submissions(filings=[_filing(items="2.06")])).filings[0]

        event = build(record, company_id=42, company_key="K", fetched_at=AS_OF)[0]

        assert event.materiality_context is MaterialityContext.NO_ESTABLISHED_AMOUNT

    def test_a_kind_without_magnitude_says_not_applicable(self) -> None:
        record = parse_submissions(submissions(filings=[_filing(items="5.02")])).filings[0]

        event = build(record, company_id=42, company_key="K", fetched_at=AS_OF)[0]

        assert event.materiality_context is MaterialityContext.NOT_APPLICABLE


class TestHistoricalEvidenceAndVocabulary:
    def test_every_event_defaults_to_not_established(self) -> None:
        """No event study exists over these kinds. The scanner's signal
        outcomes are a different event definition and are not borrowed."""
        service, store = _service(
            submissions(
                filings=[_filing(items=i) for i in ("2.02", "5.02", "1.01", "4.02", "2.03", "2.01")]
            )
        )

        service.ingest_company(1045810, now=NOW)

        events = store.events_for_company(42, as_of=AS_OF)
        assert events
        assert all(e.historical_evidence is HistoricalEvidence.NOT_ESTABLISHED for e in events)

    def test_interpretation_is_structurally_absent(self) -> None:
        record = parse_submissions(submissions()).filings[0]

        event = build(record, company_id=42, company_key="K", fetched_at=AS_OF)[0]

        assert event.interpretation is None

    def test_no_recommendation_or_direction_vocabulary_is_emitted(self) -> None:
        service, store = _service(
            submissions(
                filings=[_filing(items=i) for i in ("2.02", "5.02", "1.01", "4.02", "1.03", "2.06")]
            )
        )
        service.ingest_company(1045810, now=NOW)

        banned = (
            "buy",
            "sell",
            "hold",
            "bullish",
            "bearish",
            "positive",
            "negative",
            "outperform",
            "undervalued",
            "overvalued",
            "historically",
            "tend to",
            "usually",
        )
        for event in store.events_for_company(42, as_of=AS_OF):
            text = f"{event.fact_summary} {event.title or ''}".lower()
            for word in banned:
                assert word not in text, f"{word!r} in {text!r}"


class TestFreshness:
    def test_a_recent_event_is_current(self) -> None:
        record = parse_submissions(
            submissions(filings=[_filing(acceptanceDateTime="2026-08-26T20:00:00.000Z")])
        ).filings[0]
        event = build(record, company_id=42, company_key="K", fetched_at=AS_OF)[0]

        assert is_current(event, as_of=AS_OF) is True

    def test_an_old_event_is_not_current_but_is_still_stored(self) -> None:
        rows = [
            _filing(
                accessionNumber="old",
                filingDate="2024-01-01",
                acceptanceDateTime="2024-01-01T20:00:00.000Z",
            )
        ]
        service, store = _service(submissions(filings=rows))
        service.ingest_company(1045810, now=NOW)

        event = store.events_for_company(42, as_of=AS_OF)[0]

        assert is_current(event, as_of=AS_OF) is False
        assert store.count() == 1  # retained, not deleted

    def test_windows_differ_by_kind(self) -> None:
        from app.research_intelligence.freshness import window_days

        assert window_days(EventKind.ACCOUNTING_RESTATEMENT) > window_days(
            EventKind.MANAGEMENT_CHANGE
        )


class TestStructuralBoundaries:
    FORBIDDEN = (
        "app.broker",
        "app.paper",
        "app.discord_bot",
        "app.publishing",
        "app.notifications",
        "alpaca",
        "anthropic",
        "openai",
    )

    def test_the_research_core_reaches_no_consumer_or_model_provider(self) -> None:
        import ast
        from pathlib import Path

        for path in Path("app/research_intelligence").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert not any(name.startswith(f) for f in self.FORBIDDEN), (
                        f"{path} imports {name}"
                    )

    def test_no_module_reaches_a_language_model(self) -> None:
        from pathlib import Path

        for path in Path("app/research_intelligence").glob("*.py"):
            body = path.read_text().split('"""', 2)[-1]
            # Precise API shapes, not English words: "Completion of
            # Acquisition or Disposition of Assets" is the title of SEC Item
            # 2.01, and a substring gate on "completion" flags that as a model
            # call. The point is to catch a client, not a vocabulary.
            for token in (
                "chat.completions",
                "completions.create",
                "generate_content",
                "messages.create",
                "openai.",
                "anthropic.",
                "genai.",
            ):
                assert token not in body, f"{path} references {token}"

    def test_no_module_opens_a_network_connection(self) -> None:
        """**The gate.** Network access reaches SEC only through the injected
        EdgarClient, which owns the declared User-Agent and the rate limit. A
        second socket anywhere in this package would bypass both -- and would
        make some test somewhere depend on SEC being up."""
        from pathlib import Path

        for path in Path("app/research_intelligence").glob("*.py"):
            body = path.read_text()
            for token in ("urlopen", "requests.get", "httpx.", "urllib.request"):
                assert token not in body, f"{path} opens a connection via {token}"

    def test_the_analysis_modules_are_pure(self) -> None:
        """Classification, taxonomy, freshness and identity are functions of
        their arguments. Persistence lives in `store` and `company_names`, and
        keeping it out of the analysis modules is what lets every
        classification test run against a literal fixture."""
        from pathlib import Path

        pure = (
            "schemas.py",
            "taxonomy.py",
            "extraction.py",
            "freshness.py",
            "identity.py",
            "sec.py",
        )
        for name in pure:
            body = (Path("app/research_intelligence") / name).read_text()
            for token in ("sqlite3", "write_text", "os.environ"):
                assert token not in body, f"{name} performs I/O via {token}"


class TestIngestionReporting:
    def test_the_report_counts_what_happened(self) -> None:
        rows = [
            _filing(accessionNumber="a1", items="2.02,9.01"),
            _filing(accessionNumber="a2", items="1.01,5.02"),
            _filing(accessionNumber="a3", items="9.01"),
            _filing(accessionNumber="a4", form="6-K", items=None),
        ]
        service, _store = _service(submissions(filings=rows))

        report = service.ingest_company(1045810, now=NOW)

        assert report.filings_examined == 4
        assert report.administrative_only == 1
        assert report.events_built == 4  # 1 + 2 + 0 + 1
        assert report.classified == 3  # 2.02, then 1.01 and 5.02
        assert report.unclassified == 1  # the 6-K, which the form does not constrain
        assert report.by_kind["EARNINGS_RELEASE"] == 1

    def test_an_unavailable_source_degrades_without_raising(self) -> None:
        service, store = _service(RuntimeError("edgar down"))

        report = service.ingest_company(1045810, now=NOW)

        assert report.company_id is None
        assert "unavailable" in (report.detail or "")
        assert store.count() == 0


class TestCompanyNameBackfill:
    """Recovering a display name from the payload ingestion already holds.

    Every assertion here is about a refusal. The mechanism's value is small --
    a nicer string on a card -- and its failure mode is not: a name written
    against the wrong company would put a real business's identity on another
    company's report, which is the phase 13 defect with a new cause.
    """

    def test_a_ticker_shaped_name_with_a_cik_is_proposed(self) -> None:
        from app.research_intelligence.company_names import plan

        result = plan([(42, "0001045810", "NVDA")], {"0001045810": "NVIDIA CORP"})

        assert len(result.changes) == 1
        assert result.changes[0].proposed == "NVIDIA CORP"
        assert result.changes[0].cik == "0001045810"

    def test_a_curated_name_is_never_overwritten(self) -> None:
        """`Deutsche Telekom AG` was set deliberately. SEC's own spelling of
        some other registrant must not replace it."""
        from app.research_intelligence.company_names import plan

        result = plan([(3, "0000936340", "Deutsche Telekom AG")], {"0000936340": "DTE ENERGY CO"})

        assert result.changes == ()
        assert "Deutsche Telekom AG" in result.kept_curated

    def test_a_company_without_a_cik_is_left_alone(self) -> None:
        """**The gate.** AEP and BRK.B carry no CIK, so no authoritative name
        exists and none is guessed."""
        from app.research_intelligence.company_names import plan

        result = plan([(9, None, "BRK.B")], {"0001045810": "NVIDIA CORP"})

        assert result.changes == ()
        assert "BRK.B" in result.no_authority

    def test_a_cik_with_no_fetched_name_changes_nothing(self) -> None:
        from app.research_intelligence.company_names import plan

        assert plan([(42, "0001045810", "NVDA")], {}).changes == ()

    def test_planning_writes_nothing(self) -> None:
        """`plan` is pure: producing a proposal and mutating shared state are
        two decisions, not one."""
        import inspect

        from app.research_intelligence import company_names

        source = inspect.getsource(company_names.plan)
        for token in ("UPDATE", "INSERT", "commit", "execute"):
            assert token not in source

    def test_matching_is_never_by_name(self) -> None:
        """No fuzzy matching, and no merging of company rows."""
        import inspect

        from app.research_intelligence import company_names

        source = inspect.getsource(company_names)
        for token in ("difflib", "get_close_matches", "SequenceMatcher", "levenshtein"):
            assert token.lower() not in source.lower()
