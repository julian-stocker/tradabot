"""What a company's economics have been doing, from filings that were public then.

This layer answers one question -- *what has already happened* -- and is built
so that it cannot answer any other. There is no forecast, no extrapolation, no
fitted line, and no threshold anywhere that was chosen by looking at what a
share price did afterwards. Every tolerance below cites the distribution it came
from, measured across the 989-company universe.

Windows are what the data supports
----------------------------------
Measured, not assumed. Of 989 companies, those with enough contiguous
trailing-twelve-month points: **860 reach three years, 811 reach five, and only
116 reach ten.** The ten-year cliff is not a gap in the archive -- it is the
revenue concept changing around 2018, which the fact store correctly refuses to
stitch across. So the declared windows are 1Y, 3Y and 5Y, and the own-history
percentile is drawn over whatever contiguous run exists with its span always
stated.

Bases never mix
---------------
A trailing-twelve-month point and a fiscal-year point are different quantities.
Foreign private issuers file annually -- SAP has *zero* quarterly observations
-- so they get an annual trajectory or none, never a line that silently changes
basis halfway along.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Final

from app.advisor.facts import ShareFamily
from app.core.logging import get_logger
from app.history.schemas import (
    Change,
    CompanyTrajectory,
    Direction,
    MetricTrajectory,
    Observation,
    SeriesBasis,
    SeriesStatus,
)
from app.history.series import (
    annual_series,
    difference_series,
    instant_series,
    one_concept,
    one_unit,
    ratio_series,
    ttm_series,
)

logger = get_logger(__name__)

WINDOWS: Final[dict[str, int]] = {"1y": 4, "3y": 12, "5y": 20}
"""Window name to the number of quarterly steps back. Chosen from coverage:
860 of 989 companies reach three years of contiguous trailing-twelve-month
history and 811 reach five. A ten-year window was measured and rejected -- only
116 companies reach it, because the revenue concept changes around 2018."""

ANNUAL_WINDOWS: Final[dict[str, int]] = {"1y": 1, "3y": 3, "5y": 5}

MIN_OBSERVATIONS: Final = 5
"""The shortest declared window plus its own endpoint. Below this there is no
window to measure, so the answer is INSUFFICIENT_HISTORY rather than a change
over whatever happened to be there."""

MARGIN_STABLE_PP: Final = 1.0
"""Percentage points within which a one-year margin move is called STABLE.

Anchored on the measured distribution of one-year absolute margin moves across
the universe: |change| <= 1.0pp covers **33% of companies on operating margin
and 44% on gross margin**. A wider 2.0pp band would call 51% and 69% stable,
which describes the universe less. Chosen from the spread of the metric itself
-- no share price was consulted, and none could be: this is a statement about
what companies typically do, not about what follows."""

SHARE_STABLE_PCT: Final = 1.0
"""Percent within which a one-year share-count move is called STABLE. |change|
<= 1.0% covers 29% of the universe; share counts move slowly, so a one-percent
band is a real buyback or a real issuance."""

FINANCIAL_SIC: Final = "6"
"""SIC division H. A bank's revenue, operating margin and free cash flow are
not the industrial quantities of the same name -- a naive cross-sector margin
screen over this data returns REITs at 700%. The fact store already abandons
JPMorgan's revenue series in 2014; this refuses the rest explicitly."""

FUND_TYPES: Final[frozenset[str]] = frozenset({"ETF", "FUND", "ETN"})

INDUSTRIAL_METRICS: Final[frozenset[str]] = frozenset(
    {"revenue", "gross_margin", "operating_margin", "free_cash_flow", "fcf_margin"}
)
"""Metrics whose meaning depends on an industrial income statement. Share count
is not among them: a bank's share count is a share count."""

_MARGIN_METRICS: Final[frozenset[str]] = frozenset({"gross_margin", "operating_margin"})


def midrank_percentile(values: list[float], value: float) -> float:
    """Where ``value`` sits among ``values``. The peer layer's convention.

    Midrank, so a value equal to others in the series is placed at their centre
    rather than at one end -- the same choice ``app.peers.statistics`` made, so
    a percentile means one thing across the system.
    """
    if not values:
        return 0.0
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return 100.0 * (below + 0.5 * equal) / len(values)


