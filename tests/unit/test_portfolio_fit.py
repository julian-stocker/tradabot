"""Portfolio Fit must describe a portfolio, never tell anyone what to do with it.

The arithmetic tests use hand-checkable numbers so a wrong weight is obvious on
inspection rather than only to a fixture.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.portfolio_fit import (
    Concentration,
    FitState,
    Portfolio,
    PortfolioFitService,
    Position,
)
from app.portfolio_fit.schemas import FitConfidence, weakest

PACKAGE = Path("app/portfolio_fit")
FORBIDDEN_IMPORTS = ("app.broker", "app.paper.execution", "alpaca.trading")
FORBIDDEN_TOKENS = ("submit_order", "MarketOrderRequest", "TradingClient", "cancel_order")
ACTION_WORDS = ("BUY", "SELL", "ROTATE", "REPLACE", "REDUCE", "INCREASE",
                "target_weight", "expected_return")


def _sources() -> list[tuple[Path, str]]:
    return [(p, p.read_text()) for p in PACKAGE.glob("*.py")]


class TestCannotTrade:
    def test_no_module_imports_a_broker(self) -> None:
        """**The gate.** Portfolio analysis must not reach an execution path."""
        for path, source in _sources():
            for node in ast.walk(ast.parse(source)):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert not any(name.startswith(f) for f in FORBIDDEN_IMPORTS), (
                        f"{path} imports {name}"
                    )

    def test_no_order_submission_symbols(self) -> None:
        for path, source in _sources():
            body = source.split('"""', 2)[-1]
            for token in FORBIDDEN_TOKENS:
                assert token not in body, f"{path} references {token}"


class TestEmitsNoRecommendation:
    def test_no_action_vocabulary(self) -> None:
        """**The gate.** Describing a change is allowed; prescribing one is not."""
        for path, source in _sources():
            body = source.split('"""', 2)[-1]
            for word in ACTION_WORDS:
                assert not re.search(rf"\b{word}\b", body), f"{path} emits {word}"

    def test_report_carries_a_disclaimer(self) -> None:
        service = PortfolioFitService({}, {})
        report = service.analyse(Portfolio("P", 100.0), as_of="2026-01-02")
        assert "not investment advice" in report.disclaimer
        assert "no buy" in report.disclaimer


class TestExposureArithmetic:
    @staticmethod
    def _portfolio() -> Portfolio:
        # 600 + 300 + 100 invested, 1000 cash -> 2000 equity
        return Portfolio(
            "P",
            cash=1000.0,
            positions=(
                Position("AAA", 6.0, 100.0),
                Position("BBB", 3.0, 100.0),
                Position("CCC", 1.0, 100.0),
            ),
        )

    def test_weights_cash_and_concentration(self) -> None:
        e = PortfolioFitService({}, {"AAA": "tech", "BBB": "tech", "CCC": "energy"}).exposure(
            self._portfolio()
        )
        assert e.equity == pytest.approx(2000.0)
        assert e.cash_pct == pytest.approx(0.5)
        assert e.invested_pct == pytest.approx(0.5)
        assert e.weights["AAA"] == pytest.approx(0.30)
        assert e.weights["BBB"] == pytest.approx(0.15)
        assert e.weights["CCC"] == pytest.approx(0.05)
        assert e.sector_weights["tech"] == pytest.approx(0.45)
        assert e.top3_pct == pytest.approx(0.50)
        assert e.largest_position == ("AAA", pytest.approx(0.30))
        assert e.herfindahl == pytest.approx(0.09 + 0.0225 + 0.0025)
        assert e.concentration is Concentration.MODERATE

    def test_a_single_stock_portfolio_is_high_concentration(self) -> None:
        e = PortfolioFitService({}, {}).exposure(
            Portfolio("S", cash=0.0, positions=(Position("ONLY", 10.0, 100.0),))
        )
        assert e.weights["ONLY"] == pytest.approx(1.0)
        assert e.concentration is Concentration.HIGH
        assert e.cash_pct == pytest.approx(0.0)

    def test_an_all_cash_portfolio_is_not_a_failure(self) -> None:
        e = PortfolioFitService({}, {}).exposure(Portfolio("C", cash=5000.0))
        assert e.cash_pct == pytest.approx(1.0)
        assert e.invested_pct == pytest.approx(0.0)
        assert e.concentration is Concentration.UNKNOWN


