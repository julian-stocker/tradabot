"""A ticker must never name a company on its own.

Phase 13.0 measured the cost of letting it: DTE resolved to DTE Energy rather
than Deutsche Telekom, ALV to Autoliv, ABX to Abacus Global Management, CNR to
Core Natural Resources. Four of twelve, each a complete and confident report
about the wrong business. These tests exist so that cannot come back.
"""

from __future__ import annotations

import pytest

from app.instruments.registry import (
    Candidate,
    InstrumentRegistry,
    Resolution,
    benchmark_for,
    split_suffix,
    valuation_allowed,
)


def listing(
    symbol: str,
    mic: str,
    company: str,
    company_id: int,
    *,
    country: str = "US",
    currency: str = "USD",
    cik: str | None = "0000000001",
    reporting: str | None = "USD",
    prices: bool = True,
    facts: bool = True,
) -> Candidate:
    return Candidate(
        symbol=symbol,
        mic=mic,
        country=country,
        quote_currency=currency,
        company_name=company,
        company_id=company_id,
        cik=cik,
        reporting_currency=reporting,
        taxonomy="us-gaap",
        has_prices=prices,
        has_fundamentals=facts,
    )


COLLIDING = InstrumentRegistry(
    [
        listing("AAPL", "XNAS", "Apple Inc.", 1),
        listing("DTE", "XNAS", "DTE Energy Co", 2),
        listing(
            "DTE",
            "XETR",
            "Deutsche Telekom AG",
            3,
            country="DE",
            currency="EUR",
            cik=None,
            reporting="EUR",
            prices=False,
            facts=False,
        ),
        listing("SAP", "XNAS", "SAP SE", 4),
        listing(
            "SAP", "XETR", "SAP SE", 4, country="DE", currency="EUR", reporting="EUR", prices=False
        ),
    ]
)


class TestNeverTheWrongCompany:
    @pytest.mark.parametrize("ticker", ["DTE"])
    def test_a_cross_company_collision_refuses(self, ticker: str) -> None:
        """**The gate.** Two companies, one ticker, no choice made."""
        found = COLLIDING.resolve(ticker)
        assert found.resolution is Resolution.AMBIGUOUS_SYMBOL
        assert found.listing is None
        assert len(found.candidates) == 2
        assert "different companies" in (found.detail or "")

    def test_a_same_company_collision_also_refuses_but_says_why(self) -> None:
        """One company, two venues, two currencies — still the user's choice."""
        found = COLLIDING.resolve("SAP")
        assert found.resolution is Resolution.AMBIGUOUS_SYMBOL
        assert found.listing is None
        assert "same company" in (found.detail or "")

    def test_an_unambiguous_us_ticker_still_resolves(self) -> None:
        found = COLLIDING.resolve("AAPL")
        assert found.resolution is Resolution.SUPPORTED
        assert found.listing is not None
        assert found.listing.company_name == "Apple Inc."

    def test_a_venue_qualified_symbol_picks_exactly_one(self) -> None:
        found = COLLIDING.resolve("DTE.DE")
        assert found.listing is not None
        assert found.listing.company_name == "Deutsche Telekom AG"
        assert found.listing.mic == "XETR"

    def test_the_other_venue_is_a_different_company(self) -> None:
        assert COLLIDING.resolve("DTE.US").listing.company_name == "DTE Energy Co"

    def test_an_unknown_venue_suffix_is_refused_not_guessed(self) -> None:
        found = COLLIDING.resolve("SAP.XX")
        assert found.resolution is Resolution.UNKNOWN_SYMBOL
        assert found.listing is None

    def test_a_listing_absent_from_a_named_venue_refuses(self) -> None:
        assert COLLIDING.resolve("AAPL.DE").resolution is Resolution.UNKNOWN_SYMBOL

    @pytest.mark.parametrize("raw", ["", "!!!", "  ", "@@@@"])
    def test_malformed_input_is_its_own_state(self, raw: str) -> None:
        assert COLLIDING.resolve(raw).resolution is Resolution.MALFORMED_SYMBOL

    def test_collisions_are_enumerable(self) -> None:
        assert set(COLLIDING.collisions()) == {"DTE", "SAP"}


