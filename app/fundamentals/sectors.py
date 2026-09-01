"""Classifying a company by what it does, from the SEC's own SIC code.

The defect this exists to fix
----------------------------
The Advisor refuses generic balance-sheet, margin and cash-flow analysis for
financial companies, because net debt is a leverage reading for a manufacturer
and a description of the *business* for a bank. That refusal was driven by a
sector map covering the fifty-two watchlisted symbols and keyed by ticker.

``/check`` accepts any symbol. So JPMorgan, which is in the map, correctly
reported ``SECTOR_SPECIFIC_MODEL_REQUIRED``; Wells Fargo, which is not,
reported an **acceptable balance sheet at HIGH confidence** -- a confident
sentence about a real company that no one should act on. Royal Bank of Canada
and Toronto-Dominion produced the same reading once international fundamentals
arrived.

Why SIC
-------
It is the SEC's classification of the filer, it arrives with the submissions
document Tradabot already fetches, it costs nothing, and it covers every
company in the store -- foreign private issuers included. A commercial bank is
6021 whether it files us-gaap from Ohio or IFRS from Toronto.

SIC is coarse and occasionally dated, which is acceptable here: it is used to
decide *what not to claim*, and a coarse signal is a fine basis for refusing.
Nothing in this module decides what a company is worth.
"""

from __future__ import annotations

from typing import Final

FINANCIAL_RANGE: Final[tuple[int, int]] = (6000, 6499)
"""Depository institutions, brokers and insurers. The refusal class.

Stops short of real estate at 6500 on purpose. SIC Division H runs to 6799 and
sweeping all of it in would also silence generic analysis for every REIT --
defensible on the merits, and a change to validated US behaviour that this
phase was not asked to make. The boundary is drawn where the existing sector
map already drew it, so the only companies whose reports change are the banks
that were being read as manufacturers."""

_RANGES: Final[tuple[tuple[int, int, str], ...]] = (
    (100, 999, "agriculture"),
    (1000, 1499, "materials"),
    (1500, 1799, "industrials"),
    (2000, 2199, "consumer_staples"),
    (2200, 2399, "consumer_discretionary"),
    (2400, 2599, "industrials"),
    (2600, 2699, "materials"),
    (2700, 2799, "communication_services"),
    (2800, 2829, "materials"),
    (2830, 2836, "health_care"),
    (2840, 2899, "materials"),
    (2900, 2999, "energy"),
    (3000, 3299, "materials"),
    (3300, 3499, "industrials"),
    (3500, 3569, "industrials"),
    (3570, 3579, "information_technology"),
    (3580, 3599, "industrials"),
    (3600, 3639, "industrials"),
    (3640, 3699, "information_technology"),
    (3700, 3799, "consumer_discretionary"),
    (3800, 3829, "information_technology"),
    (3830, 3851, "health_care"),
    (3860, 3999, "consumer_discretionary"),
    (4000, 4799, "industrials"),
    (4800, 4899, "communication_services"),
    (4900, 4999, "utilities"),
    (5000, 5199, "industrials"),
    (5200, 5599, "consumer_discretionary"),
    (5600, 5699, "consumer_discretionary"),
    (5700, 5799, "consumer_discretionary"),
    (5800, 5899, "consumer_discretionary"),
    (5900, 5999, "consumer_discretionary"),
    (6000, 6199, "financials"),
    (6200, 6299, "financials"),
    (6300, 6499, "financials"),
    (6500, 6799, "real_estate"),
    (7000, 7299, "consumer_discretionary"),
    (7300, 7379, "information_technology"),
    (7380, 7399, "industrials"),
    (7400, 7999, "consumer_discretionary"),
    (8000, 8099, "health_care"),
    (8100, 8899, "industrials"),
    (9000, 9999, "unknown"),
)
"""SIC range -> sector, in the vocabulary the Advisor already speaks.

Deliberately not exhaustive to the four-digit level. This decides which
*family* of analysis applies, and a boundary case landing one sector away
changes nothing the Advisor claims -- except across the financial boundary,
which is why that boundary is drawn separately in :data:`FINANCIAL_RANGE`."""

UNKNOWN: Final = "unknown"
"""What an unclassifiable filer gets. Not a default sector -- an absence, and
the Advisor already knows how to say that it does not know."""


def sector_for(sic: str | int | None) -> str:
    """The sector a SIC code names, or ``"unknown"``.

    A malformed or missing code yields ``"unknown"`` rather than a guess: the
    only thing worse than no classification is a confident wrong one.
    """
    code = _code(sic)
    if code is None:
        return UNKNOWN
    for low, high, sector in _RANGES:
        if low <= code <= high:
            return sector
    return UNKNOWN


def is_financial(sic: str | int | None) -> bool:
    """Whether generic margin, leverage and cash-flow analysis must be refused.

    Answers ``False`` for an unknown code. Refusing everything Tradabot cannot
    classify would silently empty the report for ordinary companies whose SIC
    is missing; the sector map's own absence handling already covers that case.
    """
    code = _code(sic)
    if code is None:
        return False
    low, high = FINANCIAL_RANGE
    return low <= code <= high


def _code(sic: str | int | None) -> int | None:
    if sic is None:
        return None
    try:
        return int(str(sic).strip())
    except (TypeError, ValueError):
        return None


__all__ = ["FINANCIAL_RANGE", "UNKNOWN", "is_financial", "sector_for"]
