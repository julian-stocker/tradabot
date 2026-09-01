"""International /check wording and parsing, driven by real Discord usage.

Everything here was found by someone typing into Discord rather than by reading
code, which is why the assertions are about what a reader sees.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.discord_bot.analysis import StockCheck
from app.discord_bot.render import check_message
from app.discord_bot.resolve import Availability, Resolution
from app.instruments.registry import Candidate, InstrumentRegistry
from app.instruments.registry import Resolution as VenueResolution

# Two enums share these member names: the resolver's own outcome vocabulary and
# the Discord-facing one. Kept distinct here so an assertion cannot silently
# compare across them.


def _candidate(symbol: str, mic: str, **kw: Any) -> Candidate:
    base: dict[str, Any] = {
        "symbol": symbol,
        "mic": mic,
        "country": "US",
        "quote_currency": "USD",
        "company_name": symbol,
        "company_id": abs(hash((symbol, mic))) % 10_000,
        "cik": "0000000001",
        "reporting_currency": "USD",
        "taxonomy": "us-gaap",
        "has_prices": True,
        "has_fundamentals": True,
    }
    base.update(kw)
    return Candidate(**base)


class TestDottedTickers:
    """A dot is only a venue separator when it is not part of the ticker."""

    def test_a_class_share_ticker_resolves(self) -> None:
        """``BRK.B`` is Berkshire's class B share, not "BRK on venue B". It sat
        in the registry and could not be reached, because the parser split it
        and then rejected the half it invented."""
        registry = InstrumentRegistry([_candidate("BRK.B", "XNAS")])

        resolved = registry.resolve("BRK.B")

        assert resolved.resolution is VenueResolution.SUPPORTED
        assert resolved.listing is not None
        assert resolved.listing.symbol == "BRK.B"

    def test_a_venue_suffix_still_works(self) -> None:
        registry = InstrumentRegistry(
            [
                _candidate("SAP", "XETR", country="DE", quote_currency="EUR"),
                _candidate("SAP", "XNAS"),
            ]
        )

        resolved = registry.resolve("SAP.DE")

        assert resolved.listing is not None
        assert resolved.listing.mic == "XETR"

    def test_an_exact_symbol_outranks_a_speculative_split(self) -> None:
        """If both readings are possible, the ticker that actually exists wins."""
        registry = InstrumentRegistry(
            [
                _candidate("BF.B", "XNAS"),
                _candidate("BF", "XETR", country="DE", quote_currency="EUR"),
            ]
        )

        assert registry.resolve("BF.B").listing is not None
        assert registry.resolve("BF.B").listing.symbol == "BF.B"

    def test_an_unknown_suffix_is_still_refused(self) -> None:
        registry = InstrumentRegistry([_candidate("RY", "XTSE", country="CA")])

        assert registry.resolve("RY.CA").resolution is VenueResolution.UNKNOWN_SYMBOL


class TestParsingDiagnostics:
    """A rejection must name the half of the input that was wrong."""

    @pytest.fixture
    def registry(self) -> InstrumentRegistry:
        return InstrumentRegistry([_candidate("RY", "XTSE", country="CA")])

    def test_the_error_states_what_was_parsed(self, registry: InstrumentRegistry) -> None:
        detail = registry.resolve("RY.CA").detail or ""

        assert "RY.CA" in detail, "the normalised input"
        assert '"RY"' in detail, "the interpreted base symbol"
        assert '"CA"' in detail, "the interpreted venue suffix"
        assert "TO" in detail, "the recognised suffixes"

    def test_nothing_is_silently_repaired(self, registry: InstrumentRegistry) -> None:
        """``RY.CA`` must not quietly become ``RY.TO``. A near-miss is offered,
        never executed."""
        resolved = registry.resolve("RY.CA")

        assert resolved.listing is None
        assert resolved.candidates == ()


def _report(*, sector: bool = False, taxonomy: str = "us-gaap") -> SimpleNamespace:
    def metric(value: float | None) -> SimpleNamespace:
        return SimpleNamespace(value=value, available=value is not None)

    risks = (
        {
            "data": (
                "financial-sector company: generic margin, cash-flow and "
                "leverage analysis is refused",
            )
        }
        if sector
        else {"data": ()}
    )
    return SimpleNamespace(
        company_quality=[
            SimpleNamespace(
                name="GROWTH", metrics={"revenue_ttm": metric(6.8e10)}, labels={}, confidence="HIGH"
            ),
            SimpleNamespace(
                name="BALANCE SHEET",
                metrics={"cash": metric(4.6e10)},
                labels={"assessment": "SECTOR_SPECIFIC_MODEL_REQUIRED"}
                if sector
                else {"assessment": "INSUFFICIENT_DATA"},
                confidence="INSUFFICIENT",
            ),
            SimpleNamespace(
                name="PROFITABILITY",
                metrics={"gross_margin": metric(None)},
                labels={},
                confidence="INSUFFICIENT",
            ),
        ],
        valuation=SimpleNamespace(metrics={}, labels={}),
        market_position=SimpleNamespace(metrics={}),
        confidence={"company_analysis": "INSUFFICIENT"},
        risks=risks,
        summary="",
    )


def _card(report: Any, taxonomy: str, currency: str) -> dict[str, str]:
    listing = _candidate(
        "RY",
        "XTSE",
        country="CA",
        quote_currency=currency,
        reporting_currency=currency,
        taxonomy=taxonomy,
        has_prices=False,
    )
    return dict(
        check_message(
            StockCheck(
                requested="RY.TO",
                symbol="RY",
                resolution=Resolution.FUNDAMENTALS_ONLY,
                market_data=Availability.UNAVAILABLE,
                fundamentals=Availability.AVAILABLE,
                as_of="2026-08-14",
                checked_at=datetime(2026, 8, 14, tzinfo=UTC),
                report=report,
                listing=listing,
            )
        ).fields
    )


class TestFinancialSectorWording:
    def test_the_card_says_why_a_bank_is_not_graded(self) -> None:
        """ "Not applicable to this sector" is accurate and tells a reader
        nothing. The vocabulary already carried the reason."""
        block = _card(_report(sector=True), "ifrs-full", "CAD")["Balance sheet"]

        assert "financial company" in block
        assert "deposits" in block

    def test_the_bank_is_not_graded(self) -> None:
        """Explaining the refusal must not become a verdict."""
        block = _card(_report(sector=True), "ifrs-full", "CAD")["Balance sheet"]

        for verdict in ("Acceptable", "Leveraged", "Net cash", "Net debt"):
            assert verdict not in block

    def test_the_explanation_appears_once(self) -> None:
        """One sentence per card, not once per silenced section."""
        fields = _card(_report(sector=True), "ifrs-full", "CAD")

        assert sum(1 for v in fields.values() if "deposits" in v) == 1


class TestCoverageWording:
    def test_deliberate_refusals_read_as_partial_coverage(self) -> None:
        """ "Insufficient data" under reconciled CAD revenue reads as *these
        numbers are not trustworthy*, which is the opposite of the truth."""
        quality = _card(_report(sector=True), "ifrs-full", "CAD")["Data quality"]

        assert "Coverage" in quality
        assert "Partial" in quality
        assert "not unreliable figures" in quality

    def test_a_genuine_data_gap_still_says_insufficient(self) -> None:
        """The relabel is for refusals with a stated reason, not for absence."""
        report = _report(sector=False, taxonomy="us-gaap")
        report.company_quality[1].labels = {"assessment": "INSUFFICIENT_DATA"}
        report.company_quality[1].metrics = {"cash": SimpleNamespace(value=None, available=False)}

        quality = _card(report, "us-gaap", "USD")["Data quality"]

        assert "Confidence" in quality
        assert "Coverage" not in quality

    def test_a_healthy_us_card_is_untouched(self) -> None:
        report = _report()
        report.confidence = {"company_analysis": "HIGH"}
        report.company_quality[1].confidence = "HIGH"
        report.company_quality[2].confidence = "HIGH"

        quality = _card(report, "us-gaap", "USD")["Data quality"]

        assert "Confidence" in quality
        assert "Coverage" not in quality
