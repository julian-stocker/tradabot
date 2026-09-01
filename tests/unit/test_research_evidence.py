"""What the evidence layer will and will not read out of a filing.

The failure this file guards against is not a crash. It is a number: precise,
sourced-looking, and attached to the wrong period, the wrong basis or the wrong
currency. Every one of those reads as a finding rather than a fault, so most of
what follows asserts a **refusal** and names the reason.

The hazardous cases are not invented. They were measured on real filings during
Phase 15.1 and are reproduced here verbatim in structure:

* NVIDIA's Item 2.02 filing carries **two** EX-99 exhibits whose descriptions
  are only ``EX-99.1`` and ``EX-99.2`` -- metadata does not say which is the
  earnings release;
* its press release states GAAP and non-GAAP earnings per diluted share in one
  sentence, ``$2.46 and $2.22, respectively``;
* its income statement puts ``Three Months Ended`` beside ``Six Months
  Ended``;
* its scale lives in a caption, ``($ in millions...)``, not beside the figure;
* Apple states the period in one sentence and the figure in the next.

Nothing here touches the network.
"""

from __future__ import annotations

import html
from typing import Any

import pytest

from app.research_intelligence import content, documents, facts
from app.research_intelligence.context import magnitude
from app.research_intelligence.schemas import (
    UNKNOWN_CURRENCY,
    ContextStatus,
    DocumentRole,
    EventKind,
    EvidenceStatus,
    FactStatus,
    FiscalPeriod,
    ResearchDocument,
)
from app.research_intelligence.sec import FilingRecord
from app.research_intelligence.service import EvidenceService
from app.research_intelligence.store import EventStore

VERSION = documents.EXTRACTION_VERSION
CIK = "0001045810"
ACCESSION = "0001045810-26-000073"


# --------------------------------------------------------------- fixtures
def record(form: str = "8-K", items: tuple[str, ...] = ("2.02", "9.01")) -> FilingRecord:
    return FilingRecord(
        cik=CIK,
        accession=ACCESSION,
        form=form,
        filing_date="2026-08-26",
        acceptance="2026-08-26T16:20:31.000Z",
        report_date="2026-08-26",
        items=items,
        primary_document="nvda-20260826.htm",
        primary_description="8-K",
        size=1000,
    )


MANIFEST = html.escape(
    """<SEC-DOCUMENT>0001045810-26-000073.txt
<ACCEPTANCE-DATETIME>20260826162031
<DOCUMENT>
<TYPE>8-K
<SEQUENCE>1
<FILENAME>nvda-20260826.htm
<DESCRIPTION>8-K
<DOCUMENT>
<TYPE>EX-99.1
<SEQUENCE>2
<FILENAME>q2fy27pr.htm
<DESCRIPTION>EX-99.1
<DOCUMENT>
<TYPE>EX-99.2
<SEQUENCE>3
<FILENAME>q2fy27cfocommentary.htm
<DESCRIPTION>EX-99.2
<DOCUMENT>
<TYPE>EX-101.SCH
<SEQUENCE>4
<FILENAME>nvda-20260826.xsd
<DESCRIPTION>XBRL TAXONOMY EXTENSION SCHEMA
<DOCUMENT>
<TYPE>GRAPHIC
<SEQUENCE>5
<FILENAME>logo.jpg
<DESCRIPTION>GRAPHIC
"""
)
"""The SGML header block, HTML-escaped exactly as SEC serves it in
``{accession}-index-headers.html``. Escaped, because a parser that forgot to
unescape would find no ``<DOCUMENT>`` tags at all and silently return nothing."""


def document(
    filename: str = "q2fy27pr.htm",
    doc_type: str = "EX-99.1",
    role: DocumentRole = DocumentRole.EXHIBIT,
) -> ResearchDocument:
    return ResearchDocument(
        document_id=documents.document_id(ACCESSION, 2, filename),
        company_id=7,
        cik=CIK,
        accession=ACCESSION,
        document_type=doc_type,
        role=role,
        filename=filename,
        sequence=2,
        description=doc_type,
        source_url=f"https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/{filename}",
        published_at="2026-08-26T16:20:31.000Z",
        content_hash="deadbeef",
        raw_size=1024,
        extraction_version=VERSION,
    )


def extract(text: str) -> facts.ExtractionOutcome:
    return facts.extract(text, document=document(), event_id="e1", extraction_version=VERSION)


def only_refusal(text: str) -> facts.FactRefusal:
    outcome = extract(text)
    assert not outcome.facts, [f.metric for f in outcome.facts]
    assert len(outcome.refusals) == 1, [r.detail for r in outcome.refusals]
    return outcome.refusals[0]


# ------------------------------------------------------- manifest parsing
def test_manifest_is_parsed_from_escaped_sgml() -> None:
    parsed = documents.parse_manifest(MANIFEST, record=record(), company_id=7)
    assert [d.document_type for d in parsed] == [
        "8-K",
        "EX-99.1",
        "EX-99.2",
        "EX-101.SCH",
        "GRAPHIC",
    ]
    assert [d.sequence for d in parsed] == [1, 2, 3, 4, 5]


