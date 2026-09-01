"""What a screen asks, and what it is allowed to conclude.

A screen filters. It answers *which covered companies satisfy these stated
conditions*, and it is built so that it cannot answer anything else: there is
no score, no rank by desirability, no default "best first" order, and no field
in which a verdict could be stored. A match is a fact about a company's filings,
not an opinion about its shares.

Three outcomes, not two
-----------------------
The distinction that carries this design is between *failing* a condition and
*not being able to evaluate* it. A company with no operating margin does not
fail ``operating_margin >= 20%`` -- there is nothing to compare. Collapsing the
two would let a screen report "43 of 989 matched" when only 680 could be
assessed, which reads as a 4% hit rate and is really 6% of what was measurable.
Both numbers are reported, always.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Operator(StrEnum):
    """Comparisons a criterion may make. Deliberately few and total."""

    GTE = "gte"
    LTE = "lte"
    GT = "gt"
    LT = "lt"
    EQ = "eq"
    NEQ = "neq"


_APPLY = {
    Operator.GTE: lambda a, b: a >= b,
    Operator.LTE: lambda a, b: a <= b,
    Operator.GT: lambda a, b: a > b,
    Operator.LT: lambda a, b: a < b,
    Operator.EQ: lambda a, b: a == b,
    Operator.NEQ: lambda a, b: a != b,
}

SYMBOLS = {
    Operator.GTE: ">=",
    Operator.LTE: "<=",
    Operator.GT: ">",
    Operator.LT: "<",
    Operator.EQ: "==",
    Operator.NEQ: "!=",
}


def compare(left: Any, operator: Operator, right: Any) -> bool:
    return bool(_APPLY[operator](left, right))


class Scope(StrEnum):
    """Whether a metric belongs to the company or to one traded line.

    Kept apart because they diverge exactly where it matters. Two listings of
    one issuer share filings and must not share prices -- the defect Phase 13
    exists to prevent -- so a fundamental screen may include a cross-listed
    company once, while a market screen has to name which listing it means.
    """

    COMPANY = "COMPANY"
    LISTING = "LISTING"


class Cost(StrEnum):
    """How expensive a metric is to evaluate, measured across the universe.

    Not a hint: the screener evaluates criteria in this order so a cheap
    condition narrows the field before an expensive one runs. Measured per
    company over 989 registrants -- registry 0 ms, history 4 ms, developments
    9 ms, advisor 45 ms.
    """

    REGISTRY = "REGISTRY"
    HISTORY = "HISTORY"
    DEVELOPMENTS = "DEVELOPMENTS"
    ADVISOR = "ADVISOR"


COST_ORDER: tuple[Cost, ...] = (Cost.REGISTRY, Cost.HISTORY, Cost.DEVELOPMENTS, Cost.ADVISOR)


class Evaluation(StrEnum):
    """The three outcomes of testing one criterion against one company."""

    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    NOT_EVALUABLE = "NOT_EVALUABLE"
    """The condition could not be tested. **Never counted as a failure**: a
    company with no operating margin has not failed a margin threshold."""


class NotEvaluable(StrEnum):
    """Why a criterion could not be tested. Specific, never "missing data"."""

    SECTOR_MODEL_REQUIRED = "SECTOR_MODEL_REQUIRED"
    """A financial issuer, where the industrial line item is not the quantity
    of the same name. Inherited from the layer that owns the refusal, never
    re-decided here."""
    UNAVAILABLE = "UNAVAILABLE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    TAXONOMY_DISCONTINUITY = "TAXONOMY_DISCONTINUITY"
    CURRENCY_CHANGE = "CURRENCY_CHANGE"
    GAPPED_SERIES = "GAPPED_SERIES"
    ABANDONED_SERIES = "ABANDONED_SERIES"
    MIXED_BASIS = "MIXED_BASIS"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    """A fund. It has no company economics to test."""
    NO_MARKET_DATA = "NO_MARKET_DATA"
    """The listing carries no price series of its own, and no other listing's
    prices may stand in for it."""
    VALUATION_REFUSED = "VALUATION_REFUSED"
    """Price and earnings are in different currencies. Tradabot performs no
    conversion, so the ratio is computable and meaningless."""
    NO_RESEARCH_COVERAGE = "NO_RESEARCH_COVERAGE"
    WINDOW_UNAVAILABLE = "WINDOW_UNAVAILABLE"
    """The metric exists but not over the window asked for."""


@dataclass(frozen=True, slots=True)
class Criterion:
    """One stated condition. Immutable, and printed back to the user verbatim."""

    metric: str
    operator: Operator
    value: Any

    def describe(self, label: str | None = None, unit: str = "") -> str:
        shown = _format(self.value, unit)
        return f"{label or self.metric} {SYMBOLS[self.operator]} {shown}"

    def as_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "operator": str(self.operator), "value": self.value}


_FORMATS: dict[str, str] = {
    "PERCENT": "{:.1f}%",
    "PERCENTAGE_POINTS": "{:+.1f} pp",
    "MULTIPLE": "{:.1f}x",
    "PERCENTILE": "{:.0f}",
    "CURRENCY": "{:,.0f}",
}


def _format(value: Any, unit: str) -> str:
    """A threshold shown in the unit the metric is stated in."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value)
    shape = _FORMATS.get(unit)
    if shape is None:
        return f"{value:,.4g}"
    return shape.format(value * 100 if unit == "PERCENT" else value)


