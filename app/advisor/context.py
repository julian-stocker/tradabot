"""Handing Advisor output to Portfolio Fit, unchanged.

This is an adapter and nothing more. It runs the production
:class:`~app.advisor.service.AdvisorService`, reads fields off the report it
produces, and packs them into the compact shape Portfolio Fit displays. No
figure is recomputed on the way through: the trailing-twelve-month sums,
margins, valuation percentile and relative strength are all the Advisor's own
numbers, already validated, and the only thing added here is formatting.

It lives on the Advisor side of the boundary on purpose. Portfolio Fit declares
the protocol it needs; the package that owns the data satisfies it. That keeps
the dependency pointing inward, so the analysis layer stays importable without
a fact store, a database or a network.

Caching
-------
One report per symbol per run. A portfolio of five holdings plus a candidate
would otherwise rebuild the same company report several times over -- once for
the holding detail, once for the candidate view -- and each rebuild re-reads the
same facts to reach the same answer.
"""

from __future__ import annotations

from app.advisor.schemas import AdvisorReport, Confidence, Section, ValuationContext
from app.advisor.service import AdvisorService
from app.core.logging import get_logger
from app.portfolio_fit.context import UNAVAILABLE, CompanyContext

logger = get_logger(__name__)

_WATCHED_METRICS: tuple[str, ...] = (
    "revenue_ttm",
    "eps_ttm",
    "operating_income_ttm",
    "gross_margin",
    "operating_margin",
    "free_cash_flow",
    "market_cap",
    "ps_ttm",
)
"""Figures a consumer may reasonably watch for change. Copied off the report,
never recomputed."""


class AdvisorCompanyContext:
    """A :class:`~app.portfolio_fit.context.CompanyContextProvider` over the Advisor.

    Args:
        service: the production Advisor. Not a copy of it, not a subset.
    """

    def __init__(self, service: AdvisorService) -> None:
        self._service = service
        self._cache: dict[tuple[str, str], CompanyContext] = {}

    def context(self, symbol: str, as_of: str) -> CompanyContext:
        """Company context for one symbol, or an explicit absence.

        Never raises. A symbol the Advisor cannot describe -- no filings, no
        price history, a fact store that is not synced -- yields an unavailable
        context so the portfolio arithmetic around it still runs.
        """
        symbol = symbol.upper()
        key = (symbol, as_of)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        try:
            report = self._service.analyse(symbol, as_of=as_of)
        except Exception as exc:
            logger.warning(
                "advisor context unavailable", symbol=symbol, reason=type(exc).__name__
            )
            result = CompanyContext.missing(symbol, UNAVAILABLE)
        else:
            result = _pack(symbol, report)
        self._cache[key] = result
        return result


def _pack(symbol: str, report: AdvisorReport) -> CompanyContext:
    """Read fields off a finished report. Computes nothing."""
    confidence = report.confidence.get("company_analysis", Confidence.INSUFFICIENT)
    if confidence is Confidence.INSUFFICIENT and _is_bare(report):
        return CompanyContext.missing(symbol, "NO_FUNDAMENTAL_DATA_FOR_SYMBOL")

    labels: dict[str, str] = {}
    for section in report.company_quality:
        for name, value in section.labels.items():
            labels[name] = value

    metrics: dict[str, float | None] = {}
    for section in (*report.company_quality, report.valuation):
        for name in _WATCHED_METRICS:
            metric = section.metrics.get(name)
            if metric is not None and metric.available:
                metrics[name] = metric.value

    valuation = report.valuation
    ps_context = valuation.labels.get("ps_context")
    ps = valuation.metrics.get("ps_ttm")
    return CompanyContext(
        symbol=symbol,
        available=True,
        summary=report.summary,
        valuation_context=(
            ps_context if ps_context != str(ValuationContext.INSUFFICIENT) else None
        ),
        valuation_metric="ps_ttm",
        valuation_value=ps.value if ps is not None else None,
        market_position=_market_line(report.market_position),
        labels=labels,
        metrics=metrics,
        confidence=str(confidence),
    )


def _is_bare(report: AdvisorReport) -> bool:
    """True when no section produced a single usable figure.

    Distinguishes "the Advisor ran and found nothing" from "the Advisor ran and
    is merely unsure", because only the first is an absence of data.
    """
    sections: list[Section] = [
        *report.company_quality,
        report.valuation,
        report.market_position,
    ]
    return not any(m.available for s in sections for m in s.metrics.values())


def _market_line(market: Section) -> str | None:
    """One sentence built from figures the Advisor already computed."""
    rs = market.metrics.get("relative_strength_252d")
    ma = market.metrics.get("distance_from_ma200")
    drawdown = market.metrics.get("drawdown_from_252d_high")
    parts: list[str] = []
    if rs is not None and rs.value is not None:
        direction = "ahead of" if rs.value > 0 else "behind"
        parts.append(f"{abs(rs.value) * 100:.1f}% {direction} the benchmark over a year")
    if ma is not None and ma.value is not None:
        side = "above" if ma.value > 0 else "below"
        parts.append(f"{abs(ma.value) * 100:.1f}% {side} its 200-day average")
    if drawdown is not None and drawdown.value is not None:
        parts.append(f"{abs(drawdown.value) * 100:.1f}% off its 52-week high")
    return "; ".join(parts) if parts else None