def test_manifest_urls_stay_inside_the_filing_directory() -> None:
    for parsed in documents.parse_manifest(MANIFEST, record=record(), company_id=7):
        assert parsed.source_url.startswith(
            "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/"
        )


def test_roles_separate_prose_from_machine_readable_attachments() -> None:
    by_name = {
        d.document_type: d.role
        for d in documents.parse_manifest(MANIFEST, record=record(), company_id=7)
    }
    assert by_name["8-K"] is DocumentRole.PRIMARY
    assert by_name["EX-99.1"] is DocumentRole.EXHIBIT
    assert by_name["EX-101.SCH"] is DocumentRole.XBRL
    assert by_name["GRAPHIC"] is DocumentRole.GRAPHIC


def test_uncitable_attachments_are_not_persisted() -> None:
    """A 40-F was measured listing 333 documents, nearly all XBRL."""
    kept = documents.citable(documents.parse_manifest(MANIFEST, record=record(), company_id=7))
    assert [d.document_type for d in kept] == ["8-K", "EX-99.1", "EX-99.2"]


def test_document_identity_is_stable_across_reparses() -> None:
    first = documents.parse_manifest(MANIFEST, record=record(), company_id=7)
    second = documents.parse_manifest(MANIFEST, record=record(), company_id=7)
    assert [d.document_id for d in first] == [d.document_id for d in second]


# ---------------------------------------------------- document selection
def test_both_exhibits_are_candidates_because_metadata_does_not_choose() -> None:
    """The measured case: descriptions are only 'EX-99.1' and 'EX-99.2'.

    Neither says 'earnings release', so picking the lower number would be a
    convention this layer invented, not a fact the filing established.
    """
    parsed = documents.citable(documents.parse_manifest(MANIFEST, record=record(), company_id=7))
    selected, rule = documents.select_documents(parsed, EventKind.EARNINGS_RELEASE)
    assert [d.filename for d in selected] == ["q2fy27pr.htm", "q2fy27cfocommentary.htm"]
    assert "EX-99" in rule


def test_a_filing_with_no_exhibit_falls_back_to_the_primary_document() -> None:
    parsed = [
        d
        for d in documents.parse_manifest(MANIFEST, record=record(), company_id=7)
        if d.role is DocumentRole.PRIMARY
    ]
    selected, rule = documents.select_documents(parsed, EventKind.MANAGEMENT_CHANGE)
    assert [d.filename for d in selected] == ["nvda-20260826.htm"]
    assert "primary" in rule


@pytest.mark.parametrize("kind", [EventKind.PERIODIC_REPORT, EventKind.UNCLASSIFIED_SEC_FILING])
def test_kinds_with_no_established_claim_select_nothing(kind: EventKind) -> None:
    parsed = documents.citable(documents.parse_manifest(MANIFEST, record=record(), company_id=7))
    selected, _ = documents.select_documents(parsed, kind)
    assert selected == []


# -------------------------------------------------------- normalisation
def test_normalisation_is_deterministic() -> None:
    raw = b"<p>Revenue&nbsp;was&nbsp;$1.0 billion.</p>"
    assert content.to_text(raw, "text/html") == content.to_text(raw, "text/html")


def test_hyperlinks_are_stripped_rather_than_followed() -> None:
    raw = b'<p>See <a href="http://evil.example/x">our site</a> for more.</p>'
    text = content.to_text(raw, "text/html")
    assert "evil.example" not in text
    assert "our site" in text


def test_scripts_are_removed_entirely() -> None:
    raw = b"<p>Revenue.</p><script>alert('x')</script>"
    assert "alert" not in content.to_text(raw, "text/html")


def test_table_cells_keep_a_tab_so_the_extractor_can_refuse_them() -> None:
    raw = b"<table><tr><td>Net income</td><td>59,688</td></tr></table>"
    assert "\t" in content.to_text(raw, "text/html")


def test_content_hash_is_over_the_bytes_sec_served() -> None:
    assert content.content_hash(b"a") != content.content_hash(b"b")
    assert content.content_hash(b"a") == content.content_hash(b"a")


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("text/html; charset=utf-8", True),
        ("text/plain", True),
        ("application/pdf", False),
        ("image/jpeg", False),
        (None, False),
    ],
)
def test_only_text_content_is_readable(content_type: str | None, expected: bool) -> None:
    assert content.supported(content_type) is expected


# --------------------------------------------------------- extraction: yes
def test_a_sentence_stating_everything_yields_a_fact() -> None:
    outcome = extract(
        "NVIDIA today reported revenue for the second quarter ended July 26, 2026, "
        "of $96.2 billion, up 18% from the previous quarter."
    )
    (fact,) = outcome.facts
    assert fact.metric == "revenue"
    assert fact.value == pytest.approx(96_200_000_000)
    assert fact.currency == "USD"
    assert fact.fiscal_period is FiscalPeriod.QUARTER
    assert fact.period_end == "2026-07-26"
    assert fact.basis == "GAAP"


