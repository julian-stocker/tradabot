"""The Advisor must stay read-only and must never claim predictive power.

These are structural guards, not behavioural ones: they hold even if someone
later rewrites the internals, which is the point.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.advisor import AdvisorService, FactStore, InvestmentAssessment, PriceSeries
from app.advisor.facts import ShareFamily
from app.advisor.schemas import Confidence, weakest

ADVISOR = Path("app/advisor")
FORBIDDEN_IMPORTS = ("app.broker", "app.paper.execution", "alpaca.trading")
FORBIDDEN_TOKENS = (
    "submit_order",
    "submit_entry",
    "submit_exit",
    "MarketOrderRequest",
    "TradingClient",
    "cancel_order",
)


def _sources() -> list[tuple[Path, str]]:
    return [(p, p.read_text()) for p in ADVISOR.glob("*.py")]


class TestCannotTrade:
    def test_no_module_imports_a_broker(self) -> None:
        """**The gate.** Analysis code must not be able to reach order submission."""
        for path, source in _sources():
            tree = ast.parse(source)
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert not any(name.startswith(f) for f in FORBIDDEN_IMPORTS), (
                        f"{path} imports {name}"
                    )

    def test_no_order_submission_symbols_appear(self) -> None:
        for path, source in _sources():
            body = source.split('"""', 2)[-1]
            for token in FORBIDDEN_TOKENS:
                assert token not in body, f"{path} references {token}"


class TestMakesNoPrediction:
    def test_investment_assessment_is_empty_by_construction(self) -> None:
        assessment = InvestmentAssessment()
        assert assessment.available is False
        assert assessment.reason == "NO_VALIDATED_PREDICTIVE_EVIDENCE"

    def test_no_prediction_vocabulary_in_outputs(self) -> None:
        """No expected return, probability or buy/sell language may be emitted.

        Matched on word boundaries: ``BUYBACK_REDUCING_SHARE_COUNT`` is a factual
        description of share count, not a recommendation, and must not trip this.
        """
        banned = ("expected_return", "probability_up", "price_target", "BUY", "SELL")
        for path, source in _sources():
            body = source.split('"""', 2)[-1]
            for token in banned:
                assert not re.search(rf"\b{token}\b", body), f"{path} emits {token}"


class TestConfidenceIsConservative:
    def test_confidence_is_the_minimum_not_the_mean(self) -> None:
        """One missing critical input must cap the section, not be averaged away."""
        assert weakest(Confidence.HIGH, Confidence.INSUFFICIENT) is Confidence.INSUFFICIENT
        assert weakest(Confidence.HIGH, Confidence.MEDIUM) is Confidence.MEDIUM
        assert weakest() is Confidence.INSUFFICIENT


class TestPointInTime:
    def test_a_later_filing_cannot_change_an_earlier_answer(self) -> None:
        """**The gate.** The whole historical analysis rests on this property."""
        rows = [
            {
                "symbol": "TEST", "metric": "revenue", "concept": "Revenues",
                "unit": "USD", "value": 100.0, "form": "10-K", "filed": "2024-02-01",
                "accession": "orig", "period_start": "2023-01-01",
                "period_end": "2023-12-31",
            },
            {
                "symbol": "TEST", "metric": "revenue", "concept": "Revenues",
                "unit": "USD", "value": 90.0, "form": "10-K/A", "filed": "2025-02-01",
                "accession": "restated", "period_start": "2023-01-01",
                "period_end": "2023-12-31",
            },
        ]
        store = FactStore(rows)
        before = store.ttm("TEST", "revenue", "2024-06-30")
        after = store.ttm("TEST", "revenue", "2025-06-30")
        assert before.value == pytest.approx(100.0)
        assert after.value == pytest.approx(90.0)

    def test_nothing_is_known_before_it_was_filed(self) -> None:
        store = FactStore(
            [
                {
                    "symbol": "TEST", "metric": "revenue", "concept": "Revenues",
                    "unit": "USD", "value": 100.0, "form": "10-K", "filed": "2024-02-01",
                    "accession": "a", "period_start": "2023-01-01",
                    "period_end": "2023-12-31",
                }
            ]
        )
        assert store.ttm("TEST", "revenue", "2024-01-31").value is None


class TestYearToDateHandling:
    def test_quarterly_cash_flow_is_de_cumulated_not_summed(self) -> None:
        """A 10-Q reports cash flow year-to-date; summing it would double count."""
        rows = [
            {
                "symbol": "T", "metric": "operating_cash_flow", "concept": "OCF",
                "unit": "USD", "value": v, "form": "10-Q", "filed": f,
                "accession": f"a{i}", "period_start": "2024-01-01", "period_end": e,
            }
            for i, (v, f, e) in enumerate(
                [
                    (100.0, "2024-04-30", "2024-03-31"),
                    (250.0, "2024-07-31", "2024-06-30"),
                    (420.0, "2024-10-31", "2024-09-30"),
                    (600.0, "2025-02-28", "2024-12-31"),
                ]
            )
        ]
        store = FactStore(rows)
        quarters, _prov = store.quarterlies("T", "operating_cash_flow", "2025-06-30")
        assert quarters["2024-03-31"]["value"] == pytest.approx(100.0)
        assert quarters["2024-06-30"]["value"] == pytest.approx(150.0)
        assert quarters["2024-09-30"]["value"] == pytest.approx(170.0)
        assert quarters["2024-12-31"]["value"] == pytest.approx(180.0)
        assert store.ttm("T", "operating_cash_flow", "2025-06-30").value == pytest.approx(
            600.0
        )


