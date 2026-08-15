"""Terminal rendering for Portfolio Fit. Presentation only."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from app.portfolio_fit.schemas import PortfolioFitReport


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render(report: PortfolioFitReport) -> str:
    e, r = report.exposure, report.risk
    out: list[str] = [
        f"TRADABOT PORTFOLIO FIT — {report.portfolio}",
        f"As of: {report.as_of}",
        "",
        f"Equity:   ${e.equity:,.2f}",
        f"Cash:     ${e.cash:,.2f}  ({_pct(e.cash_pct)})",
        f"Invested: ${e.invested:,.2f}  ({_pct(e.invested_pct)})",
        "",
        "CURRENT EXPOSURE",
    ]
    if not report.holdings_detail:
        out.append("  (no positions)")
    for h in report.holdings_detail:
        out.append(
            f"  {h['symbol']:<8}{_pct(h['weight']):>8}   ${h['market_value']:>12,.2f}"
            f"   {h['sector']}"
        )
    out += ["", "SECTORS"]
    for sector, weight in sorted(e.sector_weights.items(), key=lambda kv: -kv[1]):
        out.append(f"  {sector:<24}{_pct(weight):>8}")
    out.append(f"  {'cash':<24}{_pct(e.cash_pct):>8}")
    out += [
        "",
        "CONCENTRATION",
        f"  largest position        {_pct(e.largest_position[1]) if e.largest_position else 'n/a'}",
        f"  top 3                   {_pct(e.top3_pct)}",
        f"  top 5                   {_pct(e.top5_pct)}",
        f"  assessment              {e.concentration}",
        "",
        f"HISTORICAL RISK ({r.basis}, {r.sessions_used} sessions)",
        f"  annualised volatility   {_pct(r.annualised_volatility)}",
        f"  max drawdown            {_pct(r.max_drawdown)}",
        f"  average correlation     "
        f"{'n/a' if r.average_correlation is None else f'{r.average_correlation:.2f}'}",
    ]
    if r.insufficient_reason:
        out.append(f"  note: {r.insufficient_reason}")
    if report.candidate is not None:
        out.extend(_render_candidate(report))
    out += ["", f"PORTFOLIO FIT CONFIDENCE: {report.confidence}"]
    out.extend(f"  reason: {x}" for x in report.confidence_reasons)
    out += ["", f"  {report.disclaimer}"]
    return "\n".join(out)


def _render_candidate(report: PortfolioFitReport) -> list[str]:
    c = report.candidate
    if c is None:
        return []
    out = ["", f"CANDIDATE: {c.symbol}"]
    if c.amount is not None:
        out.append(f"  hypothetical amount     ${c.amount:,.2f}")
    if c.price is not None:
        out.append(f"  price                   ${c.price:,.2f}")
    if c.weighted_average_correlation is not None:
        out.append(
            f"  avg correlation         {c.weighted_average_correlation:.2f}"
        )
    if c.max_correlation:
        out.append(
            f"  most similar holding    {c.max_correlation[0]} "
            f"({c.max_correlation[1]:.2f})"
        )
    if c.min_correlation:
        out.append(
            f"  least similar holding   {c.min_correlation[0]} "
            f"({c.min_correlation[1]:.2f})"
        )
    if c.after is not None:
        out += ["", "  AFTER (hypothetical)"]
        out.append(f"    candidate weight      {_pct(c.after.weights.get(c.symbol))}")
        out.append(
            f"    cash                  {_pct(c.before.cash_pct)} -> "
            f"{_pct(c.after.cash_pct)}"
        )
        out.append(
            f"    top-3 concentration   {_pct(c.before.top3_pct)} -> "
            f"{_pct(c.after.top3_pct)}"
        )
        if c.after_risk is not None:
            out.append(
                f"    volatility            "
                f"{_pct(c.before_risk.annualised_volatility)} -> "
                f"{_pct(c.after_risk.annualised_volatility)}"
            )
    out += ["", f"  FIT: {c.state}"]
    out.extend(f"    - {x}" for x in c.reasons)
    out.append(f"    confidence: {c.confidence}")
    return out


def to_json(report: PortfolioFitReport) -> str:
    payload: dict[str, Any] = asdict(report)
    return json.dumps(payload, indent=1, default=str)