def test_evidence_offsets_point_at_the_cited_sentence() -> None:
    text = (
        "Some preamble here.\n"
        "NVIDIA today reported revenue for the second quarter ended July 26, 2026, "
        "of $96.2 billion."
    )
    (fact,) = extract(text).facts
    assert fact.evidence is not None
    cited = text[fact.evidence.text_start : fact.evidence.text_end].strip()
    assert cited == fact.evidence.evidence_text
    assert "96.2 billion" in cited


def test_a_period_may_come_from_the_paragraph_but_not_beyond_it() -> None:
    """Apple's measured shape: period in one sentence, figure in the next."""
    same_paragraph = (
        "Apple today announced financial results for its fiscal 2026 third quarter "
        "ended June 27, 2026. The Company posted quarterly revenue of $109.4 billion."
    )
    (fact,) = extract(same_paragraph).facts
    assert fact.period_end == "2026-06-27"

    across_a_block = (
        "Apple today announced financial results for its fiscal 2026 third quarter "
        "ended June 27, 2026.\nOutlook\nRevenue is expected to be $120.0 billion."
    )
    assert not extract(across_a_block).facts


def test_a_sentence_disagreeing_with_its_paragraph_does_not_inherit() -> None:
    """The annual date governs its own sentence and is refused to the other."""
    outcome = extract(
        "Revenue for the year ended December 31, 2025 was $400.0 billion. "
        "Fourth-quarter revenue rose to $110.0 billion."
    )
    (annual,) = outcome.facts
    assert annual.fiscal_period is FiscalPeriod.YEAR
    assert annual.value == pytest.approx(400_000_000_000)
    (refusal,) = outcome.refusals
    assert refusal.status is FactStatus.AMBIGUOUS_PERIOD
    assert "quarter" in refusal.detail


def test_a_year_over_year_comparison_is_not_read_as_an_annual_period() -> None:
    outcome = extract(
        "Apple today announced results for the quarter ended June 27, 2026. "
        "The Company posted quarterly revenue of $109.4 billion, up 16 percent "
        "year over year."
    )
    (fact,) = outcome.facts
    assert fact.fiscal_period is FiscalPeriod.QUARTER


# ---------------------------------------------------------- extraction: no
def test_gaap_and_non_gaap_in_one_sentence_is_refused() -> None:
    """Measured verbatim on NVIDIA's press release."""
    refusal = only_refusal(
        "GAAP and non-GAAP earnings per diluted share for the quarter ended "
        "July 26, 2026 were $2.46 and $2.22, respectively."
    )
    assert refusal.status is FactStatus.NON_GAAP_BASIS


@pytest.mark.parametrize(
    "sentence",
    [
        "Adjusted revenue for the quarter ended July 26, 2026 was $96.2 billion.",
        "Pro forma revenue for the quarter ended July 26, 2026 was $96.2 billion.",
        "Revenue excluding China for the quarter ended July 26, 2026 was $96.2 billion.",
    ],
)
def test_any_non_gaap_framing_is_refused(sentence: str) -> None:
    assert only_refusal(sentence).status is FactStatus.NON_GAAP_BASIS


def test_three_months_beside_six_months_is_refused() -> None:
    """The column ambiguity that makes flattened tables unreadable."""
    refusal = only_refusal(
        "Revenue for the three months ended July 26, 2026 and the six months "
        "ended July 26, 2026 was $96.2 billion."
    )
    assert refusal.status is FactStatus.AMBIGUOUS_PERIOD


def test_a_figure_with_no_dated_period_anywhere_is_refused() -> None:
    refusal = only_refusal("Revenue of $96.2 billion, up 106% from a year ago.")
    assert refusal.status is FactStatus.AMBIGUOUS_PERIOD
    assert "no dated period" in refusal.detail


def test_a_scale_stated_only_in_a_caption_does_not_reach_the_figure() -> None:
    """``($ in millions)`` is a caption. The sentence still says ``$26,422``."""
    refusal = only_refusal(
        "($ in millions) Net income for the quarter ended July 26, 2026 was $26,422."
    )
    assert refusal.status is FactStatus.AMBIGUOUS_UNIT


def test_two_metrics_in_one_sentence_are_refused() -> None:
    refusal = only_refusal(
        "Revenue and net income for the quarter ended July 26, 2026 were "
        "$96.2 billion and $26.4 billion."
    )
    assert refusal.status is FactStatus.AMBIGUOUS_METRIC


def test_two_amounts_for_one_metric_are_refused() -> None:
    refusal = only_refusal(
        "Revenue for the quarter ended July 26, 2026 rose to $96.2 billion from $56.1 billion."
    )
    assert refusal.status is FactStatus.AMBIGUOUS_VALUE


def test_a_table_row_is_never_read() -> None:
    outcome = extract("Net income\t59,688\t53,954")
    assert not outcome.facts
    assert not outcome.refusals


def test_a_bare_metric_heading_is_not_counted_as_a_refusal() -> None:
    """A heading asserts nothing, so it is neither a fact nor a declined one."""
    outcome = extract("Diluted earnings per share")
    assert not outcome.facts
    assert not outcome.refusals


