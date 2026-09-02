"""Bounded research synthesis over verified evidence.

Defines what evidence a synthesis may see, what it may say, how that is checked,
and what happens when no model answers at all.

Phase 18.1 adds one provider adapter, a cost guard, a ledger, a cache and a
frozen pilot cohort. It does not add a dependency: the SDK is imported inside
:mod:`app.synthesis.openai_provider` at the point of use, so this package
imports, type-checks and tests on a machine with no model SDK and no API key.
Nothing here is wired into ``/check``, Discord, the screener or any scheduled
job -- the pilot is invoked by hand, one call at a time by default.
"""

from app.synthesis.brief import ResearchBriefBuilder, as_text, build_brief
from app.synthesis.budget import (
    BUDGET_CURRENCY,
    DEFAULT_PER_RUN_CALLS,
    MAX_PER_RUN_CALLS,
    MONTHLY_CAP_USD,
    BudgetDecision,
    BudgetVerdict,
    CostEstimate,
    CostGuard,
    current_month,
)
from app.synthesis.cache import CacheKey, SynthesisCache, config_hash, key_for
from app.synthesis.contract import (
    FORBIDDEN_PATTERNS,
    FORBIDDEN_TERMS,
    MAX_CLAIMS,
    SYNTHESIS_SCHEMA_VERSION,
    ClaimType,
    ModelMetadata,
    ResearchSynthesis,
    SynthesisClaim,
    SynthesisConfidence,
    ValidatedResearchSynthesis,
)
from app.synthesis.evidence import (
    PACKET_VERSION,
    ConflictStatus,
    ConflictType,
    EvidenceClass,
    EvidenceConflict,
    EvidenceItem,
    EvidencePacket,
    Freshness,
    Omission,
    OmissionReason,
    PacketIdentity,
    Provenance,
)
from app.synthesis.ledger import CallRecord, SynthesisLedger
from app.synthesis.openai_provider import (
    API_KEY_ENV,
    MAX_OUTPUT_TOKENS,
    OpenAIConfig,
    OpenAISynthesisProvider,
    wire_schema,
)
from app.synthesis.packet import EvidencePacketBuilder
from app.synthesis.pilot import (
    AS_OF_DATES,
    COHORT_VERSION,
    PILOT_COHORT,
    PilotRun,
    PilotSlot,
    run_pilot,
)
from app.synthesis.pricing import (
    CATALOGUE,
    PILOT_MODEL,
    PILOT_PROVIDER,
    PRICING_CHECKED,
    ModelPricing,
    pricing_for,
)
from app.synthesis.provider import (
    SYSTEM_CONTRACT,
    ProviderFailure,
    ProviderResponse,
    SynthesisProvider,
    SynthesisRequest,
    build_request,
)
from app.synthesis.rubric import (
    DISQUALIFYING,
    SUCCESS_CRITERIA,
    Comparison,
    Dimension,
    Finding,
    Score,
)
from app.synthesis.service import Outcome, SynthesisOutcome, SynthesisService
from app.synthesis.validator import (
    Rejection,
    SynthesisValidator,
    ValidationFailure,
    ValidationResult,
)

__all__ = [
    "API_KEY_ENV",
    "AS_OF_DATES",
    "BUDGET_CURRENCY",
    "CATALOGUE",
    "COHORT_VERSION",
    "DEFAULT_PER_RUN_CALLS",
    "DISQUALIFYING",
    "FORBIDDEN_PATTERNS",
    "FORBIDDEN_TERMS",
    "MAX_CLAIMS",
    "MAX_OUTPUT_TOKENS",
    "MAX_PER_RUN_CALLS",
    "MONTHLY_CAP_USD",
    "PACKET_VERSION",
    "PILOT_COHORT",
    "PILOT_MODEL",
    "PILOT_PROVIDER",
    "PRICING_CHECKED",
    "SUCCESS_CRITERIA",
    "SYNTHESIS_SCHEMA_VERSION",
    "SYSTEM_CONTRACT",
    "BudgetDecision",
    "BudgetVerdict",
    "CacheKey",
    "CallRecord",
    "ClaimType",
    "Comparison",
    "ConflictStatus",
    "ConflictType",
    "CostEstimate",
    "CostGuard",
    "Dimension",
    "EvidenceClass",
    "EvidenceConflict",
    "EvidenceItem",
    "EvidencePacket",
    "EvidencePacketBuilder",
    "Finding",
    "Freshness",
    "ModelMetadata",
    "ModelPricing",
    "Omission",
    "OmissionReason",
    "OpenAIConfig",
    "OpenAISynthesisProvider",
    "Outcome",
    "PacketIdentity",
    "PilotRun",
    "PilotSlot",
    "Provenance",
    "ProviderFailure",
    "ProviderResponse",
    "Rejection",
    "ResearchBriefBuilder",
    "ResearchSynthesis",
    "Score",
    "SynthesisCache",
    "SynthesisClaim",
    "SynthesisConfidence",
    "SynthesisLedger",
    "SynthesisOutcome",
    "SynthesisProvider",
    "SynthesisRequest",
    "SynthesisService",
    "SynthesisValidator",
    "ValidatedResearchSynthesis",
    "ValidationFailure",
    "ValidationResult",
    "as_text",
    "build_brief",
    "build_request",
    "config_hash",
    "current_month",
    "key_for",
    "pricing_for",
    "run_pilot",
    "wire_schema",
]
