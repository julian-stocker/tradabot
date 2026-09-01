"""Read-only company Advisor.

Factual analysis only: no buy/sell recommendation, no expected return, no
probability of profit. Phase 12.25 established that no company-quality or
valuation relationship in this data survives out-of-sample validation, so this
package deliberately stops at description.
"""

from app.advisor.facts import FactStore, TtmResult
from app.advisor.schemas import (
    AdvisorReport,
    Confidence,
    DataSupport,
    DenominatorBasis,
    InvestmentAssessment,
    Metric,
    Provenance,
    Section,
    ValuationContext,
)
from app.advisor.service import UNPRICED, AdvisorService, MarketIdentity, PriceSeries

__all__ = [
    "UNPRICED",
    "AdvisorReport",
    "AdvisorService",
    "Confidence",
    "DataSupport",
    "DenominatorBasis",
    "FactStore",
    "InvestmentAssessment",
    "MarketIdentity",
    "Metric",
    "PriceSeries",
    "Provenance",
    "Section",
    "TtmResult",
    "ValuationContext",
]
