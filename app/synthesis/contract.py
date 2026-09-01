"""What a synthesis is allowed to say, expressed as a type rather than a request.

The whole design rests on one choice: **forbidden claims are absent from the
schema, not discouraged in a prompt.** There is no ``PREDICTION`` member of
:class:`ClaimType`, no ``price_target`` field, no ``recommendation`` field and
no free-text top level. A model asked politely not to recommend a stock will
eventually recommend one; a model whose output must parse into a structure with
nowhere to put a recommendation cannot have its recommendation accepted.

The second choice is that every non-trivial claim must cite evidence that
exists in the packet it was given. A sentence with no evidence reference is not
a weak claim -- it is unattributable, and this system rejects it.

The line between fact and interpretation
----------------------------------------
    FACT_SUMMARY   "Operating margin rose from 21.4% to 29.3% over three years."
    INTERPRETATION "Profitability improved on this measure over that period."

The first restates a figure. The second names what the figure means on a
dimension, and is permitted only with evidence, only about the past, and only
in the metric's own terms. Neither may become:

    "Profitability should keep improving."       (predictive)
    "This is an attractive business."            (evaluative)
    "Margins historically expand after this."    (unestablished history)

The last is worth stating separately: every research event in this system
carries ``historical_evidence = NOT_ESTABLISHED``, because no event study over
these event kinds exists. A synthesis may say a development *bears on* an
observable condition. It may not say what such developments have led to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

SYNTHESIS_SCHEMA_VERSION = "18.0.0"


class ClaimType(StrEnum):
    """The only kinds of statement a synthesis may make.

    Exhaustive by design. ``PREDICTION``, ``RECOMMENDATION`` and
    ``PRICE_TARGET`` are absent so that a model emitting one produces an
    unparseable claim type rather than a claim the validator has to argue with.
    """

    FACT_SUMMARY = "FACT_SUMMARY"
    """Restates figures already in the packet."""
    INTERPRETATION = "INTERPRETATION"
    """Names what evidence indicates on a stated dimension, about the past."""
    TENSION = "TENSION"
    """Two pieces of evidence that point different ways, both cited."""
    UNCERTAINTY = "UNCERTAINTY"
    """Something the evidence does not settle, including why."""
    MONITORING_QUESTION = "MONITORING_QUESTION"
    """An observable condition whose future value would inform the reader.
    A question about a company's own reported figures -- never a price level,
    and never a trigger."""


MIN_EVIDENCE: Final[dict[ClaimType, int]] = {
    ClaimType.FACT_SUMMARY: 1,
    ClaimType.INTERPRETATION: 1,
    ClaimType.TENSION: 2,
    ClaimType.UNCERTAINTY: 1,
    ClaimType.MONITORING_QUESTION: 1,
}
"""Evidence references a claim of each type must carry.

A tension needs two by definition: it asserts that two things disagree, and a
tension citing one item is an opinion wearing a structural label."""


MAX_CLAIM_CHARS: Final = 320
MAX_CLAIMS: Final = 12
MAX_SUMMARY_CHARS: Final = 400
MAX_PER_TYPE: Final = 4
"""Bounds, so a synthesis is a reading rather than an essay. A twelve-claim
ceiling also keeps the output small enough to check by hand, which is how the
first ones will be checked."""


FORBIDDEN_TERMS: Final[tuple[str, ...]] = (
    "buy",
    "sell",
    "hold",
    "bullish",
    "bearish",
    "upside",
    "downside",
    "price target",
    "fair value",
    "expected return",
    "undervalued",
    "overvalued",
    "best stock",
    "top pick",
    "conviction",
    "position size",
    "overweight",
    "underweight",
    "accumulate",
    "outperform",
    "underperform",
    "attractive entry",
    "worth buying",
    "worth selling",
    "should own",
    "add to",
    "trim",
    "take profit",
    "stop loss",
    "breakout",
    "rally",
)
"""Terms no validated synthesis may contain.

