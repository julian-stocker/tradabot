"""The read-only Advisor: factual company analysis, never a recommendation.

Deliberately has no broker, order or execution dependency. A structural test
asserts that this package cannot reach order submission.
"""

from __future__ import annotations

import statistics as st
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from typing import Any, Final

from app.advisor.facts import FactStore, ShareFamily
from app.advisor.schemas import (
    AdvisorReport,
    Confidence,
    DataSupport,
    DenominatorBasis,
    Metric,
    Section,
    ValuationContext,
    weakest,
)

_FINANCIAL_SECTORS = frozenset({"financials"})
_SPLIT_TOLERANCE = 0.35
_MIN_HISTORY_OBSERVATIONS = 24
_STALE_FILING_DAYS = 200
_HIGH_VOLATILITY = 0.03
_DEEP_DRAWDOWN = -0.20
_SECTOR_REFUSAL = "SECTOR_SPECIFIC_MODEL_REQUIRED"
# Predeclared dilution thresholds, expressed as an annualised share-count change.
# Chosen for interpretability, never fitted to returns.
_BUYBACK_THRESHOLD = -0.01
_STABLE_THRESHOLD = 0.01
_MATERIAL_DILUTION = 0.05
_SPLIT_IMPLIED_CHANGE = 0.35
_DILUTION_YEARS = 3
_MIN_SHARE_OBSERVATIONS = 2
# A fiscal year is not a calendar year. Comparability is enforced by the gap
# between period ends, so a company with a January year-end is handled the same
# as a December one, and two observations six months apart are never called YoY.
_FISCAL_YEAR_MIN_DAYS = 330
_FISCAL_YEAR_MAX_DAYS = 400
_P10, _P25, _P75, _P90 = 0.10, 0.25, 0.75, 0.90
_DECLINE_THRESHOLD = -0.02
_GROWTH_THRESHOLD = 0.10
_SLOWDOWN_STEP = 0.03
_MIN_MARKET_SESSIONS = 220
_YEAR_SESSIONS = 252
_MA_SESSIONS = 200


@dataclass(frozen=True, slots=True)
class PriceSeries:
    """Split-adjusted closes keyed by ISO date, for one symbol."""

    closes: Mapping[str, float]

    def upto(self, as_of: str) -> list[tuple[str, float]]:
        return sorted((d, c) for d, c in self.closes.items() if d <= as_of)


@dataclass(frozen=True, slots=True)
class MarketIdentity:
    """Which price series a report may read, and what it may be compared to.

    A report is about one **listing**, so its market data belongs to that
    listing and to nothing else. Before this existed the Advisor was fed a bare
    ticker and looked the price up by it, which meant a Xetra line reached the
    US ADR's series: ``SAP.DE`` carried a USD price, a SPY-relative return and
    ``HIGH`` confidence, and the only thing standing between that and a reader
    was the renderer choosing not to print it. Worse, ``CNR.TO`` -- Canadian
    National -- reached the series of ``CNR``, *Core Natural Resources*, and
    produced an internally consistent valuation of a different company.

    So absence is represented, not worked around. ``series`` is ``None`` when
    the listing has no prices of its own, and ``benchmark`` is ``None`` when no
    benchmark has been validated for its market. Neither is ever filled in from
    a bare ticker, another venue, an ADR or a primary line: the report simply
    does without the fields those inputs would have produced.
    """

    series: str | None
    """Key into the price map, or ``None`` when this listing has no prices."""
    benchmark: str | None = None
    """Key of the validated benchmark for this listing's market, or ``None``."""
    unit_mismatch: str | None = None
    """Why this listing's price may not be divided into its company's reported
    figures. Currency, today: Novo Nordisk trades in USD and reports in DKK, so
    price over earnings is 1.99x -- a number that looks like a bargain and is a
    unit error. ``None`` when price and filings share a currency."""

    @property
    def comparable(self) -> bool:
        """Whether a relative-strength reading is meaningful at all.

        False without a benchmark, and false when the listing *is* the
        benchmark: a security's excess return over itself is zero by
        construction, and printing ``+0.0%`` invites a reader to interpret an
        identity as a finding.
        """
        return (
            self.series is not None and self.benchmark is not None and self.series != self.benchmark
        )


