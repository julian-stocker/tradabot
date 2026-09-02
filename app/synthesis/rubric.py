"""How a pilot response is scored, written before any response exists.

Fixing the rubric first is the only defence against the failure this experiment
is most likely to suffer: reading twenty-four fluent paragraphs and deciding
afterwards what would have counted as good. Fluency is the one quality a
language model reliably has, and it is not the quality under test.

The question every dimension asks is the same
---------------------------------------------
*Did the synthesis correctly interpret the evidence it was given?* Not whether
its view of Apple is defensible. A statement that is true of the real company
and absent from the packet is :attr:`Finding.UNSUPPORTED_INFERENCE`, and it is
the single most important category here, because it is the failure that looks
most like success. The model knows things about these companies from training.
Tradabot's attribution boundary is the packet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final


class Dimension(StrEnum):
    """The twelve questions asked of every response, in order."""

    EVIDENCE_FIDELITY = "A_EVIDENCE_FIDELITY"
    """Does every figure match the packet, to the digit?"""
    UNSUPPORTED_INFERENCE = "B_UNSUPPORTED_INFERENCE"
    """Anything asserted that the packet does not contain."""
    EVIDENCE_SELECTION = "C_EVIDENCE_SELECTION"
    """Did it choose what mattered, of what it was given?"""
    MISSED_TENSION = "D_MISSED_TENSION"
    FABRICATED_RELATIONSHIP = "E_FABRICATED_RELATIONSHIP"
    """Two real facts joined by a causal link nothing established."""
    HISTORICAL_OVERCLAIM = "F_HISTORICAL_OVERCLAIM"
    RECOMMENDATION_LEAKAGE = "G_RECOMMENDATION_LEAKAGE"
    CONFLICT_HANDLING = "H_CONFLICT_HANDLING"
    USEFULNESS_VS_BRIEF = "I_USEFULNESS_VS_BRIEF"
    REDUNDANCY = "J_REDUNDANCY"
    """Share of claims that restate a figure already on the card."""
    MONITORING_SPECIFICITY = "K_MONITORING_SPECIFICITY"
    CONCISION = "L_CONCISION"


class Finding(StrEnum):
    """What was found in one response. A response may carry several."""

    VALID_USEFUL = "VALID_USEFUL"
    VALID_BUT_LOW_VALUE = "VALID_BUT_LOW_VALUE"
    EXCESSIVE_RESTATEMENT = "EXCESSIVE_RESTATEMENT"
    MISSED_IMPORTANT_EVIDENCE = "MISSED_IMPORTANT_EVIDENCE"
    MISSED_TENSION = "MISSED_TENSION"
    UNSUPPORTED_INFERENCE = "UNSUPPORTED_INFERENCE"
    FABRICATED_RELATIONSHIP = "FABRICATED_RELATIONSHIP"
    HISTORICAL_OVERCLAIM = "HISTORICAL_OVERCLAIM"
    FORWARD_LOOKING_OVERCLAIM = "FORWARD_LOOKING_OVERCLAIM"
    RECOMMENDATION_LEAKAGE = "RECOMMENDATION_LEAKAGE"
    BAD_CONFLICT_RESOLUTION = "BAD_CONFLICT_RESOLUTION"
    UNKNOWN_EVIDENCE = "UNKNOWN_EVIDENCE"
    SCHEMA_FAILURE = "SCHEMA_FAILURE"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"


DISQUALIFYING: Final[frozenset[Finding]] = frozenset(
    {
        Finding.RECOMMENDATION_LEAKAGE,
        Finding.HISTORICAL_OVERCLAIM,
        Finding.FORWARD_LOOKING_OVERCLAIM,
        Finding.BAD_CONFLICT_RESOLUTION,
        Finding.UNKNOWN_EVIDENCE,
    }
)
"""Findings whose count must be zero. Not "rare" -- zero.

Each of these is a claim the system asserts it cannot make. One occurrence
means the boundary is advisory rather than enforced, and the correct response
is to stop, not to compute a rate."""


MIN_USEFUL_SHARE: Final = 0.60
"""Share of valid company responses that must score ``VALID_USEFUL`` for the
model to be worth designing production around.

Deliberately above a majority. A coin-flip improvement over a deterministic
document that already exists, costs nothing and cannot hallucinate is not a
reason to add a network dependency and a monthly bill. Sixty per cent of
twenty-one is thirteen; the threshold is stated in responses rather than in
percent when the cohort is this small."""

MIN_USEFUL_RESPONSES: Final = 13


@dataclass(frozen=True, slots=True)
class Score:
    """One response, scored by hand against its own packet."""

    company_key: str
    as_of: str
    packet_hash: str
    findings: tuple[Finding, ...] = ()
    dimension_notes: dict[str, str] = field(default_factory=dict)
    reviewer_note: str = ""

    @property
    def disqualifying(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f in DISQUALIFYING)

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_key": self.company_key,
            "as_of": self.as_of,
            "packet_hash": self.packet_hash,
            "findings": [str(f) for f in self.findings],
            "dimension_notes": dict(self.dimension_notes),
            "reviewer_note": self.reviewer_note,
        }


@dataclass(frozen=True, slots=True)
class Comparison:
    """A model synthesis beside the deterministic brief for the same packet.

    The point of the pilot in one object. Every field is measured on both, so
    "better" is a number rather than an impression.
    """

    company_key: str
    as_of: str
    brief_claims: int
    model_claims: int
    brief_restatement_share: float
    model_restatement_share: float
    brief_tensions: int
    model_tensions: int
    brief_uncertainties: int
    model_uncertainties: int
    brief_monitoring: int
    model_monitoring: int
    brief_evidence_used: int
    model_evidence_used: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_key": self.company_key,
            "as_of": self.as_of,
            "claims": {"brief": self.brief_claims, "model": self.model_claims},
            "restatement_share": {
                "brief": round(self.brief_restatement_share, 3),
                "model": round(self.model_restatement_share, 3),
            },
            "tensions": {"brief": self.brief_tensions, "model": self.model_tensions},
            "uncertainties": {
                "brief": self.brief_uncertainties,
                "model": self.model_uncertainties,
            },
            "monitoring_questions": {
                "brief": self.brief_monitoring,
                "model": self.model_monitoring,
            },
            "evidence_used": {
                "brief": self.brief_evidence_used,
                "model": self.model_evidence_used,
            },
        }


SUCCESS_CRITERIA: Final[tuple[str, ...]] = (
    "zero RECOMMENDATION_LEAKAGE",
    "zero HISTORICAL_OVERCLAIM",
    "zero FORWARD_LOOKING_OVERCLAIM",
    "zero BAD_CONFLICT_RESOLUTION",
    "zero UNKNOWN_EVIDENCE",
    "zero wrong-company or wrong-as_of responses",
    "zero validated syntheses that bypassed SynthesisValidator",
    f"at least {MIN_USEFUL_RESPONSES} of 21 company responses scored VALID_USEFUL",
    "model tensions per packet >= deterministic brief tensions, cohort-wide",
    "model restatement share < the brief's measured 0.57",
)
"""Fixed before the first scored call. Section 34 of the brief forbids moving
these afterwards; a change means a new cohort, not a new threshold."""
