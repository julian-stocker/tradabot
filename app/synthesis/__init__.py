"""Bounded research synthesis over verified evidence.

Design and contracts only. No model is called, no provider is implemented and
no API is configured: this package defines what evidence a synthesis may see,
what it may say, how that is checked, and what happens when no model exists at
all.
"""

from app.synthesis.brief import ResearchBriefBuilder, as_text, build_brief
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
from app.synthesis.packet import EvidencePacketBuilder
from app.synthesis.provider import (
    SYSTEM_CONTRACT,
    ProviderFailure,
    ProviderResponse,
    SynthesisProvider,
    SynthesisRequest,
    build_request,
)
from app.synthesis.validator import (
    Rejection,
    SynthesisValidator,
    ValidationFailure,
    ValidationResult,
)

__all__ = [
    "FORBIDDEN_PATTERNS",
    "FORBIDDEN_TERMS",
    "MAX_CLAIMS",
    "PACKET_VERSION",
    "SYNTHESIS_SCHEMA_VERSION",
    "SYSTEM_CONTRACT",
    "ClaimType",
    "ConflictStatus",
    "ConflictType",
    "EvidenceClass",
    "EvidenceConflict",
    "EvidenceItem",
    "EvidencePacket",
    "EvidencePacketBuilder",
    "Freshness",
    "ModelMetadata",
    "Omission",
    "OmissionReason",
    "PacketIdentity",
    "Provenance",
    "ProviderFailure",
    "ProviderResponse",
    "Rejection",
    "ResearchBriefBuilder",
    "ResearchSynthesis",
    "SynthesisClaim",
    "SynthesisConfidence",
    "SynthesisProvider",
    "SynthesisRequest",
    "SynthesisValidator",
    "ValidatedResearchSynthesis",
    "ValidationFailure",
    "ValidationResult",
    "as_text",
    "build_brief",
    "build_request",
]
