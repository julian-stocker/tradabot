"""Reporting currency and taxonomy: derived from filings, enforced in output.

Two defects are pinned here, and they compound.

The first is presentation: SAP SE's euro revenue rendered with a dollar sign,
because the Xetra listing resolves to the same company row as the New York ADR
and that row was seeded ``USD``/``us-gaap`` when the universe was US-only.

The second is arithmetic, and it is worse. Canadian National showed a P/E of
16.75x built from CAD earnings and a USD price; Novo Nordisk showed 1.99x, which
reads as a spectacular bargain and is a unit error. ``valuation_allowed`` was
written to prevent exactly this and was never called.
"""

from __future__ import annotations

import polars as pl
import pytest

from app.fundamentals.reporting import CURRENCY_MAJORITY, RECENT_YEARS, observe
from app.instruments.registry import Candidate, valuation_allowed


def _facts(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "cik": pl.Int64,
            "taxonomy": pl.String,
            "unit": pl.String,
            "period_end": pl.String,
        },
    )


def _row(cik: int, taxonomy: str, unit: str, year: int) -> dict[str, object]:
    return {
        "cik": cik,
        "taxonomy": taxonomy,
        "unit": unit,
        "period_end": f"{year}-12-31",
    }


class TestObserve:
    def test_reads_currency_and_taxonomy_from_the_filings(self) -> None:
        found = observe(_facts([_row(1, "ifrs-full", "EUR", 2025)] * 9))

        assert found[1].currency == "EUR"
        assert found[1].taxonomy == "ifrs-full"
        assert found[1].confident

    def test_taxonomy_and_currency_are_independent(self) -> None:
        """Canadian National files us-gaap in CAD. Neither follows from country,
        and a rule keyed on either one would get this company wrong."""
        found = observe(_facts([_row(16868, "us-gaap", "CAD", 2025)] * 5))

        assert (found[16868].taxonomy, found[16868].currency) == ("us-gaap", "CAD")

    def test_dei_does_not_vote_on_taxonomy(self) -> None:
        """Cover-page metadata is filed by us-gaap and IFRS issuers alike. Left
        in, it would make every IFRS filer look mixed."""
        found = observe(
            _facts([_row(1, "ifrs-full", "EUR", 2025)] * 3 + [_row(1, "dei", "EUR", 2025)] * 90)
        )

        assert found[1].taxonomy == "ifrs-full"

    def test_a_minority_of_foreign_facts_does_not_flip_the_currency(self) -> None:
        found = observe(
            _facts([_row(1, "ifrs-full", "EUR", 2025)] * 9 + [_row(1, "ifrs-full", "USD", 2025)])
        )

        assert found[1].currency == "EUR"
        assert found[1].currency_share == pytest.approx(0.9)

    def test_a_genuinely_mixed_history_is_not_confident(self) -> None:
        """Not every question has an answer. A company whose recent facts split
        evenly gets ``confident == False``, and the caller leaves the registry
        alone rather than resolving it by a coin toss."""
        found = observe(
            _facts(
                [_row(1, "ifrs-full", "EUR", 2025)] * 5 + [_row(1, "ifrs-full", "USD", 2025)] * 5
            )
        )

        assert found[1].currency_share < CURRENCY_MAJORITY
        assert not found[1].confident

    def test_a_superseded_presentation_currency_does_not_outvote_the_current_one(
        self,
    ) -> None:
        """A company that redomiciled reports both currencies across its history.
        What the Advisor renders is recent, so the recent window decides."""
        old = [_row(1, "ifrs-full", "GBP", 2005)] * 50
        new = [_row(1, "ifrs-full", "EUR", 2025)] * 4

        assert observe(_facts(old + new))[1].currency == "EUR"

    def test_the_recency_window_is_anchored_to_the_data_not_the_clock(self) -> None:
        """A store rebuilt from a months-old cache must give the answer it gave
        when it was written. Anchoring to ``now`` would make that untrue, and
        untrue only sometimes, which is the hard kind."""
        stale = [_row(1, "ifrs-full", "EUR", 2019)] * 4

        assert observe(_facts(stale))[1].currency == "EUR"

    def test_older_than_the_window_is_excluded(self) -> None:
        rows = [_row(1, "ifrs-full", "EUR", 2025)] + [
            _row(1, "ifrs-full", "GBP", 2025 - RECENT_YEARS - 1)
        ] * 20

        assert observe(_facts(rows))[1].currency == "EUR"

    def test_non_monetary_units_are_not_currencies(self) -> None:
        """``shares`` and ``pure`` are units too, and neither is ISO 4217."""
        rows = [_row(1, "us-gaap", "shares", 2025)] * 20 + [_row(1, "us-gaap", "pure", 2025)] * 20

        assert observe(_facts(rows))[1].currency is None

    def test_a_company_with_no_monetary_facts_gets_no_default(self) -> None:
        found = observe(_facts([_row(1, "us-gaap", "shares", 2025)]))

        assert found[1].currency is None
        assert not found[1].confident

    def test_an_empty_frame_yields_nothing(self) -> None:
        assert observe(_facts([])) == {}

    def test_a_dei_only_company_yields_nothing(self) -> None:
        assert observe(_facts([_row(1, "dei", "USD", 2025)])) == {}

    def test_ties_break_deterministically(self) -> None:
        """Same store, same answer, on every machine. A tie resolved by row order
        would make the registry depend on how polars happened to group."""
        rows = [_row(1, "us-gaap", "CAD", 2025)] * 5 + [_row(1, "us-gaap", "AUD", 2025)] * 5

        assert {observe(_facts(rows))[1].currency for _ in range(5)} == {"AUD"}


