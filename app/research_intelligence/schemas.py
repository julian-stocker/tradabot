"""What a research event is, and what Phase 15.0 refuses to claim about one.

A :class:`ResearchEvent` says *the primary source establishes that this
happened*. That is a narrower statement than it looks, and the narrowness is
the design:

* It is not a claim about what the event means for the business. SEC Item 1.01
  establishes that a material definitive agreement was entered into. It does
  not establish that a company won a major customer contract, and this phase
  never upgrades the first into the second.
* It is not a claim about direction. Materiality is how much attention the
  event warrants, never whether it is good or bad. There is no field in which
  a direction could be recorded.
* It is not a claim about consequence. ``historical_evidence`` is
  ``NOT_ESTABLISHED`` on every event this phase produces, and it is an enum
  member rather than a nullable string so the absence is structural.

Relationship to ``NEW_SEC_FILING``
----------------------------------
:mod:`app.monitoring` already emits ``NEW_SEC_FILING``, which means *an
accession appeared*. That is a different and smaller statement, and the two are
deliberately not merged: one is a transition in a monitored baseline, the other
is a classified occurrence with provenance. Monitoring answers "what changed
since the last run"; this answers "what does the filing establish".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventScope(StrEnum):
    """Whether the event is true of the issuer or of one traded line.

    Every event this phase produces is ``COMPANY``. SEC filing *metadata* names
    a registrant, never a venue -- even Item 3.01, which is about a listing
    standard, does not say which listing in the structured fields. Inventing a
    listing scope from a company-level source is exactly the borrowing Phase 13
    exists to prevent, so ``LISTING`` is defined and currently unreachable.
    """

    COMPANY = "COMPANY"
    LISTING = "LISTING"


class EventKind(StrEnum):
    """What the filing establishes, from SEC form and item semantics alone.

    Each member maps to one or more documented 8-K item codes whose official
    title names the event. Nothing here is inferred from filing text.
    """

    EARNINGS_RELEASE = "EARNINGS_RELEASE"
    """Item 2.02, whose title is *Results of Operations and Financial
    Condition*. Overwhelmingly an earnings release in practice, and the item
    admits any disclosure of results -- so the name is the common reading while
    ``fact_summary`` carries the item's own wording."""
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    """Item 5.02, which covers a chief executive resigning and a director being
    elected at the annual meeting with equal standing. Metadata cannot separate
    them, so neither the kind nor the materiality distinguishes seniority."""
    M_AND_A = "M_AND_A"
    """Item 2.01, *Completion of Acquisition or Disposition of Assets*. Two
    things the label understates: it is **completion**, not announcement, and it
    includes **disposals** as well as acquisitions. A sold division files here
    exactly as a bought one does."""
    MATERIAL_AGREEMENT = "MATERIAL_AGREEMENT"
    """Items 1.01 and 1.02 -- entry into, and termination of. Which one is
    carried by ``classifying_item`` and stated in ``fact_summary``; the kind
    does not distinguish them because both are material-agreement events."""
    DEBT_EVENT = "DEBT_EVENT"
    """Items 2.03 (creation of a direct financial obligation) and 2.04
    (triggering events that accelerate or increase one). Deliberately vaguer
    than either item rather than narrower than both."""
    ACCOUNTING_RESTATEMENT = "ACCOUNTING_RESTATEMENT"
    """Item 4.02, whose title is *Non-Reliance on Previously Issued Financial
    Statements*.

    The name is the industry term and the item is narrower than it: the
    registrant has stated that prior financials should no longer be relied
    upon. A restatement is the usual consequence and is not itself established
    by the filing, so ``fact_summary`` quotes the item title rather than the
    kind name. Read the kind as a label, the summary as the claim."""
    AUDITOR_CHANGE = "AUDITOR_CHANGE"
    BANKRUPTCY_OR_RECEIVERSHIP = "BANKRUPTCY_OR_RECEIVERSHIP"
    IMPAIRMENT = "IMPAIRMENT"
    EXIT_OR_DISPOSAL_COSTS = "EXIT_OR_DISPOSAL_COSTS"
    LISTING_RULE_MATTER = "LISTING_RULE_MATTER"
    CONTROL_CHANGE = "CONTROL_CHANGE"
    CYBERSECURITY_INCIDENT = "CYBERSECURITY_INCIDENT"
    UNREGISTERED_EQUITY_SALE = "UNREGISTERED_EQUITY_SALE"
    PERIODIC_REPORT = "PERIODIC_REPORT"
    """A scheduled financial report -- 10-K, 10-Q, 20-F, 40-F.

    Classified from the form alone, which is enough: the form *is* the
    statement that a periodic report was filed for a period. No item codes are
    involved and none are needed. Kept apart from
    ``UNCLASSIFIED_SEC_FILING`` because lumping them together would hide a real
    distinction behind an honest-sounding label -- a 10-Q is a known kind of
    filing, a 6-K is a catch-all whose contents the form does not constrain."""
    UNCLASSIFIED_SEC_FILING = "UNCLASSIFIED_SEC_FILING"
    """A filing Tradabot holds and cannot classify from metadata.

    The honest state for every foreign private issuer's 6-K, 20-F and 40-F --
    measured across SAP, Novo Nordisk and Royal Bank of Canada, 1,322 such
    filings carry **zero** item codes. Recording them unclassified keeps the
    coverage boundary visible; guessing that a 6-K "usually contains earnings"
    would put a semantic claim behind a metadata-only pipeline."""


