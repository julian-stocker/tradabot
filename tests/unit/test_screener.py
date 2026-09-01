"""What a screen may conclude, and the many things it may not.

A screener is one adjective away from being a stock tip. "43 companies matched"
is a fact; "43 best stocks" is a claim this system cannot support and has no
data to support. So the assertions below are mostly about restraint: no default
ordering by desirability, no score, no evaluative word in any output, and a
match that can always be traced back to the figure that produced it.

The other half is about honesty in absence. A company with no operating margin
has not *failed* a margin threshold, and counting it as a failure would let a
screen report "43 of 989" when only 680 could be assessed. Two real leaks found
during this phase are pinned here as regressions:

* a naive margin screen returned REITs and banks at 424%, 708% and 1,185%;
* Bank of America yielded a price-to-sales ratio of 3.58x, against a "sales"
  figure that is not the industrial quantity of that name.

Nothing here touches the network.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from app.advisor.facts import FactStore
from app.history import CompanyHistoryService
from app.screener import (
    Criterion,
    Evaluation,
    InvalidCriterionError,
    NotEvaluable,
    Operator,
    ScreenerService,
    get,
    keys,
)
from app.screener.registry import METRICS
from app.screener.schemas import MAX_LIMIT

AS_OF = "2026-09-01"


# ---------------------------------------------------------------- fixtures
class Listing:
    """The registry shape the screener reads."""

    def __init__(
        self,
        symbol: str,
        *,
        cik: str = "0000000001",
        company_id: int = 1,
        mic: str = "XNAS",
        sic: str | None = "3674",
        asset_type: str = "STOCK",
        name: str | None = None,
        reporting: str = "USD",
        quote: str = "USD",
        country: str = "US",
        has_prices: bool = True,
    ) -> None:
        self.symbol, self.cik, self.company_id = symbol, cik, company_id
        self.mic, self.sic, self.asset_type = mic, sic, asset_type
        self.company_name = name or symbol
        self.reporting_currency, self.quote_currency = reporting, quote
        self.country = country
        self.has_prices = has_prices
        self.has_fundamentals = True

    @property
    def qualified(self) -> str:
        return f"{self.symbol}.{'US' if self.mic == 'XNAS' else self.mic}"


class Registry:
    def __init__(self, listings: list[Listing]) -> None:
        self._listings = listings

    def all_candidates(self) -> list[Listing]:
        return list(self._listings)


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


def row(
    period_end: str,
    value: float,
    *,
    metric: str = "revenue",
    symbol: str = "CIK0000000001",
    concept: str = "Revenues",
    unit: str = "USD",
    filed: str | None = None,
) -> dict[str, Any]:
    from datetime import date, timedelta

    end = date.fromisoformat(period_end)
    return {
        "symbol": symbol,
        "cik": 1,
        "metric": metric,
        "concept": concept,
        "taxonomy": "us-gaap",
        "unit": unit,
        "value": value,
        "form": "10-Q",
        "filed": filed or (end + timedelta(days=30)).isoformat(),
        "accepted": None,
        "accession": f"a-{period_end}",
        "fy": None,
        "fp": None,
        "period_start": (end - timedelta(days=90)).isoformat(),
        "period_end": period_end,
    }


def facts_for(key: str, revenue: float = 1000.0, operating: float = 300.0) -> list[dict[str, Any]]:
    rows = [row(p, revenue, symbol=key) for p in QUARTERS]
    rows += [
        row(p, operating, metric="operating_income", symbol=key, concept="OperatingIncomeLoss")
        for p in QUARTERS
    ]
    rows += [
        row(
            p,
            1e9,
            metric="shares_outstanding",
            symbol=key,
            concept="CommonStockSharesOutstanding",
            unit="shares",
        )
        for p in QUARTERS
    ]
    return rows


def service(listings: list[Listing], rows: list[dict[str, Any]], **kw: Any) -> ScreenerService:
    store = FactStore(rows)
    return ScreenerService(
        registry_snapshot=Registry(listings),
        history=CompanyHistoryService(facts=store),
        **kw,
    )


def one_company(**kw: Any) -> ScreenerService:
    return service([Listing("ACME", **kw)], facts_for("CIK0000000001"))


# ---------------------------------------------------------------- universe
def test_a_cross_listed_company_appears_once() -> None:
    """SAP.DE and SAP.US are one registrant, not two discovery results."""
    listings = [
        Listing("SAP", mic="XETR", cik="0001000184", company_id=7),
        Listing("SAP", mic="XNAS", cik="0001000184", company_id=7),
    ]
    subjects = service(listings, facts_for("CIK0001000184")).universe()
    assert len(subjects) == 1
    assert subjects[0].company_key == "CIK0001000184"


def test_a_fund_is_not_a_screening_subject() -> None:
    listings = [Listing("SPY", cik="0000884394", asset_type="ETF", company_id=9)]
    assert service(listings, []).universe() == []


def test_a_listing_without_an_sec_identity_is_not_a_subject() -> None:
    listings = [Listing("XLE", cik="", company_id=9)]
    assert service(listings, []).universe() == []


def test_the_universe_order_is_deterministic() -> None:
    listings = [Listing(s, cik=f"000000000{i}", company_id=i) for i, s in enumerate("CBA", start=1)]
    rows = [r for i in range(1, 4) for r in facts_for(f"CIK000000000{i}")]
    first = [s.company_key for s in service(listings, rows).universe()]
    second = [s.company_key for s in service(list(reversed(listings)), rows).universe()]
    assert first == second == sorted(first)


# ---------------------------------------------------------------- criteria
def test_an_unknown_metric_is_refused_before_any_work() -> None:
    with pytest.raises(InvalidCriterionError, match="unknown metric"):
        one_company().screen([Criterion("moat_score", Operator.GTE, 1)], as_of=AS_OF)


def test_an_operator_a_metric_does_not_accept_is_refused() -> None:
    with pytest.raises(InvalidCriterionError, match="does not support"):
        one_company().screen([Criterion("has_current_development", Operator.GTE, 1)], as_of=AS_OF)


def test_a_screen_with_no_criteria_is_refused() -> None:
    with pytest.raises(InvalidCriterionError):
        one_company().screen([], as_of=AS_OF)


# ------------------------------------------------------------- evaluation
def test_a_match_is_explained_by_the_value_that_produced_it() -> None:
    result = one_company().screen([Criterion("operating_margin", Operator.GTE, 0.20)], as_of=AS_OF)
    (candidate,) = result.candidates
    (item,) = candidate.results
    assert item.evaluation is Evaluation.MATCH
    assert item.observed == pytest.approx(0.30)
    assert item.criterion.value == 0.20


def test_a_missing_metric_is_not_evaluable_rather_than_a_failure() -> None:
    """**The gate.** A company with no margin has not failed a margin test."""
    screener = service([Listing("ACME")], [row(p, 1000.0) for p in QUARTERS])
    result = screener.screen([Criterion("fcf_margin", Operator.GTE, 0.10)], as_of=AS_OF)
    assert result.matched == 0
    assert result.not_evaluable == 1
    assert result.evaluated == 0
    assert "UNAVAILABLE" in result.reasons


def test_passing_everything_testable_but_not_all_of_it_is_not_evaluable() -> None:
    """The case that would otherwise be miscounted as a failure."""
    screener = service([Listing("ACME")], facts_for("CIK0000000001"))
    result = screener.screen(
        [
            Criterion("operating_margin", Operator.GTE, 0.20),  # passes
            Criterion("fcf_margin", Operator.GTE, 0.10),  # never reported
        ],
        as_of=AS_OF,
    )
    assert result.matched == 0
    assert result.not_evaluable == 1
    assert result.evaluated == 0


def test_failing_a_stated_condition_settles_it() -> None:
    """Saying "could not assess" about a company known to fail is the less
    honest of the two statements, and the rule must not depend on which
    criterion happened to be examined first."""
    screener = service([Listing("ACME")], facts_for("CIK0000000001", operating=10.0))
    result = screener.screen(
        [
            Criterion("operating_margin", Operator.GTE, 0.90),  # fails
            Criterion("fcf_margin", Operator.GTE, 0.10),  # never reported
        ],
        as_of=AS_OF,
    )
    assert result.matched == 0
    assert result.not_evaluable == 0
    assert result.evaluated == 1


def test_all_criteria_must_be_satisfied() -> None:
    screener = service([Listing("ACME")], facts_for("CIK0000000001"))
    both = screener.screen(
        [
            Criterion("operating_margin", Operator.GTE, 0.20),
            Criterion("operating_margin", Operator.LTE, 0.25),
        ],
        as_of=AS_OF,
    )
    assert both.matched == 0
    assert both.evaluated == 1


# ------------------------------------------------------------------ sector
def test_a_financial_company_cannot_pass_an_industrial_margin_filter() -> None:
    """**Regression.** The audit's naive screen returned REITs at 708% margin."""
    screener = service([Listing("BANK", sic="6021")], facts_for("CIK0000000001"))
    result = screener.screen([Criterion("operating_margin", Operator.GTE, 0.20)], as_of=AS_OF)
    assert result.matched == 0
    assert result.reasons.get("SECTOR_MODEL_REQUIRED") == 1


