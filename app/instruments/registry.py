"""Resolving a typed symbol to exactly one company, or refusing.

The rule
--------
A bare ticker is a *query*, not an identity. If it names one listing, that is
the answer. If it names several, the answer is the list of candidates and a
request to choose -- never a pick.

Phase 13.0 measured what picking costs: ``DTE`` resolved to DTE Energy rather
than Deutsche Telekom, ``ALV`` to Autoliv rather than Allianz, ``ABX`` to
Abacus Global Management rather than Barrick, ``CNR`` to Core Natural Resources
rather than Canadian National Railway. Four of twelve, each producing a
complete and confident report about the wrong business.

Venue-qualified syntax
----------------------
``SAP.DE`` is user-facing shorthand, not identity. It is translated here to
``(XETR, SAP)`` through a declared suffix table and then resolved like anything
else. A suffix with no declared meaning is refused rather than guessed at, and
the resolver never invents a venue.

Local, always
-------------
Resolution is a database read. No network call happens on the path of a
``/check``, because a name lookup that can time out is a name lookup that will.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

MIC_BY_SUFFIX: dict[str, str] = {
    "DE": "XETR",
    "F": "XFRA",
    "TO": "XTSE",
    "V": "XTSV",
    "US": "XNAS",
}
"""User-facing suffixes and the venue each names. Declared, never inferred: a
suffix Tradabot has not been told about is an unknown venue, not a guess."""

COUNTRY_BY_MIC: dict[str, str] = {
    "XETR": "DE",
    "XFRA": "DE",
    "XTSE": "CA",
    "XTSV": "CA",
    "XNAS": "US",
    "XNYS": "US",
    "ARCX": "US",
}

CURRENCY_BY_MIC: dict[str, str] = {
    "XETR": "EUR",
    "XFRA": "EUR",
    "XTSE": "CAD",
    "XTSV": "CAD",
    "XNAS": "USD",
    "XNYS": "USD",
    "ARCX": "USD",
}

_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")

_FUND_TYPES: frozenset[str] = frozenset({"ETF", "FUND", "ETN"})
"""Pooled vehicles. They have holdings and a net asset value, not revenue and
margins, so "company fundamentals" is not a thing they can be missing."""


class Resolution(StrEnum):
    """What a lookup concluded. Each calls for a different response."""

    SUPPORTED = "SUPPORTED"
    """Exactly one listing, with both prices and fundamentals available."""
    MARKET_DATA_ONLY = "MARKET_DATA_ONLY"
    """One listing, priced, no company facts."""
    FUNDAMENTALS_ONLY = "FUNDAMENTALS_ONLY"
    """One listing with filings but no prices -- the ordinary state of a foreign
    listing before any international market-data provider exists."""
    AMBIGUOUS_SYMBOL = "AMBIGUOUS_SYMBOL"
    """Several listings answer to this ticker. **Never resolved by choosing.**"""
    UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL"
    MALFORMED_SYMBOL = "MALFORMED_SYMBOL"
    UNSUPPORTED_LISTING = "UNSUPPORTED_LISTING"
    """A known listing with neither prices nor fundamentals."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One listing that answers to a requested symbol."""

    symbol: str
    mic: str
    country: str
    quote_currency: str
    company_name: str
    company_id: int
    cik: str | None
    reporting_currency: str | None
    taxonomy: str | None
    has_prices: bool
    has_fundamentals: bool
    """Whether **company** fundamentals exist for this listing.

    Not the same question as having an SEC identity, which is what a CIK is.
    The SPDR S&P 500 trust files with the SEC and has one; it has no revenue,
    no margin and no balance sheet, so a card built from ``has_fundamentals``
    read off the CIK announced ``fundamentals: AVAILABLE`` and then printed
    "Insufficient data" in every fundamental section. A fund is not a company
    with missing data."""
    isin: str | None = None
    asset_type: str = "STOCK"
    sec_identity: bool = False
    """Whether the SEC knows this listing's issuer at all -- a CIK exists. Kept
    separate from :attr:`has_fundamentals` because they diverge for funds, and
    collapsing them is what produced the SPY card above."""

    def __post_init__(self) -> None:
        """A fund can never have company fundamentals, however it was built.

        Enforced on the type rather than in the loader that happened to find
        the defect: a seed, a test or a future importer constructing a
        ``Candidate`` by hand would otherwise reintroduce the claim, and the
        one place it is checked would not be the one place it is made.
        """
        if self.asset_type in _FUND_TYPES and self.has_fundamentals:
            object.__setattr__(self, "has_fundamentals", False)

    @property
    def qualified(self) -> str:
        """How a user would name this listing unambiguously."""
        suffix = next((s for s, m in MIC_BY_SUFFIX.items() if m == self.mic), None)
        return f"{self.symbol}.{suffix}" if suffix else f"{self.symbol}@{self.mic}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "qualified": self.qualified,
            "mic": self.mic,
            "country": self.country,
            "quote_currency": self.quote_currency,
            "company": self.company_name,
            "cik": self.cik,
            "reporting_currency": self.reporting_currency,
            "taxonomy": self.taxonomy,
            "has_prices": self.has_prices,
            "has_fundamentals": self.has_fundamentals,
            "asset_type": self.asset_type,
            "sec_identity": self.sec_identity,
        }