class SourceType(StrEnum):
    REGULATOR = "REGULATOR"
    ISSUER = "ISSUER"
    EXCHANGE = "EXCHANGE"
    NEWS_AGENCY = "NEWS_AGENCY"
    PUBLICATION = "PUBLICATION"


class SourceQuality(StrEnum):
    PRIMARY = "PRIMARY"
    HIGH_SECONDARY = "HIGH_SECONDARY"
    SECONDARY = "SECONDARY"


class Confidence(StrEnum):
    """Kept separate on purpose -- they fail independently."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class HistoricalEvidence(StrEnum):
    """Whether Tradabot has measured what comparable events did.

    ``NOT_ESTABLISHED`` is the only value this phase can produce, and the enum
    exists so that stays true by construction rather than by discipline. An
    event study over these event kinds does not exist; the scanner's
    ``signal_outcomes`` are a different event definition entirely and must not
    be borrowed as evidence for SEC-event effects.
    """

    NOT_ESTABLISHED = "NOT_ESTABLISHED"
    MEASURED = "MEASURED"


class MaterialityContext(StrEnum):
    """Why an event's magnitude was or was not put in proportion."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    """The event kind carries no numeric magnitude to contextualise."""
    NO_ESTABLISHED_AMOUNT = "NO_ESTABLISHED_AMOUNT"
    """The default in this phase. Filing *metadata* never carries an amount,
    and reading one out of the document text would be the semantic extraction
    this phase excludes. A dollar figure scraped from prose is not the event's
    magnitude just because it is the largest number on the page."""
    UNAVAILABLE_CURRENCY_MISMATCH = "UNAVAILABLE_CURRENCY_MISMATCH"
    """An amount exists but the company reports in another currency, and
    Tradabot performs no conversion -- the Phase 13 rule, unchanged."""
    UNAVAILABLE_NO_FUNDAMENTALS = "UNAVAILABLE_NO_FUNDAMENTALS"
    COMPUTED = "COMPUTED"


