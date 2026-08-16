"""Company context, borrowed rather than recomputed.

Two questions, one report
-------------------------
"Is this a sound company?" and "does it fit this portfolio?" are different
questions with different answers, and answering the second without showing the
first is how a diversifying addition ends up looking like a good idea on its
own terms. So the fit report carries company context alongside the portfolio
arithmetic -- adjacent, never merged into a single verdict.

Borrowed, emphatically
----------------------
Not one financial figure is derived here. There is no trailing-twelve-month
sum, no margin, no valuation percentile, no market-position calculation. The
Advisor owns all of that, a drift guard keeps it the only owner, and a second
implementation would be wrong within a quarter.

What this module defines is the *shape* of the context the fit report displays
and the protocol that hands it over. The implementation lives with the Advisor.

Missing context is not a failure
--------------------------------
Portfolio mathematics needs prices and weights, not fundamentals. A company with
no SEC filings, or a request made while the fact store is unsynced, yields
``ADVISOR_CONTEXT_UNAVAILABLE`` and the correlation, concentration and exposure
analysis continues unchanged. Failing the whole report because one narrative
block is absent would make the layer less useful precisely when data is thin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

UNAVAILABLE: Final = "ADVISOR_CONTEXT_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class CompanyContext:
    """Compact company description for one symbol, as the Advisor reported it.

    Every field is either something the Advisor said or ``None``. Nothing is
    summarised into a score, and there is no field for an outlook, because no
    validated predictive evidence supports one.
    """

    symbol: str
    available: bool
    summary: str | None = None
    valuation_context: str | None = None
    valuation_metric: str | None = None
    valuation_value: float | None = None
    market_position: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    confidence: str = "INSUFFICIENT"
    unavailable_reason: str | None = None

    @classmethod
    def missing(cls, symbol: str, reason: str = UNAVAILABLE) -> CompanyContext:
        return cls(symbol=symbol, available=False, unavailable_reason=reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "available": self.available,
            "summary": self.summary,
            "valuation_context": self.valuation_context,
            "valuation_metric": self.valuation_metric,
            "valuation_value": self.valuation_value,
            "market_position": self.market_position,
            "labels": dict(self.labels),
            "confidence": self.confidence,
            "unavailable_reason": self.unavailable_reason,
        }


@runtime_checkable
class CompanyContextProvider(Protocol):
    """Supplies company context for a symbol as of a date.

    Implementations must return :meth:`CompanyContext.missing` rather than
    raising when a company cannot be described: the caller is in the middle of
    an analysis that does not depend on this answer.
    """

    def context(self, symbol: str, as_of: str) -> CompanyContext:
        ...