class CompanyHistoryService:
    """Trajectories for one company, as of one moment. **Never raises.**

    Args:
        facts: an :class:`~app.advisor.facts.FactStore`. The only data source.
    """

    def __init__(self, *, facts: Any) -> None:
        self._facts = facts

    def for_company(
        self,
        *,
        company_key: str,
        as_of: str,
        company_id: int | None = None,
        sic: str | None = None,
        asset_type: str = "STOCK",
    ) -> CompanyTrajectory:
        """Every trajectory this company supports.

        Identity is the canonical company key -- the reporting entity's key in
        the fact store, never a listing symbol. Two listings of one issuer
        therefore share one history, which is the same rule the event layer and
        the peer layer already follow.
        """
        if asset_type.upper() in FUND_TYPES:
            return CompanyTrajectory(
                company_id=company_id,
                company_key=company_key,
                as_of=as_of,
                detail="a fund has no operations to have a trajectory",
                metrics={
                    m: MetricTrajectory(m, SeriesStatus.NOT_APPLICABLE, as_of=as_of)
                    for m in ("revenue", "operating_margin", "fcf_margin", "share_count")
                },
            )
        try:
            metrics = self._build(company_key, as_of=as_of, sic=sic)
        except Exception as exc:
            logger.warning("trajectory unavailable", company=company_key, reason=type(exc).__name__)
            return CompanyTrajectory(
                company_id=company_id,
                company_key=company_key,
                as_of=as_of,
                detail="history could not be read",
            )
        return CompanyTrajectory(
            company_id=company_id, company_key=company_key, as_of=as_of, metrics=metrics
        )

    # ------------------------------------------------------------- internals
    def _build(self, key: str, *, as_of: str, sic: str | None) -> dict[str, MetricTrajectory]:
        financial = bool(sic) and str(sic).startswith(FINANCIAL_SIC)
        flows = {
            m: self._flow(key, m, as_of)
            for m in ("revenue", "gross_profit", "operating_income", "operating_cash_flow", "capex")
        }
        annual = all(not v[0] for v in flows.values() if v[0] is not None) and bool(
            self._annual(key, "revenue", as_of)
        )

        out: dict[str, MetricTrajectory] = {}
        revenue, rev_basis = flows["revenue"]
        if annual:
            revenue, rev_basis = self._annual(key, "revenue", as_of), SeriesBasis.ANNUAL

        out["revenue"] = self._trajectory(
            "revenue",
            revenue,
            rev_basis,
            as_of,
            financial=financial,
            annual=annual,
            direction_kind=(Direction.INCREASING, Direction.DECREASING),
            tolerance=None,
        )
        for name, numerator in (
            ("gross_margin", "gross_profit"),
            ("operating_margin", "operating_income"),
        ):
            top = self._annual(key, numerator, as_of) if annual else flows[numerator][0]
            margin = ratio_series(top or [], revenue or [])
            out[name] = self._trajectory(
                name,
                margin,
                rev_basis,
                as_of,
                financial=financial,
                annual=annual,
                direction_kind=(Direction.EXPANDING, Direction.COMPRESSING),
                tolerance=MARGIN_STABLE_PP,
                ratio=True,
            )

        ocf = (
            self._annual(key, "operating_cash_flow", as_of)
            if annual
            else flows["operating_cash_flow"][0]
        )
        capex = self._annual(key, "capex", as_of) if annual else flows["capex"][0]
        fcf = difference_series(
            ocf or [],
            [replace(o, value=abs(o.value)) for o in (capex or [])],
            unit=(one_unit(ocf or []) or ""),
        )
        out["fcf_margin"] = self._trajectory(
            "fcf_margin",
            ratio_series(fcf, revenue or []),
            rev_basis,
            as_of,
            financial=financial,
            annual=annual,
            direction_kind=(Direction.EXPANDING, Direction.COMPRESSING),
            tolerance=MARGIN_STABLE_PP,
            ratio=True,
        )

        shares, share_annual = self._shares(key, as_of)
        out["share_count"] = self._trajectory(
            "share_count",
            shares,
            SeriesBasis.INSTANT,
            as_of,
            financial=False,
            annual=share_annual,
            direction_kind=(Direction.INCREASING, Direction.DECREASING),
            tolerance=SHARE_STABLE_PCT,
            relative_tolerance=True,
        )
        return out

    def _shares(self, key: str, as_of: str) -> tuple[list[Observation], bool]:
        """The longest share-count run, from whichever family reports one.

        Families are never mixed -- comparing a cover-page count with a
        period-end count is what produced Salesforce's phantom buyback -- but a
        company reporting only one of them should not be refused because the
        other is empty. Coca-Cola has no period-end series at all and a complete
        cover-page one.
        """
        best: list[Observation] = []
        best_annual = False
        for family in (ShareFamily.PERIOD_END, ShareFamily.COVER_PAGE):
            points, annual = instant_series(
                self._facts.share_series(key, as_of, family),
                filing_dated=family is ShareFamily.COVER_PAGE,
            )
            if len(points) > len(best):
                best, best_annual = points, annual
        return best, best_annual

    def _flow(
        self, key: str, metric: str, as_of: str
    ) -> tuple[list[Observation] | None, SeriesBasis]:
        quarters, _ = self._facts.quarterlies(key, metric, as_of)
        return ttm_series(quarters), SeriesBasis.TTM

    def _annual(self, key: str, metric: str, as_of: str) -> list[Observation]:
        return annual_series(self._facts.annual_rows(key, metric, as_of))

    def _trajectory(
        self,
        metric: str,
        observations: list[Observation] | None,
        basis: SeriesBasis,
        as_of: str,
        *,
        financial: bool,
        annual: bool,
        direction_kind: tuple[Direction, Direction],
        tolerance: float | None,
        ratio: bool = False,
        relative_tolerance: bool = False,
    ) -> MetricTrajectory:
        if financial and metric in INDUSTRIAL_METRICS:
            return MetricTrajectory(
                metric,
                SeriesStatus.SECTOR_MODEL_REQUIRED,
                as_of=as_of,
                detail="this line item is not comparable for a financial company",
            )
        points = observations or []
        if not points:
            return MetricTrajectory(metric, SeriesStatus.UNAVAILABLE, as_of=as_of)
        if len(points) < MIN_OBSERVATIONS:
            return MetricTrajectory(
                metric,
                SeriesStatus.INSUFFICIENT_HISTORY,
                as_of=as_of,
                detail=f"{len(points)} contiguous observation(s); {MIN_OBSERVATIONS} needed",
            )
        if not ratio and one_unit(points) is None:
            return MetricTrajectory(
                metric,
                SeriesStatus.CURRENCY_CHANGE,
                as_of=as_of,
                detail="the reporting unit changed within the series",
            )
        if one_concept(points) is None and not ratio:
            return MetricTrajectory(
                metric,
                SeriesStatus.TAXONOMY_DISCONTINUITY,
                as_of=as_of,
                detail="the underlying reported concept changed within the series",
            )

        unit = one_unit(points) or ("ratio" if ratio else "")
        # Windows follow the *cadence of the series*, never its basis. Share
        # counts are INSTANT observations that some companies report quarterly
        # and others annually; keying off the basis read twelve annual steps as
        # "three years" and reported JPMorgan buying back 10.5% a year.
        steps = ANNUAL_WINDOWS if annual else WINDOWS
        changes: dict[str, Change] = {}
        for name, back in steps.items():
            if len(points) <= back:
                continue
            change = _change(points[-1 - back], points[-1], name, back, annual=annual, ratio=ratio)
            if change is not None:
                changes[name] = change

        values = [o.value for o in points]
        return MetricTrajectory(
            metric=metric,
            status=SeriesStatus.AVAILABLE,
            basis=basis,
            unit=unit,
            currency=None if ratio else unit,
            as_of=as_of,
            current=points[-1].value,
            observations=tuple(points),
            changes=changes,
            direction=_direction(
                changes.get("1y"),
                direction_kind,
                tolerance,
                ratio=ratio,
                relative=relative_tolerance,
            ),
            percentile=midrank_percentile(values, points[-1].value),
            history_span=f"{points[0].period_end} to {points[-1].period_end}",
        )


