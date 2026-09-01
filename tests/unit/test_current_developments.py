"""What ``/check`` shows about a company's filings, and what it refuses to.

The section is one sentence away from being a tip sheet. "Earnings · Aug 26 ·
Significant" reads as a verdict unless every part of it is held to what the
source actually established, so most of what follows asserts an absence: no
direction, no forecast, no recommendation vocabulary, no green or red from an
event, and no figure that was not extracted safely.

The other half is about silence. Four different things produce an empty
section -- no qualifying filing, a company never ingested, no research store,
and a filing regime that carries no item codes -- and a reader who cannot tell
them apart cannot tell whether the silence is about the company or about
Tradabot. Each is asserted separately.

Nothing here touches the network or a live database.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.discord_bot.render import check_message
from app.discord_bot.resolve import Availability, Resolution
from app.publishing import presentation
from app.research_intelligence.developments import (
    MAX_CURRENT_DEVELOPMENTS,
    CoverageStatus,
    CurrentDevelopmentsService,
    filing_url,
)
from app.research_intelligence.schemas import (
    Confidence,
    EventKind,
    EventScope,
    EvidenceReference,
    FiscalPeriod,
    HistoricalEvidence,
    Materiality,
    ResearchEvent,
    ResearchFact,
)
from app.research_intelligence.store import EventStore

AS_OF = "2026-09-01"
CIK = "0001045810"
COMPANY = 42


def event(
    *,
    accession: str = "0001045810-26-000073",
    kind: EventKind = EventKind.EARNINGS_RELEASE,
    item: str | None = "2.02",
    published: str = "2026-08-26T16:20:31.000Z",
    form: str = "8-K",
    materiality: Materiality = Materiality.SIGNIFICANT,
    superseded_at: str | None = None,
    company_id: int = COMPANY,
    event_id: str | None = None,
) -> ResearchEvent:
    return ResearchEvent(
        event_id=event_id or f"{accession}:{item}",
        company_id=company_id,
        company_key="CIK0001045810",
        cik=CIK,
        scope=EventScope.COMPANY,
        event_kind=kind,
        published_at=published,
        fetched_at=published,
        form=form,
        accession=accession,
        item_codes=(item,) if item else (),
        classifying_item=item,
        fact_summary=f"SEC filing of form {form} reports an event under Item {item}.",
        materiality=materiality,
        superseded_at=superseded_at,
        source_url=f"https://www.sec.gov/Archives/edgar/data/1045810/{accession}.htm",
    )


def fact(
    *,
    event_id: str = "0001045810-26-000073:2.02",
    metric: str = "revenue",
    value: float = 96_200_000_000.0,
    unit: str = "CURRENCY",
    currency: str = "USD",
) -> ResearchFact:
    return ResearchFact(
        fact_id=f"{event_id}:{metric}",
        event_id=event_id,
        company_id=COMPANY,
        metric=metric,
        value=value,
        unit=unit,
        currency=currency,
        fiscal_period=FiscalPeriod.QUARTER,
        period_end="2026-07-26",
        document_id="d1",
        evidence=EvidenceReference(
            document="q2fy27pr.htm",
            url="https://www.sec.gov/Archives/edgar/data/1045810/x/q2fy27pr.htm",
            role="EXHIBIT",
            content_sha256="abc",
            evidence_text="revenue for the second quarter ended July 26, 2026, of $96.2 billion",
        ),
        extraction_confidence=Confidence.HIGH,
        extraction_version="15.1.0",
    )


def store_with(events: list[ResearchEvent], facts: list[ResearchFact] | None = None) -> EventStore:
    store = EventStore(":memory:")
    store.upsert(events)
    if facts:
        store.upsert_facts(facts)
    return store


def service(
    events: list[ResearchEvent] | None = None,
    facts: list[ResearchFact] | None = None,
    **kw: Any,
) -> CurrentDevelopmentsService:
    store = None if events is None else store_with(events, facts)
    return CurrentDevelopmentsService(store=store, **kw)


def report(
    events: list[ResearchEvent] | None = None,
    facts: list[ResearchFact] | None = None,
    *,
    as_of: str = AS_OF,
    asset_type: str = "STOCK",
    company_id: int | None = COMPANY,
    **kw: Any,
) -> Any:
    return service(events, facts, **kw).for_company(
        company_id=company_id, cik=CIK, as_of=as_of, asset_type=asset_type
    )


# ---------------------------------------------------------------- selection
def test_a_recent_filing_becomes_a_current_development() -> None:
    result = report([event()])
    assert result.status is CoverageStatus.AVAILABLE
    (development,) = result.developments
    assert development.accession == "0001045810-26-000073"
    assert [i.label for i in development.items] == ["Earnings"]
    assert development.materiality is Materiality.SIGNIFICANT


def test_at_most_three_filings_are_shown_and_the_rest_are_counted() -> None:
    events = [
        event(
            accession=f"a{i}",
            item="1.01",
            kind=EventKind.MATERIAL_AGREEMENT,
            published=f"2026-08-{10 + i:02d}T12:00:00Z",
            event_id=f"e{i}",
        )
        for i in range(6)
    ]
    result = report(events)
    assert len(result.developments) == MAX_CURRENT_DEVELOPMENTS
    assert result.suppressed == 3


def test_ordering_is_recent_first_with_critical_reserved() -> None:
    """Two tiers, and both were measured into existence.

    Ranking purely by materiality put a ten-month-old completed acquisition
    above a fortnight-old management change. Ranking purely by recency would
    let three routine filings push a restatement off a card that has already
    turned orange because of it.
    """
    old_critical = event(
        accession="old",
        kind=EventKind.ACCOUNTING_RESTATEMENT,
        item="4.02",
        materiality=Materiality.CRITICAL,
        published="2026-06-01T12:00:00Z",
    )
    recent = event(accession="new", published="2026-08-30T12:00:00Z")
    middle = event(
        accession="mid",
        item="1.01",
        kind=EventKind.MATERIAL_AGREEMENT,
        materiality=Materiality.NOTABLE,
        published="2026-08-20T12:00:00Z",
    )
    result = report([recent, middle, old_critical])
    assert [d.accession for d in result.developments] == ["old", "new", "mid"]


def test_the_same_store_always_produces_the_same_order() -> None:
    same_day = [
        event(accession=f"a{i}", published="2026-08-26T16:20:31.000Z", event_id=f"e{i}")
        for i in range(3)
    ]
    first = [d.accession for d in report(same_day).developments]
    second = [d.accession for d in report(list(reversed(same_day))).developments]
    assert first == second


def test_a_periodic_report_is_counted_but_never_given_a_slot() -> None:
    """Its contents are the fundamentals section, not a development.

    Measured: AMD filed its 10-Q and its earnings release on the same day, and
    the 10-Q won the tiebreak and pushed the release -- the one carrying an
    extracted revenue figure -- off the card.
    """
    result = report(
        [
            event(),
            event(
                accession="10q",
                kind=EventKind.PERIODIC_REPORT,
                item=None,
                form="10-Q",
                materiality=Materiality.NOTABLE,
                published="2026-08-26T18:00:00Z",
            ),
        ]
    )
    assert [d.accession for d in result.developments] == ["0001045810-26-000073"]
    assert result.periodic_current == 1


def test_only_periodic_reports_is_a_distinct_answer() -> None:
    result = report(
        [event(accession="10q", kind=EventKind.PERIODIC_REPORT, item=None, form="10-Q")]
    )
    assert result.status is CoverageStatus.NO_CURRENT_EVENTS
    assert "periodic reports" in (result.detail or "")


# ---------------------------------------------------------------- freshness
def test_an_old_event_stays_stored_and_leaves_the_section() -> None:
    old = event(published="2024-01-05T12:00:00Z")
    store = store_with([old])
    result = CurrentDevelopmentsService(store=store).for_company(
        company_id=COMPANY, cik=CIK, as_of=AS_OF
    )
    assert result.status is CoverageStatus.NO_CURRENT_EVENTS
    assert store.count() == 1  # retained, not deleted


def test_freshness_windows_differ_by_kind() -> None:
    """A restatement stays current far longer than a management change."""
    when = "2026-03-01T12:00:00Z"
    restatement = report(
        [
            event(
                kind=EventKind.ACCOUNTING_RESTATEMENT,
                item="4.02",
                materiality=Materiality.CRITICAL,
                published=when,
            )
        ]
    )
    management = report(
        [
            event(
                kind=EventKind.MANAGEMENT_CHANGE,
                item="5.02",
                materiality=Materiality.NOTABLE,
                published=when,
            )
        ]
    )
    assert restatement.has_developments
    assert not management.has_developments


# ---------------------------------------------------------------------- PIT
def test_a_filing_published_after_as_of_is_invisible() -> None:
    future = event(published="2026-10-01T12:00:00Z")
    assert not report([future], as_of="2026-09-01").has_developments


def test_the_same_store_answers_two_dates_differently() -> None:
    early = event(accession="early", published="2026-07-01T12:00:00Z")
    late = event(accession="late", published="2026-08-26T12:00:00Z")
    store = store_with([early, late])
    developments = CurrentDevelopmentsService(store=store)

    before = developments.for_company(company_id=COMPANY, cik=CIK, as_of="2026-08-01")
    after = developments.for_company(company_id=COMPANY, cik=CIK, as_of="2026-09-01")

    assert [d.accession for d in before.developments] == ["early"]
    assert {d.accession for d in after.developments} == {"early", "late"}


def test_a_figure_cannot_be_newer_than_the_filing_that_carries_it() -> None:
    future = event(published="2026-10-01T12:00:00Z")
    result = report([future], [fact()], as_of="2026-09-01")
    assert not result.has_developments


# -------------------------------------------------------------- amendments
def test_an_amendment_replaces_the_original_only_after_it_is_published() -> None:
    original = event(accession="orig", event_id="orig", published="2026-08-10T12:00:00Z")
    amendment = event(
        accession="amend",
        event_id="amend",
        form="8-K/A",
        published="2026-08-20T12:00:00Z",
    )
    store = store_with([original, amendment])
    # The real path: ingestion marks supersession, and the row stays.
    store.mark_superseded("orig", by="amend", when=amendment.published_at)
    developments = CurrentDevelopmentsService(store=store)

    before = developments.for_company(company_id=COMPANY, cik=CIK, as_of="2026-08-15")
    after = developments.for_company(company_id=COMPANY, cik=CIK, as_of="2026-09-01")

    assert [d.accession for d in before.developments] == ["orig"]
    assert [d.accession for d in after.developments] == ["amend"]
    assert store.count() == 2  # the original is superseded, never deleted


# ------------------------------------------------------------- multi-item
def test_one_filing_with_two_items_renders_as_one_development() -> None:
    """An 8-K carrying Items 1.01 and 5.02 is one thing that happened."""
    accession = "0001045810-26-000069"
    result = report(
        [
            event(
                accession=accession,
                item="1.01",
                kind=EventKind.MATERIAL_AGREEMENT,
                materiality=Materiality.NOTABLE,
                event_id="a",
            ),
            event(
                accession=accession,
                item="5.02",
                kind=EventKind.MANAGEMENT_CHANGE,
                materiality=Materiality.NOTABLE,
                event_id="b",
            ),
        ]
    )
    (development,) = result.developments
    assert {i.item for i in development.items} == {"1.01", "5.02"}
    assert {i.label for i in development.items} == {"Material agreement", "Management change"}


def test_a_grouped_filing_takes_the_highest_materiality_of_its_items() -> None:
    accession = "multi"
    result = report(
        [
            event(
                accession=accession,
                item="5.02",
                kind=EventKind.MANAGEMENT_CHANGE,
                materiality=Materiality.NOTABLE,
                event_id="a",
            ),
            event(
                accession=accession,
                item="4.02",
                kind=EventKind.ACCOUNTING_RESTATEMENT,
                materiality=Materiality.CRITICAL,
                event_id="b",
            ),
        ]
    )
    (development,) = result.developments
    assert development.materiality is Materiality.CRITICAL


# ------------------------------------------------------------------- facts
def test_an_extracted_figure_is_shown_with_its_period() -> None:
    result = report([event()], [fact()])
    (development,) = result.developments
    (figure,) = development.figures
    assert figure.label == "Revenue"
    assert figure.value == pytest.approx(96_200_000_000)
    assert figure.period == "Quarter ended 26 Jul 2026"


def test_the_same_figure_from_two_exhibits_is_printed_once() -> None:
    """Two exhibits agreeing corroborate; printed twice they look like two
    different numbers."""
    from dataclasses import replace

    first = fact()
    second = replace(first, fact_id="other", document_id="d2")
    result = report([event()], [first, second])
    (development,) = result.developments
    assert len(development.figures) == 1


def test_no_refusal_reason_ever_reaches_the_report() -> None:
    result = report([event()], [fact()])
    text = str(result.as_dict())
    for internal in (
        "AMBIGUOUS_METRIC",
        "AMBIGUOUS_PERIOD",
        "NON_GAAP_BASIS",
        "AMBIGUOUS_VALUE",
        "AMBIGUOUS_UNIT",
        "NO_STRUCTURED_FACT",
    ):
        assert internal not in text


def test_a_filing_with_evidence_but_no_figure_still_says_so() -> None:
    result = report([event()])
    (development,) = result.developments
    assert development.figures == ()
    assert development.evidence_available


# ---------------------------------------------------------------- coverage
def test_no_research_store_is_not_the_same_as_no_events() -> None:
    assert report(None).status is CoverageStatus.UNAVAILABLE
    assert report([]).status is CoverageStatus.NO_COVERAGE


def test_a_company_never_ingested_says_so() -> None:
    result = report([event(company_id=999)])
    assert result.status is CoverageStatus.NO_COVERAGE
    assert "not ingested" in (result.detail or "")


def test_a_foreign_filer_reports_a_source_limitation() -> None:
    """Silence from a 6-K filer is a gap in classification, not in events."""
    result = report(
        [
            event(
                accession=f"6k{i}",
                kind=EventKind.UNCLASSIFIED_SEC_FILING,
                item=None,
                form="6-K",
                materiality=Materiality.ROUTINE,
                event_id=f"e{i}",
            )
            for i in range(3)
        ]
    )
    assert result.status is CoverageStatus.SOURCE_LIMITATION
    assert result.unclassified_current == 3
    assert "6-K" in (result.detail or "")


def test_amended_foreign_forms_are_named_once() -> None:
    result = report(
        [
            event(
                accession="a",
                kind=EventKind.UNCLASSIFIED_SEC_FILING,
                item=None,
                form="6-K",
                event_id="a",
            ),
            event(
                accession="b",
                kind=EventKind.UNCLASSIFIED_SEC_FILING,
                item=None,
                form="6-K/A",
                event_id="b",
            ),
        ]
    )
    assert "6-K/A" not in (result.detail or "")


def test_classified_and_unclassified_together_is_partial() -> None:
    result = report(
        [
            event(),
            event(
                accession="6k",
                kind=EventKind.UNCLASSIFIED_SEC_FILING,
                item=None,
                form="6-K",
                event_id="u",
            ),
        ]
    )
    assert result.status is CoverageStatus.PARTIAL
    assert result.unclassified_current == 1


@pytest.mark.parametrize("asset_type", ["ETF", "FUND", "ETN"])
def test_a_fund_gets_no_company_research(asset_type: str) -> None:
    result = report([event()], asset_type=asset_type)
    assert result.status is CoverageStatus.NOT_APPLICABLE
    assert not result.has_developments


def test_a_listing_with_no_company_identity_is_not_guessed_at() -> None:
    assert report([event()], company_id=None).status is CoverageStatus.NO_COVERAGE


def test_an_unreadable_store_degrades_rather_than_raising() -> None:
    class Broken:
        """Stubs the method the service actually calls.

        An earlier version of this stub defined ``events_for_company``, which
        the service stopped calling when the read was bounded by the freshness
        window. The test still passed -- on the AttributeError from the missing
        method rather than on the failure it meant to exercise.
        """

        def recent_events(self, company_id: int, *, as_of: str, since: str) -> list[Any]:
            raise RuntimeError("disk I/O error")

        def has_company(self, company_id: int) -> bool:
            raise RuntimeError("disk I/O error")

    result = CurrentDevelopmentsService(store=Broken()).for_company(
        company_id=COMPANY, cik=CIK, as_of=AS_OF
    )
    assert result.status is CoverageStatus.UNAVAILABLE


# ---------------------------------------------------------------- identity
def test_cross_listed_issuers_share_one_event_history() -> None:
    """SAP's Frankfurt and US listings are one company, so one history."""
    store = store_with([event()])
    developments = CurrentDevelopmentsService(store=store)
    frankfurt = developments.for_company(company_id=COMPANY, cik=CIK, as_of=AS_OF)
    american = developments.for_company(company_id=COMPANY, cik=CIK, as_of=AS_OF)
    assert [d.accession for d in frankfurt.developments] == [
        d.accession for d in american.developments
    ]