@dataclass(frozen=True, slots=True)
class Resolved:
    """The outcome of one lookup."""

    requested: str
    resolution: Resolution
    listing: Candidate | None = None
    candidates: tuple[Candidate, ...] = ()
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.listing is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "resolution": str(self.resolution),
            "listing": self.listing.as_dict() if self.listing else None,
            "candidates": [c.as_dict() for c in self.candidates],
            "detail": self.detail,
        }


def symbol_with_suffix(raw: str) -> tuple[str, str, str]:
    """``"RY.CA"`` -> ``("RY.CA", "RY", "CA")``. The parse, spelled out.

    Used to tell a user exactly what was read, so a rejection names the half
    that was wrong instead of leaving them to guess.
    """
    normalised = raw.strip().upper()
    head, _, tail = normalised.rpartition(".")
    return normalised, head or normalised, tail


def split_suffix(raw: str) -> tuple[str, str | None, bool]:
    """Split user-facing ``SAP.DE`` into ``("SAP", "XETR", True)``.

    Returns the bare symbol, the MIC a suffix named (or ``None``), and whether a
    suffix was present at all. A present-but-unknown suffix yields ``None`` with
    ``True``, so the caller can refuse rather than treat ``SAP.XX`` as the
    symbol ``SAP.XX``.
    """
    symbol = raw.strip().upper()
    if "." not in symbol:
        return symbol, None, False
    head, _, tail = symbol.rpartition(".")
    if not head:
        return symbol, None, False
    return head, MIC_BY_SUFFIX.get(tail), True


class InstrumentRegistry:
    """Company and listing lookup over a preloaded snapshot.

    Args:
        candidates: every known listing. Loaded once per process; a name lookup
            must not depend on a query that can fail mid-answer.
    """

    def __init__(self, candidates: Sequence[Candidate]) -> None:
        self._by_symbol: dict[str, list[Candidate]] = {}
        self._by_key: dict[tuple[str, str], Candidate] = {}
        for candidate in candidates:
            self._by_symbol.setdefault(candidate.symbol, []).append(candidate)
            self._by_key[(candidate.mic, candidate.symbol)] = candidate

    @property
    def listings(self) -> int:
        return len(self._by_key)

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(self._by_symbol)

    def collisions(self) -> dict[str, list[Candidate]]:
        """Bare tickers that name more than one listing."""
        return {s: c for s, c in self._by_symbol.items() if len(c) > 1}

    def resolve(self, raw: str) -> Resolved:
        """One listing, or an explicit refusal. **Never a choice between companies.**"""
        # A dot is only a venue separator when it is not part of the ticker.
        # Berkshire Hathaway's class B share *is* the symbol "BRK.B", and
        # splitting it yielded base "BRK" with an unknown venue "B", so a symbol
        # sitting in the registry could not be reached at all. An exact match on
        # the whole input outranks any speculative split.
        literal = raw.strip().upper()
        if literal in self._by_symbol:
            return self._decide(raw, self._by_symbol[literal], literal)

        symbol, mic, had_suffix = split_suffix(raw)
        malformed = self._reject(raw, symbol, mic, had_suffix)
        if malformed is not None:
            return malformed

        if mic is not None:
            found = self._by_key.get((mic, symbol))
            if found is None:
                return Resolved(
                    raw,
                    Resolution.UNKNOWN_SYMBOL,
                    detail=f"No listing {symbol} on {mic}.",
                )
            return self._single(raw, found)

        matches = self._by_symbol.get(symbol, [])
        if not matches:
            return Resolved(
                raw,
                Resolution.UNKNOWN_SYMBOL,
                detail=f'No supported listing matches "{symbol}".',
            )
        return self._decide(raw, matches, symbol)

    def _decide(self, raw: str, matches: Sequence[Candidate], symbol: str) -> Resolved:
        """One match resolves; several refuse."""
        if len(matches) > 1:
            return self._ambiguous(raw, matches, symbol)
        return self._single(raw, matches[0])

    @staticmethod
    def _ambiguous(raw: str, matches: Sequence[Candidate], symbol: str) -> Resolved:
        """Refuse, and say what the choices are.

        Picking one would be indistinguishable from being right. Two shapes of
        ambiguity, and they are not the same mistake: different companies is a
        wrong-company risk, one company on two venues is a currency and venue
        difference. Both need the user to say which; only one would be a
        catastrophe if guessed.
        """
        companies = {c.company_id for c in matches}
        detail = (
            f'"{symbol}" names {len(matches)} listings of the same company on '
            f"different venues, in different currencies. Name the venue."
            if len(companies) == 1
            else f'"{symbol}" names listings of {len(companies)} different '
            f"companies. Name the venue."
        )
        return Resolved(
            raw,
            Resolution.AMBIGUOUS_SYMBOL,
            candidates=tuple(sorted(matches, key=lambda c: (c.country, c.mic))),
            detail=detail,
        )

    @staticmethod
    def _reject(raw: str, symbol: str, mic: str | None, had_suffix: bool) -> Resolved | None:
        """Input that cannot name anything, before any lookup happens."""
        if not symbol or not _SYMBOL.match(symbol):
            return Resolved(
                raw,
                Resolution.MALFORMED_SYMBOL,
                detail="That does not look like a ticker symbol.",
            )
        if had_suffix and mic is None:
            # Say what was actually parsed. "Unknown venue suffix" alone leaves
            # the reader guessing which half of their input Tradabot objected to.
            known = ", ".join(sorted(MIC_BY_SUFFIX))
            _, _, tail = symbol_with_suffix(raw)
            return Resolved(
                raw,
                Resolution.UNKNOWN_SYMBOL,
                detail=(
                    f'Read "{raw.strip().upper()}" as symbol "{symbol}" on venue '
                    f'"{tail}", and "{tail}" is not a venue Tradabot knows. '
                    f"Recognised suffixes: {known}."
                ),
            )
        return None

    @staticmethod
    def _single(raw: str, found: Candidate) -> Resolved:
        if found.has_prices and found.has_fundamentals:
            state = Resolution.SUPPORTED
        elif found.has_prices:
            state = Resolution.MARKET_DATA_ONLY
        elif found.has_fundamentals:
            state = Resolution.FUNDAMENTALS_ONLY
        else:
            state = Resolution.UNSUPPORTED_LISTING
        return Resolved(raw, state, listing=found, candidates=(found,))