def _candidate(**kwargs: object) -> Candidate:
    base: dict[str, object] = {
        "symbol": "X",
        "mic": "XNYS",
        "country": "US",
        "quote_currency": "USD",
        "company_name": "Example",
        "company_id": 1,
        "cik": "0000000001",
        "reporting_currency": "USD",
        "taxonomy": "us-gaap",
        "has_prices": True,
        "has_fundamentals": True,
    }
    base.update(kwargs)
    return Candidate(**base)  # type: ignore[arg-type]


class TestValuationCurrencyGuard:
    def test_matching_currencies_are_allowed(self) -> None:
        allowed, reason = valuation_allowed(_candidate())

        assert allowed
        assert reason is None

    @pytest.mark.parametrize(
        ("symbol", "reports"),
        [("NVO", "DKK"), ("CNI", "CAD"), ("UL", "EUR"), ("BCS", "GBP")],
    )
    def test_a_us_listed_foreign_issuer_gets_no_ratio(self, symbol: str, reports: str) -> None:
        """These four trade in USD and report in something else. Every one of
        them was previously given a P/E mixing the two."""
        allowed, reason = valuation_allowed(_candidate(symbol=symbol, reporting_currency=reports))

        assert not allowed
        assert reason is not None
        assert reports in reason
        assert "USD" in reason

    def test_an_unpriced_listing_is_refused_for_that_reason_first(self) -> None:
        allowed, reason = valuation_allowed(
            _candidate(has_prices=False, reporting_currency="EUR", quote_currency="EUR")
        )

        assert not allowed
        assert reason == "no market data for this listing"

    def test_an_unknown_reporting_currency_is_refused_rather_than_assumed(self) -> None:
        allowed, reason = valuation_allowed(_candidate(reporting_currency=None))

        assert not allowed
        assert reason is not None