def test_the_service_is_never_asked_by_ticker() -> None:
    source = Path("app/research_intelligence/developments.py").read_text()
    signature = source[source.index("def for_company(") : source.index("-> CurrentDevelopments")]
    assert "symbol" not in signature
    assert "ticker" not in signature
    assert "company_id" in signature


# --------------------------------------------------------------- citations
def test_a_filing_links_to_its_own_sec_index_page() -> None:
    url = filing_url(CIK, "0001045810-26-000073")
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/"
        "0001045810-26-000073-index.htm"
    )


def test_no_source_link_leaves_the_sec_archive() -> None:
    result = report([event()])
    for development in result.developments:
        assert development.source_url.startswith("https://www.sec.gov/Archives/edgar/data/")


def test_historical_evidence_is_never_established() -> None:
    result = report([event()], [fact()])
    assert result.historical_evidence is HistoricalEvidence.NOT_ESTABLISHED


# ----------------------------------------------------------------- render
def check(developments: Any = None, **kw: Any) -> Any:
    defaults: dict[str, Any] = {
        "requested": "NVDA",
        "symbol": "NVDA",
        "resolution": Resolution.SUPPORTED,
        "market_data": Availability.AVAILABLE,
        "fundamentals": Availability.AVAILABLE,
        "as_of": AS_OF,
        "checked_at": datetime(2026, 9, 1, tzinfo=UTC),
        "report": None,
        "developments": developments,
    }
    defaults.update(kw)
    from app.discord_bot.analysis import StockCheck

    return StockCheck(**defaults)


