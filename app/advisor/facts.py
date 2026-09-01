"""Point-in-time SEC fact access and TTM assembly.

This module is the **canonical owner** of TTM construction. Research code must
import it rather than re-deriving the same arithmetic, so the two can never
drift apart -- a drift guard test enforces that.

Two properties matter and are enforced here:

* **Point-in-time.** A fact is visible only from the filing that published it.
  A later restatement never rewrites what was knowable earlier.
* **Honest quarters.** A 10-Q reports cash flow year-to-date, not for the
  quarter. Summing YTD values as if they were quarters would double count, so
  quarterly figures are de-cumulated from YTD facts sharing a fiscal-year start.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any

import polars as pl

from app.advisor.schemas import DenominatorBasis, Provenance

FLOW_METRICS: frozenset[str] = frozenset(
    {
        "revenue",
        "gross_profit",
        "operating_income",
        "net_income",
        "eps_diluted",
        "operating_cash_flow",
        "capex",
    }
)
INSTANT_METRICS: frozenset[str] = frozenset(
    {
        "cash",
        "total_assets",
        "total_liabilities",
        "equity",
        "short_term_debt",
        "long_term_debt",
        "shares_outstanding",
    }
)

# Declared preference order per metric, used only to break ties. The primary
# rule is always "whatever the most recent filing used", because a company may
# legitimately change taxonomy and history must not override its present
# reporting regime.
_CONCEPT_PRIORITY: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ),
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "long_term_debt": ("LongTermDebtNoncurrent", "LongTermDebt"),
    "short_term_debt": ("ShortTermBorrowings", "DebtCurrent"),
    "shares_outstanding": (
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
    ),
}


class ShareFamily(StrEnum):
    """Share counts that look alike but measure different things.

    Mixing them silently is what produced Salesforce's phantom -12.59% "buyback":
    two cover-page values six months apart, compared as if they were fiscal
    year-ends. Each family is therefore named, and dilution may only compare
    within one.
    """

    PERIOD_END = "PERIOD_END_SHARES"
    COVER_PAGE = "COVER_PAGE_SHARES"
    WEIGHTED_AVERAGE_BASIC = "WEIGHTED_AVERAGE_BASIC_SHARES"
    WEIGHTED_AVERAGE_DILUTED = "WEIGHTED_AVERAGE_DILUTED_SHARES"
    OTHER = "OTHER_SHARES"
    AMBIGUOUS = "AMBIGUOUS_SHARES"


# Explicit concept -> family map. Membership is declared, never inferred from
# the shape of a concept name.
SHARE_FAMILY_BY_CONCEPT: dict[str, ShareFamily] = {
    "CommonStockSharesOutstanding": ShareFamily.PERIOD_END,
    "CommonStockSharesIssued": ShareFamily.OTHER,
    "EntityCommonStockSharesOutstanding": ShareFamily.COVER_PAGE,
    "WeightedAverageNumberOfSharesOutstandingBasic": ShareFamily.WEIGHTED_AVERAGE_BASIC,
    "WeightedAverageNumberOfDilutedSharesOutstanding": (ShareFamily.WEIGHTED_AVERAGE_DILUTED),
}


def share_family(concept: str) -> ShareFamily:
    """Which semantic family a share concept belongs to."""
    return SHARE_FAMILY_BY_CONCEPT.get(concept, ShareFamily.AMBIGUOUS)


_QUARTER_MIN, _QUARTER_MAX = 80, 100
_ANNUAL_MIN, _ANNUAL_MAX = 350, 380
_STALE_DAYS = 200

ABANDONED_LAG_DAYS = 550
"""How far a metric may lag the company's own latest reported period before it
stops counting as that company's current figure.

Agnico Eagle moved from us-gaap to ifrs-full in 2015. ``OperatingIncomeLoss``
stopped being filed then, and because it was still the freshest fact *for that
metric* the Advisor kept serving it: a 2013 operating income printed beside 2025
revenue, and an operating margin of 3.6% computed by dividing one by the other.
Every input was real and the ratio was invented.