def test_free_cash_flow_is_never_derived() -> None:
    outcome = extract(
        "Operating cash flow for the quarter ended July 26, 2026 was $15.4 billion "
        "and capital expenditure was $3.1 billion."
    )
    assert not any(f.metric == "free_cash_flow" for f in outcome.facts)


# ----------------------------------------------------------------- currency
def test_a_bare_dollar_resolves_to_usd_when_nothing_contests_it() -> None:
    assert facts.document_currency("Revenue was $96.2 billion.") == "USD"


def test_a_bare_dollar_is_unknown_when_the_document_also_uses_another_dollar() -> None:
    text = "Revenue was C$4.0 billion. Net income was $1.0 billion."
    assert facts.document_currency(text) == UNKNOWN_CURRENCY


def test_an_unresolvable_dollar_blocks_the_fact() -> None:
    refusal = only_refusal(
        "Amounts are stated in C$. Revenue for the quarter ended July 26, 2026 was $96.2 billion."
    )
    assert refusal.status is FactStatus.UNKNOWN_CURRENCY


def test_an_explicit_symbol_names_its_own_currency() -> None:
    (fact,) = extract("Revenue for the quarter ended June 30, 2026 was €7.0 billion.").facts
    assert fact.currency == "EUR"


def test_a_yen_sign_is_not_treated_as_a_currency() -> None:
    """``¥`` is used for both the yen and the renminbi."""
    assert "¥" not in facts.CURRENCY_SYMBOLS


# ------------------------------------------------------------- fact identity
def test_fact_identity_is_deterministic() -> None:
    text = (
        "NVIDIA today reported revenue for the second quarter ended July 26, 2026, "
        "of $96.2 billion."
    )
    assert [f.fact_id for f in extract(text).facts] == [f.fact_id for f in extract(text).facts]


def test_a_new_extraction_version_writes_beside_the_old_one() -> None:
    text = (
        "NVIDIA today reported revenue for the second quarter ended July 26, 2026, "
        "of $96.2 billion."
    )
    (old,) = facts.extract(
        text, document=document(), event_id="e1", extraction_version="15.1.0"
    ).facts
    (new,) = facts.extract(
        text, document=document(), event_id="e1", extraction_version="15.2.0"
    ).facts
    assert old.fact_id != new.fact_id


def test_a_document_contradicting_itself_yields_neither_value() -> None:
    outcome = extract(
        "Revenue for the quarter ended July 26, 2026 was $96.2 billion.\n"
        "Revenue for the quarter ended July 26, 2026 was $91.0 billion."
    )
    assert not outcome.facts
    assert any(r.status is FactStatus.AMBIGUOUS_VALUE for r in outcome.refusals)


# ------------------------------------------------------------------- store
def test_documents_and_facts_round_trip() -> None:
    store = EventStore(":memory:")
    doc = document()
    store.upsert_documents([doc])
    (fact,) = extract(
        "NVIDIA today reported revenue for the second quarter ended July 26, 2026, "
        "of $96.2 billion."
    ).facts
    assert store.upsert_facts([fact]) == 1
    assert store.upsert_facts([fact]) == 0
    stored = store.document(doc.document_id)
    assert stored is not None
    assert stored.filename == doc.filename
    (back,) = store.facts_for_event("e1")
    assert back.value == pytest.approx(fact.value)
    assert back.evidence is not None
    assert back.evidence.evidence_text == fact.evidence.evidence_text  # type: ignore[union-attr]


# ---------------------------------------------------------------- pipeline
class FakeClient:
    """Serves fixture bytes for URLs, and refuses anything off the archive."""

    def __init__(self, pages: dict[str, tuple[bytes, str]]) -> None:
        self._pages = pages
        self.requested: list[str] = []

    def archive_document(self, url: str) -> tuple[bytes, str]:
        if not url.startswith("https://www.sec.gov/Archives/edgar/data/"):
            raise RuntimeError("refused")
        self.requested.append(url)
        if url not in self._pages:
            raise RuntimeError("not found")
        return self._pages[url]


RELEASE = (
    b"<p>NVIDIA today reported revenue for the second quarter ended July 26, 2026, "
    b"of $96.2 billion.</p>"
)
BASE = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/"


DEFAULT_PAGES = {
    f"{BASE}{ACCESSION}-index-headers.html": (MANIFEST.encode(), "text/html"),
    f"{BASE}q2fy27pr.htm": (RELEASE, "text/html"),
    f"{BASE}q2fy27cfocommentary.htm": (b"<p>Commentary.</p>", "text/html"),
}


def pipeline(pages: dict[str, tuple[bytes, str]] | None = None) -> tuple[Any, Any, Any]:
    store = EventStore(":memory:")
    client = FakeClient(dict(DEFAULT_PAGES) if pages is None else pages)
    return client, store, EvidenceService(client=client, store=store, extraction_version=VERSION)


def event(kind: EventKind = EventKind.EARNINGS_RELEASE) -> Any:
    from app.research_intelligence import build

    built = build(record(), company_id=7, company_key="CIK0001045810", fetched_at="2026-08-26")
    return next(e for e in built if e.event_kind is kind)