def rendered(developments: Any) -> str:
    return check_message(check(developments)).fields.get("Current developments", "")


def test_an_unwired_research_layer_shows_no_section_at_all() -> None:
    assert "Current developments" not in check_message(check(None)).fields


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (CoverageStatus.UNAVAILABLE, "research store has not been built"),
        (CoverageStatus.NO_COVERAGE, "not ingested"),
        (CoverageStatus.NO_CURRENT_EVENTS, "currency window"),
        (CoverageStatus.NOT_APPLICABLE, "fund"),
    ],
)
def test_each_kind_of_silence_reads_differently(result: CoverageStatus, expected: str) -> None:
    """A reader must be able to tell a gap in Tradabot from a quiet company."""
    text = rendered(
        report([], asset_type="ETF") if result is CoverageStatus.NOT_APPLICABLE else _empty(result)
    )
    assert expected in text


def _empty(status: CoverageStatus) -> Any:
    from app.research_intelligence.developments import CurrentDevelopments

    return CurrentDevelopments(status=status, as_of=AS_OF, detail="no filing")


def test_the_rendered_card_names_source_item_and_materiality() -> None:
    text = rendered(report([event()], [fact()]))
    assert "Earnings" in text
    assert "Item 2.02" in text
    assert "Materiality: Significant" in text
    assert "Revenue: $96.20B" in text
    assert "Quarter ended 26 Jul 2026" in text
    assert "[SEC filing](https://www.sec.gov/Archives/edgar/data/" in text


