"""A report about one listing may contain no other listing's market data.

Phase 13.6's audit found the invariant enforced in the wrong place. The Discord
card refused, correctly, to print a Xetra listing's valuation -- but the
``AdvisorReport`` behind it held ``SAP.US``'s price, ``1509`` sessions of the
ADR's history and a SPY-relative return at ``HIGH`` confidence. ``CNR.TO`` was
worse: Canadian National's report carried a complete, internally consistent
valuation built from **Core Natural Resources**' price, shares and revenue,
because both halves were looked up by the bare ticker ``CNR``.

Nothing reached a user, because the renderer happened to check. That is the
problem these tests exist to prevent: the next consumer of ``AdvisorReport`` --
a peer comparison, a ``/fit``, a JSON export -- would not have checked, and the
defect would have arrived looking like a finding.

So every assertion here reads the **report object**. A renderer test would pass
against the defect it is meant to catch, which is exactly how this survived.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.advisor import AdvisorService, FactStore, MarketIdentity, PriceSeries
from app.advisor.facts import company_key
from app.discord_bot.analysis import StockAnalyst
from app.discord_bot.render import check_message
from app.discord_bot.resolve import Availability
from app.discord_bot.resolve import Resolution as CardResolution
from app.instruments.registry import Candidate, InstrumentRegistry

AS_OF = "2026-08-14"
_SESSIONS = 400
"""Comfortably past the 220 the market-position section requires, so a missing
section can only mean the series was withheld and never that it was too short."""


def _series(start: float, *, step: float = 0.1) -> PriceSeries:
    """A rising daily series long enough to satisfy every window."""
    days = [f"2025-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 29)]
    days += [f"2026-{m:02d}-{d:02d}" for m in range(1, 9) for d in range(1, 15)]
    return PriceSeries({day: start + i * step for i, day in enumerate(sorted(days)[:_SESSIONS])})


def _facts(key: str) -> list[dict[str, Any]]:
    """Four quarters of revenue, earnings and a share count for one company."""
    rows: list[dict[str, Any]] = []
    for i, end in enumerate(("2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30")):
        filed = f"{end[:4]}-{int(end[5:7]) + 1:02d}-15"
        rows += [
            {
                "symbol": key,
                "metric": "revenue",
                "value": 1_000.0 + i,
                "period_start": f"{end[:4]}-{int(end[5:7]) - 2:02d}-01",
                "period_end": end,
                "filed": filed,
                "form": "10-Q",
                "unit": "USD",
                "concept": "Revenues",
                "accession": f"a{i}",
                "taxonomy": "us-gaap",
            },
            {
                "symbol": key,
                "metric": "eps_diluted",
                "value": 1.0,
                "period_start": f"{end[:4]}-{int(end[5:7]) - 2:02d}-01",
                "period_end": end,
                "filed": filed,
                "form": "10-Q",
                "unit": "USD/shares",
                "concept": "EarningsPerShareDiluted",
                "accession": f"a{i}",
                "taxonomy": "us-gaap",
            },
            {
                "symbol": key,
                "metric": "shares_outstanding",
                "value": 100.0,
                "period_start": None,
                "period_end": end,
                "filed": filed,
                "form": "10-Q",
                "unit": "shares",
                "concept": "CommonStockSharesOutstanding",
                "accession": f"a{i}",
                "taxonomy": "us-gaap",
            },
        ]
    return rows


CIK = "0000016868"
KEY = company_key(int(CIK))


def _listing(symbol: str, mic: str, **kw: Any) -> Candidate:
    base: dict[str, Any] = {
        "symbol": symbol,
        "mic": mic,
        "country": "US",
        "quote_currency": "USD",
        "company_name": symbol,
        "company_id": 1,
        "cik": CIK,
        "reporting_currency": "USD",
        "taxonomy": "us-gaap",
        "has_prices": True,
        "has_fundamentals": True,
    }
    base.update(kw)
    return Candidate(**base)


def _foreign(symbol: str, mic: str, country: str, currency: str) -> Candidate:
    """A venue-native listing: filings, no prices of its own."""
    return _listing(
        symbol,
        mic,
        country=country,
        quote_currency=currency,
        reporting_currency=currency,
        has_prices=False,
    )


class _Registry:
    """The bot's registry seam. Resolution is tested elsewhere."""

    def __init__(self, registry: InstrumentRegistry) -> None:
        self._registry = registry

    def resolve(self, raw: str) -> Any:
        return self._registry.resolve(raw)


