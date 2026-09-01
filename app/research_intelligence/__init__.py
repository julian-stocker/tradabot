"""Primary-source SEC event intelligence.

Establishes what a company filing reports, from SEC form and item semantics
alone. No language model, no news provider, no web search, no sentiment, and no
claim about what an event means for a share price.

Deliberately depends on nothing that would tie it to one consumer: no broker,
no paper trading, no Discord, no publishing, no notifications, no LLM provider.
A structural test asserts the boundary. Monitoring, ``/check``, a weekly digest
and a future synthesis layer are all downstream of this, and none of them is
imported here.
"""

from app.research_intelligence.content import content_hash, to_text
from app.research_intelligence.context import magnitude
from app.research_intelligence.documents import (
    EXTRACTION_VERSION,
    citable,
    parse_manifest,
    select_documents,
)
from app.research_intelligence.extraction import build, classify, event_id, source_hash
from app.research_intelligence.facts import (
    V1_METRICS,
    ExtractionOutcome,
    FactRefusal,
    extract,
    fact_id,
)
from app.research_intelligence.freshness import is_current, window_days
from app.research_intelligence.identity import CompanyResolver
from app.research_intelligence.schemas import (
    Confidence,
    ContextStatus,
    DocumentRole,
    EventKind,
    EventScope,
    EvidenceReference,
    EvidenceStatus,
    FactStatus,
    FiscalPeriod,
    HistoricalEvidence,
    MagnitudeContext,
    Materiality,
    MaterialityContext,
    QuarantinedFiling,
    ResearchDocument,
    ResearchEvent,
    ResearchFact,
    SourceQuality,
    SourceType,
)
from app.research_intelligence.sec import (
    FilingRecord,
    SecFilingSource,
    SubmissionsPayload,
    parse_submissions,
)
from app.research_intelligence.service import (
    CLASSIFIABLE_FORMS,
    DEFAULT_FORMS,
    EvidenceReport,
    EvidenceService,
    IngestionReport,
    ResearchIngestionService,
)
from app.research_intelligence.store import EventStore
from app.research_intelligence.taxonomy import (
    ITEM_KINDS,
    ITEM_TITLES,
    kind_for,
    materiality_for,
    summarise,
    title_for,
)

__all__ = [
    "CLASSIFIABLE_FORMS",
    "DEFAULT_FORMS",
    "EXTRACTION_VERSION",
    "ITEM_KINDS",
    "ITEM_TITLES",
    "V1_METRICS",
    "CompanyResolver",
    "Confidence",
    "ContextStatus",
    "DocumentRole",
    "EventKind",
    "EventScope",
    "EventStore",
    "EvidenceReference",
    "EvidenceReport",
    "EvidenceService",
    "EvidenceStatus",
    "ExtractionOutcome",
    "FactRefusal",
    "FactStatus",
    "FilingRecord",
    "FiscalPeriod",
    "HistoricalEvidence",
    "IngestionReport",
    "MagnitudeContext",
    "Materiality",
    "MaterialityContext",
    "QuarantinedFiling",
    "ResearchDocument",
    "ResearchEvent",
    "ResearchFact",
    "ResearchIngestionService",
    "SecFilingSource",
    "SourceQuality",
    "SourceType",
    "SubmissionsPayload",
    "build",
    "citable",
    "classify",
    "content_hash",
    "event_id",
    "extract",
    "fact_id",
    "is_current",
    "kind_for",
    "magnitude",
    "materiality_for",
    "parse_manifest",
    "parse_submissions",
    "select_documents",
    "source_hash",
    "summarise",
    "title_for",
    "to_text",
    "window_days",
]