def test_the_card_states_once_that_no_price_evidence_backs_any_of_it() -> None:
    text = rendered(report([event(), event(accession="b", event_id="b")]))
    assert text.count("Historical price evidence: not established") == 1


def test_a_filing_without_a_safe_figure_says_which_it_is() -> None:
    text = rendered(report([event()]))
    assert "structured figures were not extracted safely" in text


def test_the_field_never_exceeds_the_discord_limit() -> None:
    many = [
        event(
            accession=f"a{i}",
            item="1.01",
            kind=EventKind.MATERIAL_AGREEMENT,
            published=f"2026-08-{10 + i:02d}T12:00:00Z",
            event_id=f"e{i}",
        )
        for i in range(6)
    ]
    facts = [fact(event_id=f"e{i}", metric=m) for i in range(6) for m in ("revenue", "net_income")]
    for field in check_message(check(report(many, facts))).fields.values():
        assert len(field) <= 1024


def test_dropping_for_length_is_announced_not_silent() -> None:
    from app.discord_bot.render import _within_limit

    blocks = ["x" * 400 for _ in range(4)]
    text = _within_limit(blocks, ["_footer._"])
    assert len(text) <= 1024
    assert "omitted for length" in text


# ------------------------------------------------------------- neutrality
def test_a_development_never_turns_the_card_green_or_red() -> None:
    good = presentation.COLOURS[presentation.Semantic.GOOD]
    bad = presentation.COLOURS[presentation.Semantic.BAD]
    for kind, materiality in (
        (EventKind.EARNINGS_RELEASE, Materiality.SIGNIFICANT),
        (EventKind.ACCOUNTING_RESTATEMENT, Materiality.CRITICAL),
        (EventKind.BANKRUPTCY_OR_RECEIVERSHIP, Materiality.CRITICAL),
        (EventKind.M_AND_A, Materiality.SIGNIFICANT),
    ):
        colour = check_message(check(report([event(kind=kind, materiality=materiality)]))).colour
        assert colour not in (good, bad), kind


