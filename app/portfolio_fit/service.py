"""Portfolio Fit: how a company sits inside a specific portfolio.

Read-only by construction. This module holds no broker handle and cannot place,
cancel or modify anything -- a structural test enforces that.

Everything here is descriptive. Correlation and volatility are historical
description, not forecasts, and no output expresses an action.
"""

from __future__ import annotations

import math
import statistics as st
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from app.portfolio_fit.schemas import (
    CandidateFit,
    Concentration,
    Exposure,
    FitConfidence,
    FitState,
    Portfolio,
    PortfolioFitReport,
    RiskEstimate,
    weakest,
)

# Preregistered correlation horizons. All three are reported so no single
# window can be chosen after the fact to suit a conclusion.
CORRELATION_HORIZONS: tuple[int, ...] = (60, 126, 252)
_PRIMARY_HORIZON = 252
_TRADING_DAYS = 252
_MIN_SESSIONS = 40

# Descriptive concentration bands. Declared, not fitted to any outcome.
_TOP3_MODERATE = 0.50
_TOP3_HIGH = 0.70
_HIGH_CORRELATION = 0.70
_SECTOR_HEAVY = 0.40
_MATERIAL_WEIGHT_SHIFT = 0.05
_LOW_AVERAGE_CORRELATION = 0.30


def _returns(closes: Sequence[float]) -> list[float]:
    return [
        closes[i] / closes[i - 1] - 1
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]


def _correlation(a: Sequence[float], b: Sequence[float]) -> float | None:
    n = min(len(a), len(b))
    if n < _MIN_SESSIONS:
        return None
    x, y = list(a[-n:]), list(b[-n:])
    mx, my = st.mean(x), st.mean(y)
    num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    dx = math.sqrt(sum((v - mx) ** 2 for v in x))
    dy = math.sqrt(sum((v - my) ** 2 for v in y))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