def _analyst(candidates: list[Candidate], prices: dict[str, PriceSeries]) -> StockAnalyst:
    store = FactStore(_facts(KEY))
    return StockAnalyst(
        registry=_Registry(InstrumentRegistry(candidates)),
        advisor=AdvisorService(store, prices),
        universe=sorted(prices),
        fundamentals=frozenset(store.symbols),
        fact_store_ready=True,
        as_of=AS_OF,
    )


# The four same-company cross-venue pairs the audit found, plus the two
# Canadian listings whose bare tickers name a *different* US company.
_PAIRS = [
    pytest.param("SAP", "XETR", "DE", "EUR", id="SAP.DE"),
    pytest.param("RY", "XTSE", "CA", "CAD", id="RY.TO"),
    pytest.param("TD", "XTSE", "CA", "CAD", id="TD.TO"),
    pytest.param("CNQ", "XTSE", "CA", "CAD", id="CNQ.TO"),
    pytest.param("SHOP", "XTSE", "CA", "CAD", id="SHOP.TO"),
    pytest.param("CNR", "XTSE", "CA", "CAD", id="CNR.TO"),
]

_SUFFIX = {"XETR": "DE", "XTSE": "TO"}

_MARKET_DERIVED = (
    "return_20d",
    "return_60d",
    "return_252d",
    "relative_strength_252d",
    "distance_from_ma200",
    "drawdown_from_252d_high",
)
_PRICE_DERIVED = (
    "market_cap",
    "pe_ttm",
    "ps_ttm",
    "p_fcf",
    "earnings_yield",
    "fcf_yield",
    "ps_percentile_own_history",
)


