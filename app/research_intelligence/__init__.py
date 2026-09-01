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

from app.research_intelligence.extraction import build, classify, event_id, source_hash
from app.research_intelligence.freshness import is_current, window_days
from app.research_intelligence.identity import CompanyResolver
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
from app.research_intelligence.sec import (
    FilingRecord,
    SecFilingSource,
    SubmissionsPayload,
    parse_submissions,
)
from app.research_intelligence.service import (
    CLASSIFIABLE_FORMS,
    DEFAULT_FORMS,
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
    "ITEM_KINDS",
    "ITEM_TITLES",
    "CompanyResolver",
    "Confidence",
    "EventKind",
    "EventScope",
    "EventStore",
    "EvidenceReference",
    "FilingRecord",
    "HistoricalEvidence",
    "IngestionReport",
    "Materiality",
    "MaterialityContext",
    "QuarantinedFiling",
    "ResearchEvent",
    "ResearchIngestionService",
    "SecFilingSource",
    "SourceQuality",
    "SourceType",
    "SubmissionsPayload",
    "build",
    "classify",
    "event_id",
    "is_current",
    "kind_for",
    "materiality_for",
    "parse_submissions",
    "source_hash",
    "summarise",
    "title_for",
    "window_days",
]