class PortfolioFitService:
    """Builds one descriptive portfolio report, optionally against a candidate."""

    def __init__(
        self,
        prices: Mapping[str, Mapping[str, float]],
        sectors: Mapping[str, str] | None = None,
    ) -> None:
        self._prices = prices
        self._sectors = sectors or {}

    # ------------------------------------------------------------ helpers
    def _series(self, symbol: str, as_of: str, sessions: int) -> list[float]:
        closes = self._prices.get(symbol)
        if not closes:
            return []
        days = sorted(d for d in closes if d <= as_of)
        return [closes[d] for d in days[-(sessions + 1) :]]

    def _price_at(self, symbol: str, as_of: str) -> float | None:
        closes = self._prices.get(symbol)
        if not closes:
            return None
        days = [d for d in closes if d <= as_of]
        return closes[max(days)] if days else None

    # ------------------------------------------------------------ exposure
    def exposure(self, portfolio: Portfolio) -> Exposure:
        equity = portfolio.equity
        if equity <= 0:
            return Exposure(0.0, portfolio.cash, 0.0, 0.0, 0.0)
        weights: dict[str, float] = {}
        for p in portfolio.positions:
            weights[p.symbol] = weights.get(p.symbol, 0.0) + p.market_value / equity
        sectors: dict[str, float] = {}
        for symbol, weight in weights.items():
            sector = self._sectors.get(symbol, "unknown")
            sectors[sector] = sectors.get(sector, 0.0) + weight
        ordered = sorted(weights.items(), key=lambda kv: -kv[1])
        top3 = sum(w for _s, w in ordered[:3])
        top5 = sum(w for _s, w in ordered[:5])
        hhi = sum(w * w for w in weights.values()) if weights else None
        band = Concentration.UNKNOWN
        if weights:
            band = (
                Concentration.HIGH
                if top3 >= _TOP3_HIGH
                else Concentration.MODERATE
                if top3 >= _TOP3_MODERATE
                else Concentration.LOW
            )
        return Exposure(
            equity=equity,
            cash=portfolio.cash,
            invested=portfolio.invested,
            cash_pct=portfolio.cash / equity,
            invested_pct=portfolio.invested / equity,
            weights=weights,
            sector_weights=sectors,
            largest_position=ordered[0] if ordered else None,
            top3_pct=top3,
            top5_pct=top5,
            herfindahl=hhi,
            concentration=band,
        )

    # ------------------------------------------------------------ risk
    def risk(
        self, portfolio: Portfolio, as_of: str, sessions: int = _PRIMARY_HORIZON
    ) -> RiskEstimate:
        equity = portfolio.equity
        if equity <= 0 or not portfolio.positions:
            return RiskEstimate(insufficient_reason="no positions")
        series: dict[str, list[float]] = {}
        for p in portfolio.positions:
            history = _returns(self._series(p.symbol, as_of, sessions))
            if len(history) >= _MIN_SESSIONS:
                series[p.symbol] = history
        if not series:
            return RiskEstimate(insufficient_reason="insufficient price history")
        n = min(len(r) for r in series.values())
        weights = {
            s: sum(p.market_value for p in portfolio.positions if p.symbol == s) / equity
            for s in series
        }
        combined = [
            sum(weights[s] * series[s][-n:][i] for s in series) for i in range(n)
        ]
        vol = st.pstdev(combined) * math.sqrt(_TRADING_DAYS) if n > 1 else None
        equity_curve = [1.0]
        for step in combined:
            equity_curve.append(equity_curve[-1] * (1 + step))
        peak, dd = equity_curve[0], 0.0
        for v in equity_curve:
            peak = max(peak, v)
            dd = min(dd, v / peak - 1)
        pairs = [
            c
            for i, a in enumerate(series)
            for b in list(series)[i + 1 :]
            if (c := _correlation(series[a], series[b])) is not None
        ]
        return RiskEstimate(
            annualised_volatility=vol,
            max_drawdown=dd,
            sessions_used=n,
            average_correlation=st.mean(pairs) if pairs else None,
        )

    # ------------------------------------------------------------ candidate
    def candidate_fit(
        self,
        portfolio: Portfolio,
        symbol: str,
        as_of: str,
        amount: float | None = None,
    ) -> CandidateFit:
        symbol = symbol.upper()
        before = self.exposure(portfolio)
        before_risk = self.risk(portfolio, as_of)
        price = self._price_at(symbol, as_of)
        held = any(p.symbol == symbol for p in portfolio.positions)
        cand = _returns(self._series(symbol, as_of, _PRIMARY_HORIZON))

        correlations: dict[str, float] = {}
        for p in portfolio.positions:
            if p.symbol == symbol:
                continue
            other = _returns(self._series(p.symbol, as_of, _PRIMARY_HORIZON))
            c = _correlation(cand, other)
            if c is not None:
                correlations[p.symbol] = c
        weighted = None
        if correlations and before.equity > 0:
            total = sum(before.weights.get(s, 0.0) for s in correlations)
            if total > 0:
                weighted = sum(
                    before.weights.get(s, 0.0) * c for s, c in correlations.items()
                ) / total
        ordered = sorted(correlations.items(), key=lambda kv: -kv[1])

        after = after_risk = None
        if amount is not None and price is not None and price > 0:
            hypothetical = portfolio.with_added(symbol, amount, price)
            after = self.exposure(hypothetical)
            after_risk = self.risk(hypothetical, as_of)

        state, reasons = self._describe_fit(
            symbol, held, bool(cand), before, after, correlations, weighted
        )

        confidence = FitConfidence.INSUFFICIENT
        if cand and correlations:
            confidence = (
                FitConfidence.HIGH
                if before_risk.sessions_used >= _PRIMARY_HORIZON
                else FitConfidence.MEDIUM
            )
        elif cand:
            confidence = FitConfidence.LOW
        return CandidateFit(
            symbol=symbol,
            amount=amount,
            price=price,
            already_held=held,
            before=before,
            after=after,
            before_risk=before_risk,
            after_risk=after_risk,
            correlations=correlations,
            weighted_average_correlation=weighted,
            max_correlation=ordered[0] if ordered else None,
            min_correlation=ordered[-1] if ordered else None,
            state=state,
            reasons=tuple(reasons),
            confidence=confidence,
        )

    def _describe_fit(
        self,
        symbol: str,
        held: bool,
        has_history: bool,
        before: Exposure,
        after: Exposure | None,
        correlations: Mapping[str, float],
        weighted: float | None,
    ) -> tuple[FitState, tuple[str, ...]]:
        """Name the shape of the change. Never whether to act on it."""
        if not has_history:
            return FitState.INSUFFICIENT, ("no usable price history for the candidate",)
        reasons: list[str] = []
        sector = self._sectors.get(symbol, "unknown")
        sector_before = before.sector_weights.get(sector, 0.0)
        high_corr = sorted(s for s, c in correlations.items() if c >= _HIGH_CORRELATION)
        if held:
            state = FitState.ALREADY_HELD
            reasons.append(
                f"already held at {before.weights.get(symbol, 0.0) * 100:.1f}% of equity"
            )
        elif high_corr:
            state = FitState.HIGH_OVERLAP
            reasons.append("correlated at or above 0.70 with " + ", ".join(high_corr))
        elif sector_before >= _SECTOR_HEAVY:
            state = FitState.INCREASES_CONCENTRATION
            reasons.append(f"{sector} is already {sector_before * 100:.1f}% of equity")
        elif weighted is not None and weighted < _LOW_AVERAGE_CORRELATION:
            state = FitState.IMPROVES_DIVERSIFICATION
            reasons.append(f"average correlation to current holdings is {weighted:.2f}")
        else:
            state = FitState.NEUTRAL
            reasons.append("no material concentration or correlation change")
        if after is not None:
            if abs(after.top3_pct - before.top3_pct) >= _MATERIAL_WEIGHT_SHIFT:
                reasons.append(
                    f"top-3 concentration moves {before.top3_pct * 100:.1f}% to "
                    f"{after.top3_pct * 100:.1f}%"
                )
            sector_after = after.sector_weights.get(sector, 0.0)
            if abs(sector_after - sector_before) >= _MATERIAL_WEIGHT_SHIFT:
                reasons.append(
                    f"{sector} exposure moves {sector_before * 100:.1f}% to "
                    f"{sector_after * 100:.1f}%"
                )
        else:
            reasons.append(
                "no hypothetical amount supplied, so after-weights are not computed"
            )
        return state, tuple(reasons)

    # ------------------------------------------------------------ report
    def analyse(
        self,
        portfolio: Portfolio,
        as_of: str | None = None,
        candidate: str | None = None,
        amount: float | None = None,
    ) -> PortfolioFitReport:
        when = as_of or portfolio.as_of or datetime.now(UTC).date().isoformat()
        exposure = self.exposure(portfolio)
        risk = self.risk(portfolio, when)
        detail = tuple(
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "price": p.price,
                "market_value": p.market_value,
                "weight": exposure.weights.get(p.symbol, 0.0),
                "sector": self._sectors.get(p.symbol, "unknown"),
                "unrealised": p.unrealised,
            }
            for p in sorted(portfolio.positions, key=lambda x: -x.market_value)
        )
        fit = (
            self.candidate_fit(portfolio, candidate, when, amount)
            if candidate
            else None
        )
        reasons: list[str] = []
        history = (
            FitConfidence.HIGH
            if risk.sessions_used >= _PRIMARY_HORIZON
            else FitConfidence.MEDIUM
            if risk.sessions_used >= _MIN_SESSIONS
            else FitConfidence.INSUFFICIENT
        )
        if risk.insufficient_reason:
            reasons.append(risk.insufficient_reason)
        unknown = [s for s in exposure.weights if s not in self._sectors]
        sector_conf = FitConfidence.MEDIUM
        if unknown:
            sector_conf = FitConfidence.LOW
            reasons.append(f"{len(unknown)} holdings have no sector mapping")
        else:
            reasons.append(
                "sector labels are proxy-derived and are not an official classification"
            )
        return PortfolioFitReport(
            portfolio=portfolio.name,
            as_of=when,
            exposure=exposure,
            risk=risk,
            holdings_detail=detail,
            candidate=fit,
            confidence=weakest(history, sector_conf),
            confidence_reasons=tuple(reasons),
        )