@dataclass(frozen=True, slots=True)
class CriterionResult:
    """How one company answered one condition, with the value that answered it."""

    criterion: Criterion
    evaluation: Evaluation
    observed: Any = None
    reason: NotEvaluable | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.criterion.as_dict(),
            "evaluation": str(self.evaluation),
            "observed": self.observed,
            "reason": str(self.reason) if self.reason else None,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ScreenCandidate:
    """One company's full answer, kept whole so a match can be audited.

    Every criterion's observed value is retained, not only the ones that
    passed. A screen that returns tickers is not auditable; a screen that shows
    the figure it compared and the threshold it compared against is.
    """

    company_id: int
    company_key: str
    symbol: str
    company_name: str
    listing: str
    sic: str | None
    results: tuple[CriterionResult, ...]

    @property
    def evaluation(self) -> Evaluation:
        """One outcome for the whole company.

        **Failing a stated condition settles it.** A company known to fall
        below a threshold is NO_MATCH even if some other criterion could not be
        tested -- saying "we could not assess this company" about one we know
        fails a requirement is the less honest of the two statements.

        NOT_EVALUABLE is therefore reserved for a company that failed *nothing*
        and still could not be fully tested, which is exactly the case that
        would otherwise be miscounted as a failure. The rule is also
        order-independent, which matters because criteria are evaluated
        cheapest-first: the bucket a company lands in cannot depend on which
        condition happened to be examined first.
        """
        if any(r.evaluation is Evaluation.NO_MATCH for r in self.results):
            return Evaluation.NO_MATCH
        if any(r.evaluation is Evaluation.NOT_EVALUABLE for r in self.results):
            return Evaluation.NOT_EVALUABLE
        return Evaluation.MATCH

    @property
    def reasons(self) -> tuple[NotEvaluable, ...]:
        return tuple(r.reason for r in self.results if r.reason is not None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "company_key": self.company_key,
            "symbol": self.symbol,
            "company_name": self.company_name,
            "listing": self.listing,
            "sic": self.sic,
            "evaluation": str(self.evaluation),
            "results": [r.as_dict() for r in self.results],
        }


DEFAULT_LIMIT = 20
MAX_LIMIT = 50
"""Rows returned. A screen that prints two hundred companies has not helped
anyone choose; the count of matches is always reported in full, so trimming the
list never hides how many there were."""


@dataclass(frozen=True, slots=True)
class ScreenResult:
    """What a screen found, and how much of the universe it could actually test."""

    as_of: str
    criteria: tuple[Criterion, ...]
    universe: int
    evaluated: int
    """Companies for which every criterion could be tested."""
    matched: int
    not_evaluable: int
    candidates: tuple[ScreenCandidate, ...]
    reasons: dict[str, int] = field(default_factory=dict)
    sort_metric: str | None = None
    descending: bool = False
    limit: int = DEFAULT_LIMIT
    truncated: int = 0
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "criteria": [c.as_dict() for c in self.criteria],
            "universe": self.universe,
            "evaluated": self.evaluated,
            "matched": self.matched,
            "not_evaluable": self.not_evaluable,
            "reasons": dict(self.reasons),
            "sort_metric": self.sort_metric,
            "descending": self.descending,
            "limit": self.limit,
            "truncated": self.truncated,
            "duration_seconds": round(self.duration_seconds, 2),
            "candidates": [c.as_dict() for c in self.candidates],
        }