class TestHypotheticalAddition:
    def test_whole_shares_and_real_cash_spent(self) -> None:
        """$500 at $120 buys 4 shares for $480, not 4.1667 shares for $500."""
        base = Portfolio("P", cash=1000.0, positions=(Position("AAA", 5.0, 100.0),))
        after = base.with_added("NEW", 500.0, 120.0)
        new = next(p for p in after.positions if p.symbol == "NEW")
        assert new.quantity == pytest.approx(4.0)
        assert after.cash == pytest.approx(520.0)
        assert after.equity == pytest.approx(1500.0)

    def test_adding_to_an_existing_holding_merges(self) -> None:
        base = Portfolio("P", cash=1000.0, positions=(Position("AAA", 5.0, 100.0),))
        after = base.with_added("AAA", 300.0, 100.0)
        assert len([p for p in after.positions if p.symbol == "AAA"]) == 1
        assert after.weight_of("AAA") == pytest.approx(800.0 / 1500.0)

    def test_no_size_is_invented_when_no_amount_is_given(self) -> None:
        prices = {
            "AAA": {f"2026-01-{d:02d}": 100.0 + d for d in range(1, 29)},
            "NEW": {f"2026-01-{d:02d}": 50.0 + d for d in range(1, 29)},
        }
        service = PortfolioFitService(prices, {"AAA": "tech", "NEW": "energy"})
        fit = service.candidate_fit(
            Portfolio("P", 1000.0, (Position("AAA", 5.0, 128.0),)), "NEW", "2026-01-28"
        )
        assert fit.amount is None
        assert fit.after is None
        assert any("no hypothetical amount" in r for r in fit.reasons)


class TestOverlapDetection:
    @staticmethod
    def _prices(n: int = 300) -> dict[str, dict[str, float]]:
        days = [f"2025-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 26)][:n]
        twin_a, twin_b, opposite = {}, {}, {}
        for i, day in enumerate(days):
            move = 1.0 + (0.02 if i % 2 else -0.015)
            twin_a[day] = 100.0 * move ** i
            twin_b[day] = 50.0 * move ** i          # identical shape
            opposite[day] = 80.0 * (1.0 - (0.02 if i % 2 else -0.015)) ** i
        return {"TWIN_A": twin_a, "TWIN_B": twin_b, "OPP": opposite}

    def test_a_correlated_candidate_is_flagged_as_overlap(self) -> None:
        """**The gate.** Four semiconductor names are one risk, not four."""
        prices = self._prices()
        service = PortfolioFitService(prices, dict.fromkeys(prices, "semiconductors"))
        portfolio = Portfolio("P", 100.0, (Position("TWIN_A", 10.0, 100.0),))
        fit = service.candidate_fit(portfolio, "TWIN_B", "2025-12-25")
        assert fit.max_correlation is not None
        assert fit.max_correlation[1] > 0.9
        assert fit.state is FitState.HIGH_OVERLAP

    def test_an_already_held_symbol_is_named_as_such(self) -> None:
        prices = self._prices()
        service = PortfolioFitService(prices, dict.fromkeys(prices, "tech"))
        portfolio = Portfolio(
            "P", 100.0, (Position("TWIN_A", 10.0, 100.0), Position("OPP", 5.0, 80.0))
        )
        fit = service.candidate_fit(portfolio, "TWIN_A", "2025-12-25")
        assert fit.already_held is True
        assert fit.state is FitState.ALREADY_HELD


class TestConfidence:
    def test_confidence_is_the_minimum(self) -> None:
        assert weakest(FitConfidence.HIGH, FitConfidence.LOW) is FitConfidence.LOW
        assert weakest() is FitConfidence.INSUFFICIENT

    def test_missing_history_does_not_fabricate_risk(self) -> None:
        service = PortfolioFitService({}, {})
        risk = service.risk(
            Portfolio("P", 0.0, (Position("X", 1.0, 10.0),)), "2026-01-02"
        )
        assert risk.annualised_volatility is None
        assert risk.insufficient_reason is not None
        assert risk.basis == "HISTORICAL ESTIMATE"
