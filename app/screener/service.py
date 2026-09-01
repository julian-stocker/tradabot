"""Finding companies that satisfy stated conditions, and nothing more.

The screener owns no financial arithmetic. Every value it compares was computed
by the layer that defines it -- trajectories by
:mod:`app.history`, valuation and market position by the Advisor, events by the
research layer -- and every refusal is inherited rather than re-decided. A
financial issuer is not excluded here because this module has an opinion about
banks; it is excluded because :class:`~app.history.CompanyHistoryService`
already answered ``SECTOR_MODEL_REQUIRED`` and that answer is passed through.

Cheap conditions run first
--------------------------
Measured per company across 989 registrants: the instrument registry is free,
a full set of trajectories costs 4 ms, current developments 9 ms and an
``AdvisorReport`` **45 ms**. Evaluating in that order is ordinary query
planning, not a second source of truth: a screen for ``operating margin >= 25%
and P/E <= 30`` narrows to a few hundred companies for 4 seconds and then pays
the Advisor only for those, instead of 45 seconds for all of them.

The order never changes the answer. Criteria are independent and combined with
AND, so evaluating them in a different sequence can only change what work was
avoided.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.screener import registry
from app.screener.schemas import (
    COST_ORDER,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Cost,
    Criterion,
    CriterionResult,
    Evaluation,
    NotEvaluable,
    Operator,
    ScreenCandidate,
    ScreenResult,
    compare,
)

logger = get_logger(__name__)

FUND_TYPES = frozenset({"ETF", "FUND", "ETN"})

_STATUS_REASONS: dict[str, NotEvaluable] = {
    "SECTOR_MODEL_REQUIRED": NotEvaluable.SECTOR_MODEL_REQUIRED,
    "UNAVAILABLE": NotEvaluable.UNAVAILABLE,
    "INSUFFICIENT_HISTORY": NotEvaluable.INSUFFICIENT_HISTORY,
    "TAXONOMY_DISCONTINUITY": NotEvaluable.TAXONOMY_DISCONTINUITY,
    "CURRENCY_CHANGE": NotEvaluable.CURRENCY_CHANGE,
    "GAPPED_SERIES": NotEvaluable.GAPPED_SERIES,
    "ABANDONED_SERIES": NotEvaluable.ABANDONED_SERIES,
    "MIXED_BASIS": NotEvaluable.MIXED_BASIS,
    "NOT_APPLICABLE": NotEvaluable.NOT_APPLICABLE,
}
"""Trajectory status to screening reason. A straight pass-through, so a
refusal keeps the name the owning layer gave it."""


class InvalidCriterionError(ValueError):
    """A criterion the registry does not accept. Raised before any work."""


@dataclass(frozen=True, slots=True)
class _Subject:
    """One company and the listing a market filter would have to mean."""

    company_id: int
    company_key: str
    listing: Any

    @property
    def symbol(self) -> str:
        return str(self.listing.symbol)


class ScreenerService:
    """Deterministic company discovery over the existing canonical layers.

    Args:
        registry_snapshot: the instrument registry. Owns identity and sector.
        history: :class:`~app.history.CompanyHistoryService`.
        advisor: the Advisor, for valuation and market position. Optional --
            without it, those criteria are ``NOT_EVALUABLE`` rather than wrong.
        developments: the current-developments service. Optional on the same
            terms.
    """

    def __init__(
        self,
        *,
        registry_snapshot: Any,
        history: Any,
        advisor: Any = None,
        developments: Any = None,
    ) -> None:
        self._registry = registry_snapshot
        self._history = history
        self._advisor = advisor
        self._developments = developments
        self._trajectories: dict[str, Any] = {}
        self._reports: dict[str, Any] = {}

    # ------------------------------------------------------------- universe
    def universe(self) -> list[_Subject]:
        """One entry per company, with a deterministic preferred listing.

        A cross-listed issuer appears **once**. Which listing represents it for
        a market filter is decided the only way that cannot silently borrow
        prices: the listing that has its own price series wins, and among
        several the first by qualified name. SAP.DE and SAP.US are one company
        here, and a market criterion is answered by whichever of them actually
        has a series -- never by the other one's.
        """
        by_company: dict[str, list[Any]] = {}
        for candidate in self._registry.all_candidates():
            if not candidate.cik or str(candidate.asset_type).upper() in FUND_TYPES:
                continue
            from app.advisor.facts import company_key  # noqa: PLC0415

            by_company.setdefault(company_key(int(candidate.cik)), []).append(candidate)

        from app.instruments.registry import market_inputs  # noqa: PLC0415

        subjects: list[_Subject] = []
        for key, listings in sorted(by_company.items()):
            ordered = sorted(listings, key=lambda c: c.qualified)
            preferred = next((c for c in ordered if market_inputs(c)[0] is not None), ordered[0])
            subjects.append(
                _Subject(company_id=preferred.company_id, company_key=key, listing=preferred)
            )
        return subjects

    # --------------------------------------------------------------- screen
    def screen(
        self,
        criteria: Sequence[Criterion],
        *,
        as_of: str,
        limit: int = DEFAULT_LIMIT,
        sort_metric: str | None = None,
        descending: bool = False,
    ) -> ScreenResult:
        """Companies satisfying every criterion, as of one moment.

        Raises:
            InvalidCriterionError: for an unknown metric or an operator the metric
                does not accept. Checked before any company is touched, so a
                typo costs nothing and never silently returns an empty screen.
        """
        started = time.monotonic()
        self._validate(criteria, sort_metric)
        # Scoped to this run and discarded with it. Five trajectory criteria
        # asked the history layer for the same company five times, costing 15.4s
        # where one pass costs 4s. Deliberately not a service-level cache: a
        # screen dated to a past `as_of` must never read a memo built for
        # another date, so it is emptied on entry rather than reused.
        self._trajectories = {}
        self._reports = {}
        bounded = max(1, min(int(limit), MAX_LIMIT))
        subjects = self.universe()

        ordered = sorted(criteria, key=lambda c: COST_ORDER.index(_cost(c)))
        alive: dict[str, list[CriterionResult]] = {s.company_key: [] for s in subjects}
        by_key = {s.company_key: s for s in subjects}
        dropped: dict[str, list[CriterionResult]] = {}

        for criterion in ordered:
            for key in list(alive):
                result = self._evaluate(by_key[key], criterion, as_of=as_of)
                alive[key].append(result)
                if result.evaluation is Evaluation.NO_MATCH:
                    # Only a definite failure settles a company. Under the
                    # precedence rule a NO_MATCH can never become anything
                    # else, so the remaining -- possibly expensive -- criteria
                    # are skipped for it.
                    #
                    # A NOT_EVALUABLE does *not* settle it, and stopping there
                    # made the classification depend on which criterion the
                    # cost ordering happened to examine first: the same company
                    # came back NO_MATCH one way round and NOT_EVALUABLE the
                    # other. Optimisation must not decide semantics, so an
                    # untestable criterion keeps the company in the loop until
                    # something definite happens or the criteria run out.
                    dropped[key] = alive.pop(key)

        candidates = [
            self._candidate(by_key[key], tuple(results))
            for key, results in (*alive.items(), *dropped.items())
        ]
        return self._result(
            candidates,
            criteria=tuple(criteria),
            as_of=as_of,
            universe=len(subjects),
            limit=bounded,
            sort_metric=sort_metric,
            descending=descending,
            duration=time.monotonic() - started,
        )

    # ------------------------------------------------------------ internals
    def _validate(self, criteria: Sequence[Criterion], sort_metric: str | None) -> None:
        if not criteria:
            msg = "a screen needs at least one criterion"
            raise InvalidCriterionError(msg)
        for criterion in criteria:
            metric = registry.get(criterion.metric)
            if metric is None:
                msg = f"unknown metric {criterion.metric!r}; see the metric registry"
                raise InvalidCriterionError(msg)
            if not metric.supports(criterion.operator):
                allowed = ", ".join(str(o) for o in metric.operators)
                msg = f"{criterion.metric} does not support {criterion.operator}; use {allowed}"
                raise InvalidCriterionError(msg)
        if sort_metric is not None and registry.get(sort_metric) is None:
            msg = f"unknown sort metric {sort_metric!r}"
            raise InvalidCriterionError(msg)

    def _candidate(
        self, subject: _Subject, results: tuple[CriterionResult, ...]
    ) -> ScreenCandidate:
        listing = subject.listing
        return ScreenCandidate(
            company_id=subject.company_id,
            company_key=subject.company_key,
            symbol=str(listing.symbol),
            company_name=str(listing.company_name),
            listing=str(listing.qualified),
            sic=str(listing.sic) if listing.sic else None,
            results=results,
        )

    def _result(
        self,
        candidates: list[ScreenCandidate],
        *,
        criteria: tuple[Criterion, ...],
        as_of: str,
        universe: int,
        limit: int,
        sort_metric: str | None,
        descending: bool,
        duration: float,
    ) -> ScreenResult:
        matched = [c for c in candidates if c.evaluation is Evaluation.MATCH]
        not_evaluable = [c for c in candidates if c.evaluation is Evaluation.NOT_EVALUABLE]
        reasons: dict[str, int] = {}
        for candidate in not_evaluable:
            for reason in dict.fromkeys(candidate.reasons):
                reasons[str(reason)] = reasons.get(str(reason), 0) + 1

        # Alphabetical by symbol unless a metric was explicitly named. There is
        # deliberately no default "best first": ordering by desirability is the
        # judgement this system does not make.
        matched.sort(key=lambda c: (c.symbol, c.company_key))
        if sort_metric is not None:
            matched.sort(
                key=lambda c: (
                    _observed(c, sort_metric) is None,
                    _observed(c, sort_metric) or 0.0,
                    c.symbol,
                ),
                reverse=descending,
            )
        return ScreenResult(
            as_of=as_of,
            criteria=criteria,
            universe=universe,
            evaluated=len(candidates) - len(not_evaluable),
            matched=len(matched),
            not_evaluable=len(not_evaluable),
            candidates=tuple(matched[:limit]),
            reasons=dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            sort_metric=sort_metric,
            descending=descending,
            limit=limit,
            truncated=max(0, len(matched) - limit),
            duration_seconds=duration,
        )

    # ---------------------------------------------------------- evaluation
    def _evaluate(self, subject: _Subject, criterion: Criterion, *, as_of: str) -> CriterionResult:
        """One criterion against one company. **Never raises.**"""
        metric = registry.get(criterion.metric)
        if metric is not None and not metric.financial_ok and _is_financial(subject.listing):
            # The declared sector boundary, enforced rather than merely stated.
            # The history layer refuses these itself; the Advisor does not, and
            # Bank of America was measured returning a price-to-sales ratio of
            # 3.58x against a "sales" figure that is not the industrial one.
            return CriterionResult(
                criterion,
                Evaluation.NOT_EVALUABLE,
                reason=NotEvaluable.SECTOR_MODEL_REQUIRED,
                detail="this line item is not comparable for a financial company",
            )
        try:
            reader = {
                Cost.REGISTRY: self._from_registry,
                Cost.HISTORY: self._from_history,
                Cost.DEVELOPMENTS: self._from_developments,
                Cost.ADVISOR: self._from_advisor,
            }[_cost(criterion)]
            observed, reason, detail = reader(subject, criterion, as_of)
        except Exception as exc:
            logger.warning(
                "criterion failed",
                company=subject.company_key,
                metric=criterion.metric,
                reason=type(exc).__name__,
            )
            return CriterionResult(
                criterion, Evaluation.NOT_EVALUABLE, reason=NotEvaluable.UNAVAILABLE
            )
        if reason is not None:
            return CriterionResult(
                criterion,
                Evaluation.NOT_EVALUABLE,
                observed=observed,
                reason=reason,
                detail=detail,
            )
        matched = compare(observed, criterion.operator, criterion.value)
        return CriterionResult(
            criterion,
            Evaluation.MATCH if matched else Evaluation.NO_MATCH,
            observed=observed,
        )

    def _from_registry(
        self, subject: _Subject, criterion: Criterion, _as_of: str
    ) -> tuple[Any, NotEvaluable | None, str | None]:
        listing = subject.listing
        if criterion.metric == "sic":
            if not listing.sic:
                return None, NotEvaluable.UNAVAILABLE, "no SEC classification"
            # Prefix comparison, so "73" selects all of SIC 73xx. Never inferred
            # from a company name.
            want = str(criterion.value)
            value = (
                str(listing.sic)[: len(want)]
                if criterion.operator in (Operator.EQ, Operator.NEQ)
                else str(listing.sic)
            )
            return value, None, None
        return None, NotEvaluable.UNAVAILABLE, None

    def _from_history(
        self, subject: _Subject, criterion: Criterion, as_of: str
    ) -> tuple[Any, NotEvaluable | None, str | None]:
        listing = subject.listing
        report = self._trajectories.get(subject.company_key)
        if report is None:
            report = self._history.for_company(
                company_key=subject.company_key,
                as_of=as_of,
                company_id=subject.company_id,
                sic=listing.sic,
                asset_type=str(listing.asset_type),
            )
            self._trajectories[subject.company_key] = report
        metric, field, window = _decompose(criterion.metric)
        found = report.get(metric)
        if found is None:
            return None, NotEvaluable.UNAVAILABLE, None
        if not found.available:
            reason = _STATUS_REASONS.get(str(found.status), NotEvaluable.UNAVAILABLE)
            return None, reason, found.detail
        if field == "current":
            return found.current, None, None
        if field == "percentile":
            return found.percentile, None, None
        return _from_change(found.changes.get(window or ""), field, window)

    def _from_developments(
        self, subject: _Subject, criterion: Criterion, as_of: str
    ) -> tuple[Any, NotEvaluable | None, str | None]:
        if self._developments is None:
            return None, NotEvaluable.NO_RESEARCH_COVERAGE, "research layer not wired in"
        listing = subject.listing
        report = self._developments.for_company(
            company_id=subject.company_id,
            cik=listing.cik,
            as_of=as_of,
            company_key=subject.company_key,
            asset_type=str(listing.asset_type),
        )
        status = str(report.status)
        if status in ("UNAVAILABLE", "NO_COVERAGE", "NOT_APPLICABLE"):
            return None, NotEvaluable.NO_RESEARCH_COVERAGE, report.detail
        if criterion.metric == "has_current_development":
            return report.has_developments, None, None
        kinds = {str(i.kind) for d in report.developments for i in d.items}
        if criterion.metric == "development_kind":
            return str(criterion.value) if str(criterion.value) in kinds else "", None, None
        bands = [str(d.materiality) for d in report.developments]
        order = ("ROUTINE", "NOTABLE", "SIGNIFICANT", "CRITICAL")
        highest = max(bands, key=order.index) if bands else ""
        return highest, None, None

    def _from_advisor(
        self, subject: _Subject, criterion: Criterion, as_of: str
    ) -> tuple[Any, NotEvaluable | None, str | None]:
        if self._advisor is None:
            return None, NotEvaluable.UNAVAILABLE, "advisor not wired in"
        from app.advisor.service import MarketIdentity  # noqa: PLC0415
        from app.instruments.registry import market_inputs, valuation_allowed  # noqa: PLC0415

        listing = subject.listing
        series, benchmark, mismatch = market_inputs(listing)
        metric = registry.get(criterion.metric)
        if series is None:
            # No price series of this listing's own. Another venue's prices are
            # a different security in a different currency, and are never
            # substituted -- the Phase 13 rule, unchanged.
            return None, NotEvaluable.NO_MARKET_DATA, "this listing has no price series"
        if metric is not None and metric.unit == "MULTIPLE":
            allowed, why = valuation_allowed(listing)
            if not allowed:
                return None, NotEvaluable.VALUATION_REFUSED, why
        report = self._reports.get(subject.company_key)
        if report is None:
            report = self._advisor.analyse(
                listing.symbol,
                as_of=as_of,
                company_key=subject.company_key,
                market=MarketIdentity(series=series, benchmark=benchmark, unit_mismatch=mismatch),
            )
            self._reports[subject.company_key] = report
        for section in (report.valuation, report.market_position):
            found = section.metrics.get(criterion.metric)
            if found is not None and found.value is not None:
                return found.value, None, None
        return None, NotEvaluable.UNAVAILABLE, None


def _from_change(
    change: Any, field: str, window: str | None
) -> tuple[Any, NotEvaluable | None, str | None]:
    """One window's movement, in the unit the metric is stated in."""
    if change is None:
        return None, NotEvaluable.WINDOW_UNAVAILABLE, f"no {window} window in this series"
    if field == "change_pp":
        return change.absolute, None, None
    value = change.annualised if field == "cagr" else change.relative
    if value is None:
        # A sign change or a zero base. There is a direction and no rate.
        return None, NotEvaluable.WINDOW_UNAVAILABLE, "no valid rate across these endpoints"
    return value, None, None


