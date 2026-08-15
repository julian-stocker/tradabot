"""Advisor output types.

Read-only, factual. Nothing here expresses an expected return, a probability of
profit, or a buy/sell intent -- no validated predictive evidence exists for any
of those, and Phase 12.25 established that explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Confidence(StrEnum):
    """How much the *data* supports a section. Never how good the company looks."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT = "INSUFFICIENT"


_ORDER: dict[str, int] = {"INSUFFICIENT": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


def weakest(*levels: Confidence) -> Confidence:
    """Confidence is the minimum of its inputs, never the mean.

    A section is only as trustworthy as its worst required input, so one missing
    denominator caps the whole section rather than being averaged away.
    """
    present = [x for x in levels if x is not None]
    if not present:
        return Confidence.INSUFFICIENT
    return min(present, key=lambda c: _ORDER[str(c)])


class ValuationContext(StrEnum):
    """Where a multiple sits in the company's OWN available history.

    A price context, not an attractiveness judgement: LOW_VS_HISTORY does not
    mean cheap-and-worth-buying.
    """

    VERY_LOW = "VERY_LOW_VS_HISTORY"
    LOW = "LOW_VS_HISTORY"
    NORMAL = "NORMAL_VS_HISTORY"
    HIGH = "HIGH_VS_HISTORY"
    VERY_HIGH = "VERY_HIGH_VS_HISTORY"
    INSUFFICIENT = "INSUFFICIENT_HISTORY"


class DataSupport(StrEnum):
    """How much *information* Tradabot holds for a horizon. Not an outlook."""

    WEAK = "WEAK"
    PARTIAL = "PARTIAL"
    STRONGEST_AVAILABLE = "STRONGEST_AVAILABLE"


class DenominatorBasis(StrEnum):
    TRUE_TTM = "TRUE_TTM"
    FY_FALLBACK = "FY_FALLBACK"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a fundamental number came from, so any figure can be audited."""

    concept: str
    unit: str
    form: str | None
    filed: str | None
    accession: str | None
    period_end: str | None
    value: float


@dataclass(frozen=True, slots=True)
class Metric:
    """One factual number, or an explicit absence. Never an imputed default."""

    name: str
    value: float | None
    basis: DenominatorBasis = DenominatorBasis.TRUE_TTM
    unavailable_reason: str | None = None
    provenance: tuple[Provenance, ...] = ()

    @property
    def available(self) -> bool:
        return self.value is not None


@dataclass(frozen=True, slots=True)
class Section:
    """A named block of metrics with its own confidence and reasons."""

    name: str
    metrics: dict[str, Metric] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    confidence: Confidence = Confidence.INSUFFICIENT
    confidence_reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InvestmentAssessment:
    """Deliberately empty boundary for a future recommendation layer.

    Distinguishing "analysis unavailable" from "analysis exists but the evidence
    for a recommendation does not" is the whole point of this type.
    """

    available: bool = False
    reason: str = "NO_VALIDATED_PREDICTIVE_EVIDENCE"


@dataclass(frozen=True, slots=True)
class AdvisorReport:
    symbol: str
    as_of: str
    profile: Section
    company_quality: tuple[Section, ...]
    valuation: Section
    market_position: Section
    risks: dict[str, tuple[str, ...]]
    horizon_data_support: dict[str, dict[str, Any]]
    summary: str
    confidence: dict[str, Confidence]
    investment_assessment: InvestmentAssessment = InvestmentAssessment()
    disclaimer: str = (
        "Factual analysis only. This is not investment advice, contains no "
        "buy or sell recommendation, and makes no claim about future returns."
    )