def test_a_critical_filing_reads_as_unusual_not_as_a_verdict() -> None:
    critical = report(
        [
            event(
                kind=EventKind.ACCOUNTING_RESTATEMENT, item="4.02", materiality=Materiality.CRITICAL
            )
        ]
    )
    assert (
        check_message(check(critical)).colour == presentation.COLOURS[presentation.Semantic.UNUSUAL]
    )


def test_an_ordinary_earnings_release_does_not_colour_the_card() -> None:
    """SIGNIFICANT covers every earnings release; colouring on it would turn
    the card orange four times a year and mean nothing."""
    ordinary = check_message(check(report([event()]))).colour
    assert ordinary == check_message(check(None)).colour


BANNED = (
    "buy",
    "sell",
    "hold",
    "bullish",
    "bearish",
    "upside",
    "downside",
    "price target",
    "expected return",
    "recommend",
    "catalyst",
    "outperform",
    "undervalued",
    "overvalued",
    "forecast",
    "should ",
    "will rise",
    "will fall",
)


def test_no_user_visible_string_uses_recommendation_vocabulary() -> None:
    texts = [
        rendered(report([event()], [fact()])),
        rendered(report([])),
        rendered(report(None)),
        rendered(report([event(kind=EventKind.UNCLASSIFIED_SEC_FILING, item=None, form="6-K")])),
        rendered(report([event()], asset_type="ETF")),
    ]
    for text in texts:
        lowered = text.lower()
        for word in BANNED:
            assert word not in lowered, f"{word!r} in {text!r}"