class TestAReportNeverBorrowsAnotherListingsPrices:
    """Cases A to F. Every assertion reads the report, not the card."""

    @pytest.mark.parametrize(("symbol", "mic", "country", "currency"), _PAIRS)
    def test_the_venue_native_report_holds_no_price_at_all(
        self, symbol: str, mic: str, country: str, currency: str
    ) -> None:
        """**The gate.** The US line is priced; the foreign one must not be."""
        analyst = _analyst(
            [_listing(symbol, "XNAS"), _foreign(symbol, mic, country, currency)],
            {symbol: _series(100.0), "SPY": _series(50.0)},
        )

        report = analyst.check(f"{symbol}.{_SUFFIX[mic]}").report

        assert report is not None
        assert report.profile.metrics["price"].value is None
        assert report.profile.labels["price_history_sessions"] == "0"
        for name in _PRICE_DERIVED:
            assert report.valuation.metrics[name].value is None, name

    @pytest.mark.parametrize(("symbol", "mic", "country", "currency"), _PAIRS)
    def test_no_market_position_is_computed_for_it(
        self, symbol: str, mic: str, country: str, currency: str
    ) -> None:
        """No drawdown, no 52-week position, no relative strength."""
        analyst = _analyst(
            [_listing(symbol, "XNAS"), _foreign(symbol, mic, country, currency)],
            {symbol: _series(100.0), "SPY": _series(50.0)},
        )

        position = analyst.check(f"{symbol}.{_SUFFIX[mic]}").report.market_position

        assert not position.metrics
        assert "no market data for this listing" in " ".join(position.confidence_reasons)

    @pytest.mark.parametrize(("symbol", "mic", "country", "currency"), _PAIRS)
    def test_the_us_sibling_still_gets_its_own_prices(
        self, symbol: str, mic: str, country: str, currency: str
    ) -> None:
        """Withholding must be listing-scoped, not company-scoped."""
        analyst = _analyst(
            [_listing(symbol, "XNAS"), _foreign(symbol, mic, country, currency)],
            {symbol: _series(100.0), "SPY": _series(50.0)},
        )

        report = analyst.check(f"{symbol}.US").report

        assert report.profile.metrics["price"].value is not None
        assert report.market_position.metrics["return_252d"].value is not None

    def test_a_bare_ticker_naming_another_company_is_not_a_price_source(self) -> None:
        """The worst case the audit found, and the reason facts moved too.

        ``CNR`` on Toronto is Canadian National. ``CNR`` on Nasdaq is Core
        Natural Resources. Reading either half by the bare ticker produced a
        coherent valuation of the wrong business inside the right company's
        report.
        """
        other = _listing("CNR", "XNAS", company_id=999, cik="0001000000")
        analyst = _analyst(
            [other, _foreign("CNR", "XTSE", "CA", "CAD")],
            {"CNR": _series(100.0), "SPY": _series(50.0)},
        )

        report = analyst.check("CNR.TO").report

        assert report.valuation.metrics["market_cap"].value is None
        assert report.valuation.metrics["pe_ttm"].value is None

    @pytest.mark.parametrize(("symbol", "mic", "country", "currency"), _PAIRS)
    def test_no_spy_relative_metric_survives_outside_the_us(
        self, symbol: str, mic: str, country: str, currency: str
    ) -> None:
        """Case F. SPY was validated against US listings and means nothing here."""
        analyst = _analyst(
            [_listing(symbol, "XNAS"), _foreign(symbol, mic, country, currency)],
            {symbol: _series(100.0), "SPY": _series(50.0)},
        )

        report = analyst.check(f"{symbol}.{_SUFFIX[mic]}").report

        assert "relative_strength_252d" not in report.market_position.metrics
        for section in (report.profile, report.valuation, report.market_position):
            for name in _MARKET_DERIVED:
                assert section.metrics.get(name) is None or (section.metrics[name].value is None)

    def test_a_priced_foreign_venue_would_use_its_own_series_not_the_adr(self) -> None:
        """The rule is about *which* series, not about being foreign.

        When an international market-data source arrives, the Xetra line reads
        the Xetra series. Nothing here has to change for that to be true, and
        this test fails if someone reintroduces a nationality check instead.
        """
        xetra = _listing(
            "SAP",
            "XETR",
            country="DE",
            quote_currency="EUR",
            reporting_currency="EUR",
            has_prices=True,
        )
        analyst = _analyst(
            [_listing("SAP", "XNAS"), xetra],
            {"SAP": _series(100.0), "SPY": _series(50.0)},
        )

        report = analyst.check("SAP.DE").report

        assert report.profile.metrics["price"].value is not None
        # Still no benchmark: none has been validated for Xetra.
        assert report.market_position.metrics["relative_strength_252d"].value is None


class TestUnitsAreNotMixedInsideTheReport:
    """Case M, enforced where the ratio is formed rather than where it prints."""

    def test_a_currency_mismatch_withholds_every_ratio(self) -> None:
        """Novo Nordisk trades in USD and reports in DKK; 1.99x was a unit error."""
        analyst = _analyst(
            [_listing("NVO", "XNAS", reporting_currency="DKK")],
            {"NVO": _series(100.0), "SPY": _series(50.0)},
        )

        check = analyst.check("NVO")

        assert check.valuation_refusal is not None
        for name in ("pe_ttm", "ps_ttm", "p_fcf", "earnings_yield", "fcf_yield"):
            metric = check.report.valuation.metrics[name]
            assert metric.value is None, name
            assert "no currency conversion" in (metric.unavailable_reason or "")

    def test_but_the_listing_keeps_its_own_price_and_market_position(self) -> None:
        """A US-listed foreign issuer is still a US listing. Identity, not
        nationality, owns market data."""
        analyst = _analyst(
            [_listing("CNI", "XNAS", reporting_currency="CAD")],
            {"CNI": _series(100.0), "SPY": _series(50.0)},
        )

        report = analyst.check("CNI").report

        assert report.profile.metrics["price"].value is not None
        assert report.market_position.metrics["relative_strength_252d"].value is not None

    def test_a_matching_currency_is_untouched(self) -> None:
        analyst = _analyst(
            [_listing("AAPL", "XNAS")],
            {"AAPL": _series(100.0), "SPY": _series(50.0)},
        )

        valuation = analyst.check("AAPL").report.valuation

        assert valuation.metrics["pe_ttm"].value is not None
        assert valuation.metrics["market_cap"].value is not None


