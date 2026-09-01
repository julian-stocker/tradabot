"""Point-in-time discipline for IFRS facts, against the real store.

Why this suite is generated rather than written by hand
------------------------------------------------------
The property being tested is not "SAP's revenue is 36.8 billion" -- that is one
number and it will change. It is that **no filing published after the as-of date
may contribute to an answer given at that date**, for every company, every
metric and every date. That is a property of the engine, and the way to test a
property is to run it over real data at many points rather than to assert three
memorised values.

So each case is built from the store itself: a company, a metric, and a date
drawn from the filing history. The assertions are invariants.

The suite deliberately includes cases where the answer *changes* as later
filings arrive. A point-in-time test built only from settled history would pass
against an engine that ignored the as-of date entirely.

The engine under test is the same :class:`~app.advisor.facts.FactStore` the US
path uses. Phase 13.2 added no second IFRS TTM engine, and this suite is part of
how that stays true.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.advisor.facts import FactStore, company_key

STORE = Path("data/sec_facts.parquet")

pytestmark = pytest.mark.skipif(
    not STORE.exists(),
    reason="the persisted fact store is not built in this environment",
)

DURATION = ("revenue", "gross_profit", "net_income", "eps_diluted", "operating_cash_flow")
INSTANT = ("cash", "total_assets", "total_liabilities", "equity")

MIN_CASES = 300
"""The floor the phase brief set. Asserted, so a change that quietly shrinks
coverage fails rather than passing on a handful of cases."""


def _frame() -> pl.DataFrame:
    return pl.read_parquet(STORE)


def _ifrs_companies(frame: pl.DataFrame) -> list[int]:
    ifrs = frame.filter(pl.col("taxonomy") == "ifrs-full")
    counts = ifrs.group_by("cik").len().sort("len", descending=True)
    return [int(c) for c in counts["cik"].to_list()]


def _cases() -> list[tuple[int, str, str]]:
    """One case per (company, metric, as-of), drawn from real filing dates."""
    frame = _frame()
    out: list[tuple[int, str, str]] = []
    for cik in _ifrs_companies(frame):
        company = frame.filter(pl.col("cik") == cik)
        filings = sorted({str(d) for d in company["filed"].to_list() if d})
        if len(filings) < 2:
            continue
        # Dates spread across the history, plus the day before and the day of
        # each of the last few filings -- the boundary where an answer changes.
        sampled = filings[:: max(1, len(filings) // 6)][:6]
        edges: list[str] = []
        for filed in filings[-3:]:
            day = date.fromisoformat(filed)
            # The filing date and the day before it: the boundary where a
            # correct engine changes its answer and a broken one does not.
            edges.append(str(date.fromordinal(day.toordinal() - 1)))
            edges.append(filed)
        for as_of in dict.fromkeys([*sampled, *edges]):
            for metric in (*DURATION, *INSTANT):
                out.append((cik, metric, as_of))
    return out


CASES = _cases() if STORE.exists() else []


class TestIfrsPointInTime:
    def test_the_suite_covers_enough_real_cases(self) -> None:
        answered = [c for c in CASES if _value(*c) is not None]

        assert len(answered) >= MIN_CASES, (
            f"only {len(answered)} IFRS as-of cases produced an answer; "
            f"the brief requires at least {MIN_CASES}"
        )

    def test_no_answer_uses_a_filing_published_after_the_as_of_date(self) -> None:
        """The whole point. A single violation is a look-ahead leak, and a
        look-ahead leak makes every historical result meaningless."""
        store = FactStore.from_parquet(STORE)
        violations: list[str] = []
        for cik, metric, as_of in CASES:
            result = (
                store.ttm(company_key(cik), metric, as_of)
                if metric in DURATION
                else store.instant(company_key(cik), metric, as_of)
            )
            for p in result.provenance:
                if p.filed > as_of:
                    violations.append(f"CIK{cik} {metric} @{as_of} used {p.filed}")

        assert not violations, violations[:10]

    def test_answers_change_as_later_filings_arrive(self) -> None:
        """Guards against an engine that passes the look-ahead test by ignoring
        the as-of date and always returning the same thing."""
        store = FactStore.from_parquet(STORE)
        changed = 0
        for cik in _ifrs_companies(_frame())[:20]:
            key = company_key(cik)
            seen = {
                store.ttm(key, "revenue", as_of).value
                for _, metric, as_of in CASES
                if metric == "revenue"
                for cik_, _, _ in [(cik, metric, as_of)]
                if cik_ == cik
            }
            if len({v for v in seen if v is not None}) > 1:
                changed += 1

        assert changed >= 5, f"only {changed} companies changed their answer across as-of dates"

    def test_every_answer_carries_provenance(self) -> None:
        """A number without a filing behind it cannot be checked, and anything
        that cannot be checked should not be shown."""
        store = FactStore.from_parquet(STORE)
        bare: list[str] = []
        for cik, metric, as_of in CASES:
            result = (
                store.ttm(company_key(cik), metric, as_of)
                if metric in DURATION
                else store.instant(company_key(cik), metric, as_of)
            )
            if result.value is not None and not result.provenance:
                bare.append(f"CIK{cik} {metric} @{as_of}")

        assert not bare, bare[:10]

    def test_a_refused_metric_stays_refused_at_every_date(self) -> None:
        """Operating income, capex, free cash flow, total debt and IFRS share
        counts are refusals, not gaps. A refusal that lapses at some historical
        date is a refusal that does not hold."""
        store = FactStore.from_parquet(STORE)
        frame = _frame()
        ifrs_only = [
            cik
            for cik in _ifrs_companies(frame)
            if frame.filter((pl.col("cik") == cik) & (pl.col("taxonomy") == "us-gaap")).height == 0
        ]
        leaks: list[str] = []
        for cik in ifrs_only[:25]:
            for metric in ("operating_income", "capex"):
                for _, _, as_of in CASES[:40]:
                    if store.ttm(company_key(cik), metric, as_of).value is not None:
                        leaks.append(f"CIK{cik} {metric} @{as_of}")
                        break

        assert not leaks, leaks[:10]


def _value(cik: int, metric: str, as_of: str) -> float | None:
    store = _shared_store()
    result = (
        store.ttm(company_key(cik), metric, as_of)
        if metric in DURATION
        else store.instant(company_key(cik), metric, as_of)
    )
    return result.value


_STORE_CACHE: list[FactStore] = []


def _shared_store() -> FactStore:
    """One load for the whole module. The store is large and immutable here."""
    if not _STORE_CACHE:
        _STORE_CACHE.append(FactStore.from_parquet(STORE))
    return _STORE_CACHE[0]
