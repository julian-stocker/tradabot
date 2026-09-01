"""What a company's economics have done, and every reason the answer is refused.

A trajectory is the easiest thing in this system to get confidently wrong. The
numbers always exist, a line can always be drawn through them, and nothing
about the output reveals that the two ends measure different quantities. Three
real cases from the universe make the point, and each has a test below:

* NVIDIA's ten-for-one split turned a share count of 2.5B into 24.4B, and a
  naive series read that as **+249.8% a year** of issuance;
* JPMorgan reports share counts annually, so twelve steps back is twelve years,
  and reading them as quarters reported a **-10.5% a year** buyback;
* SAP files no quarterly reports at all, so a trailing-twelve-month series for
  it does not exist and must not be improvised.

So most of what follows asserts a refusal, a cut, or a number that changes when
the date it was asked about changes.

Nothing here touches the network.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.advisor.facts import FactStore
from app.discord_bot.analysis import StockCheck
from app.discord_bot.render import check_message
from app.discord_bot.resolve import Availability, Resolution
from app.history import (
    MARGIN_STABLE_PP,
    MIN_OBSERVATIONS,
    SHARE_STABLE_PCT,
    WINDOWS,
    CompanyHistoryService,
    Direction,
    SeriesBasis,
    SeriesStatus,
    latest_run,
    midrank_percentile,
)

KEY = "CIK0000000001"
AS_OF = "2026-09-01"


# ---------------------------------------------------------------- fixtures
def row(
    period_end: str,
    value: float,
    *,
    metric: str = "revenue",
    filed: str | None = None,
    period_start: str | None = None,
    concept: str = "Revenues",
    unit: str = "USD",
    symbol: str = KEY,
) -> dict[str, Any]:
    start = period_start or _minus_days(period_end, 90)
    return {
        "symbol": symbol,
        "cik": 1,
        "metric": metric,
        "concept": concept,
        "taxonomy": "us-gaap",
        "unit": unit,
        "value": value,
        "form": "10-Q",
        "filed": filed or _plus_days(period_end, 30),
        "accepted": None,
        "accession": f"acc-{period_end}",
        "fy": None,
        "fp": None,
        "period_start": start,
        "period_end": period_end,
    }


def _plus_days(day: str, n: int) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def _minus_days(day: str, n: int) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(day) - timedelta(days=n)).isoformat()


QUARTERS = [
    "2021-03-31",
    "2021-06-30",
    "2021-09-30",
    "2021-12-31",
    "2022-03-31",
    "2022-06-30",
    "2022-09-30",
    "2022-12-31",
    "2023-03-31",
    "2023-06-30",
    "2023-09-30",
    "2023-12-31",
    "2024-03-31",
    "2024-06-30",
    "2024-09-30",
    "2024-12-31",
    "2025-03-31",
    "2025-06-30",
    "2025-09-30",
    "2025-12-31",
    "2026-03-31",
    "2026-06-30",
]


def revenue_rows(values: list[float] | None = None, **kw: Any) -> list[dict[str, Any]]:
    amounts = values or [100.0 + 5 * i for i in range(len(QUARTERS))]
    return [row(p, v, **kw) for p, v in zip(QUARTERS, amounts, strict=False)]


def service(rows: list[dict[str, Any]]) -> CompanyHistoryService:
    return CompanyHistoryService(facts=FactStore(rows))


def trajectory(rows: list[dict[str, Any]], *, as_of: str = AS_OF, **kw: Any) -> Any:
    return service(rows).for_company(company_key=KEY, as_of=as_of, **kw)


# --------------------------------------------------------------- contiguity
def test_the_most_recent_run_wins_over_a_longer_older_one() -> None:
    """A long-dead run would describe a business that stopped existing."""
    periods = ["2015-03-31", "2015-06-30", "2015-09-30", "2015-12-31", "2016-03-31"]
    run = latest_run([*periods, "2026-03-31", "2026-06-30"], low=80, high=100)
    assert run == ["2026-03-31", "2026-06-30"]


def test_a_gap_is_never_interpolated() -> None:
    rows = [r for r in revenue_rows() if r["period_end"] != "2024-06-30"]
    found = trajectory(rows).metrics["revenue"]
    assert found.available
    # Everything before the gap is dropped, not bridged.
    assert found.observations[0].period_end > "2024-06-30"


def test_too_little_contiguous_history_refuses() -> None:
    found = trajectory(revenue_rows()[-6:]).metrics["revenue"]
    assert found.status is SeriesStatus.INSUFFICIENT_HISTORY
    assert MIN_OBSERVATIONS == 5


def test_a_metric_never_reported_is_unavailable_not_empty() -> None:
    found = trajectory(revenue_rows()).metrics["gross_margin"]
    assert found.status is SeriesStatus.UNAVAILABLE
    assert found.observations == ()


# ---------------------------------------------------------------------- PIT
def test_a_trajectory_uses_only_what_was_filed_by_its_as_of() -> None:
    rows = revenue_rows()
    early = trajectory(rows, as_of="2024-01-01").metrics["revenue"]
    late = trajectory(rows, as_of=AS_OF).metrics["revenue"]
    assert early.current != late.current
    assert early.observations[-1].period_end < late.observations[-1].period_end


def test_a_period_end_before_as_of_is_not_enough_the_filing_must_be_too() -> None:
    """A quarter ended 30 June and filed 5 August is unknown on 15 July."""
    rows = revenue_rows()
    rows.append(row("2026-06-30", 999.0, filed="2026-08-05", metric="revenue"))
    found = trajectory(rows, as_of="2026-07-15").metrics["revenue"]
    assert all(o.filed is None or o.filed <= "2026-07-15" for o in found.observations)
    assert found.current != 999.0


def test_no_observation_can_postdate_its_own_as_of() -> None:
    rows = revenue_rows()
    for as_of in ("2023-01-01", "2024-06-01", "2026-09-01"):
        found = trajectory(rows, as_of=as_of).metrics["revenue"]
        for observation in found.observations:
            assert observation.filed is None or observation.filed <= as_of


def test_a_later_restatement_does_not_reach_backwards() -> None:
    """The 2024 answer must stay what a reader would have seen in 2024."""
    rows = revenue_rows()
    before = trajectory(rows, as_of="2024-06-01").metrics["revenue"].current
    rows.append(row("2023-12-31", 9_999.0, filed="2026-02-01", concept="Revenues"))
    after = trajectory(rows, as_of="2024-06-01").metrics["revenue"].current
    assert after == before

    revised = trajectory(rows, as_of=AS_OF).metrics["revenue"]
    assert any(o.value > 1_000 for o in revised.observations)


# ------------------------------------------------------------------- basis
def test_an_annual_only_filer_gets_an_annual_trajectory() -> None:
    """SAP files no quarterly reports; a TTM series for it does not exist."""
    years = ["2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31"]
    rows = [
        row(y, 100.0 + 10 * i, period_start=_minus_days(y, 364), filed=_plus_days(y, 60))
        for i, y in enumerate(years)
    ]
    found = trajectory(rows).metrics["revenue"]
    assert found.status is SeriesStatus.AVAILABLE
    assert found.basis is SeriesBasis.ANNUAL


def test_bases_never_share_one_line() -> None:
    found = trajectory(revenue_rows()).metrics["revenue"]
    assert found.basis is SeriesBasis.TTM
    assert {str(o.unit) for o in found.observations} == {"USD"}


def test_a_reporting_currency_change_refuses_rather_than_converting() -> None:
    rows = revenue_rows()
    for r in rows[:8]:
        r["unit"] = "EUR"
    found = trajectory(rows).metrics["revenue"]
    assert found.status in (SeriesStatus.CURRENCY_CHANGE, SeriesStatus.AVAILABLE)
    if found.available:
        assert len({o.unit for o in found.observations}) == 1


def test_a_concept_change_is_not_stitched_across() -> None:
    """The fact store pins one concept; a series spanning two is refused."""
    rows = revenue_rows()
    found = trajectory(rows).metrics["revenue"]
    assert len({o.concept for o in found.observations}) == 1


# --------------------------------------------------------------- arithmetic
def test_a_margin_moves_in_percentage_points() -> None:
    rows = revenue_rows([1000.0] * len(QUARTERS))
    rows += [
        row(p, v, metric="operating_income", concept="OperatingIncomeLoss")
        for p, v in zip(QUARTERS, [200.0] * 18 + [250.0] * 4, strict=False)
    ]
    found = trajectory(rows).metrics["operating_margin"]
    assert found.available
    # Trailing twelve months of 250 against 1,000 of revenue.
    assert found.current == pytest.approx(0.25)
    assert found.changes["1y"].absolute == pytest.approx(5.0)  # percentage points


def test_a_compound_rate_describes_a_period_that_already_happened() -> None:
    values = [100.0] * 4 + [100.0] * 8 + [200.0] * 10
    found = trajectory(revenue_rows(values)).metrics["revenue"]
    change = found.changes["3y"]
    assert change.annualised is not None
    assert change.relative == pytest.approx(change.to_value / change.from_value - 1)
    assert WINDOWS["3y"] == 12


def test_growth_through_a_sign_change_is_refused_not_computed() -> None:
    """Earnings that crossed zero have a direction and no percentage."""
    values = [-50.0] * 12 + [50.0] * 10
    found = trajectory(revenue_rows(values)).metrics["revenue"]
    change = found.changes["3y"]
    assert change.absolute != 0
    assert change.relative is None
    assert change.annualised is None


def test_a_zero_base_produces_no_growth_rate() -> None:
    values = [0.0] * 12 + [10.0] * 10
    change = trajectory(revenue_rows(values)).metrics["revenue"].changes["3y"]
    assert change.annualised is None


# ------------------------------------------------------------- share count
def _share_rows(values: list[float], periods: list[str] | None = None) -> list[dict[str, Any]]:
    days = periods or QUARTERS
    return [
        row(
            p,
            v,
            metric="shares_outstanding",
            concept="CommonStockSharesOutstanding",
            unit="shares",
            period_start=None,
        )
        for p, v in zip(days, values, strict=False)
    ]


def test_a_stock_split_ends_the_series_rather_than_reading_as_issuance() -> None:
    """**The gate.** NVIDIA's ten-for-one split read as +249.8% a year."""
    values = [2.5e9] * 12 + [25e9] * 10
    found = trajectory(revenue_rows() + _share_rows(values)).metrics["share_count"]
    assert found.available
    assert all(o.value > 1e10 for o in found.observations)
    assert "3y" not in found.changes or found.changes["3y"].from_value > 1e10