A backstop, not the mechanism. The schema already has nowhere to put a
recommendation; this catches one smuggled into prose. Matched on word
boundaries -- an earlier gate in this project banned the substring ``rating``
and rejected the word ``operating``."""

FORBIDDEN_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    (
        r"\bwill (?:rise|fall|increase|decrease|outperform|continue)\b",
        "a claim about what happens next",
    ),
    (r"\b(?:likely|expected|projected) to\b", "a forward-looking expectation"),
    (
        r"\bhistorically (?:leads?|results?|precedes?)\b",
        "a historical-effect claim; historical_evidence is NOT_ESTABLISHED",
    ),
    (
        r"\bevents? (?:like this|of this kind) (?:have|has|tend)\b",
        "a historical-effect claim about an event kind",
    ),
    (r"\btarget (?:price|of \$)\b", "a price target"),
    (
        r"\b(?:good|bad|great|poor|strong|weak) (?:investment|stock|buy|company)\b",
        "an investment judgement",
    ),
    (r"\brecommend\w*\b", "a recommendation"),
    (
        r"\bignore (?:previous|prior|above|all) instructions?\b",
        "text following an instruction embedded in evidence",
    ),
)
"""Disguised equivalents, matched semantically rather than by keyword. The last
entry catches a synthesis that obeyed an instruction planted in filing text."""


@dataclass(frozen=True, slots=True)
class SynthesisClaim:
    """One attributable statement."""

    claim_id: str
    claim_type: ClaimType
    text: str
    evidence_ids: tuple[str, ...]
    temporal_scope: str = "PAST"
    """``PAST`` or ``CURRENT``. There is deliberately no ``FUTURE``."""
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "claim_type": str(self.claim_type),
            "text": self.text,
            "evidence_ids": list(self.evidence_ids),
            "temporal_scope": self.temporal_scope,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    """What produced a synthesis, in enough detail to reproduce the inputs."""

    provider: str
    model: str
    schema_version: str = SYNTHESIS_SCHEMA_VERSION
    packet_version: str = ""
    packet_hash: str = ""
    template_version: str = ""
    temperature: float | None = None
    response_hash: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "schema_version": self.schema_version,
            "packet_version": self.packet_version,
            "packet_hash": self.packet_hash,
            "template_version": self.template_version,
            "temperature": self.temperature,
            "response_hash": self.response_hash,
        }


class SynthesisConfidence(StrEnum):
    """How complete the *evidence* was. **Not** a probability about the shares.

    Derived from coverage, conflicts, staleness and refusals -- never from any
    view about what the company's shares will do, which this system has no
    basis to hold.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass(frozen=True, slots=True)
class ResearchSynthesis:
    """A candidate synthesis. **Untrusted until validated.**

    Whatever produced this -- a model, a fixture, a deterministic brief -- it is
    not trusted on arrival. :class:`~app.synthesis.validator.SynthesisValidator`
    decides whether it may be shown, and nothing renders it before then.
    """

    company_key: str
    as_of: str
    summary: str
    claims: tuple[SynthesisClaim, ...] = ()
    confidence: SynthesisConfidence = SynthesisConfidence.LOW
    limitations: tuple[str, ...] = ()
    metadata: ModelMetadata | None = None

    def of_type(self, claim_type: ClaimType) -> tuple[SynthesisClaim, ...]:
        return tuple(c for c in self.claims if c.claim_type is claim_type)

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(e for c in self.claims for e in c.evidence_ids)

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_key": self.company_key,
            "as_of": self.as_of,
            "summary": self.summary,
            "claims": [c.as_dict() for c in self.claims],
            "confidence": str(self.confidence),
            "limitations": list(self.limitations),
            "metadata": self.metadata.as_dict() if self.metadata else None,
        }


@dataclass(frozen=True, slots=True)
class ValidatedResearchSynthesis:
    """A synthesis that passed every gate. **The only renderable form.**

    Presentation accepts this type and not :class:`ResearchSynthesis`, so an
    unvalidated candidate cannot reach a card by omission -- it would not
    type-check.
    """

    synthesis: ResearchSynthesis
    packet_hash: str
    validator_version: str = SYNTHESIS_SCHEMA_VERSION
    checks_passed: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.synthesis.as_dict(),
            "packet_hash": self.packet_hash,
            "validator_version": self.validator_version,
            "checks_passed": list(self.checks_passed),
        }