class TestDataAvailabilityStates:
    def test_a_listing_with_filings_but_no_prices(self) -> None:
        """The ordinary state of a foreign listing before any provider exists."""
        found = COLLIDING.resolve("SAP.DE")
        assert found.resolution is Resolution.FUNDAMENTALS_ONLY

    def test_a_listing_with_neither_is_named_not_hidden(self) -> None:
        found = COLLIDING.resolve("DTE.DE")
        assert found.resolution is Resolution.UNSUPPORTED_LISTING
        assert found.listing is not None
        assert found.listing.company_name == "Deutsche Telekom AG"


class TestCurrencyRule:
    def test_matching_currencies_allow_valuation(self) -> None:
        ok, why = valuation_allowed(listing("AAPL", "XNAS", "Apple", 1))
        assert ok
        assert why is None

    def test_mismatched_currencies_refuse_valuation(self) -> None:
        """**The gate.** No FX exists, so a mixed ratio would be wrong by the rate."""
        ok, why = valuation_allowed(
            listing("SAP", "XNYS", "SAP SE", 4, currency="USD", reporting="EUR")
        )
        assert not ok
        assert "USD" in (why or "")
        assert "EUR" in (why or "")
        assert "no currency conversion" in (why or "").replace("performs ", "")

    def test_an_unknown_reporting_currency_refuses(self) -> None:
        ok, _why = valuation_allowed(listing("X", "XETR", "X", 9, reporting=None))
        assert not ok

    def test_a_listing_without_prices_refuses(self) -> None:
        ok, why = valuation_allowed(listing("SAP", "XETR", "SAP", 4, prices=False))
        assert not ok
        assert "market data" in (why or "")


class TestBenchmarkSafety:
    def test_us_listings_keep_spy(self) -> None:
        assert benchmark_for(listing("AAPL", "XNAS", "Apple", 1)) == "SPY"

    @pytest.mark.parametrize(("mic", "country"), [("XETR", "DE"), ("XTSE", "CA")])
    def test_non_us_listings_have_no_benchmark(self, mic: str, country: str) -> None:
        """**The gate.** SPY vs a Xetra line would look normal and mean nothing."""
        assert benchmark_for(listing("X", mic, "X", 9, country=country, currency="EUR")) is None


class TestSuffixSyntax:
    @pytest.mark.parametrize(
        ("raw", "symbol", "mic"),
        [
            ("SAP.DE", "SAP", "XETR"),
            ("shop.to", "SHOP", "XTSE"),
            ("AAPL", "AAPL", None),
            (" ry.to ", "RY", "XTSE"),
        ],
    )
    def test_suffixes_translate_to_declared_venues(
        self, raw: str, symbol: str, mic: str | None
    ) -> None:
        assert split_suffix(raw)[:2] == (symbol, mic)

    def test_an_undeclared_suffix_yields_no_venue(self) -> None:
        _symbol, mic, had = split_suffix("SAP.ZZ")
        assert had is True
        assert mic is None


class TestIfrsRefusals:
    def test_ambiguous_ifrs_metrics_are_not_mapped(self) -> None:
        """**The gate.** The Salesforce share-count defect must not return."""
        from app.fundamentals.concepts import IFRS_CONCEPTS, IFRS_REFUSED

        for metric in IFRS_REFUSED:
            assert metric not in IFRS_CONCEPTS
        assert "shares_outstanding" in IFRS_REFUSED
        assert "capex" in IFRS_REFUSED
        assert "free_cash_flow" in IFRS_REFUSED

    def test_shares_issued_is_never_treated_as_outstanding(self) -> None:
        from app.fundamentals.concepts import METRIC_BY_IFRS_CONCEPT

        assert "NumberOfSharesIssued" not in METRIC_BY_IFRS_CONCEPT
        assert "IssuedCapital" not in METRIC_BY_IFRS_CONCEPT

    def test_direct_metrics_are_mapped(self) -> None:
        from app.fundamentals.concepts import METRIC_BY_IFRS_CONCEPT

        assert METRIC_BY_IFRS_CONCEPT["ProfitLoss"] == "net_income"
        assert METRIC_BY_IFRS_CONCEPT["GrossProfit"] == "gross_profit"
        assert METRIC_BY_IFRS_CONCEPT["Revenue"] == "revenue"