def valuation_allowed(candidate: Candidate) -> tuple[bool, str | None]:
    """Whether a valuation ratio may be computed for this listing.

    Only when the price and the reported figures are in the same currency. There
    is no FX in Tradabot, and a price-to-sales built from a EUR numerator and a
    USD denominator would be wrong by whatever the rate happened to be -- while
    looking entirely normal.
    """
    if not candidate.has_prices:
        return False, "no market data for this listing"
    if candidate.reporting_currency is None:
        return False, "the company's reporting currency is unknown"
    if candidate.reporting_currency != candidate.quote_currency:
        return False, (
            f"this listing trades in {candidate.quote_currency} while the company "
            f"reports in {candidate.reporting_currency}; Tradabot performs no "
            f"currency conversion, so a valuation ratio would mix the two"
        )
    return True, None


def benchmark_for(candidate: Candidate) -> str | None:
    """The validated benchmark for a listing's country, or ``None``.

    Only the US has one. SPY was validated against US listings and means nothing
    for a Xetra line: the comparison would render as a normal-looking percentage
    and quietly measure a currency and a time zone as much as a company. Choosing
    DAX or TSX here would be an uncalibrated guess, so relative strength is simply
    unavailable outside the US until a benchmark is validated per market.
    """
    return "SPY" if candidate.country == "US" else None


def load(database: str) -> InstrumentRegistry:
    """Build the registry from the local database. One query, no network."""
    import sqlite3  # noqa: PLC0415

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT l.symbol, l.mic, l.country, l.quote_currency, l.isin, "
            "       c.name, c.id, c.cik, c.reporting_currency, c.taxonomy, "
            "       l.instrument_id IS NOT NULL AS has_prices, "
            "       c.cik IS NOT NULL AS sec_identity, l.asset_type "
            "FROM listings l JOIN companies c ON c.id = l.company_id"
        ).fetchall()
    finally:
        connection.close()
    return InstrumentRegistry(
        [
            Candidate(
                symbol=r[0],
                mic=r[1],
                country=r[2],
                quote_currency=r[3],
                isin=r[4],
                company_name=r[5],
                company_id=r[6],
                cik=r[7],
                reporting_currency=r[8],
                taxonomy=r[9],
                has_prices=bool(r[10]),
                # A CIK proves an SEC identity, not a set of company
                # fundamentals; `Candidate` narrows this for funds.
                has_fundamentals=bool(r[11]),
                asset_type=str(r[12]),
                sec_identity=bool(r[11]),
            )
            for r in rows
        ]
    )