def _change(
    start: Observation,
    end: Observation,
    name: str,
    back: int,
    *,
    annual: bool,
    ratio: bool,
) -> Change | None:
    years = back if annual else back / 4
    absolute = (end.value - start.value) * (100 if ratio else 1)
    relative: float | None = None
    annualised: float | None = None
    if not ratio and start.value > 0 and end.value > 0:
        # Both endpoints positive, so the ratio is meaningful and the root is
        # real. Earnings that crossed zero have a direction and no growth rate,
        # and a compound rate through a sign change is arithmetic nonsense.
        relative = end.value / start.value - 1
        if years > 0:
            annualised = (end.value / start.value) ** (1 / years) - 1
    return Change(
        window=name,
        from_value=start.value,
        to_value=end.value,
        from_period=start.period_end,
        to_period=end.period_end,
        absolute=absolute,
        relative=relative,
        annualised=annualised,
    )


def _direction(
    change: Change | None,
    kind: tuple[Direction, Direction],
    tolerance: float | None,
    *,
    ratio: bool,
    relative: bool,
) -> Direction | None:
    """A word for the one-year move, only where a measured tolerance exists.

    Set for the shortest window alone. A direction over five years would
    describe a company that no longer trades on the same terms, and calling
    that "expanding" flattens five years into one adjective.
    """
    if change is None or tolerance is None:
        return None
    moved = (change.relative or 0.0) * 100 if relative else (change.absolute if ratio else 0.0)
    if abs(moved) <= tolerance:
        return Direction.STABLE
    return kind[0] if moved > 0 else kind[1]