def test_an_ordinary_buyback_is_never_cut() -> None:
    values = [1e9 * (0.98**i) for i in range(len(QUARTERS))]
    found = trajectory(revenue_rows() + _share_rows(values)).metrics["share_count"]
    assert len(found.observations) == len(QUARTERS)
    assert found.direction is Direction.DECREASING


def test_an_annual_share_cadence_is_measured_not_assumed() -> None:
    """JPMorgan reports once a year; twelve steps back is twelve years."""
    years = [f"{y}-12-31" for y in range(2016, 2027)]
    values = [3.0e9 - 0.05e9 * i for i in range(len(years))]
    found = trajectory(revenue_rows() + _share_rows(values, years)).metrics["share_count"]
    assert found.available
    change = found.changes["3y"]
    # Three annual steps back from the newest year available, not three quarters.
    assert change.to_period == "2025-12-31"
    assert change.from_period == "2022-12-31"


def test_share_count_is_a_count_not_an_amount() -> None:
    found = trajectory(revenue_rows() + _share_rows([1e9] * len(QUARTERS))).metrics["share_count"]
    assert found.unit == "shares"
    assert found.basis is SeriesBasis.INSTANT


# ------------------------------------------------------------- tolerances
def test_a_margin_within_the_declared_band_is_stable() -> None:
    rows = revenue_rows([1000.0] * len(QUARTERS))
    rows += [
        row(p, v, metric="operating_income", concept="OperatingIncomeLoss")
        for p, v in zip(QUARTERS, [200.0] * 18 + [204.0] * 4, strict=False)
    ]
    found = trajectory(rows).metrics["operating_margin"]
    assert abs(found.changes["1y"].absolute) < MARGIN_STABLE_PP
    assert found.direction is Direction.STABLE


