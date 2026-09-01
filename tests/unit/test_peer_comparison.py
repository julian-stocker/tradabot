"""A percentile is a claim about comparability, and these tests police it.

Cross-sectional statistics fail quietly. A peer group that silently widened, a
metric that borrowed a price from another listing, a percentile computed against
five companies and rendered to one decimal place -- none of those raise, and all
of them produce a number that looks exactly like a good one.

So the assertions here are mostly about what does *not* appear: no comparison
below the declared minimum, no valuation for a listing the Advisor refused to
price, no financial-sector company graded on an operating margin, and no word
anywhere that could be read as advice.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.advisor import AdvisorService, FactStore, MarketIdentity, PriceSeries
from app.advisor.facts import company_key
from app.instruments.registry import Candidate, InstrumentRegistry, market_inputs
from app.peers import (
    MIN_PEERS,
    PeerComparisonService,
    PeerOutcome,
    PeerUniverse,
    describe,
    percentile_rank,
    quantile,
    usable,
)

AS_OF = "2026-08-14"
PRIOR = "2025-08-14"


def _series(start: float) -> PriceSeries:
    days = [f"2024-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 29)]
    days += [f"2025-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 29)]
    days += [f"2026-{m:02d}-{d:02d}" for m in range(1, 9) for d in range(1, 15)]
    return PriceSeries({d: start + i * 0.01 for i, d in enumerate(sorted(days))})


def _facts(key: str, revenue: float, income: float, gross: float) -> list[dict[str, Any]]:
    """Two years of quarterly filings, so a year-on-year growth read exists."""
    rows: list[dict[str, Any]] = []
    ends = (
        "2024-09-30",
        "2024-12-31",
        "2025-03-31",
        "2025-06-30",
        "2025-09-30",
        "2025-12-31",
        "2026-03-31",
        "2026-06-30",
    )
    for i, end in enumerate(ends):
        filed = f"{end[:4]}-{int(end[5:7]) + 1:02d}-15"
        scale = 1.0 if i >= 4 else 0.8  # the older year is smaller -> growth
        for metric, value, unit, concept in (
            ("revenue", revenue * scale, "USD", "Revenues"),
            ("operating_income", income * scale, "USD", "OperatingIncomeLoss"),
            ("gross_profit", gross * scale, "USD", "GrossProfit"),
            ("operating_cash_flow", income * scale, "USD", "NetCashProvidedBy"),
            ("capex", income * scale * 0.1, "USD", "PaymentsToAcquire"),
            ("eps_diluted", 2.0, "USD/shares", "EarningsPerShareDiluted"),
        ):
            rows.append(
                {
                    "symbol": key,
                    "metric": metric,
                    "value": value,
                    "period_start": f"{end[:4]}-{int(end[5:7]) - 2:02d}-01",
                    "period_end": end,
                    "filed": filed,
                    "form": "10-Q",
                    "unit": unit,
                    "concept": concept,
                    "accession": f"{key}-{i}",
                    "taxonomy": "us-gaap",
                }
            )
        rows.append(
            {
                "symbol": key,
                "metric": "shares_outstanding",
                "value": 1_000.0,
                "period_start": None,
                "period_end": end,
                "filed": filed,
                "form": "10-Q",
                "unit": "shares",
                "concept": "CommonStockSharesOutstanding",
                "accession": f"{key}-{i}",
                "taxonomy": "us-gaap",
            }
        )
    return rows


def _listing(symbol: str, cid: int, sic: str = "3674", **kw: Any) -> Candidate:
    base: dict[str, Any] = {
        "symbol": symbol,
        "mic": "XNAS",
        "country": "US",
        "quote_currency": "USD",
        "company_name": f"{symbol} Inc.",
        "company_id": cid,
        "cik": f"{cid:010d}",
        "reporting_currency": "USD",
        "taxonomy": "us-gaap",
        "has_prices": True,
        "has_fundamentals": True,
        "sec_identity": True,
        "sic": sic,
        "sic_description": "Semiconductors & Related Devices",
    }
    base.update(kw)
    return Candidate(**base)


def _fixture(
    count: int = MIN_PEERS + 2, *, sic: str = "3674", extra: list[Candidate] | None = None
) -> tuple[PeerComparisonService, list[Candidate], AdvisorService]:
    """A synthetic industry with `count` companies of increasing profitability."""
    listings = [_listing(f"P{i:02d}", 100 + i, sic) for i in range(count)]
    listings += extra or []
    rows: list[dict[str, Any]] = []
    prices: dict[str, PriceSeries] = {"SPY": _series(50.0)}
    for i, listing in enumerate(listings):
        key = company_key(int(listing.cik)) if listing.cik else listing.symbol
        rows += _facts(key, 1_000.0, 100.0 + i * 10, 400.0 + i * 10)
        if listing.has_prices:
            prices[listing.symbol] = _series(10.0 + i)
    store = FactStore(rows)
    advisor = AdvisorService(store, prices)
    universe = PeerUniverse(listings)
    service = PeerComparisonService(universe=universe, advisor=advisor, facts=store)
    return service, listings, advisor


def _report(advisor: AdvisorService, listing: Candidate, as_of: str = AS_OF) -> Any:
    series, benchmark, mismatch = market_inputs(listing)
    return advisor.analyse(
        listing.symbol,
        as_of=as_of,
        company_key=company_key(int(listing.cik)) if listing.cik else None,
        market=MarketIdentity(series=series, benchmark=benchmark, unit_mismatch=mismatch),
    )


def _compare(service: Any, advisor: Any, listing: Candidate, as_of: str = AS_OF) -> Any:
    return service.compare(listing, _report(advisor, listing, as_of), as_of=as_of)


class TestStatistics:
    """The two conventions, spelled out in tests as well as in the docstring."""

    def test_quantile_interpolates_between_order_statistics(self) -> None:
        assert quantile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
        assert quantile([1.0, 2.0, 3.0, 4.0], 0.25) == 1.75
        assert quantile([1.0, 2.0, 3.0, 4.0], 0.75) == 3.25

    def test_a_single_value_is_its_own_quantile(self) -> None:
        assert quantile([7.0], 0.5) == 7.0

    def test_percentile_rank_counts_ties_as_half(self) -> None:
        """**The gate.** Counting ties as 'below' would rank a company at the
        median as the top of a group where every peer matched it."""
        assert percentile_rank(2.0, [1.0, 2.0, 3.0]) == pytest.approx(50.0)
        assert percentile_rank(2.0, [2.0, 2.0, 2.0, 2.0]) == pytest.approx(50.0)

    def test_percentile_rank_is_order_independent(self) -> None:
        peers = [5.0, 1.0, 3.0, 2.0, 4.0]
        assert percentile_rank(3.0, peers) == percentile_rank(3.0, sorted(peers))
        assert percentile_rank(3.0, peers) == percentile_rank(3.0, sorted(peers, reverse=True))

    def test_the_extremes_are_reachable(self) -> None:
        assert percentile_rank(9.0, [1.0, 2.0]) == pytest.approx(100.0)
        assert percentile_rank(0.0, [1.0, 2.0]) == pytest.approx(0.0)

    @pytest.mark.parametrize("bad", [None, float("nan"), float("inf")])
    def test_undefined_values_never_enter_a_distribution(self, bad: float | None) -> None:
        assert usable(bad, positive_only=False) is False

    def test_a_negative_multiple_is_undefined_not_cheap(self) -> None:
        """A price-to-earnings on negative earnings must not rank as the
        lowest multiple in the group."""
        assert usable(-4.0, positive_only=True) is False
        assert usable(-4.0, positive_only=False) is True


class TestMinimumSample:
    """Cases 3 and 9. The floor is pre-declared and never negotiated away."""

    def test_a_small_industry_refuses_rather_than_widening_forever(self) -> None:
        service, listings, advisor = _fixture(count=3, sic="9999")

        result = _compare(service, advisor, listings[0])

        assert result.outcome is PeerOutcome.INSUFFICIENT_SAMPLE
        assert result.comparisons == ()
        assert str(MIN_PEERS) in (result.detail or "")

    def test_a_group_exactly_at_the_floor_is_compared(self) -> None:
        # MIN_PEERS peers plus the subject itself.
        service, listings, advisor = _fixture(count=MIN_PEERS + 1)

        result = _compare(service, advisor, listings[0])

        assert result.outcome is PeerOutcome.AVAILABLE
        assert result.group is not None
        assert result.group.size == MIN_PEERS

    def test_a_group_one_short_of_the_floor_refuses(self) -> None:
        service, listings, advisor = _fixture(count=MIN_PEERS)

        result = _compare(service, advisor, listings[0])

        assert result.outcome is PeerOutcome.INSUFFICIENT_SAMPLE

    def test_the_floor_applies_per_metric_not_only_per_group(self) -> None:
        """A group of fourteen may hold only five companies with a usable
        free-cash-flow multiple, and that metric must refuse on its own."""
        service, listings, advisor = _fixture(count=MIN_PEERS + 2)
        result = _compare(service, advisor, listings[0])

        for comparison in result.comparisons:
            assert comparison.peer_count >= MIN_PEERS


class TestPeerGroupIsInspectable:
    """Case 2 and 12. A percentile whose universe is hidden cannot be argued with."""

    def test_the_group_names_its_basis_code_and_members(self) -> None:
        service, listings, advisor = _fixture()

        group = _compare(service, advisor, listings[0]).group

        assert group is not None
        assert group.code == "3674"
        assert group.as_of == AS_OF
        assert len(group.members) == MIN_PEERS + 2
        assert all(m.company_id for m in group.members)

    def test_the_subject_is_excluded_from_its_own_peer_set(self) -> None:
        service, listings, advisor = _fixture()

        group = _compare(service, advisor, listings[0]).group

        assert group is not None
        subject = next(m for m in group.members if m.company_id == listings[0].company_id)
        assert subject.included is False
        assert subject.reason == "the subject"
        assert listings[0].company_id not in {m.company_id for m in group.included}

    def test_membership_is_deterministic_across_runs(self) -> None:
        first, listings, advisor_a = _fixture()
        second, _, advisor_b = _fixture()

        a = _compare(first, advisor_a, listings[0]).group
        b = _compare(second, advisor_b, listings[0]).group

        assert a is not None
        assert b is not None
        assert [m.company_id for m in a.included] == [m.company_id for m in b.included]

    def test_excluded_members_carry_a_reason(self) -> None:
        unusable = _listing("NOFACTS", 900, has_fundamentals=False)
        service, listings, advisor = _fixture(extra=[unusable])

        group = _compare(service, advisor, listings[0]).group

        assert group is not None
        excluded = {m.symbol: m.reason for m in group.excluded}
        assert excluded["NOFACTS"] == "no company fundamentals"


class TestPointInTime:
    """Case 4. The whole comparison must be as of one date, on both sides."""

    def test_a_past_as_of_sees_only_what_was_filed_by_then(self) -> None:
        """**The gate.** A 2025 subject must not be ranked against peers'
        2026 filings."""
        service, listings, advisor = _fixture()

        past = _compare(service, advisor, listings[0], as_of=PRIOR)
        present = _compare(service, advisor, listings[0], as_of=AS_OF)

        assert past.as_of == PRIOR
        assert present.as_of == AS_OF
        assert past.group is not None
        assert past.group.as_of == PRIOR

    def test_peer_values_move_with_as_of(self) -> None:
        """If peer metrics were computed at 'latest' rather than at `as_of`,
        the two runs would agree and this would pass for the wrong reason."""
        service, listings, advisor = _fixture()

        past = _compare(service, advisor, listings[0], as_of=PRIOR)
        present = _compare(service, advisor, listings[0], as_of=AS_OF)

        by_metric_past = {c.metric: c.median for c in past.comparisons}
        by_metric_now = {c.metric: c.median for c in present.comparisons}
        shared = set(by_metric_past) & set(by_metric_now)
        assert shared, "no metric was comparable at both dates"
        assert any(by_metric_past[m] != by_metric_now[m] for m in shared), (
            "peer medians are identical across a year -- as_of is being ignored"
        )

    def test_each_as_of_is_cached_separately(self) -> None:
        service, listings, advisor = _fixture()

        _compare(service, advisor, listings[0], as_of=PRIOR)
        _compare(service, advisor, listings[0], as_of=AS_OF)

        keys = {as_of for _group, as_of in service._cache}
        assert keys == {PRIOR, AS_OF}


class TestFinancialSector:
    """Case 7, option A: refuse rather than invent a bank model."""

    @pytest.mark.parametrize("sic", ["6021", "6022", "6199", "6331"])
    def test_a_financial_issuer_is_not_compared(self, sic: str) -> None:
        bank = _listing("BANK", 500, sic)
        service, _listings, advisor = _fixture(count=MIN_PEERS + 2, sic=sic, extra=[bank])

        result = _compare(service, advisor, bank)

        assert result.outcome is PeerOutcome.SECTOR_MODEL_REQUIRED
        assert result.comparisons == ()
        assert "financial" in (result.detail or "")

    def test_a_bank_never_enters_an_industrial_distribution(self) -> None:
        """Excluded from every group, not only from its own -- a bank inside an
        industrial distribution would distort it for everyone else."""
        bank = _listing("BANK", 500, "6021")
        universe = PeerUniverse([*(_listing(f"P{i:02d}", 100 + i) for i in range(3)), bank])

        assert universe.eligible(universe.company(500)) == "financial-sector issuer"


class TestNoClassification:
    def test_an_issuer_without_a_sic_has_no_peer_group(self) -> None:
        """True of funds and of issuers Tradabot could not verify against EDGAR."""
        fund = _listing("SPY", 700, sic=None, asset_type="ETF", sic_description=None)
        service, _listings, advisor = _fixture(extra=[fund])

        result = _compare(service, advisor, fund)

        assert result.outcome is PeerOutcome.NO_CLASSIFICATION
        assert result.group is None

    def test_an_issuer_without_an_sec_identity_has_no_peer_group(self) -> None:
        unknown = _listing("MBG", 800, cik=None, sec_identity=False, has_fundamentals=False)
        service, _listings, advisor = _fixture(extra=[unknown])

        assert _compare(service, advisor, unknown).outcome is PeerOutcome.NO_CLASSIFICATION


class TestPhase13IsolationHolds:
    """Cases 8 and 15. Peer comparison must not become a new way to borrow."""

    @pytest.mark.parametrize(
        ("symbol", "mic", "currency"),
        [
            ("SAP", "XETR", "EUR"),
            ("RY", "XTSE", "CAD"),
            ("TD", "XTSE", "CAD"),
            ("CNQ", "XTSE", "CAD"),
            ("SHOP", "XTSE", "CAD"),
            ("CNR", "XTSE", "CAD"),
        ],
    )
    def test_an_unpriced_listing_contributes_no_valuation_metric(
        self, symbol: str, mic: str, currency: str
    ) -> None:
        """**The gate.** The US sibling is priced; the venue-native line is not,
        and must not gain a multiple through it."""
        us = _listing(symbol, 600, sic="3674")
        foreign = _listing(
            symbol,
            600,
            sic="3674",
            mic=mic,
            country="DE" if mic == "XETR" else "CA",
            quote_currency=currency,
            reporting_currency=currency,
            has_prices=False,
        )
        service, _listings, advisor = _fixture(extra=[us, foreign])

        result = _compare(service, advisor, foreign)

        priced = {c.metric for c in result.comparisons}
        assert not priced & {"pe_ttm", "ps_ttm", "p_fcf"}
        refused = {r.metric for r in result.refusals}
        assert {"pe_ttm", "ps_ttm", "p_fcf"} <= refused

    def test_a_currency_mismatch_withholds_multiples_but_keeps_margins(self) -> None:
        """A US-listed foreign issuer is priced, and still cannot be given a
        multiple -- the phase 13.7 gate, reaching the peer layer unchanged."""
        mismatched = _listing("NVO", 610, reporting_currency="DKK")
        service, _listings, advisor = _fixture(extra=[mismatched])

        result = _compare(service, advisor, mismatched)

        compared = {c.metric for c in result.comparisons}
        assert not compared & {"pe_ttm", "ps_ttm", "p_fcf"}

    def test_no_peer_metric_is_a_benchmark_relative_figure(self) -> None:
        """Relative strength is a market-position reading validated only for US
        listings. It is not a peer dimension and must never become one."""
        from app.peers.schemas import V1_METRICS

        keys = {spec.key for spec in V1_METRICS}
        assert not any("relative" in k or "benchmark" in k or "spy" in k for k in keys)


class TestNoRecommendationVocabulary:
    """Case 15 and the product principle. Position, never merit."""

    BANNED = (
        "buy",
        "sell",
        "hold",
        "overvalued",
        "undervalued",
        "cheap",
        "expensive",
        "attractive",
        "best",
        "top pick",
        "outperform",
        "underperform",
        "score",
        "better than",
        "worse than",
        "superior",
        "excellent",
    )

    def test_the_generated_sentence_never_grades(self) -> None:
        service, listings, advisor = _fixture()

        for listing in listings:
            sentence = describe(_compare(service, advisor, listing)).lower()
            for word in self.BANNED:
                assert word not in sentence, f"{word!r} in {sentence!r}"

    def test_the_sentence_states_position(self) -> None:
        service, listings, advisor = _fixture()

        sentence = describe(_compare(service, advisor, listings[-1]))

        assert "comparable peers" in sentence

    def test_a_refused_comparison_says_nothing(self) -> None:
        service, listings, advisor = _fixture(count=3, sic="9999")

        assert describe(_compare(service, advisor, listings[0])) == ""

    def test_the_package_emits_no_composite_score(self) -> None:
        """No single number ranking companies against each other."""
        from app.peers.schemas import MetricComparison, PeerComparison

        assert not hasattr(PeerComparison, "score")
        assert not hasattr(MetricComparison, "score")


class TestStructuralBoundaries:
    """Case 15. The analysis core is reusable precisely because it depends on
    nothing that would tie it to one consumer."""

    FORBIDDEN = (
        "app.broker",
        "app.paper",
        "app.publishing",
        "app.discord_bot",
        "app.notifications",
        "alpaca",
    )

    def test_the_peer_package_reaches_no_broker_or_consumer(self) -> None:
        import ast
        from pathlib import Path

        for path in Path("app/peers").glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert not any(name.startswith(f) for f in self.FORBIDDEN), (
                        f"{path} imports {name}"
                    )


class TestUniverseSelection:
    def test_one_listing_per_issuer_preferring_a_valuation_capable_one(self) -> None:
        """An issuer with a priced, currency-matched line and a second line must
        contribute through the one whose multiples survive the gate."""
        matched = _listing("RY", 620, mic="XNAS", reporting_currency="USD")
        mismatched = _listing("RY", 620, mic="XTSE", quote_currency="CAD", reporting_currency="USD")
        universe = PeerUniverse([matched, mismatched])

        company = universe.company(620)

        assert company is not None
        assert company.listing.mic == "XNAS"

    def test_an_unpriced_line_loses_to_a_priced_one(self) -> None:
        unpriced = _listing(
            "SAP", 630, mic="XETR", quote_currency="EUR", reporting_currency="EUR", has_prices=False
        )
        priced = _listing("SAP", 630, mic="XNAS", reporting_currency="EUR")
        universe = PeerUniverse([unpriced, priced])

        assert universe.company(630).listing.has_prices is True

    def test_registry_candidates_come_back_in_a_stable_order(self) -> None:
        listings = [_listing(f"P{i:02d}", 100 + i) for i in range(5)]
        forward = InstrumentRegistry(listings).all_candidates()
        reversed_ = InstrumentRegistry(list(reversed(listings))).all_candidates()

        assert [c.symbol for c in forward] == [c.symbol for c in reversed_]