class TestRenderedCurrency:
    """The card itself. A figure is only right if its unit is right."""

    @staticmethod
    def _card(reports: str, quote: str, *, priced: bool = True) -> dict[str, str]:
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from app.discord_bot.analysis import StockCheck
        from app.discord_bot.render import check_message
        from app.discord_bot.resolve import Availability, Resolution

        def metric(value: float | None) -> SimpleNamespace:
            return SimpleNamespace(value=value, available=value is not None)

        report = SimpleNamespace(
            company_quality=[
                SimpleNamespace(
                    name="GROWTH",
                    metrics={"revenue_ttm": metric(3.68e10), "eps_ttm": metric(6.1)},
                    labels={},
                    confidence="HIGH",
                ),
                SimpleNamespace(
                    name="BALANCE SHEET",
                    metrics={
                        "cash": metric(8.22e9),
                        "total_debt": metric(1.0e10),
                        "net_cash_or_debt": metric(-1.78e9),
                    },
                    labels={"assessment": "ACCEPTABLE"},
                    confidence="HIGH",
                ),
            ],
            valuation=SimpleNamespace(
                metrics={"pe_ttm": metric(19.9)},
                labels={"ps_context": "NORMAL_VS_HISTORY"},
            ),
            market_position=SimpleNamespace(metrics={"relative_strength_252d": metric(0.31)}),
            confidence={"company_analysis": "HIGH"},
            summary="",
        )
        listing = _candidate(reporting_currency=reports, quote_currency=quote, has_prices=priced)
        allowed, reason = valuation_allowed(listing)
        return check_message(
            StockCheck(
                requested="X",
                symbol="X",
                resolution=Resolution.SUPPORTED,
                market_data=(Availability.AVAILABLE if priced else Availability.UNAVAILABLE),
                fundamentals=Availability.AVAILABLE,
                as_of="2026-08-14",
                checked_at=datetime(2026, 8, 14, tzinfo=UTC),
                report=report,
                listing=listing,
                valuation_refusal=None if allowed else reason,
            )
        ).fields

    def test_a_euro_filer_is_not_printed_in_dollars(self) -> None:
        fields = self._card("EUR", "EUR", priced=False)

        assert "€36.80B" in fields["Growth"]
        assert "$" not in fields["Growth"]

    def test_per_share_figures_carry_the_currency_too(self) -> None:
        assert "€6.10" in self._card("EUR", "EUR", priced=False)["Growth"]

    def test_canadian_dollars_are_distinguished_from_us_dollars(self) -> None:
        """``CA$`` rather than ``$``. Royal Bank's revenue in plain dollars is a
        different and much larger claim."""
        assert "CA$8.22B" in self._card("CAD", "CAD", priced=False)["Balance sheet"]

    def test_the_net_position_line_follows_the_reporting_currency(self) -> None:
        assert "€1.78B" in self._card("EUR", "EUR", priced=False)["Balance sheet"]

    def test_an_unlisted_currency_renders_as_its_code(self) -> None:
        """Better a plain ``DKK`` than a guessed glyph."""
        assert "DKK 36.80B" in self._card("DKK", "DKK", priced=False)["Growth"]

    def test_us_companies_are_untouched(self) -> None:
        fields = self._card("USD", "USD")

        assert "$36.80B" in fields["Growth"]
        assert "P/E" in fields["Valuation"]

    def test_a_currency_mismatch_withholds_the_ratio(self) -> None:
        """Novo Nordisk's 1.99x. The arithmetic completes; the meaning does not."""
        fields = self._card("DKK", "USD")

        assert "P/E" not in fields["Valuation"]
        assert "DKK" in fields["Valuation"]
        assert fields["Valuation"].startswith("Unavailable")

    def test_a_withheld_valuation_is_not_narrated_in_the_summary(self) -> None:
        """The band is computed from the same mismatched price. Suppressing the
        field but keeping "valuation is normal" would leak the conclusion while
        hiding the number that would let a reader doubt it."""
        summary = self._card("DKK", "USD").get("Summary", "")

        assert "aluation" not in summary

    def test_an_unpriced_listing_is_not_narrated_either(self) -> None:
        summary = self._card("EUR", "EUR", priced=False).get("Summary", "")

        assert "aluation" not in summary