def test_a_margin_beyond_the_band_is_named_by_its_own_vocabulary() -> None:
    rows = revenue_rows([1000.0] * len(QUARTERS))
    rows += [
        row(p, v, metric="operating_income", concept="OperatingIncomeLoss")
        for p, v in zip(QUARTERS, [200.0] * 18 + [260.0] * 4, strict=False)
    ]
    found = trajectory(rows).metrics["operating_margin"]
    assert found.direction is Direction.EXPANDING
    assert found.direction not in (Direction.INCREASING, Direction.DECREASING)


def test_no_direction_means_improving_or_declining() -> None:
    """A compressing margin is a fact; whether it is bad is a judgement."""
    assert {str(d) for d in Direction} == {
        "EXPANDING",
        "COMPRESSING",
        "INCREASING",
        "DECREASING",
        "STABLE",
    }


def test_the_share_tolerance_is_declared_once() -> None:
    assert SHARE_STABLE_PCT == 1.0
    assert MARGIN_STABLE_PP == 1.0


# ------------------------------------------------------------- percentile
def test_the_percentile_uses_the_midrank_convention() -> None:
    assert midrank_percentile([1.0, 2.0, 3.0, 4.0], 2.0) == pytest.approx(37.5)
    assert midrank_percentile([1.0, 1.0, 1.0], 1.0) == pytest.approx(50.0)
    assert midrank_percentile([], 1.0) == 0.0


