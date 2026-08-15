"""Read-only Portfolio Fit.

Answers how a company sits inside a specific portfolio -- concentration, sector
exposure, correlation and historical risk. It never answers whether to buy,
sell, rotate or resize: no predictive evidence supports those, and the layer is
structurally unable to reach a broker.
"""

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
    "CandidateFit",
    "Concentration",
    "Exposure",
    "FitConfidence",
    "FitState",
    "Portfolio",
    "PortfolioFitReport",
    "PortfolioFitService",
    "Position",
    "RiskEstimate",
]