UNPRICED: Final[MarketIdentity] = MarketIdentity(series=None, benchmark=None)
"""A listing Tradabot holds no market data for. Every market-derived field on
its report is unavailable, with a stated reason."""


def _no_price_reason(market: MarketIdentity) -> str:
    """Why a price-derived field is absent, distinguishing the two causes.

    "Tradabot holds no series for this listing" and "the series exists and is
    empty at this date" are different states, and a report that flattened them
    would leave a reader unable to tell an unsupported venue from a stale sync.
    """
    if market.series is None:
        return "no market data for this listing; prices are never taken from another"
    return "no price history"


def _metric(name: str, result: Any, reason: str | None = None) -> Metric:
    if result is None or result.value is None:
        return Metric(name, None, DenominatorBasis.UNAVAILABLE, reason or "not reported")
    return Metric(name, float(result.value), result.basis, None, result.provenance)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _growth_label(current: float | None, prior: float | None) -> str:
    if current is None or prior is None:
        return "INSUFFICIENT_DATA"
    if current < _DECLINE_THRESHOLD:
        return "DECLINING"
    if current >= _GROWTH_THRESHOLD:
        return "GROWTH"
    if current > 0 and current < prior - _SLOWDOWN_STEP:
        return "SLOWING"
    return "STABLE"


def _context(percentile: float | None) -> ValuationContext:
    if percentile is None:
        return ValuationContext.INSUFFICIENT
    if percentile <= _P10:
        return ValuationContext.VERY_LOW
    if percentile <= _P25:
        return ValuationContext.LOW
    if percentile <= _P75:
        return ValuationContext.NORMAL
    if percentile <= _P90:
        return ValuationContext.HIGH
    return ValuationContext.VERY_HIGH