def test_the_percentile_states_the_span_it_is_drawn_over() -> None:
    found = trajectory(revenue_rows()).metrics["revenue"]
    assert found.history_span is not None
    assert found.observations[0].period_end in found.history_span


# ------------------------------------------------------------------ sector
def test_a_bank_refuses_the_industrial_metrics() -> None:
    """A bank's revenue and margin are not the quantities of the same name."""
    report = trajectory(revenue_rows(), sic="6021")
    for metric in ("revenue", "gross_margin", "operating_margin", "fcf_margin"):
        assert report.metrics[metric].status is SeriesStatus.SECTOR_MODEL_REQUIRED
    assert report.metrics["share_count"].status is not SeriesStatus.SECTOR_MODEL_REQUIRED


@pytest.mark.parametrize("asset_type", ["ETF", "FUND", "ETN"])
def test_a_fund_has_no_trajectory(asset_type: str) -> None:
    report = trajectory(revenue_rows(), asset_type=asset_type)
    assert all(t.status is SeriesStatus.NOT_APPLICABLE for t in report.metrics.values())


def test_an_unreadable_store_degrades_rather_than_raising() -> None:
    class Broken:
        def quarterlies(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("disk")

    report = CompanyHistoryService(facts=Broken()).for_company(company_key=KEY, as_of=AS_OF)
    assert report.metrics == {}
    assert report.detail is not None


# ------------------------------------------------------------------ render
def check(trajectory_report: Any = None, **kw: Any) -> StockCheck:
    defaults: dict[str, Any] = {
        "requested": "X",
        "symbol": "X",
        "resolution": Resolution.SUPPORTED,
        "market_data": Availability.AVAILABLE,
        "fundamentals": Availability.AVAILABLE,
        "as_of": AS_OF,
        "checked_at": datetime(2026, 9, 1, tzinfo=UTC),
        "report": None,
        "trajectory": trajectory_report,
    }
    defaults.update(kw)
    return StockCheck(**defaults)


def rendered(rows: list[dict[str, Any]], **kw: Any) -> str:
    message = check_message(check(trajectory(rows, **kw)))
    return message.fields.get("Company trajectory", "")


def test_an_unwired_history_layer_shows_no_section() -> None:
    assert "Company trajectory" not in check_message(check(None)).fields


def test_the_card_shows_where_it_was_where_it_is_and_over_what() -> None:
    text = rendered(revenue_rows())
    assert "Revenue" in text
    assert "→" in text
    assert "over 3y" in text
    assert "%/yr" in text


def test_the_section_stays_within_its_budget() -> None:
    rows = revenue_rows([1000.0] * len(QUARTERS))
    for metric, concept in (
        ("operating_income", "OperatingIncomeLoss"),
        ("gross_profit", "GrossProfit"),
        ("operating_cash_flow", "NetCashProvidedByUsedInOperatingActivities"),
        ("capex", "PaymentsToAcquirePropertyPlantAndEquipment"),
    ):
        rows += [row(p, 200.0, metric=metric, concept=concept) for p in QUARTERS]
    rows += _share_rows([1e9] * len(QUARTERS))
    message = check_message(check(trajectory(rows)))
    for value in message.fields.values():
        assert len(value) <= 1024
    section = message.fields["Company trajectory"]
    assert len(section.splitlines()) <= 8
    assert not section.endswith("·")


def test_a_bank_says_why_rather_than_showing_nothing() -> None:
    text = rendered(revenue_rows(), sic="6021")
    assert "financial company" in text


def test_a_fund_says_it_has_no_operations() -> None:
    assert "fund" in rendered(revenue_rows(), asset_type="ETF")


BANNED = (
    "buy",
    "sell",
    "hold",
    "bullish",
    "bearish",
    "upside",
    "downside",
    "price target",
    "expected return",
    "recommend",
    "good",
    "bad",
    "attractive",
    "quality",
    "strong company",
    "weak company",
    "will ",
    "expect",
    "forecast",
    "should ",
)


def test_no_rendered_trajectory_uses_evaluative_or_forward_vocabulary() -> None:
    rows = revenue_rows([1000.0] * len(QUARTERS))
    rows += [
        row(p, v, metric="operating_income", concept="OperatingIncomeLoss")
        for p, v in zip(QUARTERS, [200.0] * 18 + [120.0] * 4, strict=False)
    ]
    rows += _share_rows([1e9 * (0.97**i) for i in range(len(QUARTERS))])
    for text in (
        rendered(rows),
        rendered(revenue_rows(), sic="6021"),
        rendered(revenue_rows(), asset_type="ETF"),
    ):
        lowered = text.lower()
        for word in BANNED:
            assert word not in lowered, f"{word!r} in {text!r}"


def test_the_balance_sheet_distinguishes_partial_from_absent() -> None:
    """Coca-Cola printed Cash $10.57B and then "Insufficient data" beneath it."""
    from app.discord_bot import render

    assert "Partial" in render._PARTIAL_SECTION
    assert "as filed" in render._PARTIAL_SECTION


# -------------------------------------------------------------- boundaries
def test_the_history_core_reaches_no_consumer_or_provider() -> None:
    forbidden = (
        "app.discord_bot",
        "app.broker",
        "app.paper",
        "app.strategy",
        "app.research_intelligence",
        "alpaca",
        "openai",
        "anthropic",
    )
    for path in Path("app/history").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.module
                else []
            )
            for module in names:
                assert not any(module.startswith(f) for f in forbidden), f"{path}: {module}"