The lag is measured against the company rather than the clock, so an annual
filer is not punished for filing annually. Eighteen months clears a full annual
cycle plus a late filing; past that the series has missed a reporting period it
should have had, which means the company stopped reporting it."""
_TTM_QUARTERS = 4


def _duration(row: dict[str, Any]) -> int | None:
    start, end = row.get("period_start"), row.get("period_end")
    if not start or not end:
        return None
    try:
        return (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days
    except ValueError:
        return None


def company_key(cik: int | str) -> str:
    """The canonical fact key for a company, from its CIK.

    Prefixed and zero-padded so it can never collide with a ticker: ``CIK0000320193``
    is not a symbol anyone can type, which is what keeps the two index spaces
    from silently overlapping.
    """
    return f"CIK{int(cik):010d}"


@dataclass(frozen=True, slots=True)
class TtmResult:
    """A trailing-twelve-month value, or an explicit reason there isn't one."""

    value: float | None
    basis: DenominatorBasis
    status: str
    denominator_age_days: int | None = None
    provenance: tuple[Provenance, ...] = ()
    detail: str | None = None


class FactStore:
    """Point-in-time SEC facts, indexed by symbol and metric.

    The backing file is configuration, not a hardcoded research path, so the
    same store serves production and research.
    """

    def __init__(self, rows: Iterable[dict[str, Any]]) -> None:
        self._by: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._companies: set[str] = set()
        self._reported: dict[str, list[tuple[str, str]]] = {}
        """Per key, every ``(filed, period_end)`` the company published. Sorted
        and turned into a running maximum in :meth:`_seal`, so the yardstick for
        abandonment can itself be read as of a date rather than with hindsight."""
        for row in rows:
            filed, value = row.get("filed"), row.get("value")
            if filed is None or value is None:
                continue
            metric = str(row["metric"])
            symbol = str(row["symbol"])
            self._by.setdefault((symbol, metric), []).append(row)
            period = str(row.get("period_end") or "")
            if period:
                self._reported.setdefault(symbol, []).append((str(filed), period))
            # A second index on company identity. Facts belong to the reporting
            # entity, not to a ticker: SAP SE files once and trades in Frankfurt
            # and New York, and both listings must see the same numbers without
            # a second copy existing. The symbol index stays because a US
            # listing's ticker is a valid handle for its own company, and
            # removing it would change validated behaviour for no gain.
            cik = row.get("cik")
            if cik is not None:
                key = company_key(cik)
                self._companies.add(key)
                self._by.setdefault((key, metric), []).append(row)
                if period:
                    self._reported.setdefault(key, []).append((str(filed), period))

        self._seal()

    def _seal(self) -> None:
        """Collapse each key's filings into a running maximum period by filing
        date, so ``_horizon`` is one binary search rather than a scan."""
        for key, pairs in self._reported.items():
            pairs.sort()
            best = ""
            sealed: list[tuple[str, str]] = []
            for filed, period in pairs:
                best = max(best, period)
                sealed.append((filed, best))
            self._reported[key] = sealed

    def _horizon(self, symbol: str, as_of: str) -> str | None:
        """The freshest period this key had reported anything for, as known at
        ``as_of``. Never a period only a later filing revealed."""
        sealed = self._reported.get(symbol)
        if not sealed:
            return None
        index = bisect_right(sealed, (as_of, chr(0x10FFFF))) - 1
        return sealed[index][1] if index >= 0 else None

    @classmethod
    def from_parquet(cls, path: str | Path) -> FactStore:
        frame = pl.read_parquet(path)
        return cls(frame.iter_rows(named=True))

    @property
    def companies(self) -> frozenset[str]:
        """Company keys with at least one fact. CIK-derived, never tickers."""
        return frozenset(self._companies)

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(sym for sym, _metric in self._by)

    def _known(self, symbol: str, metric: str, as_of: str) -> list[dict[str, Any]]:
        return [r for r in self._by.get((symbol, metric), ()) if str(r["filed"]) <= as_of]

    def quarterlies(
        self, symbol: str, metric: str, as_of: str
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Provenance]]:
        """Derived quarterly values known at ``as_of``, keyed by period end."""
        rows = [r for r in self._known(symbol, metric, as_of) if _duration(r) is not None]
        if not rows:
            return {}, {}

        latest: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            key = (row["period_start"], row["period_end"], row["concept"], row["unit"])
            held = latest.get(key)
            if held is None or str(row["filed"]) > str(held["filed"]):
                latest[key] = row

        chosen = _choose_concept(metric, list(latest.values()))
        selected = [r for r in latest.values() if (str(r["concept"]), str(r["unit"])) == chosen]

        quarters, provenance, cumulative = _split_by_duration(selected)
        for group in cumulative.values():
            ordered = sorted(group, key=lambda r: str(r["period_end"]))
            for index, row in enumerate(ordered):
                end = str(row["period_end"])
                if end in quarters:
                    continue
                span = _duration(row) or 0
                if index == 0:
                    if _QUARTER_MIN <= span <= _QUARTER_MAX:
                        quarters[end] = row
                        provenance[end] = _provenance(row, row["value"])
                    continue
                previous = ordered[index - 1]
                step = span - (_duration(previous) or 0)
                if not _QUARTER_MIN <= step <= _QUARTER_MAX:
                    continue
                value = float(row["value"]) - float(previous["value"])
                quarters[end] = {**row, "value": value}
                provenance[end] = _provenance(row, value)
        return quarters, provenance

    def _abandoned(self, symbol: str, period_end: str, as_of: str) -> bool:
        """Whether this metric stopped being reported while the company did not.

        Compares the metric's freshest period against the freshest period the
        company reported anything for *as of the same date*, so the judgement is
        point-in-time too: a metric is not retroactively abandoned at a date when
        it was still current.
        """
        horizon = self._horizon(symbol, as_of)
        if horizon is None or not period_end:
            return False
        try:
            lag = (date.fromisoformat(horizon) - date.fromisoformat(period_end)).days
        except ValueError:
            return False
        return lag > ABANDONED_LAG_DAYS

    def ttm(self, symbol: str, metric: str, as_of: str) -> TtmResult:
        """Sum of the four most recent contiguous quarters known at ``as_of``."""
        quarters, provenance = self.quarterlies(symbol, metric, as_of)
        if len(quarters) >= _TTM_QUARTERS:
            ends = sorted(quarters)[-_TTM_QUARTERS:]
            contiguous = True
            for earlier, later in pairwise(ends):
                gap = (date.fromisoformat(later) - date.fromisoformat(earlier)).days
                if not _QUARTER_MIN <= gap <= _QUARTER_MAX:
                    contiguous = False
                    break
            if contiguous and self._abandoned(symbol, ends[-1], as_of):
                return TtmResult(
                    value=None, basis=DenominatorBasis.UNAVAILABLE, status="ABANDONED_SERIES"
                )
            if contiguous:
                age = (date.fromisoformat(as_of) - date.fromisoformat(ends[-1])).days
                return TtmResult(
                    value=sum(float(quarters[end]["value"]) for end in ends),
                    basis=DenominatorBasis.TRUE_TTM,
                    status="STALE" if age > _STALE_DAYS else "VALID",
                    denominator_age_days=age,
                    provenance=tuple(provenance[end] for end in ends if end in provenance),
                )
        return self._annual(symbol, metric, as_of)

    def _annual(self, symbol: str, metric: str, as_of: str) -> TtmResult:
        """Latest annual fact, labelled so it is never mistaken for a real TTM."""
        rows = [
            r
            for r in self._known(symbol, metric, as_of)
            if (span := _duration(r)) is not None and _ANNUAL_MIN <= span <= _ANNUAL_MAX
        ]
        if not rows:
            return TtmResult(None, DenominatorBasis.UNAVAILABLE, "MISSING")
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            end = str(row["period_end"])
            held = latest.get(end)
            if held is None or str(row["filed"]) > str(held["filed"]):
                latest[end] = row
        end = max(latest)
        if self._abandoned(symbol, end, as_of):
            return TtmResult(None, DenominatorBasis.UNAVAILABLE, "ABANDONED_SERIES")
        row = latest[end]
        age = (date.fromisoformat(as_of) - date.fromisoformat(end)).days
        return TtmResult(
            value=float(row["value"]),
            basis=DenominatorBasis.FY_FALLBACK,
            status="FY_FALLBACK",
            denominator_age_days=age,
            provenance=(_provenance(row, row["value"]),),
        )

    def annual_rows(self, symbol: str, metric: str, as_of: str) -> list[dict[str, Any]]:
        """Fiscal-year rows known at ``as_of``, unaggregated.

        For consumers that need the annual series itself rather than a single
        latest value -- a foreign private issuer files annually, so an annual
        series is the only history it has. The point-in-time filter stays here
        rather than at the call site, so no consumer can accidentally read a
        row that was not yet public.
        """
        return [
            r
            for r in self._known(symbol, metric, as_of)
            if (span := _duration(r)) is not None and _ANNUAL_MIN <= span <= _ANNUAL_MAX
        ]

    def instant(self, symbol: str, metric: str, as_of: str) -> TtmResult:
        """Latest balance-sheet value known at ``as_of``."""
        rows = [r for r in self._known(symbol, metric, as_of) if r.get("period_end")]
        if not rows:
            return TtmResult(None, DenominatorBasis.UNAVAILABLE, "MISSING")
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            end = str(row["period_end"])
            held = latest.get(end)
            if held is None or str(row["filed"]) > str(held["filed"]):
                latest[end] = row
        end = max(latest)
        if self._abandoned(symbol, end, as_of):
            return TtmResult(None, DenominatorBasis.UNAVAILABLE, "ABANDONED_SERIES")
        row = latest[end]
        age = (date.fromisoformat(as_of) - date.fromisoformat(end)).days
        return TtmResult(
            value=float(row["value"]),
            basis=DenominatorBasis.TRUE_TTM,
            status="STALE" if age > _STALE_DAYS else "VALID",
            denominator_age_days=age,
            provenance=(_provenance(row, row["value"]),),
        )

    def share_series(
        self, symbol: str, as_of: str, family: ShareFamily = ShareFamily.PERIOD_END
    ) -> list[tuple[str, float, Provenance]]:
        """Observations of one share family, latest-filed per period, oldest first.

        Restatements are respected: a period reported twice keeps the value from
        the most recent filing available at ``as_of``.
        """
        rows = [
            r
            for metric in ("shares_outstanding", "shares_diluted")
            for r in self._known(symbol, metric, as_of)
            if r.get("period_end") and share_family(str(r["concept"])) is family
        ]
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            end = str(row["period_end"])
            held = latest.get(end)
            if held is None or str(row["filed"]) > str(held["filed"]):
                latest[end] = row
        return [
            (end, float(latest[end]["value"]), _provenance(latest[end], latest[end]["value"]))
            for end in sorted(latest)
        ]

    def latest_filing(self, symbol: str, as_of: str, forms: Sequence[str]) -> str | None:
        dates = [
            str(r["filed"])
            for metric in FLOW_METRICS | INSTANT_METRICS
            for r in self._known(symbol, metric, as_of)
            if str(r.get("form")) in forms
        ]
        return max(dates) if dates else None