def test_the_pipeline_stores_documents_and_cited_facts() -> None:
    _client, store, service = pipeline()
    report = service.collect(record(), events=[event()], company_id=7)
    assert report.status is EvidenceStatus.OK
    assert report.documents_selected == 2
    assert report.facts_extracted == 1
    assert store.count_facts() == 1


def test_a_second_pass_refetches_nothing() -> None:
    client, _store, service = pipeline()
    service.collect(record(), events=[event()], company_id=7)
    before = len(client.requested)
    again = service.collect(record(), events=[event()], company_id=7)
    assert again.documents_cached == 2
    assert again.documents_fetched == 0
    assert len(client.requested) == before  # not even the manifest


def test_changed_archive_content_keeps_the_original_provenance() -> None:
    client, store, service = pipeline()
    service.collect(record(), events=[event()], company_id=7)
    original = store.documents_for_accession(ACCESSION)
    kept = next(d for d in original if d.filename == "q2fy27pr.htm").content_hash

    client._pages[f"{BASE}q2fy27pr.htm"] = (b"<p>Something else entirely.</p>", "text/html")
    report = service.collect(record(), events=[event()], company_id=7, refresh=True)
    assert report.status is EvidenceStatus.CHANGED_SOURCE_CONTENT
    after = next(
        d for d in store.documents_for_accession(ACCESSION) if d.filename == "q2fy27pr.htm"
    )
    assert after.content_hash == kept


def test_an_unsupported_content_type_is_named_not_guessed_at() -> None:
    _client, _store, service = pipeline(
        {
            f"{BASE}{ACCESSION}-index-headers.html": (MANIFEST.encode(), "text/html"),
            f"{BASE}q2fy27pr.htm": (b"%PDF-1.4", "application/pdf"),
            f"{BASE}q2fy27cfocommentary.htm": (b"%PDF-1.4", "application/pdf"),
        }
    )
    report = service.collect(record(), events=[event()], company_id=7)
    assert report.status is EvidenceStatus.UNSUPPORTED_CONTENT_TYPE
    assert report.facts_extracted == 0


def test_an_unreachable_manifest_costs_no_classification() -> None:
    _client, store, service = pipeline({})
    report = service.collect(record(), events=[event()], company_id=7)
    assert report.status is EvidenceStatus.CONTENT_FETCH_FAILED
    assert store.count_facts() == 0


def test_a_foreign_filing_gains_evidence_without_gaining_a_classification() -> None:
    """Evidence availability does not authorize event classification."""
    from app.research_intelligence import build

    foreign = FilingRecord(
        cik=CIK,
        accession=ACCESSION,
        form="6-K",
        filing_date="2026-08-26",
        acceptance="2026-08-26T16:20:31.000Z",
        report_date=None,
        items=(),
        primary_document="sap.htm",
        primary_description="6-K",
        size=10,
    )
    (built,) = build(foreign, company_id=7, company_key="CIK0001045810", fetched_at="2026-08-26")
    assert built.event_kind is EventKind.UNCLASSIFIED_SEC_FILING
    _, _, service = pipeline()
    report = service.collect(foreign, events=[built], company_id=7)
    assert report.status is EvidenceStatus.NO_RELEVANT_EXHIBIT


# ------------------------------------------------------------------ context
class FakeTtm:
    def __init__(self, value: float | None, unit: str = "USD", status: str = "VALID") -> None:
        self.value = value
        self.status = status
        self.provenance = (type("P", (), {"unit": unit})(),) if value is not None else ()


class FakeStore:
    def __init__(self, result: FakeTtm) -> None:
        self._result = result
        self.asked: list[tuple[str, str, str]] = []

    def ttm(self, symbol: str, metric: str, as_of: str) -> FakeTtm:
        self.asked.append((symbol, metric, as_of))
        return self._result


def a_fact(**overrides: Any) -> Any:
    (fact,) = extract(
        "NVIDIA today reported revenue for the second quarter ended July 26, 2026, "
        "of $96.2 billion."
    ).facts
    from dataclasses import replace

    return replace(fact, **overrides) if overrides else fact


def test_magnitude_is_a_share_of_the_companys_own_trailing_year() -> None:
    store = FakeStore(FakeTtm(400_000_000_000.0))
    result = magnitude(
        a_fact(), store=store, symbol="CIK0001045810", as_of="2026-08-26T16:20:31.000Z"
    )
    assert result.status is ContextStatus.COMPUTED
    assert result.ratio == pytest.approx(0.2405)


def test_the_comparator_is_read_as_of_the_event_not_today() -> None:
    store = FakeStore(FakeTtm(400_000_000_000.0))
    magnitude(a_fact(), store=store, symbol="CIK0001045810", as_of="2026-08-26T16:20:31.000Z")
    assert store.asked == [("CIK0001045810", "revenue", "2026-08-26")]


def test_a_mismatched_currency_is_never_converted() -> None:
    store = FakeStore(FakeTtm(400_000_000_000.0, unit="DKK"))
    result = magnitude(a_fact(), store=store, symbol="X", as_of="2026-08-26")
    assert result.status is ContextStatus.CURRENCY_MISMATCH
    assert result.ratio is None


