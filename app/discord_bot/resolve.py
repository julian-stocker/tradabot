"""Deciding which security a typed ticker refers to, and refusing to guess.

The failure this exists to prevent
----------------------------------
Someone types ``/check NVD``. There is no such instrument. The tempting
behaviour -- notice that NVDA is one character away and analyse that -- would
produce a complete, confident, well-formatted report about **a different
company than the one asked about**, with nothing on screen to say so. That is
worse than an error, because an error is obvious and a wrong answer is not.

The same applies across venues. ``SAP.DE`` is a Frankfurt listing; ``SAP`` is a
US ADR. They are related, they are not the same instrument, and they differ in
currency, hours, liquidity and tax treatment. Nothing here maps one to the
other, and a future equivalence layer would have to say so explicitly.

So: exact match, or refuse. A suggestion may be *offered*, never executed.

Support is a data question, not a nationality question
------------------------------------------------------
A symbol is supported to the extent that data exists for it. Foreign issuers
file with the SEC; some US tickers have no usable fundamentals. Encoding
"non-US means unsupported" would be wrong in both directions, so market data and
fundamentals are resolved independently and reported independently.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from enum import StrEnum
from typing import Any

_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
"""What a ticker can look like after normalisation. Deliberately permissive
about dots and dashes, because real symbols contain both (``BRK.B``, ``RDS-A``)
and rejecting them would be a US-shaped assumption."""

_SUGGESTION_CUTOFF = 0.8
"""How close a near-miss must be before it is even mentioned. High on purpose:
a weak suggestion invites the user to accept it without thinking, which
reintroduces the substitution this module exists to prevent."""


class Availability(StrEnum):
    """Whether one family of data exists for a security."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class Resolution(StrEnum):
    """The outcome of looking a symbol up. Deliberately not collapsed.

    Each of these calls for a different response, and merging them into one
    "error" would hide the difference between "we have never heard of this",
    "we know it but have no fundamentals" and "our data store is not built yet".
    """

    SUPPORTED = "SUPPORTED"
    """Prices and fundamentals both available."""
    PARTIAL_DATA = "PARTIAL_DATA"
    """Known instrument, some data family missing."""
    MARKET_DATA_ONLY = "MARKET_DATA_ONLY"
    """Prices available, no SEC company facts. Common for funds and ETFs."""
    FUNDAMENTALS_UNAVAILABLE = "FUNDAMENTALS_UNAVAILABLE"
    """Fundamentals absent for a symbol that should have them."""
    UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL"
    """No exact instrument matches."""
    DATA_NOT_SYNCED = "DATA_NOT_SYNCED"
    """The fact store has never been built, so nothing can be answered yet."""
    ANALYSIS_FAILED = "ANALYSIS_FAILED"
    """The lookup itself failed. Distinct from having no data."""
    MALFORMED_SYMBOL = "MALFORMED_SYMBOL"
    """The input is not shaped like a ticker at all."""


@dataclass(frozen=True, slots=True)
class Resolved:
    """What is known about a requested symbol, before any analysis runs."""

    requested: str
    symbol: str
    resolution: Resolution
    market_data: Availability = Availability.UNAVAILABLE
    fundamentals: Availability = Availability.UNAVAILABLE
    suggestion: str | None = None
    """A near-miss worth mentioning. **Never acted upon** -- the user must issue
    the corrected command themselves."""
    detail: str | None = None

    @property
    def analysable(self) -> bool:
        """Whether there is enough to say anything at all."""
        return self.market_data is Availability.AVAILABLE or (
            self.fundamentals is Availability.AVAILABLE
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "symbol": self.symbol,
            "resolution": str(self.resolution),
            "market_data": str(self.market_data),
            "fundamentals": str(self.fundamentals),
            "suggestion": self.suggestion,
            "detail": self.detail,
        }


def normalise(raw: str) -> str:
    """Trim and upper-case a typed ticker.

    Case and whitespace are typing artefacts, not information: ``" nvda "`` and
    ``"NVDA"`` are the same request. Nothing beyond that is altered -- no
    character substitution, no suffix stripping, no exchange guessing.
    """
    return raw.strip().upper()


def resolve(
    raw: str,
    *,
    universe: Sequence[str],
    fundamentals: Mapping[str, Any] | frozenset[str] | None = None,
    fact_store_ready: bool = True,
) -> Resolved:
    """Which instrument a typed ticker refers to, and what data exists for it.

    Args:
        raw: exactly what the user typed.
        universe: canonical instrument symbols with price history.
        fundamentals: symbols with SEC company facts. ``None`` when unknown.
        fact_store_ready: whether the fact store is usable at all.
    """
    symbol = normalise(raw)
    if not symbol or not _TICKER.match(symbol):
        return Resolved(
            requested=raw,
            symbol=symbol,
            resolution=Resolution.MALFORMED_SYMBOL,
            detail="That does not look like a ticker symbol.",
        )

    known = set(universe)
    if symbol not in known:
        # A suggestion is offered only when one candidate is clearly closest.
        # Two equally plausible matches means the right answer is "say which".
        close = get_close_matches(symbol, sorted(known), n=2, cutoff=_SUGGESTION_CUTOFF)
        suggestion = close[0] if len(close) == 1 else None
        return Resolved(
            requested=raw,
            symbol=symbol,
            resolution=Resolution.UNKNOWN_SYMBOL,
            suggestion=suggestion,
            detail=f'No supported instrument exactly matches "{symbol}".',
        )

    if not fact_store_ready:
        return Resolved(
            requested=raw,
            symbol=symbol,
            resolution=Resolution.DATA_NOT_SYNCED,
            market_data=Availability.AVAILABLE,
            detail="The fundamentals data store has not been built yet.",
        )

    has_facts = fundamentals is None or symbol in fundamentals
    if has_facts:
        return Resolved(
            requested=raw,
            symbol=symbol,
            resolution=Resolution.SUPPORTED,
            market_data=Availability.AVAILABLE,
            fundamentals=Availability.AVAILABLE,
        )
    return Resolved(
        requested=raw,
        symbol=symbol,
        resolution=Resolution.MARKET_DATA_ONLY,
        market_data=Availability.AVAILABLE,
        fundamentals=Availability.UNAVAILABLE,
        detail=(
            "No SEC company-facts record is available for this security. That is "
            "an absence of data, not a judgement about the company."
        ),
    )