_SUFFIXES = (
    "_change_1y",
    "_change_3y",
    "_change_5y",
    "_cagr_1y",
    "_cagr_3y",
    "_cagr_5y",
    "_own_percentile",
)


def _decompose(key: str) -> tuple[str, str, str | None]:
    """``operating_margin_change_3y`` -> metric, field, window."""
    for suffix in _SUFFIXES:
        if key.endswith(suffix):
            base = key[: -len(suffix)]
            if suffix == "_own_percentile":
                return base, "percentile", None
            window = suffix[-2:]
            kind = "cagr" if "_cagr_" in suffix else "change"
            if kind == "change" and base in ("gross_margin", "operating_margin", "fcf_margin"):
                return base, "change_pp", window
            return base, kind if kind == "cagr" else "change_rel", window
    return key, "current", None


def _is_financial(listing: Any) -> bool:
    """SIC division H. The same test the history layer applies."""
    return bool(listing.sic) and str(listing.sic).startswith("6")


def _cost(criterion: Criterion) -> Cost:
    metric = registry.get(criterion.metric)
    return metric.cost if metric else Cost.ADVISOR


def _observed(candidate: ScreenCandidate, metric: str) -> float | None:
    for result in candidate.results:
        if result.criterion.metric == metric and isinstance(result.observed, (int, float)):
            return float(result.observed)
    return None


def now_iso() -> str:
    return datetime.now(UTC).date().isoformat()