class TestTheLegacyPathIsUnchanged:
    """A caller holding only a bare ticker means the US line that names."""

    def test_omitting_the_market_identity_matches_naming_it_explicitly(self) -> None:
        service = AdvisorService(
            FactStore(_facts("AAPL")), {"AAPL": _series(100.0), "SPY": _series(50.0)}
        )

        implicit = service.analyse("AAPL", as_of=AS_OF)
        explicit = service.analyse("AAPL", as_of=AS_OF, market=MarketIdentity("AAPL", "SPY"))

        assert implicit == explicit
        assert implicit.valuation.metrics["pe_ttm"].value is not None


class TestTheAmbiguityCardShowsEveryListing:
    """Cases G to J, and L. The card that exists to point at the alternatives
    must actually contain them."""

    @pytest.mark.parametrize(
        ("symbol", "mic", "country", "currency", "suffix"),
        [
            ("SAP", "XETR", "DE", "EUR", "DE"),
            ("RY", "XTSE", "CA", "CAD", "TO"),
            ("TD", "XTSE", "CA", "CAD", "TO"),
            ("CNQ", "XTSE", "CA", "CAD", "TO"),
        ],
    )
    def test_both_venues_of_one_company_are_offered(
        self, symbol: str, mic: str, country: str, currency: str, suffix: str
    ) -> None:
        """**The gate.** Fields were keyed by company name, and two listings of
        one issuer share it, so one silently overwrote the other: ``/check SAP``
        said "2 listings, name the venue" and offered only ``SAP.US``."""
        analyst = _analyst(
            [_listing(symbol, "XNAS"), _foreign(symbol, mic, country, currency)],
            {symbol: _series(100.0), "SPY": _series(50.0)},
        )

        check = analyst.check(symbol)
        message = check_message(check)

        assert check.resolution is CardResolution.AMBIGUOUS_SYMBOL
        assert f"{symbol}.{suffix}" in message.fields
        assert f"{symbol}.US" in message.fields
        rendered = " ".join(message.fields.values())
        assert f"/check symbol:{symbol}.{suffix}" in rendered
        assert f"/check symbol:{symbol}.US" in rendered

    def test_two_different_companies_remain_visible_and_named(self) -> None:
        """Case L. The original defect class must not regress."""
        analyst = _analyst(
            [
                _listing("DTE", "XNAS", company_name="DTE Energy Co"),
                _listing(
                    "DTE",
                    "XETR",
                    company_name="Deutsche Telekom AG",
                    company_id=2,
                    country="DE",
                    quote_currency="EUR",
                    cik=None,
                    reporting_currency="EUR",
                    has_prices=False,
                    has_fundamentals=False,
                ),
            ],
            {"DTE": _series(100.0), "SPY": _series(50.0)},
        )

        fields = check_message(analyst.check("DTE")).fields

        assert set(fields) == {"DTE.DE", "DTE.US", "Why"}
        assert "Deutsche Telekom AG" in fields["DTE.DE"]
        assert "DTE Energy Co" in fields["DTE.US"]

    def test_it_still_refuses_rather_than_choosing(self) -> None:
        analyst = _analyst(
            [_listing("SAP", "XNAS"), _foreign("SAP", "XETR", "DE", "EUR")],
            {"SAP": _series(100.0), "SPY": _series(50.0)},
        )

        check = analyst.check("SAP")

        assert check.report is None
        assert check.listing is None
        assert "Name the venue" in (check.detail or "")


class TestExactSymbolResolution:
    def test_a_class_share_still_resolves_to_itself(self) -> None:
        """Case K. ``BRK.B`` is a symbol, not ``BRK`` on a venue called ``B``."""
        analyst = _analyst(
            [_listing("BRK.B", "XNAS", has_fundamentals=False, cik=None)],
            {"BRK.B": _series(100.0), "SPY": _series(50.0)},
        )

        check = analyst.check("BRK.B")

        assert check.resolution is CardResolution.MARKET_DATA_ONLY
        assert check.listing is not None
        assert check.listing.symbol == "BRK.B"
        assert check.report.profile.metrics["price"].value is not None