def test_no_directional_word_appears_in_the_label_vocabulary() -> None:
    from app.research_intelligence.developments import KIND_LABELS, METRIC_LABELS

    vocabulary = " ".join([*KIND_LABELS.values(), *METRIC_LABELS.values()]).lower()
    for word in ("positive", "negative", "strong", "weak", "good", "bad", "risk"):
        assert word not in vocabulary


# -------------------------------------------------------------- boundaries
def test_the_service_makes_no_network_request() -> None:
    """``/check`` reads only the local store. SEC being slow cannot delay a card."""
    source = Path("app/research_intelligence/developments.py").read_text()
    for token in (
        "urlopen",
        "requests.",
        "httpx",
        "urllib",
        "EdgarClient",
        "archive_document",
        "submissions(",
    ):
        assert token not in source, f"developments.py references {token}"


def test_the_service_holds_no_discord_object() -> None:
    """The report is reusable by monitoring, the newsletter and a web view.

    Docstrings are stripped before the scan. The prose names Discord as the
    first consumer, which is documentation; the gate is about what the *code*
    touches, and a substring search over comments catches vocabulary rather
    than dependency -- the same mistake two earlier gates in this project made.
    """
    import ast

    tree = ast.parse(Path("app/research_intelligence/developments.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    code = ast.unparse(tree).lower()
    for token in ("discord", "embed", "notificationmessage", "check_message"):
        assert token not in code


def test_no_trading_path_is_reachable_from_the_research_read_model() -> None:
    import ast

    source = Path("app/research_intelligence/developments.py").read_text()
    forbidden = (
        "app.broker",
        "app.paper",
        "app.strategy",
        "app.discord_bot",
        "alpaca",
        "openai",
        "anthropic",
    )
    for node in ast.walk(ast.parse(source)):
        names = (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module]
            if isinstance(node, ast.ImportFrom) and node.module
            else []
        )
        for name in names:
            assert not any(name.startswith(f) for f in forbidden), name


def test_the_selection_budget_is_declared_once_and_not_per_company() -> None:
    source = Path("app/research_intelligence/developments.py").read_text()
    assert re.search(r"^MAX_CURRENT_DEVELOPMENTS: Final = 3$", source, re.M)
    assert source.count("MAX_CURRENT_DEVELOPMENTS") <= 3


def test_an_old_history_is_coverage_not_absence() -> None:
    """The bounded query returns nothing; the company is still covered.

    Reading only the last two years keeps a 904-event filer cheap, but an empty
    window and an uningested company produce the same empty list and mean
    opposite things.
    """
    result = report([event(published="2020-01-05T12:00:00Z")])
    assert result.status is CoverageStatus.NO_CURRENT_EVENTS
    assert result.status is not CoverageStatus.NO_COVERAGE


def test_the_query_window_covers_the_longest_freshness_rule() -> None:
    """No kind may outlive the bound the store is asked with."""
    from app.research_intelligence.freshness import FRESHNESS_DAYS, MAX_WINDOW_DAYS

    assert max(FRESHNESS_DAYS.values()) <= MAX_WINDOW_DAYS
    longest = max(FRESHNESS_DAYS, key=lambda k: FRESHNESS_DAYS[k])
    edge = event(
        kind=longest,
        item="4.02",
        materiality=Materiality.CRITICAL,
        published="2025-01-05T12:00:00Z",
    )
    assert report([edge], as_of="2026-09-01").has_developments
