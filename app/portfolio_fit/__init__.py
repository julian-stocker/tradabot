"""Read-only Portfolio Fit.

Answers how a company sits inside a specific portfolio -- concentration, sector
exposure, correlation and historical risk. It never answers whether to buy,
sell, rotate or resize: no predictive evidence supports those, and the layer is
structurally unable to reach a broker.
"""

from app.portfolio_fit.accounts import (
    AccountSnapshot,
    PortfolioAccountReader,
    SnapshotPosition,
    safe_account_reference,
    unavailable,
)
from app.portfolio_fit.context import (
    UNAVAILABLE,
    CompanyContext,
    CompanyContextProvider,
)
from app.portfolio_fit.schemas import (
    CandidateFit,
    Concentration,
    Exposure,
    FitConfidence,
    FitState,
    Portfolio,
    PortfolioFitReport,
    Position,
    RiskEstimate,
)
from app.portfolio_fit.service import CORRELATION_HORIZONS, PortfolioFitService

__all__ = [
    "CORRELATION_HORIZONS",
    "UNAVAILABLE",
    "AccountSnapshot",
    "CandidateFit",
    "CompanyContext",
    "CompanyContextProvider",
    "Concentration",
    "Exposure",
    "FitConfidence",
    "FitState",
    "Portfolio",
    "PortfolioAccountReader",
    "PortfolioFitReport",
    "PortfolioFitService",
    "Position",
    "RiskEstimate",
    "SnapshotPosition",
    "safe_account_reference",
    "unavailable",
]
