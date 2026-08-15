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
    "WeightedAverageNumberOfDilutedSharesOutstanding": (
        ShareFamily.WEIGHTED_AVERAGE_DILUTED
    ),
}


def share_family(concept: str) -> ShareFamily:
    """Which semantic family a share concept belongs to."""
    return SHARE_FAMILY_BY_CONCEPT.get(concept, ShareFamily.AMBIGUOUS)


_QUARTER_MIN, _QUARTER_MAX = 80, 100
_ANNUAL_MIN, _ANNUAL_MAX = 350, 380
_STALE_DAYS = 200
_TTM_QUARTERS = 4


def _duration(row: dict[str, Any]) -> int | None:
    start, end = row.get("period_start"), row.get("period_end")
    if not start or not end:
        return None
    try:
        return (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days
    except ValueError:
        return None


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
        for row in rows:
            filed, value = row.get("filed"), row.get("value")
            if filed is None or value is None:
                continue
            key = (str(row["symbol"]), str(row["metric"]))
            self._by.setdefault(key, []).append(row)

    @classmethod
    def from_parquet(cls, path: str | Path) -> FactStore:
        frame = pl.read_parquet(path)
        return cls(frame.iter_rows(named=True))

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
        selected = [
            r for r in latest.values() if (str(r["concept"]), str(r["unit"])) == chosen
        ]

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
        row = latest[end]
        age = (date.fromisoformat(as_of) - date.fromisoformat(end)).days
        return TtmResult(
            value=float(row["value"]),
            basis=DenominatorBasis.FY_FALLBACK,
            status="FY_FALLBACK",
            denominator_age_days=age,
            provenance=(_provenance(row, row["value"]),),
        )

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


def _choose_concept(
    metric: str, rows: list[dict[str, Any]]
) -> tuple[str, str]:
    """Which XBRL concept represents this metric under the current regime.

    Historical frequency is the wrong signal: a company that switched taxonomy
    accumulates many observations under the concept it has since abandoned, and
    picking that one silently reports a stale or narrower measure. The concept
    used by the most recent filing wins; the declared priority order only breaks
    ties among concepts filed on the same day.
    """
    latest_filed = max(str(r["filed"]) for r in rows)
    current = {
        (str(r["concept"]), str(r["unit"]))
        for r in rows
        if str(r["filed"]) == latest_filed
    }
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