class AdvisorService:
    """Builds one factual report for one symbol, optionally as of a past date."""

    def __init__(
        self,
        facts: FactStore,
        prices: Mapping[str, PriceSeries],
        sectors: Mapping[str, str] | None = None,
        benchmark: str = "SPY",
        company_sectors: Mapping[str, str] | None = None,
    ) -> None:
        self._facts = facts
        self._prices = prices
        self._sectors = sectors or {}
        self._benchmark = benchmark
        self._company_sectors = company_sectors or {}
        """Sector by company key, derived from the SEC's SIC classification.

        A **fallback**, consulted only where the curated map is silent. SIC and
        the curated map disagree on four of the fifty-two -- it files Visa and
        Mastercard as business services and UnitedHealth as a medical service
        plan -- and letting a derived signal overrule a human judgement would
        change validated results as a side effect of adding coverage.

        What it does fix is the silence: a hundred and twenty-two US listings
        and every foreign issuer had no classification at all, so Wells Fargo
        was read as a manufacturer with an acceptable amount of debt."""

    def analyse(
        self,
        symbol: str,
        as_of: str | None = None,
        company_key: str | None = None,
        market: MarketIdentity | None = None,
    ) -> AdvisorReport:
        """One factual report.

        ``symbol`` names the listing being reported on. ``company_key`` names
        the *reporting entity* whose filings are used, and ``market`` names the
        *price series* the report may read and the benchmark it may compare to.
        All three are separate because they vary independently: two listings of
        one issuer share filings and must not share prices, and a listing may
        have filings without having prices at all.

        ``market`` defaults to :class:`MarketIdentity` over ``symbol`` with this
        service's configured benchmark, which is what a caller holding only a
        bare ticker means -- the US line that ticker names. A caller that knows
        about listings passes one explicitly, and passes :data:`UNPRICED` when
        the listing has no prices, rather than letting the symbol stand in.
        """
        symbol = symbol.upper()
        facts_key = company_key or symbol
        identity = market if market is not None else MarketIdentity(symbol, self._benchmark)
        when = as_of or datetime.now(UTC).date().isoformat()
        # Curated first, derived second, unknown last.
        sector = self._sectors.get(symbol) or self._company_sectors.get(facts_key, "unknown")
        financial = sector in _FINANCIAL_SECTORS

        quality, qconf = self._company_quality(symbol, when, financial, facts_key)
        valuation = self._valuation(facts_key, when, identity)
        position = self._market_position(when, identity)
        risks = self._risks(quality, valuation, position, financial)
        # Two distinct readings, neither of which inflates the other.
        #
        # `company_analysis` covers the four sections a factual company view
        # actually rests on. Dilution is a Capital Structure concern: not knowing
        # it leaves that section unusable, but it does not make revenue, margins
        # or the balance sheet less trustworthy.
        #
        # `overall` remains the strict minimum across every section, capital
        # structure included, so the conservative number is never weakened.
        core = [s.confidence for s in quality if s.name != "CAPITAL STRUCTURE"]
        confidence = {
            "company_quality": qconf,
            "company_analysis": weakest(*core, valuation.confidence, position.confidence),
            "valuation": valuation.confidence,
            "market_position": position.confidence,
        }
        confidence["overall"] = weakest(qconf, valuation.confidence, position.confidence)
        return AdvisorReport(
            symbol=symbol,
            as_of=when,
            profile=self._profile(symbol, when, sector, valuation, identity),
            company_quality=quality,
            valuation=valuation,
            market_position=position,
            risks=risks,
            horizon_data_support=self._horizons(),
            summary=self._summary(symbol, quality, valuation, position),
            confidence=confidence,
        )

    # ---------------------------------------------------------------- sections
    def _series(self, market: MarketIdentity) -> PriceSeries | None:
        """The one price series this report is allowed to read.

        The single place a :class:`MarketIdentity` becomes prices. An unpriced
        listing returns ``None`` here and every market-derived field downstream
        is absent as a consequence, rather than each of them having to remember
        not to fall back to the bare ticker.
        """
        return self._prices.get(market.series) if market.series is not None else None

    def _profile(
        self,
        symbol: str,
        when: str,
        sector: str,
        valuation: Section,
        market: MarketIdentity,
    ) -> Section:
        series = self._series(market)
        history = series.upto(when) if series else []
        mcap = valuation.metrics.get("market_cap")
        return Section(
            name="COMPANY PROFILE",
            metrics={
                "price": Metric(
                    "price",
                    history[-1][1] if history else None,
                    unavailable_reason=None if history else _no_price_reason(market),
                ),
                "market_cap": mcap or Metric("market_cap", None),
            },
            labels={
                "symbol": symbol,
                "sector_proxy": sector,
                "price_history_sessions": str(len(history)),
                "industry": "unavailable",
            },
            confidence=Confidence.HIGH if history else Confidence.INSUFFICIENT,
            notes=("sector is a correlation-derived proxy, not an official classification",),
        )

    def _company_quality(
        self, symbol: str, when: str, financial: bool, facts_key: str | None = None
    ) -> tuple[tuple[Section, ...], Confidence]:
        f = self._facts
        # Facts are read by company identity; `symbol` continues to name the
        # listing whose prices are used elsewhere in the report.
        key = facts_key or symbol
        rev, eps = f.ttm(key, "revenue", when), f.ttm(key, "eps_diluted", when)
        oi = f.ttm(key, "operating_income", when)
        ocf, capex = f.ttm(key, "operating_cash_flow", when), f.ttm(key, "capex", when)
        gp = f.ttm(key, "gross_profit", when)
        cash, ltd = f.instant(key, "cash", when), f.instant(key, "long_term_debt", when)
        std, equity = (
            f.instant(key, "short_term_debt", when),
            f.instant(key, "equity", when),
        )
        shares = f.instant(key, "shares_outstanding", when)

        fcf = None
        if ocf.value is not None and capex.value is not None:
            fcf = ocf.value - capex.value

        growth = Section(
            name="GROWTH",
            metrics={
                "revenue_ttm": _metric("revenue_ttm", rev),
                "eps_ttm": _metric("eps_ttm", eps),
                "operating_income_ttm": _metric("operating_income_ttm", oi),
            },
            labels={"revenue_basis": str(rev.basis)},
            confidence=Confidence.HIGH if rev.value is not None else Confidence.INSUFFICIENT,
        )
        margin_reason = _SECTOR_REFUSAL if financial else None
        profitability = Section(
            name="PROFITABILITY",
            metrics={
                "gross_margin": Metric(
                    "gross_margin",
                    None if financial else _ratio(gp.value, rev.value),
                    unavailable_reason=margin_reason,
                ),
                "operating_margin": Metric(
                    "operating_margin",
                    None if financial else _ratio(oi.value, rev.value),
                    unavailable_reason=margin_reason,
                ),
            },
            confidence=Confidence.INSUFFICIENT
            if financial
            else (Confidence.HIGH if oi.value is not None else Confidence.LOW),
            notes=(
                ("bank and insurer margins are not comparable to industrial companies",)
                if financial
                else ()
            ),
        )
        cash_section = Section(
            name="CASH GENERATION",
            metrics={
                "operating_cash_flow_ttm": _metric("operating_cash_flow_ttm", ocf),
                "capex_ttm": _metric("capex_ttm", capex),
                "free_cash_flow": Metric(
                    "free_cash_flow",
                    None if financial else fcf,
                    ocf.basis,
                    _SECTOR_REFUSAL if financial else None,
                ),
                "fcf_margin": Metric(
                    "fcf_margin",
                    None if financial else _ratio(fcf, rev.value),
                    ocf.basis,
                    _SECTOR_REFUSAL if financial else None,
                ),
            },
            labels={"denominator_basis": str(ocf.basis)},
            confidence=Confidence.INSUFFICIENT
            if financial or fcf is None
            else (
                Confidence.MEDIUM if ocf.basis is DenominatorBasis.FY_FALLBACK else Confidence.HIGH
            ),
            confidence_reasons=(
                ("free cash flow uses the annual figure, not a true TTM",)
                if ocf.basis is DenominatorBasis.FY_FALLBACK
                else ()
            ),
        )
        debt = None
        if ltd.value is not None or std.value is not None:
            debt = (ltd.value or 0.0) + (std.value or 0.0)
        net = None if (cash.value is None or debt is None) else cash.value - debt
        balance_label = "INSUFFICIENT_DATA"
        if financial:
            balance_label = _SECTOR_REFUSAL
        elif net is not None:
            if net > 0:
                balance_label = "NET_CASH"
            elif ocf.value and abs(net) <= 2 * ocf.value:
                balance_label = "ACCEPTABLE"
            else:
                balance_label = "LEVERAGED"
        balance = Section(
            name="BALANCE SHEET",
            metrics={
                "cash": _metric("cash", cash),
                "total_debt": Metric("total_debt", debt),
                "net_cash_or_debt": Metric("net_cash_or_debt", net),
                "equity": _metric("equity", equity),
            },
            labels={"assessment": balance_label},
            confidence=Confidence.INSUFFICIENT if net is None or financial else Confidence.HIGH,
        )
        capital = self._capital_structure(symbol, when, shares, key)
        sections = (growth, profitability, cash_section, balance, capital)
        overall = weakest(*(s.confidence for s in sections))
        return sections, overall

    def _capital_structure(
        self, symbol: str, when: str, shares: Any, facts_key: str | None = None
    ) -> Section:
        """Dilution from period-end share counts, refusing when a split is implied.

        Period-end shares outstanding -- not weighted-average diluted shares --
        answer "how many claims exist on this business now". The weighted
        average is an averaging artefact of the reporting period and would
        understate a buyback that happened late in the year.

        SEC share counts are as-reported and NOT split-adjusted, so a 4:1 split
        looks like 300% dilution. Any implied split withholds the judgement.
        """
        history = self._share_history(symbol, when, facts_key)
        metrics = {"shares_outstanding": _metric("shares_outstanding", shares)}
        if len(history) < _MIN_SHARE_OBSERVATIONS:
            return Section(
                name="CAPITAL STRUCTURE",
                metrics=metrics,
                labels={
                    "dilution": "INSUFFICIENT_DATA",
                    "share_family": str(ShareFamily.PERIOD_END),
                },
                confidence=Confidence.INSUFFICIENT,
                confidence_reasons=(
                    "no comparable fiscal-year-end PERIOD_END share series; "
                    "cover-page and weighted-average shares are not substituted",
                ),
            )
        # Only the window actually used may veto the reading. A split years
        # before the comparison period is real history, not evidence about the
        # current share count, and letting it refuse forever made AAPL, KO,
        # NVDA and TSLA permanently unreadable.
        window = history[-(_DILUTION_YEARS + 1) :]
        split_implied = any(
            abs(later / earlier - 1) > _SPLIT_IMPLIED_CHANGE
            for (_de, earlier), (_dl, later) in pairwise(window)
            if earlier > 0
        )
        if split_implied:
            return Section(
                name="CAPITAL STRUCTURE",
                metrics=metrics,
                labels={
                    "dilution": "SPLIT_ADJUSTMENT_REQUIRED",
                    "share_family": str(ShareFamily.PERIOD_END),
                },
                confidence=Confidence.INSUFFICIENT,
                confidence_reasons=(
                    "share count changed by more than 35% year over year, implying a "
                    "stock split; as-reported counts are not split-adjusted",
                ),
                notes=("a split is not dilution and a reverse split is not a buyback",),
            )
        # A share count that is byte-identical across several fiscal years is not
        # a real outstanding-share series -- it is a repeated comparative or a
        # mis-tagged constant. Boeing filed 1,012,261,159 for 2020 through 2025
        # while actually issuing heavily, which would otherwise render as
        # "STABLE +0.00%" at HIGH confidence: a confidently wrong answer.
        if len({value for _end, value in window}) == 1:
            return Section(
                name="CAPITAL STRUCTURE",
                metrics=metrics,
                labels={
                    "dilution": "INSUFFICIENT_DATA",
                    "share_family": str(ShareFamily.PERIOD_END),
                },
                confidence=Confidence.INSUFFICIENT,
                confidence_reasons=(
                    "period-end share count is identical across every fiscal year in "
                    "the comparison window, which no real share series does",
                ),
            )
        yoy = window[-1][1] / window[-2][1] - 1 if window[-2][1] > 0 else None
        span = min(len(window) - 1, _DILUTION_YEARS)
        annualised = None
        if span >= 1 and window[-1 - span][1] > 0:
            annualised = (window[-1][1] / window[-1 - span][1]) ** (1 / span) - 1
        metrics["share_count_yoy"] = Metric("share_count_yoy", yoy)
        metrics[f"share_count_cagr_{span}y"] = Metric(f"share_count_cagr_{span}y", annualised)
        label = "INSUFFICIENT_DATA"
        if annualised is not None:
            if annualised < _BUYBACK_THRESHOLD:
                label = "BUYBACK_REDUCING_SHARE_COUNT"
            elif annualised <= _STABLE_THRESHOLD:
                label = "STABLE"
            elif annualised <= _MATERIAL_DILUTION:
                label = "DILUTING"
            else:
                label = "MATERIAL_DILUTION"
        confidence = Confidence.INSUFFICIENT
        reasons: tuple[str, ...] = ()
        if annualised is not None:
            if len(window) - 1 >= _DILUTION_YEARS:
                confidence = Confidence.HIGH
            else:
                confidence = Confidence.MEDIUM
                reasons = (f"only {len(window)} annual share observations",)
        return Section(
            name="CAPITAL STRUCTURE",
            metrics=metrics,
            labels={
                "dilution": label,
                "share_family": str(ShareFamily.PERIOD_END),
                "observations_in_window": str(len(window)),
                "total_observations": str(len(history)),
            },
            confidence=confidence,
            confidence_reasons=reasons,
        )

    def _share_history(
        self, symbol: str, when: str, facts_key: str | None = None
    ) -> list[tuple[str, float]]:
        """A fiscal-year chain of PERIOD_END share counts, oldest first.

        Built by walking back from the newest observation and taking the one
        roughly a fiscal year earlier each time. Observations that are not about
        a year apart are skipped rather than compared, which is precisely what
        the calendar-year bucketing used to get wrong.
        """
        series = self._facts.share_series(facts_key or symbol, when, ShareFamily.PERIOD_END)
        if not series:
            return []
        chain: list[tuple[str, float]] = [(series[-1][0], series[-1][1])]
        anchor = date.fromisoformat(series[-1][0])
        for period_end, value, _prov in reversed(series[:-1]):
            gap = (anchor - date.fromisoformat(period_end)).days
            if _FISCAL_YEAR_MIN_DAYS <= gap <= _FISCAL_YEAR_MAX_DAYS:
                chain.append((period_end, value))
                anchor = date.fromisoformat(period_end)
        return list(reversed(chain))

    def _valuation(self, key: str, when: str, market: MarketIdentity) -> Section:
        """Price over fundamentals, where both belong to the thing being reported.

        ``key`` is the company; ``market`` is the listing. Both halves used to
        be read by the bare ticker, so ``CNR.TO`` -- Canadian National -- was
        valued at Core Natural Resources' price against Core Natural Resources'
        revenue, inside a report whose growth section was Canadian National's.
        Every figure was reproducible and the company was the wrong one.
        """
        f = self._facts
        series = self._series(market)
        history = series.upto(when) if series else []
        price = history[-1][1] if history else None
        shares = f.instant(key, "shares_outstanding", when)
        rev = f.ttm(key, "revenue", when)
        eps = f.ttm(key, "eps_diluted", when)
        ocf, capex = f.ttm(key, "operating_cash_flow", when), f.ttm(key, "capex", when)
        fcf = ocf.value - capex.value if ocf.value is not None and capex.value is not None else None
        split_suspect = self._split_suspect(key, when)
        reasons: list[str] = []
        if price is None:
            reasons.append(_no_price_reason(market))
        if split_suspect:
            reasons.append(
                "as-reported share count implies a stock split; per-share valuation withheld"
            )
        mcap = (
            price * shares.value
            if price is not None and shares.value is not None and not split_suspect
            else None
        )
        # A market capitalisation is a price times a share count, so it stays in
        # the price's own currency and survives a mismatch. Every ratio below
        # divides that price by a reported figure, and those do not.
        mixed = market.unit_mismatch
        if mixed is not None:
            reasons.append(mixed)
        pe = _ratio(price, eps.value) if not split_suspect and mixed is None else None
        ps = _ratio(mcap, rev.value) if mixed is None else None
        pfcf = _ratio(mcap, fcf) if mixed is None else None
        percentile = self._ps_percentile(key, when, ps, market)
        if percentile is None and price is not None and mixed is None:
            reasons.append("insufficient valuation history for a percentile")
        if ocf.basis is DenominatorBasis.FY_FALLBACK:
            reasons.append("cash-flow denominator is annual, not TTM")
        confidence = Confidence.INSUFFICIENT
        if mcap is not None and mixed is None:
            confidence = Confidence.MEDIUM if percentile is None else Confidence.HIGH
        return Section(
            name="VALUATION",
            metrics={
                "market_cap": Metric("market_cap", mcap),
                "pe_ttm": Metric("pe_ttm", pe, eps.basis, mixed),
                "ps_ttm": Metric("ps_ttm", ps, rev.basis, mixed),
                "p_fcf": Metric("p_fcf", pfcf, ocf.basis, mixed),
                "earnings_yield": Metric(
                    "earnings_yield",
                    _ratio(eps.value, price) if mixed is None else None,
                    unavailable_reason=mixed,
                ),
                "fcf_yield": Metric(
                    "fcf_yield",
                    _ratio(fcf, mcap) if mixed is None else None,
                    unavailable_reason=mixed,
                ),
                "ps_percentile_own_history": Metric(
                    "ps_percentile_own_history", percentile, unavailable_reason=mixed
                ),
            },
            labels={"ps_context": str(_context(percentile))},
            confidence=confidence,
            confidence_reasons=tuple(reasons),
            notes=(
                "percentiles use an expanding window of prior observations only; "
                "no future information is used",
            ),
        )

    def _split_suspect(self, key: str, when: str) -> bool:
        quarters, _prov = self._facts.quarterlies(key, "eps_diluted", when)
        counts = self._facts.instant(key, "shares_outstanding", when)
        if counts.value is None or not quarters:
            return False
        return False

    def _ps_percentile(
        self,
        key: str,
        when: str,
        current: float | None,
        market: MarketIdentity,
    ) -> float | None:
        if current is None or current <= 0:
            return None
        series = self._series(market)
        if series is None:
            return None
        history = series.upto(when)
        by_month: dict[str, tuple[str, float]] = {}
        for day, close in history:
            by_month[day[:7]] = (day, close)
        observations: list[float] = []
        for day, close in sorted(by_month.values()):
            if day >= when:
                continue
            shares = self._facts.instant(key, "shares_outstanding", day)
            revenue = self._facts.ttm(key, "revenue", day)
            value = _ratio(
                close * shares.value if shares.value is not None else None, revenue.value
            )
            if value is not None and value > 0:
                observations.append(value)
        if len(observations) < _MIN_HISTORY_OBSERVATIONS:
            return None
        return sum(1 for v in observations if v <= current) / len(observations)

    def _relative_strength(
        self, own: float | None, when: str, market: MarketIdentity
    ) -> tuple[float | None, str | None]:
        """One year of excess return over a validated benchmark, or why not.

        Three distinct absences, kept distinct because they call for different
        wording on the card: no benchmark exists for this market, the listing
        *is* the benchmark, or the benchmark itself is too short to compare to.
        """
        if market.benchmark is None:
            return None, (
                "no benchmark has been validated for this listing's market; "
                "SPY was validated against US listings and means nothing here"
            )
        if market.series == market.benchmark:
            return None, (
                "this listing is the benchmark; its excess return over itself "
                "is zero by construction"
            )
        bench = self._prices.get(market.benchmark)
        bench_closes = [c for _d, c in bench.upto(when)] if bench else []
        reference = (
            bench_closes[-1] / bench_closes[-_YEAR_SESSIONS - 1] - 1
            if len(bench_closes) > _YEAR_SESSIONS
            else None
        )
        if own is None or reference is None:
            return None, "not enough benchmark history for a one-year comparison"
        return own - reference, None

    def _market_position(self, when: str, market: MarketIdentity) -> Section:
        """How the *listing* has traded. Never how some other listing traded.

        Both inputs are named by :class:`MarketIdentity`: the series is the
        listing's own, and the benchmark is one validated for the listing's
        market. Outside the US no benchmark has been validated, so relative
        strength is absent with a reason rather than silently measured against
        SPY -- which would render as an ordinary percentage while comparing a
        different currency and a different trading day.
        """
        series = self._series(market)
        history = series.upto(when) if series else []
        if len(history) < _MIN_MARKET_SESSIONS:
            return Section(
                name="MARKET POSITION",
                confidence=Confidence.INSUFFICIENT,
                confidence_reasons=(
                    _no_price_reason(market)
                    if market.series is None or not history
                    else "fewer than 220 sessions of price history",
                ),
            )
        closes = [c for _d, c in history]

        def ret(seq: list[float], span: int) -> float | None:
            return seq[-1] / seq[-span - 1] - 1 if len(seq) > span else None

        own = ret(closes, _YEAR_SESSIONS)
        rs, rs_reason = self._relative_strength(own, when, market)
        ma200 = st.mean(closes[-_MA_SESSIONS:])
        peak = max(closes[-_YEAR_SESSIONS:]) if len(closes) >= _YEAR_SESSIONS else max(closes)
        return Section(
            name="MARKET POSITION",
            metrics={
                "return_20d": Metric("return_20d", ret(closes, 20)),
                "return_60d": Metric("return_60d", ret(closes, 60)),
                "return_252d": Metric("return_252d", own),
                "relative_strength_252d": Metric(
                    "relative_strength_252d", rs, unavailable_reason=rs_reason
                ),
                "distance_from_ma200": Metric("distance_from_ma200", closes[-1] / ma200 - 1),
                "drawdown_from_252d_high": Metric("drawdown_from_252d_high", closes[-1] / peak - 1),
            },
            confidence=Confidence.HIGH,
            notes=("describes the STOCK, not the company",),
        )

    def _risks(
        self,
        quality: Sequence[Section],
        valuation: Section,
        market: Section,
        financial: bool,
    ) -> dict[str, tuple[str, ...]]:
        business: list[str] = []
        valuation_risks: list[str] = []
        market_risks: list[str] = []
        data: list[str] = []
        by_name = {s.name: s for s in quality}
        balance = by_name.get("BALANCE SHEET")
        if balance is not None and balance.labels.get("assessment") == "LEVERAGED":
            business.append("net debt exceeds twice operating cash flow")
        cash_section = by_name.get("CASH GENERATION")
        if cash_section is not None:
            fcf = cash_section.metrics.get("free_cash_flow")
            if fcf is not None and fcf.available and fcf.value is not None and fcf.value < 0:
                business.append("free cash flow is negative")
            if cash_section.labels.get("denominator_basis") == str(DenominatorBasis.FY_FALLBACK):
                data.append("cash-flow figures use the annual filing, not a true TTM")
        context = valuation.labels.get("ps_context", "")
        if context in {str(ValuationContext.HIGH), str(ValuationContext.VERY_HIGH)}:
            valuation_risks.append(f"price to sales is {context.lower().replace('_', ' ')}")
        if context == str(ValuationContext.INSUFFICIENT):
            data.append("not enough valuation history for context")
        pe = valuation.metrics.get("pe_ttm")
        if pe is not None and not pe.available:
            valuation_risks.append("price/earnings undefined (no positive TTM earnings)")
        for key, message in (
            ("drawdown_from_252d_high", "trading well below its 52-week high"),
            ("distance_from_ma200", "trading below its 200-day average"),
        ):
            metric = market.metrics.get(key)
            if metric is None or metric.value is None:
                continue
            threshold = _DEEP_DRAWDOWN if key == "drawdown_from_252d_high" else 0.0
            if metric.value < threshold:
                market_risks.append(message)
        if financial:
            data.append(
                "financial-sector company: generic margin, cash-flow and leverage "
                "analysis is refused pending a sector-specific model"
            )
        if market.confidence is Confidence.INSUFFICIENT:
            data.append("insufficient price history for market position")
        return {
            "business": tuple(business),
            "valuation": tuple(valuation_risks),
            "market": tuple(market_risks),
            "data": tuple(data),
        }

    def _horizons(self) -> dict[str, dict[str, Any]]:
        return {
            "1W": {
                "data_support": str(DataSupport.WEAK),
                "reason": "short-horizon predictive research failed confirmation",
            },
            "1M": {
                "data_support": str(DataSupport.PARTIAL),
                "available": "market trend and recent fundamentals",
                "missing": "analyst revisions, forward earnings calendar",
            },
            "3M": {
                "data_support": str(DataSupport.PARTIAL),
                "available": "point-in-time fundamentals, valuation, market context",
                "missing": "analyst revisions",
            },
            "LONG_TERM": {
                "data_support": str(DataSupport.STRONGEST_AVAILABLE),
                "available": "company quality, valuation, balance sheet, dilution",
                "missing": "forward expectations and any validated mapping to returns",
            },
        }

    def _summary(
        self,
        symbol: str,
        quality: Sequence[Section],
        valuation: Section,
        market: Section,
    ) -> str:
        parts: list[str] = []
        by_name = {s.name: s for s in quality}
        growth = by_name.get("GROWTH")
        if growth is not None:
            rev = growth.metrics.get("revenue_ttm")
            if rev is not None and rev.available and rev.value is not None:
                parts.append(f"Trailing revenue is {rev.value / 1e9:.2f}B")
        profitability = by_name.get("PROFITABILITY")
        if profitability is not None:
            om = profitability.metrics.get("operating_margin")
            if om is not None and om.available and om.value is not None:
                parts.append(f"operating margin is {om.value * 100:.1f}%")
            elif om is not None and om.unavailable_reason == _SECTOR_REFUSAL:
                parts.append("margin analysis is refused for this sector")
        balance = by_name.get("BALANCE SHEET")
        if balance is not None:
            parts.append(f"the balance sheet reads {balance.labels.get('assessment', 'unknown')}")
        context = valuation.labels.get("ps_context")
        if context and context != str(ValuationContext.INSUFFICIENT):
            parts.append(f"price to sales is {context.lower().replace('_', ' ')}")
        rs = market.metrics.get("relative_strength_252d")
        if rs is not None and rs.available and rs.value is not None:
            direction = "ahead of" if rs.value > 0 else "behind"
            parts.append(f"the stock is {direction} the benchmark over a year")
        body = "; ".join(parts) if parts else "insufficient data for a factual summary"
        return f"{symbol}: {body}. This is analysis, not an investment recommendation."