def test_no_comparator_on_file_is_named_rather_than_filled_in() -> None:
    store = FakeStore(FakeTtm(None, status="MISSING"))
    result = magnitude(a_fact(), store=store, symbol="X", as_of="2026-08-26")
    assert result.status is ContextStatus.NO_PIT_COMPARATOR


def test_a_per_share_figure_has_no_magnitude_to_scale() -> None:
    store = FakeStore(FakeTtm(400_000_000_000.0))
    result = magnitude(
        a_fact(unit=facts.Unit.CURRENCY_PER_SHARE), store=store, symbol="X", as_of="2026-08-26"
    )
    assert result.status is ContextStatus.NO_ESTABLISHED_AMOUNT


def test_a_balance_sheet_instant_is_not_divided_by_a_flow() -> None:
    store = FakeStore(FakeTtm(400_000_000_000.0))
    result = magnitude(
        a_fact(fiscal_period=FiscalPeriod.INSTANT), store=store, symbol="X", as_of="2026-08-26"
    )
    assert result.status is ContextStatus.INCOMPATIBLE_PERIOD


def test_an_unknown_currency_blocks_the_comparison() -> None:
    store = FakeStore(FakeTtm(400_000_000_000.0))
    result = magnitude(
        a_fact(currency=UNKNOWN_CURRENCY), store=store, symbol="X", as_of="2026-08-26"
    )
    assert result.status is ContextStatus.UNKNOWN_CURRENCY


# ------------------------------------------------------------------ boundary
class TestSecurityBoundary:
    """Filing text is data. It never chooses where a request goes.

    The threat is concrete rather than theoretical: an exhibit is a document
    written by the party being reported on, and it arrives full of filenames,
    hyperlinks and prose. If any of it could steer a fetch, a filer could point
    Tradabot at a host of their choosing simply by naming it.
    """

    def test_the_client_refuses_a_url_outside_the_edgar_archive(self) -> None:
        from app.fundamentals.client import EdgarClient, EdgarUnavailableError

        client = EdgarClient()
        for url in (
            "http://evil.example/x",
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany",
            "https://efts.sec.gov/LATEST/search-index?q=x",
            "https://www.sec.gov/Archives/edgar/data/1045810/../../../etc/passwd",
        ):
            with pytest.raises(EdgarUnavailableError):
                client.archive_document(url)

    def test_only_the_service_layer_retrieves_anything(self) -> None:
        """Parsing, normalising and extracting are offline by construction."""
        from pathlib import Path

        for name in ("documents.py", "content.py", "facts.py", "context.py"):
            body = (Path("app/research_intelligence") / name).read_text()
            assert "archive_document" not in body, f"{name} fetches"

    def test_efts_is_not_referenced_anywhere_in_the_package(self) -> None:
        from pathlib import Path

        for path in Path("app/research_intelligence").glob("*.py"):
            assert "efts.sec.gov" not in path.read_text().replace("``efts.sec.gov``", "").replace(
                "`efts.sec.gov`", ""
            ), f"{path} references EDGAR full-text search"

    def test_no_extracted_fact_carries_an_interpretation(self) -> None:
        (fact,) = extract(
            "NVIDIA today reported revenue for the second quarter ended July 26, 2026, "
            "of $96.2 billion."
        ).facts
        fields = set(fact.as_dict())
        assert not fields & {
            "interpretation",
            "direction",
            "sentiment",
            "recommendation",
            "target",
            "forecast",
        }


# ---------------------------------------------------------- metric scope
@pytest.mark.parametrize(
    "sentence",
    [
        # Scope nouns.
        "Data Center segment revenue for the quarter ended July 26, 2026 was $6.7 billion.",
        "Gaming business revenue for the quarter ended July 26, 2026 was $779 million.",
        "Product revenue for the quarter ended June 27, 2026 was $28.0 billion.",
        "Geographic revenue for the quarter ended June 27, 2026 was $28.0 billion.",
        # Hyphenation, which a scope-noun blacklist split apart and let through.
        "Business-unit revenue for the quarter ended June 27, 2026 was $28.0 billion.",
        "Client business-unit revenue for the quarter ended June 27, 2026 was $28.0 billion.",
        # Named segments, products and geographies.
        "Services revenue for the quarter ended June 27, 2026 was $28.0 billion.",
        "iPhone revenue for the quarter ended June 27, 2026 was $28.0 billion.",
        "Automotive revenue for the quarter ended June 27, 2026 was $28.0 billion.",
        "Domestic revenue for the quarter ended June 27, 2026 was $28.0 billion.",
        # Lowercase revenue qualities no noun list would have anticipated.
        "recurring revenue for the quarter ended June 27, 2026 was $28.0 billion.",
        "subscription revenue for the quarter ended June 27, 2026 was $28.0 billion.",
        "wholesale revenue for the quarter ended June 27, 2026 was $28.0 billion.",
    ],
)
def test_a_narrowed_figure_is_never_stored_as_the_companys(sentence: str) -> None:
    """The one way this module could produce a *wrong* number, not no number.

    ``Data Center segment revenue`` matches the label ``revenue``. Stored
    without the qualifier it is a figure four times too small, under the right
    metric name, with a citation that reads correctly.

    The last six cases are why the rule is an allowlist. A blacklist of scope
    nouns passed every one of them: it split ``business-unit`` on the hyphen,
    and it had never heard of ``recurring``. A list of English nouns is never
    finished, so the unknown word has to refuse.
    """
    refusal = only_refusal(sentence)
    assert refusal.status is FactStatus.AMBIGUOUS_METRIC
    assert "narrowed by" in refusal.detail


