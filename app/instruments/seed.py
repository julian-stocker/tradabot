"""Populating the company and listing registry.

US listings come from the broker-tradable instruments already in the database,
one company each. Their CIK comes from the SEC's own ticker file, which is
correct for US listings because that is exactly what that file maps.

International listings are **hand-declared**, and a CIK is attached only when
the SEC's entity name has been confirmed to match the intended company. That
confirmation is the whole point: the SEC ticker file maps ``DTE`` to DTE Energy
and ``ABX`` to Abacus Global Management, so trusting it for a foreign ticker is
the defect Phase 13.1 exists to remove.

A company Tradabot cannot verify gets ``cik = None`` and no fundamentals, which
is an honest state. Guessing would reintroduce the problem in a new place.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.instruments.registry import COUNTRY_BY_MIC, CURRENCY_BY_MIC


@dataclass(frozen=True, slots=True)
class DeclaredListing:
    """One internationally declared listing and the company behind it."""

    symbol: str
    mic: str
    company: str
    country: str
    reporting_currency: str
    sec_ticker: str | None = None
    """The US ticker under which the SEC knows this *company*, when it is an SEC
    registrant. Never the foreign ticker: that is what mis-resolves."""
    expect_entity: str | None = None
    """A fragment of the SEC entity name that must match before a CIK is
    attached. The guard that stops Deutsche Telekom acquiring DTE Energy's
    filings."""
    isin: str | None = None


GERMANY: tuple[DeclaredListing, ...] = (
    DeclaredListing(
        "SAP",
        "XETR",
        "SAP SE",
        "DE",
        "EUR",
        sec_ticker="SAP",
        expect_entity="SAP",
        isin="DE0007164600",
    ),
    DeclaredListing("DTE", "XETR", "Deutsche Telekom AG", "DE", "EUR", isin="DE0005557508"),
    DeclaredListing("ALV", "XETR", "Allianz SE", "DE", "EUR", isin="DE0008404005"),
    DeclaredListing("SIE", "XETR", "Siemens AG", "DE", "EUR", isin="DE0007236101"),
    DeclaredListing("MBG", "XETR", "Mercedes-Benz Group AG", "DE", "EUR", isin="DE0007100000"),
    DeclaredListing("RHM", "XETR", "Rheinmetall AG", "DE", "EUR", isin="DE0007030009"),
)

CANADA: tuple[DeclaredListing, ...] = (
    DeclaredListing(
        "RY",
        "XTSE",
        "Royal Bank of Canada",
        "CA",
        "CAD",
        sec_ticker="RY",
        expect_entity="ROYAL BANK",
    ),
    DeclaredListing(
        "TD",
        "XTSE",
        "Toronto-Dominion Bank",
        "CA",
        "CAD",
        sec_ticker="TD",
        expect_entity="TORONTO DOMINION",
    ),
    DeclaredListing(
        "CNQ",
        "XTSE",
        "Canadian Natural Resources Limited",
        "CA",
        "CAD",
        sec_ticker="CNQ",
        expect_entity="CANADIAN NATURAL",
    ),
    DeclaredListing(
        "SHOP", "XTSE", "Shopify Inc.", "CA", "CAD", sec_ticker="SHOP", expect_entity="Shopify"
    ),
    DeclaredListing(
        "CNR",
        "XTSE",
        "Canadian National Railway Company",
        "CA",
        "CAD",
        sec_ticker="CNI",
        expect_entity="CANADIAN NATIONAL",
    ),
    DeclaredListing("ABX", "XTSE", "Barrick Mining Corporation", "CA", "CAD"),
)

DECLARED: tuple[DeclaredListing, ...] = GERMANY + CANADA


def currency_for(mic: str) -> str:
    return CURRENCY_BY_MIC.get(mic, "USD")


def country_for(mic: str) -> str:
    return COUNTRY_BY_MIC.get(mic, "US")