def _choose_concept(metric: str, rows: list[dict[str, Any]]) -> tuple[str, str]:
    """Which XBRL concept represents this metric under the current regime.

    Historical frequency is the wrong signal: a company that switched taxonomy
    accumulates many observations under the concept it has since abandoned, and
    picking that one silently reports a stale or narrower measure. The concept
    used by the most recent filing wins; the declared priority order only breaks
    ties among concepts filed on the same day.
    """
    latest_filed = max(str(r["filed"]) for r in rows)
    current = {(str(r["concept"]), str(r["unit"])) for r in rows if str(r["filed"]) == latest_filed}
    priority = _CONCEPT_PRIORITY.get(metric, ())

    def rank(combo: tuple[str, str]) -> tuple[int, int]:
        concept = combo[0]
        order = priority.index(concept) if concept in priority else len(priority)
        coverage = -sum(1 for r in rows if str(r["concept"]) == concept)
        return (order, coverage)

    if current:
        return min(current, key=rank)
    combos = {(str(r["concept"]), str(r["unit"])) for r in rows}
    return min(combos, key=rank)


def _split_by_duration(
    selected: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Provenance], dict[Any, list[dict[str, Any]]]]:
    """Separate as-reported quarters from year-to-date cumulative facts."""
    quarters: dict[str, dict[str, Any]] = {}
    provenance: dict[str, Provenance] = {}
    cumulative: dict[Any, list[dict[str, Any]]] = {}
    for row in selected:
        span = _duration(row)
        if span is None:
            continue
        if _QUARTER_MIN <= span <= _QUARTER_MAX:
            end = str(row["period_end"])
            quarters[end] = row
            provenance[end] = _provenance(row, row["value"])
            # Also keep it in the cumulative chain: the first fiscal quarter IS
            # the first year-to-date period, and later quarters de-cumulate
            # against it. Omitting it would break the chain at Q2.
            cumulative.setdefault(row["period_start"], []).append(row)
        elif _QUARTER_MAX < span <= _ANNUAL_MAX:
            cumulative.setdefault(row["period_start"], []).append(row)
    return quarters, provenance, cumulative


def _provenance(row: dict[str, Any], value: Any) -> Provenance:
    return Provenance(
        concept=str(row["concept"]),
        unit=str(row["unit"]),
        form=None if row.get("form") is None else str(row["form"]),
        filed=None if row.get("filed") is None else str(row["filed"]),
        accession=None if row.get("accession") is None else str(row["accession"]),
        period_end=None if row.get("period_end") is None else str(row["period_end"]),
        value=float(value),
    )
