"""Portfolio Fit output types.

Deliberately separate from the Advisor. A company can be financially strong and
still be a poor fit because the portfolio already carries that exposure; a
mediocre company can reduce concentration without that making it worth owning.
Conflating the two is the mistake this layer exists to avoid.

Nothing here expresses a buy, sell, target weight or expected return: no
predictive evidence supports any of those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Concentration(StrEnum):
    LOW = "LOW_CONCENTRATION"
    MODERATE = "MODERATE_CONCENTRATION"
    HIGH = "HIGH_CONCENTRATION"
    UNKNOWN = "INSUFFICIENT_DATA"


class FitState(StrEnum):
    """How a hypothetical addition changes the portfolio's shape. Not advice."""

    IMPROVES_DIVERSIFICATION = "IMPROVES_DIVERSIFICATION"
    NEUTRAL = "NEUTRAL_FIT"
    INCREASES_CONCENTRATION = "INCREASES_CONCENTRATION"
    HIGH_OVERLAP = "HIGH_OVERLAP"
    ALREADY_HELD = "ALREADY_HELD"
    INSUFFICIENT = "INSUFFICIENT_DATA"


class FitConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT = "INSUFFICIENT"


_ORDER: dict[str, int] = {"INSUFFICIENT": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


def weakest(*levels: FitConfidence) -> FitConfidence:
    """Confidence is the minimum of its inputs, never the mean."""
    present = [x for x in levels if x is not None]
    if not present:
        return FitConfidence.INSUFFICIENT
    return min(present, key=lambda c: _ORDER[str(c)])


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: float
    price: float
    cost_basis: float | None = None

    @property
    def market_value(self) -> float:
        return self.quantity * self.price

    @property
    def unrealised(self) -> float | None:
        if self.cost_basis is None:
            return None
        return self.market_value - self.cost_basis


@dataclass(frozen=True, slots=True)
class Portfolio:
    """A portfolio snapshot. Hypothetical portfolios need no broker at all."""

    name: str
    cash: float
    positions: tuple[Position, ...] = ()
    as_of: str | None = None

    @property
    def invested(self) -> float:
        return sum(p.market_value for p in self.positions)

    @property
    def equity(self) -> float:
        return self.cash + self.invested

    def weight_of(self, symbol: str) -> float:
        if self.equity <= 0:
            return 0.0
        return sum(
            p.market_value for p in self.positions if p.symbol == symbol
        ) / self.equity

    def with_added(self, symbol: str, amount: float, price: float) -> Portfolio:
        """A hypothetical portfolio after spending ``amount`` on ``symbol``.

        Whole shares only, matching how the accounts actually trade, and cash is
        reduced by what is really spent rather than by the requested amount.
        """
        if price <= 0:
            return self
        quantity = float(int(amount / price))
        spent = quantity * price
        merged: list[Position] = []
        added = False
        for p in self.positions:
            if p.symbol == symbol:
                merged.append(
                    Position(symbol, p.quantity + quantity, price, p.cost_basis)
                )
                added = True
            else:
                merged.append(p)
        if not added and quantity > 0:
            merged.append(Position(symbol, quantity, price, spent))
        return Portfolio(self.name, self.cash - spent, tuple(merged), self.as_of)


@dataclass(frozen=True, slots=True)
class Exposure:
    equity: float
    cash: float
    invested: float
    cash_pct: float
    invested_pct: float
    weights: dict[str, float] = field(default_factory=dict)
    sector_weights: dict[str, float] = field(default_factory=dict)
    largest_position: tuple[str, float] | None = None
    top3_pct: float = 0.0
    top5_pct: float = 0.0
    herfindahl: float | None = None
    concentration: Concentration = Concentration.UNKNOWN
    sector_confidence: str = "PROXY_DERIVED"


@dataclass(frozen=True, slots=True)
class RiskEstimate:
    """Historical description. Never a forecast."""

    basis: str = "HISTORICAL ESTIMATE"
    annualised_volatility: float | None = None
    max_drawdown: float | None = None
    sessions_used: int = 0
    average_correlation: float | None = None
    insufficient_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateFit:
    symbol: str
    amount: float | None
    price: float | None
    already_held: bool
    before: Exposure
    after: Exposure | None
    before_risk: RiskEstimate
    after_risk: RiskEstimate | None
    correlations: dict[str, float] = field(default_factory=dict)
    weighted_average_correlation: float | None = None
    max_correlation: tuple[str, float] | None = None
    min_correlation: tuple[str, float] | None = None
    state: FitState = FitState.INSUFFICIENT
    reasons: tuple[str, ...] = ()
    confidence: FitConfidence = FitConfidence.INSUFFICIENT


@dataclass(frozen=True, slots=True)
class PortfolioFitReport:
    portfolio: str
    as_of: str
    exposure: Exposure
    risk: RiskEstimate
    holdings_detail: tuple[dict[str, Any], ...] = ()
    candidate: CandidateFit | None = None
    confidence: FitConfidence = FitConfidence.INSUFFICIENT
    confidence_reasons: tuple[str, ...] = ()
    disclaimer: str = (
        "Portfolio analysis only. This is not investment advice, contains no buy "
        "or sell recommendation, and makes no claim about future returns."
    )
