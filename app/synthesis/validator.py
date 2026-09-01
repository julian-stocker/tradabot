"""The gate every synthesis passes before anyone sees it.

The provider is never trusted -- not because a model is untrustworthy in
principle, but because nothing downstream can tell a careful sentence from a
plausible one, and the only defence that scales is a check that runs every
time. The validator is deterministic, offline, and sits *after* whatever
produced the candidate.

It fails closed. A synthesis that breaks one rule is rejected whole rather than
trimmed to the acceptable parts: a reading whose conclusion rested on a claim
that has just been deleted is not a shorter valid reading, it is a different
one nobody wrote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from app.synthesis.contract import (
    FORBIDDEN_PATTERNS,
    FORBIDDEN_TERMS,
    MAX_CLAIM_CHARS,
    MAX_CLAIMS,
    MAX_PER_TYPE,
    MAX_SUMMARY_CHARS,
    MIN_EVIDENCE,
    SYNTHESIS_SCHEMA_VERSION,
    ClaimType,
    ResearchSynthesis,
    ValidatedResearchSynthesis,
)
from app.synthesis.evidence import EvidencePacket


class Rejection(StrEnum):
    """Why a candidate was refused. One reason per rule, never "invalid"."""

    WRONG_COMPANY = "WRONG_COMPANY"
    WRONG_AS_OF = "WRONG_AS_OF"
    UNKNOWN_EVIDENCE_ID = "UNKNOWN_EVIDENCE_ID"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    FORBIDDEN_CLAIM = "FORBIDDEN_CLAIM"
    HISTORICAL_OVERCLAIM = "HISTORICAL_OVERCLAIM"
    FORWARD_LOOKING = "FORWARD_LOOKING"
    PROMPT_INJECTION_FOLLOWED = "PROMPT_INJECTION_FOLLOWED"
    TOO_LONG = "TOO_LONG"
    TOO_MANY_CLAIMS = "TOO_MANY_CLAIMS"
    EMPTY = "EMPTY"
    SCHEMA_VERSION_MISMATCH = "SCHEMA_VERSION_MISMATCH"
    PACKET_MISMATCH = "PACKET_MISMATCH"
    RESOLVED_UNRESOLVED_CONFLICT = "RESOLVED_UNRESOLVED_CONFLICT"


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    reason: Rejection
    detail: str
    claim_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": str(self.reason),
            "detail": self.detail,
            "claim_id": self.claim_id,
        }


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Accepted or refused, with every rule that ran."""

    valid: bool
    validated: ValidatedResearchSynthesis | None = None
    failures: tuple[ValidationFailure, ...] = ()
    checks: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "failures": [f.as_dict() for f in self.failures],
            "checks": list(self.checks),
        }


CHECKS: Final[tuple[str, ...]] = (
    "schema_version",
    "identity",
    "as_of",
    "non_empty",
    "claim_counts",
    "lengths",
    "evidence_exists",
    "evidence_sufficiency",
    "claim_types",
    "forbidden_terms",
    "forbidden_patterns",
    "temporal_scope",
    "historical_evidence",
    "conflict_resolution",
)

_TERM_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = tuple(
    (re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE), term) for term in FORBIDDEN_TERMS
)
_SEMANTIC_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = tuple(
    (re.compile(pattern, re.IGNORECASE), why) for pattern, why in FORBIDDEN_PATTERNS
)

ALLOWED_SCOPES: Final[frozenset[str]] = frozenset({"PAST", "CURRENT"})


