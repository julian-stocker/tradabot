"""Who is comparable to whom, decided from data Tradabot already holds.

Peer definition is the part of a cross-sectional layer that is easiest to get
quietly wrong. A group that is too broad compares a railway to a software
company and calls the difference a percentile; a group assembled by hand is a
judgement nobody can audit. So the definition here is **the SEC's own industry
classification and nothing else** -- the same SIC code that already decides
whether the Advisor will read a balance sheet at all.

Two levels, both real
---------------------
The four-digit SIC industry is the most specific grouping available. It is often
too small: measured against the current registry, only 47% of classified
companies sit in a four-digit group of eight or more, and Apple's ``3571``
(Electronic Computers) holds three companies in total.

So there is exactly one fallback, to the three-digit **industry group** that the
four-digit code belongs to -- ``3571`` to ``357``, Computer & Office Equipment.
That is a level of the SEC's published hierarchy rather than a bucket invented
here, and it lifts coverage to 60%. The chain stops there. Two-digit major
groups would cover 87%, and would also put a semiconductor maker and a
household-appliance maker in one distribution, which is a wider group rather
than a comparable one.

When neither level clears the minimum the answer is that there is no comparison,
not a wider net.

A stated look-ahead assumption
------------------------------
SIC comes from the issuer's current EDGAR submission, so peer *membership* is
today's classification even when the metrics are point-in-time. A company that
changed its SIC would be grouped by where it sits now. This is the one place
this layer is not strictly point-in-time; it is recorded rather than hidden, and
the alternative -- reconstructing historical classifications -- needs filing
history Tradabot does not store.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.fundamentals.sectors import is_financial
from app.instruments.registry import Candidate

_SIC_LENGTH = 4


@dataclass(frozen=True, slots=True)
class PeerCompany:
    """One issuer in the comparison universe, with the listing to price it by."""

    company_id: int
    name: str
    sic: str
    listing: Candidate
    """The listing whose prices and currency the issuer's valuation uses.

    One issuer may have several. The choice is deterministic and prefers a
    listing whose quote currency matches the reporting currency, because that
    is the one whose multiples survive :func:`valuation_allowed`. Picking
    arbitrarily would let a peer group's composition depend on row order."""

    @property
    def cik(self) -> str | None:
        return self.listing.cik

    @property
    def symbol(self) -> str:
        return self.listing.symbol


def _rank(candidate: Candidate) -> tuple[int, int, str, str]:
    """Ordering for choosing one listing per issuer. Lower sorts first.

    Priced beats unpriced, and a listing that can carry a valuation beats one
    that cannot, so an issuer contributes multiples wherever it possibly can.
    Ties break on MIC then symbol so the result never depends on input order.
    """
    from app.instruments.registry import valuation_allowed  # noqa: PLC0415

    allowed, _ = valuation_allowed(candidate)
    return (
        0 if candidate.has_prices else 1,
        0 if allowed else 1,
        candidate.mic,
        candidate.symbol,
    )


class PeerUniverse:
    """Every issuer that can take part in a comparison, indexed by industry.

    Built once from the instrument registry -- the same snapshot ``/check``
    resolves against, so a company cannot be a peer under one identity model and
    something else under another.
    """

    def __init__(self, candidates: Sequence[Candidate]) -> None:
        by_company: dict[int, list[Candidate]] = {}
        for candidate in candidates:
            if candidate.sic and candidate.sec_identity:
                by_company.setdefault(candidate.company_id, []).append(candidate)

        self._companies: dict[int, PeerCompany] = {}
        for company_id, listings in by_company.items():
            chosen = min(listings, key=_rank)
            sic = str(chosen.sic)
            if len(sic) != _SIC_LENGTH:
                continue
            self._companies[company_id] = PeerCompany(
                company_id=company_id,
                name=chosen.company_name,
                sic=sic,
                listing=chosen,
            )

        self._by_sic4: dict[str, list[int]] = {}
        self._by_sic3: dict[str, list[int]] = {}
        for company in self._companies.values():
            self._by_sic4.setdefault(company.sic, []).append(company.company_id)
            self._by_sic3.setdefault(company.sic[:3], []).append(company.company_id)
        # Sorted so a group's membership -- and therefore every percentile
        # computed from it -- is identical on every run.
        for index in (self._by_sic4, self._by_sic3):
            for members in index.values():
                members.sort()

    @property
    def size(self) -> int:
        return len(self._companies)

    def company(self, company_id: int) -> PeerCompany | None:
        return self._companies.get(company_id)

    def by_sic4(self, sic: str) -> tuple[PeerCompany, ...]:
        return tuple(self._companies[i] for i in self._by_sic4.get(sic, ()))

    def by_sic3(self, sic: str) -> tuple[PeerCompany, ...]:
        return tuple(self._companies[i] for i in self._by_sic3.get(sic[:3], ()))

    def eligible(self, company: PeerCompany) -> str | None:
        """Why this company cannot be compared at all, or ``None``.

        Financial-sector issuers are excluded from every group, not only from
        their own: the metrics compared here describe an operating company, and
        a bank sitting inside an industrial distribution would distort it for
        everyone else.
        """
        if not company.listing.has_fundamentals:
            return "no company fundamentals"
        if is_financial(company.sic):
            return "financial-sector issuer"
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "companies": self.size,
            "sic4_groups": len(self._by_sic4),
            "sic3_groups": len(self._by_sic3),
        }


def load(registry: Any) -> PeerUniverse:
    """Build the universe from a loaded :class:`InstrumentRegistry`."""
    return PeerUniverse(registry.all_candidates())