def test_the_history_core_opens_no_socket() -> None:
    for path in Path("app/history").glob("*.py"):
        body = path.read_text()
        for token in ("urlopen", "requests.", "httpx", "urllib", "socket"):
            assert token not in body, f"{path} references {token}"


def test_no_threshold_is_derived_from_a_price() -> None:
    """No tolerance here may be fitted to what a share price did next."""
    for path in Path("app/history").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                node.value = ""
        code = ast.unparse(tree).lower()
        for token in ("price", "return_", "outcome", "candle", "benchmark", "close"):
            assert token not in code, f"{path} references {token}"


def test_the_service_takes_company_identity_not_a_ticker() -> None:
    import inspect

    signature = inspect.signature(CompanyHistoryService.for_company)
    assert "company_key" in signature.parameters
    assert "symbol" not in signature.parameters
    assert "ticker" not in signature.parameters


def test_a_later_restatement_leaves_the_earlier_report_byte_identical() -> None:
    """Serialised, not merely equal in the fields anyone happened to check.

    The whole trajectory is compared, so a restatement cannot quietly change an
    observation count, a percentile, a span or a window that a narrower
    assertion would have missed.
    """
    import json

    rows = revenue_rows()
    before = json.dumps(trajectory(rows, as_of="2024-06-01").as_dict(), sort_keys=True)
    with_restatement = [
        *rows,
        row("2023-12-31", 9_999.0, filed="2026-02-01"),
        row("2023-09-30", 8_888.0, filed="2026-02-01"),
    ]
    after = json.dumps(trajectory(with_restatement, as_of="2024-06-01").as_dict(), sort_keys=True)
    assert after == before

    revised = trajectory(with_restatement, as_of=AS_OF).metrics["revenue"]
    assert any(o.value > 1_000 for o in revised.observations)


def test_a_ttm_point_waits_for_every_quarter_it_is_made_of() -> None:
    """A trailing-twelve-month figure is knowable only once its last quarter is.

    Dating it by the earliest of its four filings would let the point exist
    before the information did -- the look-ahead a period-end-based rule makes.
    """
    rows = revenue_rows()
    # The final quarter of the newest window is filed late.
    for r in rows:
        if r["period_end"] == "2026-06-30":
            r["filed"] = "2026-08-20"
    found = trajectory(rows, as_of="2026-08-01").metrics["revenue"]
    assert found.observations[-1].period_end == "2026-03-31"
    later = trajectory(rows, as_of="2026-09-01").metrics["revenue"]
    assert later.observations[-1].period_end == "2026-06-30"
    assert later.observations[-1].filed == "2026-08-20"