class SynthesisValidator:
    """Checks a candidate against the packet it was supposedly built from.

    The packet is required, not optional. Validating a synthesis without the
    evidence it cites would reduce every check to a spelling test.
    """

    def validate(self, candidate: ResearchSynthesis, *, packet: EvidencePacket) -> ValidationResult:
        """Accept or refuse. **Never raises.**"""
        failures: list[ValidationFailure] = []
        try:
            failures.extend(self._identity(candidate, packet))
            failures.extend(self._shape(candidate))
            failures.extend(self._evidence(candidate, packet))
            failures.extend(self._language(candidate))
            failures.extend(self._conflicts(candidate, packet))
        except Exception as exc:  # pragma: no cover - defensive
            failures.append(
                ValidationFailure(Rejection.EMPTY, f"unreadable candidate: {type(exc).__name__}")
            )
        if failures:
            return ValidationResult(valid=False, failures=tuple(failures), checks=CHECKS)
        return ValidationResult(
            valid=True,
            validated=ValidatedResearchSynthesis(
                synthesis=candidate,
                packet_hash=packet.packet_hash,
                checks_passed=CHECKS,
            ),
            checks=CHECKS,
        )

    # ------------------------------------------------------------- identity
    def _identity(
        self, candidate: ResearchSynthesis, packet: EvidencePacket
    ) -> list[ValidationFailure]:
        out: list[ValidationFailure] = []
        if candidate.company_key != packet.identity.company_key:
            out.append(
                ValidationFailure(
                    Rejection.WRONG_COMPANY,
                    f"synthesis names {candidate.company_key!r}, "
                    f"packet is {packet.identity.company_key!r}",
                )
            )
        if candidate.as_of != packet.as_of:
            out.append(
                ValidationFailure(
                    Rejection.WRONG_AS_OF,
                    f"synthesis dated {candidate.as_of!r}, packet is {packet.as_of!r}",
                )
            )
        metadata = candidate.metadata
        if metadata is not None:
            if metadata.schema_version != SYNTHESIS_SCHEMA_VERSION:
                out.append(
                    ValidationFailure(
                        Rejection.SCHEMA_VERSION_MISMATCH,
                        f"built against schema {metadata.schema_version!r}",
                    )
                )
            if metadata.packet_hash and metadata.packet_hash != packet.packet_hash:
                out.append(
                    ValidationFailure(
                        Rejection.PACKET_MISMATCH,
                        "the synthesis was produced from different evidence",
                    )
                )
        return out

    def _shape(self, candidate: ResearchSynthesis) -> list[ValidationFailure]:
        out: list[ValidationFailure] = []
        if not candidate.summary.strip() or not candidate.claims:
            out.append(ValidationFailure(Rejection.EMPTY, "no summary or no claims"))
        if len(candidate.claims) > MAX_CLAIMS:
            out.append(
                ValidationFailure(
                    Rejection.TOO_MANY_CLAIMS,
                    f"{len(candidate.claims)} claims exceeds {MAX_CLAIMS}",
                )
            )
        if len(candidate.summary) > MAX_SUMMARY_CHARS:
            out.append(
                ValidationFailure(
                    Rejection.TOO_LONG,
                    f"summary is {len(candidate.summary)} characters",
                )
            )
        for claim_type in ClaimType:
            found = candidate.of_type(claim_type)
            if len(found) > MAX_PER_TYPE:
                out.append(
                    ValidationFailure(
                        Rejection.TOO_MANY_CLAIMS,
                        f"{len(found)} {claim_type} claims exceeds {MAX_PER_TYPE}",
                    )
                )
        for claim in candidate.claims:
            if len(claim.text) > MAX_CLAIM_CHARS:
                out.append(
                    ValidationFailure(
                        Rejection.TOO_LONG,
                        f"claim is {len(claim.text)} characters",
                        claim.claim_id,
                    )
                )
            if claim.temporal_scope not in ALLOWED_SCOPES:
                out.append(
                    ValidationFailure(
                        Rejection.FORWARD_LOOKING,
                        f"temporal scope {claim.temporal_scope!r} is not "
                        f"{' or '.join(sorted(ALLOWED_SCOPES))}",
                        claim.claim_id,
                    )
                )
        return out

    # ------------------------------------------------------------- evidence
    def _evidence(
        self, candidate: ResearchSynthesis, packet: EvidencePacket
    ) -> list[ValidationFailure]:
        out: list[ValidationFailure] = []
        known = packet.evidence_ids
        for claim in candidate.claims:
            unknown = [e for e in claim.evidence_ids if e not in known]
            if unknown:
                # A cited identifier that is not in the packet is either invented
                # or remembered from elsewhere. Both are unattributable.
                out.append(
                    ValidationFailure(
                        Rejection.UNKNOWN_EVIDENCE_ID,
                        f"cites {unknown!r}, which is not in this packet",
                        claim.claim_id,
                    )
                )
            required = MIN_EVIDENCE.get(claim.claim_type, 1)
            if len(set(claim.evidence_ids)) < required:
                out.append(
                    ValidationFailure(
                        Rejection.MISSING_EVIDENCE,
                        f"{claim.claim_type} needs {required} distinct evidence "
                        f"reference(s), has {len(set(claim.evidence_ids))}",
                        claim.claim_id,
                    )
                )
        return out

    # ------------------------------------------------------------- language
    def _language(self, candidate: ResearchSynthesis) -> list[ValidationFailure]:
        out: list[ValidationFailure] = []
        texts = [(None, candidate.summary)] + [(c.claim_id, c.text) for c in candidate.claims]
        for claim_id, text in texts:
            for pattern, term in _TERM_PATTERNS:
                if pattern.search(text):
                    out.append(
                        ValidationFailure(Rejection.FORBIDDEN_CLAIM, f"contains {term!r}", claim_id)
                    )
            for pattern, why in _SEMANTIC_PATTERNS:
                if not pattern.search(text):
                    continue
                reason = Rejection.FORBIDDEN_CLAIM
                if "historical" in why:
                    reason = Rejection.HISTORICAL_OVERCLAIM
                elif "happens next" in why or "forward-looking" in why:
                    reason = Rejection.FORWARD_LOOKING
                elif "instruction" in why:
                    reason = Rejection.PROMPT_INJECTION_FOLLOWED
                out.append(ValidationFailure(reason, why, claim_id))
        return out

    def _conflicts(
        self, candidate: ResearchSynthesis, packet: EvidencePacket
    ) -> list[ValidationFailure]:
        """An unresolved conflict may be described, never decided.

        If the packet says two sources disagree and nothing explains it, a
        synthesis that cites both and states which is correct has made a
        judgement the evidence does not support.
        """
        unresolved = {
            (c.evidence_a, c.evidence_b) for c in packet.conflicts if str(c.status) == "UNRESOLVED"
        }
        if not unresolved:
            return []
        out: list[ValidationFailure] = []
        settling = re.compile(
            r"\b(?:the correct|actually is|in fact is|the true|should be read as)\b",
            re.IGNORECASE,
        )
        for claim in candidate.claims:
            cited = set(claim.evidence_ids)
            touches = any(a in cited and b in cited for a, b in unresolved)
            if (
                touches
                and claim.claim_type is not ClaimType.TENSION
                and settling.search(claim.text)
            ):
                out.append(
                    ValidationFailure(
                        Rejection.RESOLVED_UNRESOLVED_CONFLICT,
                        "decides a conflict the packet leaves unresolved",
                        claim.claim_id,
                    )
                )
        return out