@pytest.mark.parametrize(
    "sentence",
    [
        "The Company posted quarterly revenue of $109.4 billion for the quarter "
        "ended June 27, 2026.",
        "Total revenue for the quarter ended June 27, 2026 was $109.4 billion.",
        "Company gross margin for the quarter ended June 27, 2026 was 50.1 percent.",
        "Revenue for the quarter ended June 27, 2026 was $109.4 billion.",
        "NVIDIA today reported revenue for the second quarter ended July 26, 2026, "
        "of $96.2 billion.",
        "Consolidated revenue for the quarter ended June 27, 2026 was $109.4 billion.",
    ],
)
def test_neutral_words_before_a_metric_do_not_narrow_it(sentence: str) -> None:
    """Determiners, entity words, basis words, period adjectives and glue.

    None of these categories can change *what* is being measured, which is why
    they are the only things allowed to precede a label.
    """
    assert extract(sentence).facts


# ------------------------------------------------------------ period safety
def test_the_five_period_shapes_stay_distinct() -> None:
    assert len({p.value for p in FiscalPeriod}) == 5
    cases = {
        "Revenue for the three months ended June 30, 2026 was $9.0 billion.": (
            FiscalPeriod.QUARTER
        ),
        "Revenue for the six months ended June 30, 2026 was $18.0 billion.": (
            FiscalPeriod.YEAR_TO_DATE
        ),
        "Revenue for the year ended December 31, 2025 was $36.0 billion.": FiscalPeriod.YEAR,
        "Revenue for the trailing twelve months ended June 30, 2026 was $36.0 billion.": (
            FiscalPeriod.TRAILING_TWELVE_MONTHS
        ),
    }
    for sentence, shape in cases.items():
        (fact,) = extract(sentence).facts
        assert fact.fiscal_period is shape, sentence


def test_a_paragraph_naming_two_periods_supplies_neither() -> None:
    """The income-statement shape: three months beside six months."""
    refusal = only_refusal(
        "Results for the three months ended June 30, 2026 and the six months "
        "ended June 30, 2026 follow. Revenue was $9.0 billion."
    )
    assert refusal.status is FactStatus.AMBIGUOUS_PERIOD
    assert "2 dated periods in the paragraph" in refusal.detail


@pytest.mark.parametrize(
    "sentence",
    [
        "Operating income is expected to be $40.0 billion.",
        "We expect net income of approximately $35.0 billion.",
        "Revenue guidance is $108.0 billion.",
        "Revenue is anticipated to be $108.0 billion.",
        "The Company's outlook is for revenue of $108.0 billion.",
        "Revenue will be approximately $108.0 billion.",
    ],
)
def test_guidance_never_inherits_a_reported_period(sentence: str) -> None:
    """Measured hole: guidance shares a paragraph with results routinely.

    ``Revenue for the quarter ended June 30, 2026 was $90.0 billion. Operating
    income is expected to be $40.0 billion.`` emitted the guidance figure as
    that quarter's operating income, correctly cited to a sentence saying
    "expected". A forecast is not a reported figure, so it is refused whole.
    """
    outcome = extract(f"Revenue for the quarter ended June 30, 2026 was $90.0 billion. {sentence}")
    assert all(
        f.evidence is not None and "$90.0 billion" in str(f.evidence.evidence_text)
        for f in outcome.facts
    )
    assert any(r.status is FactStatus.NO_STRUCTURED_FACT for r in outcome.refusals)


def test_period_inheritance_stops_at_the_block_boundary() -> None:
    outcome = extract(
        "Revenue for the quarter ended June 30, 2026 was $90.0 billion.\n"
        "Operating income was $40.0 billion."
    )
    assert [f.metric for f in outcome.facts] == ["revenue"]
    (refusal,) = outcome.refusals
    assert refusal.metric == "operating_income"
    assert refusal.status is FactStatus.AMBIGUOUS_PERIOD


# --------------------------------------------------------- gaap/non-gaap
@pytest.mark.parametrize(
    "sentence",
    [
        "Adjusted earnings per diluted share for the quarter ended June 30, 2026 was $2.22.",
        "Non-GAAP net income for the quarter ended June 30, 2026 was $53.9 billion.",
        "Adjusted operating income for the quarter ended June 30, 2026 was $3.1 billion.",
        "Non-GAAP gross margin for the quarter ended June 30, 2026 was 72.7%.",
        "Revenue on a constant currency basis for the quarter ended June 30, 2026 "
        "was $96.2 billion.",
        "Organic revenue for the quarter ended June 30, 2026 was $96.2 billion.",
    ],
)
def test_a_non_gaap_figure_never_becomes_a_canonical_looking_fact(sentence: str) -> None:
    assert only_refusal(sentence).status is FactStatus.NON_GAAP_BASIS