class TestFundSemantics:
    """Case N. A CIK is an SEC identity, not a set of company fundamentals."""

    def test_a_fund_does_not_claim_company_fundamentals(self) -> None:
        registry = InstrumentRegistry(
            [_listing("SPY", "ARCX", asset_type="ETF", sec_identity=True)]
        )

        found = registry.resolve("SPY")

        assert found.listing is not None
        assert found.listing.sec_identity is True
        assert found.listing.has_fundamentals is False

    def test_an_operating_company_with_a_cik_is_unaffected(self) -> None:
        registry = InstrumentRegistry(
            [_listing("AAPL", "XNAS", asset_type="STOCK", sec_identity=True)]
        )

        assert registry.resolve("AAPL").listing.has_fundamentals is True

    def test_the_card_says_fund_rather_than_missing_data(self) -> None:
        analyst = _analyst(
            [_listing("SPY", "ARCX", asset_type="ETF", has_fundamentals=False)],
            {"SPY": _series(100.0)},
        )

        quality = check_message(analyst.check("SPY")).fields["Data quality"]

        assert "fund, not an operating company" in quality
        assert "absence of data" not in quality

    def test_a_security_is_not_compared_to_itself(self) -> None:
        """``SPY`` against ``SPY`` is zero by construction, and ``+0.0%`` reads
        as a finding rather than as an identity."""
        analyst = _analyst(
            [_listing("SPY", "ARCX", asset_type="ETF", has_fundamentals=False)],
            {"SPY": _series(100.0)},
        )

        check = analyst.check("SPY")
        metric = check.report.market_position.metrics["relative_strength_252d"]

        assert metric.value is None
        assert "itself" in (metric.unavailable_reason or "")
        assert "1Y vs benchmark" not in check_message(check).fields["Market position"]


class TestRefusalCopyMatchesTheResolution:
    """Case O."""

    def test_a_known_but_unsupported_listing_is_not_called_unknown(self) -> None:
        """Saying "no supported instrument matches MBG" directly above a field
        naming Mercedes-Benz Group on Xetra contradicted itself."""
        analyst = _analyst(
            [
                _listing(
                    "MBG",
                    "XETR",
                    company_name="Mercedes-Benz Group AG",
                    country="DE",
                    quote_currency="EUR",
                    cik=None,
                    reporting_currency="EUR",
                    has_prices=False,
                    has_fundamentals=False,
                )
            ],
            {"AAPL": _series(100.0)},
        )

        check = analyst.check("MBG")
        message = check_message(check)

        assert check.resolution is CardResolution.UNSUPPORTED_LISTING
        assert "No supported instrument matches" not in message.body
        assert "listing Tradabot knows" in message.body
        assert "Mercedes-Benz Group AG" in message.fields["Listing"]

    def test_a_genuinely_unknown_symbol_still_says_so(self) -> None:
        analyst = _analyst([_listing("AAPL", "XNAS")], {"AAPL": _series(100.0)})

        message = check_message(analyst.check("ZZZZ"))

        assert "No supported listing matches" in message.body
        assert "Listing" not in message.fields

    def test_an_unknown_venue_suffix_names_what_was_parsed(self) -> None:
        analyst = _analyst([_listing("RY", "XNAS")], {"RY": _series(100.0)})

        message = check_message(analyst.check("RY.CA"))

        assert '"CA" is not a venue Tradabot knows' in message.body


class TestNothingHereIsAnalysable:
    """The two availability flags stay independent of each other."""

    def test_a_venue_native_listing_is_fundamentals_only(self) -> None:
        analyst = _analyst(
            [_listing("SAP", "XNAS"), _foreign("SAP", "XETR", "DE", "EUR")],
            {"SAP": _series(100.0), "SPY": _series(50.0)},
        )

        check = analyst.check("SAP.DE")

        assert check.market_data is Availability.UNAVAILABLE
        assert check.fundamentals is Availability.AVAILABLE
        assert check.checked_at.tzinfo is UTC or isinstance(check.checked_at, datetime)
