"""Turning one filing into the events its metadata establishes.

No document is read. Classification uses the form and the item codes SEC
publishes in the submissions payload, mapped through
:mod:`app.research_intelligence.taxonomy`. That is the entire mechanism, and it
is why extraction confidence is HIGH: the registrant asserted the category by
filing under it, so there is no reading step that could be wrong.

Multi-item filings become multiple events
-----------------------------------------
An 8-K carrying Items 1.01 and 5.02 reports two unrelated occurrences. They are
emitted as two events sharing one accession, each with its own
``classifying_item`` and each carrying the whole ``item_codes`` tuple so the
grouping survives. The alternative -- one event with a list of kinds -- would
force a single materiality band and a single freshness window onto a material
agreement and a change of officers, and would leave ``events_by_kind`` unable
to answer its own question. Idempotency survives because identity includes the
item, so re-ingesting the filing regenerates exactly the same two identities.

Evidence is a pointer
---------------------
Each event references the filing's primary document by URL. The document itself
is not stored: SEC keeps it, the URL fetches it, and a content hash is recorded
only when a document was actually retrieved. Mirroring filings locally would
add gigabytes for a corpus already hosted and versioned by the regulator.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from app.research_intelligence.schemas import (
    Confidence,
    EventKind,
    EventScope,
    EvidenceReference,
    HistoricalEvidence,
    Materiality,
    MaterialityContext,
    ResearchEvent,
    SourceQuality,
    SourceType,
)
from app.research_intelligence.sec import FilingRecord
from app.research_intelligence.taxonomy import (
    is_administrative,
    kind_for,
    kind_for_form,
    materiality_for,
    summarise,
)

ITEM_CODE_FORMS: frozenset[str] = frozenset({"8-K"})
"""Forms whose metadata carries classifiable item codes.

Measured in phase 14.0 across SAP (396 filings), Novo Nordisk (904) and Royal
Bank of Canada (22): foreign private issuers file 6-K, 20-F and 40-F, and
**not one of those 1,322 filings carried an item code**. The boundary is a
property of the forms, not of the companies, so it is expressed as a set of
forms rather than a nationality test."""


def event_id(cik: str, accession: str, kind: EventKind, item: str | None) -> str:
    """A stable identity for one classified occurrence.

    Derived from primary-source identifiers only -- the registrant, the
    accession and the item that classified it -- so the same filing ingested
    twice yields the same identity and the store's primary key absorbs the
    second write. Nothing time-varying enters it: including ``fetched_at``
    would make every re-ingestion a new event, which is precisely the
    duplication the identity exists to prevent.
    """
    basis = f"{str(cik).zfill(10)}|{accession}|{kind}|{item or ''}"
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def source_hash(record: FilingRecord) -> str:
    """A fingerprint of the filing's own metadata.

    Distinct from a document hash: this covers what SEC said about the filing,
    so a corrected item list or a re-stated acceptance time is detectable even
    when no document was downloaded.
    """
    basis = "|".join(
        [
            record.cik,
            record.accession,
            record.form,
            record.filing_date,
            record.acceptance or "",
            record.report_date or "",
            ",".join(record.items),
            record.primary_document,
        ]
    )
    return hashlib.sha256(basis.encode()).hexdigest()


def _evidence(record: FilingRecord) -> tuple[EvidenceReference, ...]:
    if not record.primary_document:
        return ()
    return (
        EvidenceReference(
            document=record.primary_document,
            url=record.archive_url(record.primary_document),
            role="PRIMARY",
            byte_size=record.size or None,
        ),
    )


def classify(record: FilingRecord) -> list[tuple[EventKind, str | None]]:
    """The event kinds this filing establishes, with the item that established each.

    An empty list means the filing produced only administrative items and is
    not an occurrence at the company.
    """
    if record.base_form not in ITEM_CODE_FORMS or not record.items:
        # No item codes. Some forms still establish their own event -- a 10-Q
        # is the quarterly report, and saying so claims nothing extra. A 6-K
        # does not: it is the catch-all report of a foreign private issuer,
        # and what it contains is unconstrained by the form.
        by_form = kind_for_form(record.base_form)
        return [(by_form or EventKind.UNCLASSIFIED_SEC_FILING, None)]

    found: list[tuple[EventKind, str | None]] = []
    unmapped: list[str] = []
    for item in record.items:
        if is_administrative(item):
            continue
        kind = kind_for(item)
        if kind is None:
            unmapped.append(item)
            continue
        found.append((kind, item))
    if found:
        return found
    if unmapped:
        # Real disclosures whose subject the form deliberately leaves open --
        # Regulation FD and "Other Events" chief among them. Recorded, not guessed.
        return [(EventKind.UNCLASSIFIED_SEC_FILING, unmapped[0])]
    return []


def build(
    record: FilingRecord,
    *,
    company_id: int,
    company_key: str,
    fetched_at: str,
) -> list[ResearchEvent]:
    """Every event one filing establishes. Deterministic for a given record."""
    events: list[ResearchEvent] = []
    evidence = _evidence(record)
    digest = source_hash(record)
    for kind, item in classify(record):
        events.append(
            ResearchEvent(
                event_id=event_id(record.cik, record.accession, kind, item),
                company_id=company_id,
                company_key=company_key,
                cik=record.cik,
                scope=EventScope.COMPANY,
                event_kind=kind,
                published_at=record.published_at,
                fetched_at=fetched_at,
                occurred_at=record.report_date,
                source_type=SourceType.REGULATOR,
                source_quality=SourceQuality.PRIMARY,
                source_url=record.archive_url(record.primary_document or None),
                source_document_id=record.accession,
                source_hash=digest,
                form=record.form,
                accession=record.accession,
                item_codes=record.items,
                classifying_item=item,
                title=record.primary_description or None,
                fact_summary=summarise(kind, item, record.form),
                evidence=evidence,
                materiality=materiality_for(kind, record.base_form),
                materiality_context=_context_for(kind),
                source_confidence=Confidence.HIGH,
                extraction_confidence=(
                    Confidence.LOW if kind is EventKind.UNCLASSIFIED_SEC_FILING else Confidence.HIGH
                ),
                historical_evidence=HistoricalEvidence.NOT_ESTABLISHED,
                amends_accession=record.accession if record.is_amendment else None,
            )
        )
    return events


def _context_for(kind: EventKind) -> MaterialityContext:
    """Why no magnitude ratio accompanies this event.

    Always an absence in this phase, and the reason differs by kind. A
    management change has no magnitude to put in proportion; an impairment or a
    debt event does, but the amount lives in the document's prose and reading
    it out is the semantic extraction this phase excludes. A number lifted from
    filing text is not the event's magnitude merely because it is the largest
    figure on the page.
    """
    with_amounts = {
        EventKind.IMPAIRMENT,
        EventKind.DEBT_EVENT,
        EventKind.M_AND_A,
        EventKind.EXIT_OR_DISPOSAL_COSTS,
        EventKind.UNREGISTERED_EQUITY_SALE,
    }
    if kind in with_amounts:
        return MaterialityContext.NO_ESTABLISHED_AMOUNT
    return MaterialityContext.NOT_APPLICABLE


def direction_free(events: Iterable[ResearchEvent]) -> bool:
    """Whether no event carries a directional claim. Used by tests and audits."""
    banned = ("bullish", "bearish", "positive for", "negative for", "buy", "sell")
    return not any(
        word in (event.fact_summary or "").lower() for event in events for word in banned
    )


__all__ = [
    "ITEM_CODE_FORMS",
    "Materiality",
    "build",
    "classify",
    "direction_free",
    "event_id",
    "source_hash",
]