def test_every_emitted_fact_is_gaap_by_construction() -> None:
    text = (
        "NVIDIA today reported revenue for the second quarter ended July 26, 2026, "
        "of $96.2 billion.\n"
        "Apple today announced results for the quarter ended June 27, 2026. "
        "Company gross margin was 50.1 percent."
    )
    facts_found = extract(text).facts
    assert facts_found
    assert {f.basis for f in facts_found} == {"GAAP"}


def test_the_extractor_has_no_non_gaap_emission_path() -> None:
    """There is no branch that sets a basis other than GAAP."""
    from pathlib import Path

    body = Path("app/research_intelligence/facts.py").read_text()
    assert body.count('basis="') == 1
    assert 'basis="GAAP"' in body


# ------------------------------------------------------- provenance chain
def test_a_fact_round_trips_to_the_sec_archive_url_and_hash() -> None:
    doc = document()
    (fact,) = facts.extract(
        "NVIDIA today reported revenue for the second quarter ended July 26, 2026, "
        "of $96.2 billion.",
        document=doc,
        event_id="e1",
        extraction_version=VERSION,
    ).facts
    assert fact.document_id == doc.document_id
    assert fact.evidence is not None
    assert fact.evidence.url == doc.source_url
    assert fact.evidence.url.startswith("https://www.sec.gov/Archives/edgar/data/")
    assert fact.evidence.content_sha256 == doc.content_hash
    assert fact.evidence.document == doc.filename


def test_facts_from_two_exhibits_keep_their_own_documents() -> None:
    first, second = document("q2fy27pr.htm"), document("q2fy27cfocommentary.htm")
    sentence = (
        "NVIDIA today reported revenue for the second quarter ended July 26, 2026, "
        "of $96.2 billion."
    )
    (a,) = facts.extract(sentence, document=first, event_id="e1", extraction_version=VERSION).facts
    (b,) = facts.extract(sentence, document=second, event_id="e1", extraction_version=VERSION).facts
    assert a.document_id != b.document_id
    assert a.fact_id != b.fact_id
    assert a.evidence is not None
    assert b.evidence is not None
    assert a.evidence.document == "q2fy27pr.htm"
    assert b.evidence.document == "q2fy27cfocommentary.htm"


def test_two_documents_disagreeing_both_survive_with_their_own_provenance() -> None:
    """No winner is picked, because the filing does not name one.

    Identity includes the document, so a disagreement between two exhibits is
    two rows a reader can compare -- not one row chosen by exhibit number.
    """
    store = EventStore(":memory:")
    first, second = document("q2fy27pr.htm"), document("q2fy27cfocommentary.htm")
    store.upsert_documents([first, second])
    stored = []
    for doc, amount in ((first, "$96.2 billion"), (second, "$91.0 billion")):
        (fact,) = facts.extract(
            f"Revenue for the quarter ended July 26, 2026 was {amount}.",
            document=doc,
            event_id="e1",
            extraction_version=VERSION,
        ).facts
        stored.append(fact)
    assert store.upsert_facts(stored) == 2
    back = store.facts_for_event("e1")
    assert len({f.value for f in back}) == 2
    assert len({f.document_id for f in back}) == 2


def test_a_reparse_at_a_new_version_leaves_the_old_rows_in_place() -> None:
    store = EventStore(":memory:")
    doc = document()
    store.upsert_documents([doc])
    sentence = "Revenue for the quarter ended July 26, 2026 was $96.2 billion."
    for version in (VERSION, "15.2.0"):
        store.upsert_facts(
            facts.extract(sentence, document=doc, event_id="e1", extraction_version=version).facts
        )
    versions = {f.extraction_version for f in store.facts_for_event("e1")}
    assert versions == {VERSION, "15.2.0"}
    assert store.count_facts() == 2


def test_a_hyperlink_inside_an_exhibit_is_never_fetched() -> None:
    """The filer writes the exhibit. It must not choose where requests go."""
    hostile = (
        b"<p>Revenue for the quarter ended July 26, 2026 was $96.2 billion. "
        b'See <a href="http://evil.example/x">details</a> and '
        b'<a href="https://www.sec.gov/Archives/edgar/data/999/other.htm">more</a>.</p>'
    )
    client, store, service = pipeline(
        {
            f"{BASE}{ACCESSION}-index-headers.html": (MANIFEST.encode(), "text/html"),
            f"{BASE}q2fy27pr.htm": (hostile, "text/html"),
            f"{BASE}q2fy27cfocommentary.htm": (b"<p>Commentary.</p>", "text/html"),
        }
    )
    service.collect(record(), events=[event()], company_id=7)
    assert store.count_facts() == 1
    for url in client.requested:
        assert url.startswith(BASE), url
