"""A metric the company stopped reporting is not that company's current figure.

The defect
----------
Agnico Eagle moved from us-gaap to ifrs-full in 2015. ``OperatingIncomeLoss``
stopped being filed then, and because the store answered "the freshest fact for
this metric" rather than "this company's current operating income", it kept
serving a value from a filing dated November 2013.

The card printed that beside 2025 revenue, and the Advisor divided one by the
other to produce an operating margin of 3.6%. Every input was a real filed fact
and the ratio was invented.

The rule
--------
Lag is measured against **the company's own latest reported period**, never the
clock. An annual filer is not penalised for filing annually; a company that
files quarterly and stopped reporting one line eighteen months ago has skipped a
reporting cycle it should have had.

The judgement is itself point-in-time: a metric is not retroactively abandoned
at a date when it was still being filed.
"""

from __future__ import annotations

from typing import Any

from app.advisor.facts import ABANDONED_LAG_DAYS, FactStore, company_key

CIK = 2809


def _fact(
    metric: str,
    period_end: str,
    filed: str,
    value: float,
    *,
    start: str | None = None,
    concept: str = "X",
    taxonomy: str = "us-gaap",
) -> dict[str, Any]:
    return {
        "symbol": "TEST",
        "cik": CIK,
        "metric": metric,
        "concept": concept,
        "taxonomy": taxonomy,
        "unit": "USD",
        "value": value,
        "form": "10-K",
        "filed": filed,
        "accepted": None,
        "accession": f"acc-{period_end}",
        "fy": int(period_end[:4]),
        "fp": "FY",
        "period_start": start,
        "period_end": period_end,
    }


def _annual(metric: str, year: int, value: float, **kw: Any) -> dict[str, Any]:
    return _fact(metric, f"{year}-12-31", f"{year + 1}-02-15", value, start=f"{year}-01-01", **kw)


def _instant(metric: str, year: int, value: float) -> dict[str, Any]:
    return _fact(metric, f"{year}-12-31", f"{year + 1}-02-15", value)


KEY = company_key(CIK)
NOW = "2026-08-14"


class TestAbandonedDurationSeries:
    def test_a_metric_the_company_stopped_reporting_is_withheld(self) -> None:
        """The Agnico Eagle case, reduced to its essentials."""
        store = FactStore(
            [
                *(_annual("revenue", y, 1.0e10) for y in range(2020, 2026)),
                _annual("operating_income", 2013, 4.29e8),
            ]
        )

        assert store.ttm(KEY, "revenue", NOW).value is not None
        result = store.ttm(KEY, "operating_income", NOW)
        assert result.value is None
        assert result.status == "ABANDONED_SERIES"

    def test_a_currently_reported_metric_is_untouched(self) -> None:
        store = FactStore(
            [
                *(_annual("revenue", y, 1.0e10) for y in range(2020, 2026)),
                *(_annual("operating_income", y, 2.0e9) for y in range(2020, 2026)),
            ]
        )

        assert store.ttm(KEY, "operating_income", NOW).value == 2.0e9

    def test_an_annual_filer_is_not_punished_for_filing_annually(self) -> None:
        """Lag is measured against the company, not the calendar. Every metric
        here is a year old and none of them is abandoned."""
        store = FactStore(
            [
                _annual("revenue", 2025, 1.0e10),
                _annual("operating_income", 2025, 2.0e9),
            ]
        )

        assert store.ttm(KEY, "operating_income", NOW).value == 2.0e9

    def test_a_metric_one_reporting_cycle_behind_survives(self) -> None:
        """A single missed quarter is ordinary reporting noise, not abandonment."""
        store = FactStore(
            [
                _annual("revenue", 2025, 1.0e10),
                _annual("operating_income", 2024, 2.0e9),
            ]
        )

        assert store.ttm(KEY, "operating_income", NOW).value == 2.0e9

    def test_the_threshold_is_where_it_is_declared(self) -> None:
        """Just inside and just outside ABANDONED_LAG_DAYS, so the constant and
        the behaviour cannot drift apart."""
        from datetime import date, timedelta

        latest = date(2025, 12, 31)
        inside = latest - timedelta(days=ABANDONED_LAG_DAYS - 5)
        outside = latest - timedelta(days=ABANDONED_LAG_DAYS + 5)

        def store_for(when: date) -> FactStore:
            return FactStore(
                [
                    _annual("revenue", 2025, 1.0e10),
                    _fact("equity", when.isoformat(), "2024-01-15", 5.0e9),
                ]
            )

        assert store_for(inside).instant(KEY, "equity", NOW).value == 5.0e9
        assert store_for(outside).instant(KEY, "equity", NOW).value is None


class TestAbandonedInstantSeries:
    def test_a_stale_balance_sheet_line_is_withheld(self) -> None:
        store = FactStore(
            [
                *(_instant("total_assets", y, 9.0e10) for y in range(2020, 2026)),
                _instant("total_liabilities", 2018, 4.0e10),
            ]
        )

        assert store.instant(KEY, "total_assets", NOW).value is not None
        assert store.instant(KEY, "total_liabilities", NOW).value is None

    def test_a_company_with_one_metric_only_is_not_self_abandoning(self) -> None:
        """Its own fact is the freshest thing it reported, so the lag is zero."""
        store = FactStore([_instant("cash", 2019, 1.0e9)])

        assert store.instant(KEY, "cash", NOW).value == 1.0e9


class TestPointInTimeHonoured:
    def test_a_metric_is_not_retroactively_abandoned(self) -> None:
        """Asked as of 2014, operating income was the company's current figure
        and must still be answered. Abandonment is a fact about later history,
        and reading it backwards would corrupt every historical result."""
        store = FactStore(
            [
                *(_annual("revenue", y, 1.0e10) for y in range(2012, 2026)),
                _annual("operating_income", 2013, 4.29e8),
            ]
        )

        assert store.ttm(KEY, "operating_income", "2014-06-30").value == 4.29e8
        assert store.ttm(KEY, "operating_income", NOW).value is None

    def test_the_yardstick_itself_respects_the_as_of_date(self) -> None:
        """The company's latest period is read as of the query date too, so a
        filing published after ``as_of`` cannot make an older metric look
        abandoned."""
        store = FactStore(
            [
                _annual("revenue", 2025, 1.0e10),
                _annual("operating_income", 2020, 2.0e9),
            ]
        )

        assert store.ttm(KEY, "operating_income", "2021-06-30").value == 2.0e9