class TestServiceShape:
    def test_a_report_carries_a_disclaimer_and_no_recommendation(self) -> None:
        service = AdvisorService(FactStore([]), {"X": PriceSeries({"2024-01-02": 10.0})})
        report = service.analyse("X", as_of="2024-06-30")
        assert report.investment_assessment.available is False
        assert "not investment advice" in report.disclaimer
        assert report.confidence["overall"] is Confidence.INSUFFICIENT


class TestConceptSelection:
    """A company that changes XBRL taxonomy must not be read through its old one.

    Phase 12.26 shipped a rule that picked the historically most frequent
    concept. Apple has filed 210 facts under ``SalesRevenueNet`` and 117 under
    ``RevenueFromContractWithCustomerExcludingAssessedTax``, so frequency chose
    the abandoned concept and understated revenue by 39%.
    """

    @staticmethod
    def _rows() -> list[dict[str, object]]:
        old = [
            {
                "symbol": "T", "metric": "revenue", "concept": "SalesRevenueNet",
                "unit": "USD", "value": 10.0, "form": "10-Q", "filed": f"201{i}-04-30",
                "accession": f"old{i}", "period_start": f"201{i}-01-01",
                "period_end": f"201{i}-03-31",
            }
            for i in range(5)
        ]
        new = [
            {
                "symbol": "T", "metric": "revenue",
                "concept": "RevenueFromContractWithCustomerExcludingAssessedTax",
                "unit": "USD", "value": 100.0, "form": "10-Q",
                "filed": f"2024-{m:02d}-28", "accession": f"new{m}",
                "period_start": s, "period_end": e,
            }
            for m, s, e in (
                (4, "2024-01-01", "2024-03-31"),
                (7, "2024-04-01", "2024-06-30"),
                (10, "2024-07-01", "2024-09-30"),
                (12, "2024-10-01", "2024-12-31"),
            )
        ]
        return old + new

    def test_the_recent_concept_wins_over_the_more_frequent_one(self) -> None:
        """**The gate.** Five old facts must not outvote the current taxonomy."""
        store = FactStore(self._rows())
        result = store.ttm("T", "revenue", "2025-06-30")
        assert result.value == pytest.approx(400.0)
        concepts = {p.concept for p in result.provenance}
        assert concepts == {"RevenueFromContractWithCustomerExcludingAssessedTax"}
        assert "SalesRevenueNet" not in concepts

    def test_selection_is_point_in_time(self) -> None:
        """A 2019 query must see the taxonomy in use in 2019, not the later one."""
        store = FactStore(self._rows())
        quarters, _prov = store.quarterlies("T", "revenue", "2019-12-31")
        assert quarters
        assert all(
            str(row["concept"]) == "SalesRevenueNet" for row in quarters.values()
        )