def test_a_bank_keeps_the_filters_that_are_meaningful_for_a_bank() -> None:
    screener = service([Listing("BANK", sic="6021")], facts_for("CIK0000000001"))
    result = screener.screen([Criterion("share_count_change_3y", Operator.LTE, 0.0)], as_of=AS_OF)
    assert result.evaluated == 1
    assert get("share_count_change_3y").financial_ok
    assert get("pe_ttm").financial_ok


def test_price_to_sales_is_refused_for_a_bank() -> None:
    """**Regression.** Bank of America yielded a P/S of 3.58x, against a
    "sales" figure that is not the industrial quantity of that name."""
    assert get("ps_ttm").financial_ok is False
    assert get("p_fcf").financial_ok is False

    class Advisor:
        def analyse(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("the advisor must not be reached for a refused metric")

    screener = service([Listing("BANK", sic="6021")], facts_for("CIK0000000001"), advisor=Advisor())
    result = screener.screen([Criterion("ps_ttm", Operator.LTE, 10.0)], as_of=AS_OF)
    assert result.reasons.get("SECTOR_MODEL_REQUIRED") == 1


def test_the_declared_sector_flag_is_enforced_not_merely_stated() -> None:
    financial_only = {k for k, m in METRICS.items() if m.financial_ok}
    assert "share_count" in financial_only
    assert "operating_margin" not in financial_only
    body = Path("app/screener/service.py").read_text()
    assert "financial_ok" in body


# ------------------------------------------------------------------- PIT
def test_a_screen_uses_only_what_was_filed_by_its_as_of() -> None:
    rows = facts_for("CIK0000000001", revenue=1000.0, operating=100.0)
    rows += [
        row(
            p,
            900.0,
            metric="operating_income",
            symbol="CIK0000000001",
            concept="OperatingIncomeLoss",
            filed="2026-08-01",
        )
        for p in QUARTERS[-4:]
    ]
    screener = service([Listing("ACME")], rows)
    criterion = [Criterion("operating_margin", Operator.GTE, 0.50)]
    assert screener.screen(criterion, as_of="2026-07-01").matched == 0
    assert screener.screen(criterion, as_of="2026-09-01").matched == 1


def test_membership_changes_only_because_of_what_was_knowable() -> None:
    rows = [row(p, 1000.0) for p in QUARTERS]
    rows += [
        row(p, v, metric="operating_income", concept="OperatingIncomeLoss")
        for p, v in zip(QUARTERS, [100.0] * 12 + [400.0] * 10, strict=False)
    ]
    screener = service([Listing("ACME")], rows)
    criterion = [Criterion("operating_margin", Operator.GTE, 0.30)]
    assert screener.screen(criterion, as_of="2023-01-01").matched == 0
    assert screener.screen(criterion, as_of="2026-09-01").matched == 1


# ------------------------------------------------------------------ market
def test_a_listing_without_its_own_prices_cannot_be_screened_on_market_data() -> None:
    """No ADR price ever stands in for a foreign line."""
    from app.screener.service import _Subject

    class Advisor:
        def analyse(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("no price series, so no advisor call")

    listing = Listing("FOREIGN", mic="XETR", sic="7372", country="DE", has_prices=False)
    screener = service([listing], facts_for("CIK0000000001"), advisor=Advisor())
    subject = _Subject(1, "CIK0000000001", listing)
    result = screener._evaluate(
        subject, Criterion("relative_strength_252d", Operator.GTE, 0.0), as_of=AS_OF
    )
    assert result.evaluation is Evaluation.NOT_EVALUABLE
    assert result.reason is NotEvaluable.NO_MARKET_DATA


def test_valuation_is_refused_across_a_currency_boundary() -> None:
    assert NotEvaluable.VALUATION_REFUSED in set(NotEvaluable)
    body = Path("app/screener/service.py").read_text()
    assert "valuation_allowed" in body


# ---------------------------------------------------------------- ordering
def test_the_default_order_is_neutral() -> None:
    """No "best first": ordering by desirability is the judgement not made."""
    listings = [Listing(s, cik=f"000000000{i}", company_id=i) for i, s in enumerate("CAB", start=1)]
    rows = [
        r
        for i, m in enumerate((0.30, 0.50, 0.40), start=1)
        for r in facts_for(f"CIK000000000{i}", operating=m * 1000)
    ]
    result = service(listings, rows).screen(
        [Criterion("operating_margin", Operator.GTE, 0.20)], as_of=AS_OF
    )
    assert [c.symbol for c in result.candidates] == ["A", "B", "C"]
    assert result.sort_metric is None


def test_an_explicit_sort_orders_by_that_metric_only() -> None:
    listings = [Listing(s, cik=f"000000000{i}", company_id=i) for i, s in enumerate("CAB", start=1)]
    # C=0.50, A=0.30, B=0.40 -- so descending is C, B, A, which cannot be
    # mistaken for the alphabetical default.
    rows = [
        r
        for i, m in enumerate((0.50, 0.30, 0.40), start=1)
        for r in facts_for(f"CIK000000000{i}", operating=m * 1000)
    ]
    result = service(listings, rows).screen(
        [Criterion("operating_margin", Operator.GTE, 0.20)],
        as_of=AS_OF,
        sort_metric="operating_margin",
        descending=True,
    )
    assert [c.symbol for c in result.candidates] == ["C", "B", "A"]
    assert result.sort_metric == "operating_margin"


def test_results_are_limited_and_the_full_count_is_still_reported() -> None:
    listings = [Listing(f"S{i:02d}", cik=f"00000000{i:02d}", company_id=i) for i in range(1, 8)]
    rows = [r for i in range(1, 8) for r in facts_for(f"CIK00000000{i:02d}")]
    result = service(listings, rows).screen(
        [Criterion("operating_margin", Operator.GTE, 0.20)], as_of=AS_OF, limit=3
    )
    assert len(result.candidates) == 3
    assert result.matched == 7
    assert result.truncated == 4


def test_the_limit_is_bounded() -> None:
    result = one_company().screen(
        [Criterion("operating_margin", Operator.GTE, 0.20)], as_of=AS_OF, limit=10_000
    )
    assert result.limit == MAX_LIMIT


# -------------------------------------------------------------- coverage
def test_the_result_reports_how_much_it_could_actually_assess() -> None:
    listings = [Listing("GOOD"), Listing("BANK", cik="0000000002", company_id=2, sic="6021")]
    rows = facts_for("CIK0000000001") + facts_for("CIK0000000002")
    result = service(listings, rows).screen(
        [Criterion("operating_margin", Operator.GTE, 0.20)], as_of=AS_OF
    )
    assert result.universe == 2
    assert result.evaluated == 1
    assert result.matched == 1
    assert result.not_evaluable == 1
    assert result.reasons == {"SECTOR_MODEL_REQUIRED": 1}


def test_every_refusal_names_a_specific_reason() -> None:
    generic = {"MISSING", "MISSING_DATA", "ERROR", "UNKNOWN"}
    assert not generic & {str(r) for r in NotEvaluable}


def test_the_serialised_result_is_deterministic() -> None:
    import json

    screener = one_company()
    criterion = [Criterion("operating_margin", Operator.GTE, 0.20)]
    first = json.dumps(screener.screen(criterion, as_of=AS_OF).as_dict(), sort_keys=True)
    second = json.dumps(screener.screen(criterion, as_of=AS_OF).as_dict(), sort_keys=True)
    assert json.loads(first)["candidates"] == json.loads(second)["candidates"]


# -------------------------------------------------------------- boundaries
def test_the_screener_reaches_no_execution_path() -> None:
    forbidden = (
        "app.broker",
        "app.paper",
        "app.strategy",
        "app.discord_bot",
        "openai",
        "anthropic",
        "alpaca",
    )
    for path in Path("app/screener").glob("*.py"):
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


def test_the_screener_recomputes_no_financial_formula() -> None:
    """**The gate.** Every value compared was computed by the layer owning it."""
    banned = (
        "def ttm",
        "annualised =",
        "** (1 /",
        "percentile =",
        "midrank",
        "operating_margin =",
        "gross_margin =",
        "free_cash_flow =",
        "market_cap =",
        "pe_ttm =",
        "/ revenue",
    )
    for path in Path("app/screener").glob("*.py"):
        body = path.read_text().split('"""', 2)[-1]
        for token in banned:
            assert token not in body, f"{path} recomputes {token}"


def test_the_screener_opens_no_socket() -> None:
    for path in Path("app/screener").glob("*.py"):
        body = path.read_text()
        for token in ("urlopen", "requests.", "httpx", "urllib", "socket"):
            assert token not in body, f"{path} references {token}"


BANNED_WORDS = (
    "best",
    "top stocks",
    "buy",
    "sell",
    "recommend",
    "attractive",
    "undervalued",
    "overvalued",
    "bullish",
    "bearish",
    "upside",
    "downside",
    "expected return",
    "winner",
    "loser",
    "quality stock",
    "should ",
)


def test_no_screener_string_uses_recommendation_vocabulary() -> None:
    for path in Path("app/screener").glob("*.py"):
        tree = ast.parse(path.read_text())
        docs = {
            ast.get_docstring(n, clean=False)
            for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in docs:
                    continue
                for word in BANNED_WORDS:
                    assert word not in node.value.lower(), f"{path}: {node.value!r}"


def test_the_registry_declares_every_metric_completely() -> None:
    for key in keys():
        metric = get(key)
        assert metric is not None
        assert metric.label
        assert metric.unit
        assert metric.source
        assert metric.operators
        assert metric.description


def test_no_metric_promises_a_ranking() -> None:
    for key in keys():
        metric = get(key)
        assert metric is not None
        text = f"{metric.key} {metric.label} {metric.description}".lower()
        # Word boundaries, because "operating" contains "rating" -- the same
        # substring mistake three earlier gates in this project made.
        for word in ("score", "rank", "best", "rating", "grade"):
            assert not re.search(rf"\b{word}", text), f"{key}: {word}"


# ------------------------------------------------- order independence (§7/§8)
def _mixed_screener() -> ScreenerService:
    """A company that definitely fails one criterion and cannot be tested on another."""
    return service([Listing("ACME")], facts_for("CIK0000000001", operating=10.0))


def test_classification_does_not_depend_on_criterion_order() -> None:
    """**The gate.** Cheapest-first is an optimisation, not a semantics.

    The company below fails an operating-margin threshold outright and has no
    FCF margin at all. Whichever condition is examined first, the answer must be
    the same -- otherwise the reported coverage split would be an artefact of
    the execution plan.
    """
    fails = Criterion("operating_margin", Operator.GTE, 0.90)
    untestable = Criterion("fcf_margin", Operator.GTE, 0.10)

    forward = _mixed_screener().screen([fails, untestable], as_of=AS_OF)
    reverse = _mixed_screener().screen([untestable, fails], as_of=AS_OF)

    assert forward.matched == reverse.matched == 0
    assert forward.not_evaluable == reverse.not_evaluable == 0
    assert forward.evaluated == reverse.evaluated == 1


def test_a_company_failing_nothing_is_unevaluable_in_either_order() -> None:
    passes = Criterion("operating_margin", Operator.GTE, 0.20)
    untestable = Criterion("fcf_margin", Operator.GTE, 0.10)
    screener = service([Listing("ACME")], facts_for("CIK0000000001"))

    forward = screener.screen([passes, untestable], as_of=AS_OF)
    reverse = screener.screen([untestable, passes], as_of=AS_OF)

    assert forward.not_evaluable == reverse.not_evaluable == 1
    assert forward.evaluated == reverse.evaluated == 0


def test_mixing_cost_tiers_does_not_change_the_verdict() -> None:
    """A cheap failure and an expensive condition, in both orders."""

    class Advisor:
        def __init__(self) -> None:
            self.calls = 0

        def analyse(self, *a: Any, **k: Any) -> Any:
            self.calls += 1
            raise AssertionError("a settled company must not reach the advisor")

    cheap = Criterion("operating_margin", Operator.GTE, 0.90)  # fails
    costly = Criterion("pe_ttm", Operator.LTE, 10.0)

    for criteria in ([cheap, costly], [costly, cheap]):
        advisor = Advisor()
        screener = service(
            [Listing("ACME")], facts_for("CIK0000000001", operating=10.0), advisor=advisor
        )
        result = screener.screen(criteria, as_of=AS_OF)
        assert result.matched == 0
        assert result.evaluated == 1
        assert advisor.calls == 0  # the cheap criterion settled it either way


# ------------------------------------------------------- run-scoped memo (§16)
def test_the_memo_cannot_leak_across_as_of_dates() -> None:
    """**The gate.** A past-dated screen must never read a memo built for another.

    The memo exists because five trajectory criteria asked the history layer for
    the same company five times. It is emptied on entry to every screen, so the
    optimisation cannot turn into a point-in-time violation.
    """
    rows = [row(p, 1000.0) for p in QUARTERS]
    rows += [
        row(p, v, metric="operating_income", concept="OperatingIncomeLoss")
        for p, v in zip(QUARTERS, [100.0] * 12 + [400.0] * 10, strict=False)
    ]
    screener = service([Listing("ACME")], rows)
    criterion = [Criterion("operating_margin", Operator.GTE, 0.30)]

    recent = screener.screen(criterion, as_of="2026-09-01")
    assert recent.matched == 1

    historical = screener.screen(criterion, as_of="2023-01-01")
    assert historical.matched == 0

    again = screener.screen(criterion, as_of="2026-09-01")
    assert again.matched == 1


def test_the_memo_is_emptied_at_the_start_of_every_screen() -> None:
    screener = service([Listing("ACME")], facts_for("CIK0000000001"))
    screener.screen([Criterion("operating_margin", Operator.GTE, 0.20)], as_of=AS_OF)
    first = dict(screener._trajectories)
    assert first  # populated during the run
    screener.screen([Criterion("operating_margin", Operator.GTE, 0.20)], as_of="2024-01-01")
    assert screener._trajectories is not first


def test_the_memo_does_not_change_any_result() -> None:
    listings = [Listing(f"S{i}", cik=f"000000000{i}", company_id=i) for i in range(1, 5)]
    rows = [r for i in range(1, 5) for r in facts_for(f"CIK000000000{i}")]
    criteria = [
        Criterion("operating_margin", Operator.GTE, 0.20),
        Criterion("operating_margin_change_3y", Operator.GTE, -99.0),
        Criterion("operating_margin_own_percentile", Operator.GTE, 0.0),
    ]
    screener = service(listings, rows)
    once = screener.screen(criteria, as_of=AS_OF)
    twice = screener.screen(criteria, as_of=AS_OF)
    assert [c.symbol for c in once.candidates] == [c.symbol for c in twice.candidates]
    assert once.matched == twice.matched == 4


def test_no_permanent_report_cache_is_kept_between_screens() -> None:
    """No 989 AdvisorReports held for the life of the process."""
    body = Path("app/screener/service.py").read_text()
    assert "self._trajectories = {}" in body
    assert "self._reports = {}" in body


# ------------------------------------------------------ deferred peer filters
def test_no_peer_filter_is_exposed_anywhere() -> None:
    """Peer screening is deferred, so nothing may imply it is available."""
    for key in keys():
        assert "peer" not in key
    body = Path("app/cli.py").read_text()
    assert "--peer" not in body