class Materiality(StrEnum):
    """How much attention the event warrants. **Never a direction.**

    Deliberately the same four bands :mod:`app.monitoring.schemas` already
    uses, so one vocabulary describes attention across the whole system. There
    is no GOOD, BAD, BULLISH or BEARISH member and no field to put one in: a
    restatement is CRITICAL because the form's own semantics say the previously
    issued statements cannot be relied upon, not because it is bad news.
    """

    ROUTINE = "ROUTINE"
    NOTABLE = "NOTABLE"
    SIGNIFICANT = "SIGNIFICANT"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """Where in the primary source the event can be checked.

    A pointer, not a copy. The document stays at SEC; what is retained is
    enough to fetch it again and to prove the retained copy would be the same
    one -- URL, the document's role in the filing, and a content hash.
    """

    document: str
    """Filename within the accession, e.g. ``nvda-20260826.htm``."""
    url: str
    role: str
    """``PRIMARY`` for the filing document, or the exhibit type where known."""
    content_sha256: str | None = None
    byte_size: int | None = None
    text_start: int | None = None
    """Character offset into the *normalised* text -- see
    :mod:`app.research_intelligence.content`, whose normalisation is fixed and
    version-stamped precisely so these offsets keep meaning the same words."""
    text_end: int | None = None
    evidence_text: str | None = None
    """The cited sentence, stored verbatim.

    Kept as well as the offsets, not instead of them: the offsets locate the
    claim in the document, and the text lets a reader check it without
    refetching. If SEC ever changed the document, the mismatch is visible
    rather than silent."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "document": self.document,
            "url": self.url,
            "role": self.role,
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
            "text_start": self.text_start,
            "text_end": self.text_end,
            "evidence_text": self.evidence_text,
        }


@dataclass(frozen=True, slots=True)
class ResearchEvent:
    """One classified occurrence, traceable to the document that establishes it."""

    event_id: str
    company_id: int
    company_key: str
    cik: str
    scope: EventScope
    event_kind: EventKind

    published_at: str
    """When the source made it public. SEC ``acceptanceDateTime``, to the
    second. Never the filing date, which is a calendar day."""
    fetched_at: str
    """When Tradabot retrieved it. Never confused with either of the others:
    it says when we learned, not when it happened or when it was public."""
    occurred_at: str | None = None
    """When the event happened, when the source says so. SEC ``reportDate`` --
    the 8-K cover page's "date of earliest event reported". ``None`` when the
    filing carries none, and never invented from the filing date."""

    source_type: SourceType = SourceType.REGULATOR
    source_quality: SourceQuality = SourceQuality.PRIMARY
    source_url: str = ""
    source_document_id: str = ""
    source_hash: str = ""

    form: str = ""
    accession: str = ""
    item_codes: tuple[str, ...] = ()
    """Every item code on the filing, not only the one that produced this
    event. Kept whole so the classification can always be re-derived."""
    classifying_item: str | None = None
    """The single item code this event was classified from."""

    title: str | None = None
    fact_summary: str = ""
    evidence: tuple[EvidenceReference, ...] = ()

    materiality: Materiality = Materiality.ROUTINE
    materiality_context: MaterialityContext = MaterialityContext.NOT_APPLICABLE
    materiality_detail: str | None = None

    source_confidence: Confidence = Confidence.HIGH
    extraction_confidence: Confidence = Confidence.HIGH

    interpretation: None = None
    """Structurally absent in Phase 15.0. Typed ``None`` rather than
    ``str | None`` so that writing one is a type error, not a judgement call."""
    historical_evidence: HistoricalEvidence = HistoricalEvidence.NOT_ESTABLISHED

    supersedes_event_id: str | None = None
    amends_accession: str | None = None
    """The accession this filing amends, when the form marks it an amendment.
    Recorded separately from ``supersedes_event_id`` because knowing a filing
    is an amendment is not the same as knowing which event it supersedes."""

    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "company_id": self.company_id,
            "company_key": self.company_key,
            "cik": self.cik,
            "scope": str(self.scope),
            "event_kind": str(self.event_kind),
            "occurred_at": self.occurred_at,
            "published_at": self.published_at,
            "fetched_at": self.fetched_at,
            "source_type": str(self.source_type),
            "source_quality": str(self.source_quality),
            "source_url": self.source_url,
            "source_document_id": self.source_document_id,
            "source_hash": self.source_hash,
            "form": self.form,
            "accession": self.accession,
            "item_codes": list(self.item_codes),
            "classifying_item": self.classifying_item,
            "title": self.title,
            "fact_summary": self.fact_summary,
            "evidence": [e.as_dict() for e in self.evidence],
            "materiality": str(self.materiality),
            "materiality_context": str(self.materiality_context),
            "materiality_detail": self.materiality_detail,
            "source_confidence": str(self.source_confidence),
            "extraction_confidence": str(self.extraction_confidence),
            "interpretation": None,
            "historical_evidence": str(self.historical_evidence),
            "supersedes_event_id": self.supersedes_event_id,
            "amends_accession": self.amends_accession,
        }


@dataclass(frozen=True, slots=True)
class QuarantinedFiling:
    """A filing that could not be attached to a company, kept rather than dropped.

    Phase 13 established that guessing an identity produces a confident report
    about the wrong company. The same rule applies on ingestion: a CIK that
    does not map to exactly one known company is quarantined with the reason,
    never attached to the nearest match.
    """

    cik: str
    accession: str
    form: str
    reason: str
    fetched_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "cik": self.cik,
            "accession": self.accession,
            "form": self.form,
            "reason": self.reason,
            "fetched_at": self.fetched_at,
        }


class DocumentRole(StrEnum):
    """What a document is within its filing, from the SEC exhibit type."""

    PRIMARY = "PRIMARY"
    EXHIBIT = "EXHIBIT"
    XBRL = "XBRL"
    GRAPHIC = "GRAPHIC"
    OTHER = "OTHER"


class EvidenceStatus(StrEnum):
    """Why evidence was or was not obtained. Specific, never "insufficient data"."""

    OK = "OK"
    NO_RELEVANT_EXHIBIT = "NO_RELEVANT_EXHIBIT"
    AMBIGUOUS_DOCUMENT = "AMBIGUOUS_DOCUMENT"
    """Several documents could support the event and metadata does not say
    which. Refused rather than picked -- the same rule identity resolution
    applies to companies."""
    UNSUPPORTED_CONTENT_TYPE = "UNSUPPORTED_CONTENT_TYPE"
    CONTENT_FETCH_FAILED = "CONTENT_FETCH_FAILED"
    CHANGED_SOURCE_CONTENT = "CHANGED_SOURCE_CONTENT"
    """The URL returned different bytes than last time. SEC archive documents
    are expected to be immutable; when one is not, the prior provenance is kept
    and the change is recorded rather than overwritten."""


class FactStatus(StrEnum):
    """Why a numeric fact was or was not emitted."""

    OK = "OK"
    NO_STRUCTURED_FACT = "NO_STRUCTURED_FACT"
    AMBIGUOUS_METRIC = "AMBIGUOUS_METRIC"
    """More than one metric label, or a GAAP/non-GAAP pair, in the same
    sentence. "earnings per diluted share were $2.46 and $2.22, respectively"
    names two bases and two values; choosing one would be a coin toss."""
    AMBIGUOUS_PERIOD = "AMBIGUOUS_PERIOD"
    """No explicit period, or more than one. A press-release table carrying
    "Three Months Ended" beside "Six Months Ended" is the canonical case."""
    AMBIGUOUS_UNIT = "AMBIGUOUS_UNIT"
    AMBIGUOUS_VALUE = "AMBIGUOUS_VALUE"
    UNKNOWN_CURRENCY = "UNKNOWN_CURRENCY"
    NON_GAAP_BASIS = "NON_GAAP_BASIS"
    """The sentence reports a non-GAAP measure. Tradabot's canonical history is
    as-reported GAAP, so a non-GAAP figure is not comparable to it and is not
    emitted rather than being silently mixed in."""


class ContextStatus(StrEnum):
    """Why a magnitude was or was not put in proportion."""

    COMPUTED = "COMPUTED"
    NO_ESTABLISHED_AMOUNT = "NO_ESTABLISHED_AMOUNT"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    UNKNOWN_CURRENCY = "UNKNOWN_CURRENCY"
    NO_PIT_COMPARATOR = "NO_PIT_COMPARATOR"
    """No canonical figure was on file as of the event's publication. Using a
    later filing's number would answer the question with information nobody
    had at the time."""
    INCOMPATIBLE_PERIOD = "INCOMPATIBLE_PERIOD"


class FiscalPeriod(StrEnum):
    """The period shape a figure describes. Never inferred from position."""

    QUARTER = "QUARTER"
    YEAR = "YEAR"
    YEAR_TO_DATE = "YEAR_TO_DATE"
    TRAILING_TWELVE_MONTHS = "TRAILING_TWELVE_MONTHS"
    INSTANT = "INSTANT"


UNKNOWN_CURRENCY = "UNKNOWN"
"""Currency when the document does not establish one. Never defaulted to USD
merely because the filing is with the SEC -- foreign private issuers report in
their own currency and file here."""


@dataclass(frozen=True, slots=True)
class ResearchDocument:
    """One SEC-hosted document, described rather than mirrored.

    Separate from :class:`ResearchEvent` because the relationship is many to
    many: one 8-K's press release supports every event that filing produced,
    and one event may cite the primary document and an exhibit. Making the
    exhibit an event would have duplicated the filing once per attachment.
    """

    document_id: str
    company_id: int
    cik: str
    accession: str
    document_type: str
    """The SEC exhibit type verbatim -- ``EX-99.1``, ``8-K``, ``EX-101.SCH``."""
    role: DocumentRole
    filename: str
    sequence: int
    description: str | None
    source_url: str
    published_at: str
    fetched_at: str | None = None
    content_type: str | None = None
    content_hash: str | None = None
    text_length: int | None = None
    raw_size: int | None = None
    status: EvidenceStatus = EvidenceStatus.OK
    extraction_version: str = ""

    @property
    def retrieved(self) -> bool:
        return self.content_hash is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "company_id": self.company_id,
            "cik": self.cik,
            "accession": self.accession,
            "document_type": self.document_type,
            "role": str(self.role),
            "filename": self.filename,
            "sequence": self.sequence,
            "description": self.description,
            "source_url": self.source_url,
            "published_at": self.published_at,
            "fetched_at": self.fetched_at,
            "content_type": self.content_type,
            "content_hash": self.content_hash,
            "text_length": self.text_length,
            "raw_size": self.raw_size,
            "status": str(self.status),
            "extraction_version": self.extraction_version,
        }


@dataclass(frozen=True, slots=True)
class ResearchFact:
    """One figure a disclosure document explicitly stated.

    **Not a canonical fundamental.** :class:`~app.advisor.facts.FactStore`
    remains the authority on what a company's financial history is; this
    records what one document said on one day, which is a different question
    and answerable from the document alone. Nothing here is written back into
    canonical fundamentals.
    """

    fact_id: str
    event_id: str
    company_id: int
    metric: str
    value: float
    unit: str
    currency: str = UNKNOWN_CURRENCY
    fiscal_period: FiscalPeriod | None = None
    period_start: str | None = None
    period_end: str | None = None
    instant: str | None = None
    basis: str = "GAAP"
    document_id: str = ""
    evidence: EvidenceReference | None = None
    extraction_method: str = ""
    extraction_confidence: Confidence = Confidence.HIGH
    extraction_version: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "event_id": self.event_id,
            "company_id": self.company_id,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "currency": self.currency,
            "fiscal_period": str(self.fiscal_period) if self.fiscal_period else None,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "instant": self.instant,
            "basis": self.basis,
            "document_id": self.document_id,
            "evidence": self.evidence.as_dict() if self.evidence else None,
            "extraction_method": self.extraction_method,
            "extraction_confidence": str(self.extraction_confidence),
            "extraction_version": self.extraction_version,
        }


@dataclass(frozen=True, slots=True)
class MagnitudeContext:
    """A figure placed against a canonical comparator, or the reason it was not."""

    status: ContextStatus
    metric: str | None = None
    comparator: str | None = None
    comparator_value: float | None = None
    ratio: float | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "metric": self.metric,
            "comparator": self.comparator,
            "comparator_value": self.comparator_value,
            "ratio": self.ratio,
            "detail": self.detail,
        }