class TestShareFactSemantics:
    """Share counts that look alike measure different things.

    Salesforce filed cover-page counts of 937M (2025-11-28) and 819M
    (2026-05-21). Comparing them produced a phantom -12.59% "annual buyback"
    from observations six months apart. Dilution must therefore draw on one
    semantic family, at comparable fiscal period ends.
    """

    @staticmethod
    def _rows() -> list[dict[str, object]]:
        period_end = [
            ("2024-01-31", 971_000_000.0, "2024-03-06"),
            ("2025-01-31", 962_000_000.0, "2025-03-05"),
            ("2026-01-31", 929_000_000.0, "2026-03-02"),
        ]
        cover = [
            ("2025-11-28", 937_000_000.0, "2025-12-04"),
            ("2026-05-21", 819_000_000.0, "2026-05-28"),
        ]
        rows: list[dict[str, object]] = []
        for end, value, filed in period_end:
            rows.append(
                {
                    "symbol": "T", "metric": "shares_outstanding",
                    "concept": "CommonStockSharesOutstanding", "unit": "shares",
                    "value": value, "form": "10-K", "filed": filed,
                    "accession": f"pe{end}", "period_start": None, "period_end": end,
                }
            )
        for end, value, filed in cover:
            rows.append(
                {
                    "symbol": "T", "metric": "shares_outstanding",
                    "concept": "EntityCommonStockSharesOutstanding", "unit": "shares",
                    "value": value, "form": "10-Q", "filed": filed,
                    "accession": f"cp{end}", "period_start": None, "period_end": end,
                }
            )
        return rows

    def test_families_are_separated(self) -> None:
        store = FactStore(self._rows())
        period_end = store.share_series("T", "2026-08-13", ShareFamily.PERIOD_END)
        cover = store.share_series("T", "2026-08-13", ShareFamily.COVER_PAGE)
        assert [v for _e, v, _p in period_end] == [971e6, 962e6, 929e6]
        assert [v for _e, v, _p in cover] == [937e6, 819e6]

    def test_the_cover_page_pair_can_never_drive_dilution(self) -> None:
        """**The gate.** 819M must not appear in the dilution source at all."""
        store = FactStore(self._rows())
        values = [v for _e, v, _p in store.share_series("T", "2026-08-13")]
        assert 819_000_000.0 not in values
        assert 937_000_000.0 not in values

    def test_dilution_uses_comparable_fiscal_year_ends(self) -> None:
        service = AdvisorService(
            FactStore(self._rows()), {"T": PriceSeries({"2026-08-13": 100.0})}
        )
        report = service.analyse("T", as_of="2026-08-13")
        capital = next(
            s for s in report.company_quality if s.name == "CAPITAL STRUCTURE"
        )
        yoy = capital.metrics["share_count_yoy"].value
        assert yoy is not None
        assert yoy == pytest.approx(929 / 962 - 1, abs=1e-9)
        # the defect value must be nowhere near the answer
        assert yoy != pytest.approx(-0.12593, abs=1e-3)
        assert capital.labels["share_family"] == str(ShareFamily.PERIOD_END)

    def test_weighted_average_shares_are_not_a_substitute(self) -> None:
        """Weighted-average shares serve EPS, not period-end ownership."""
        rows = [
            {
                "symbol": "W", "metric": "shares_diluted",
                "concept": "WeightedAverageNumberOfDilutedSharesOutstanding",
                "unit": "shares", "value": 100.0, "form": "10-K", "filed": f"202{i}-03-01",
                "accession": f"w{i}", "period_start": f"202{i}-01-01",
                "period_end": f"202{i}-12-31",
            }
            for i in range(3, 7)
        ]
        store = FactStore(rows)
        assert store.share_series("W", "2026-08-13", ShareFamily.PERIOD_END) == []
        service = AdvisorService(store, {"W": PriceSeries({"2026-08-13": 10.0})})
        capital = next(
            s
            for s in service.analyse("W", as_of="2026-08-13").company_quality
            if s.name == "CAPITAL STRUCTURE"
        )
        assert capital.labels["dilution"] == "INSUFFICIENT_DATA"
        assert capital.confidence is Confidence.INSUFFICIENT

    def test_non_annual_gaps_are_skipped_not_compared(self) -> None:
        """Two period-end facts six months apart are not a year-over-year pair."""
        rows = [
            {
                "symbol": "G", "metric": "shares_outstanding",
                "concept": "CommonStockSharesOutstanding", "unit": "shares",
                "value": v, "form": "10-K", "filed": "2026-07-01",
                "accession": f"g{e}", "period_start": None, "period_end": e,
            }
            for e, v in (
                ("2024-01-31", 1000.0),
                ("2025-01-31", 990.0),
                ("2025-07-31", 500.0),  # mid-year, must not anchor a YoY step
                ("2026-01-31", 980.0),
            )
        ]
        service = AdvisorService(
            FactStore(rows), {"G": PriceSeries({"2026-08-13": 10.0})}
        )
        capital = next(
            s
            for s in service.analyse("G", as_of="2026-08-13").company_quality
            if s.name == "CAPITAL STRUCTURE"
        )
        yoy = capital.metrics["share_count_yoy"].value
        assert yoy == pytest.approx(980 / 990 - 1, abs=1e-9)


class TestDegenerateShareSeries:
    def test_a_constant_share_series_is_refused(self) -> None:
        """**The gate.** Boeing filed one identical value for six years.

        Reporting that as "STABLE +0.00%" at HIGH confidence is a confidently
        wrong answer about a company that issued heavily.
        """
        rows = [
            {
                "symbol": "D", "metric": "shares_outstanding",
                "concept": "CommonStockSharesOutstanding", "unit": "shares",
                "value": 1_012_261_159.0, "form": "10-K", "filed": "2026-01-30",
                "accession": "d1", "period_start": None, "period_end": e,
            }
            for e in ("2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31")
        ]
        service = AdvisorService(
            FactStore(rows), {"D": PriceSeries({"2026-08-13": 10.0})}
        )
        capital = next(
            s
            for s in service.analyse("D", as_of="2026-08-13").company_quality
            if s.name == "CAPITAL STRUCTURE"
        )
        assert capital.labels["dilution"] == "INSUFFICIENT_DATA"
        assert capital.confidence is Confidence.INSUFFICIENT
        assert "identical" in " ".join(capital.confidence_reasons)
