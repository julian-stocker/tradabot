"""Which companies recurring ingestion is allowed to poll.

Not "every row in the registry". A symbol is a reason to *show* something, not
a reason to ask SEC about it every day, and three groups in the registry are
either unaskable or not companies at all.

Measured against the live registry
----------------------------------
1,015 listings, 1,009 distinct companies. Of those:

* **997 listings carry a CIK** and 18 do not -- eight sector ETFs, five German
  issuers that do not file with the SEC, and a handful of registry gaps. A
  company with no CIK has no SEC identity, so there is nothing to poll.
* **991 distinct CIKs**, because six issuers are cross-listed: SAP, Canadian
  National, Canadian Natural, Alphabet, Royal Bank of Canada and Toronto
  Dominion. ``SAP.DE`` and ``SAP.US`` are one registrant filing one set of
  documents, and polling both would double the request for an identical answer.
* **Two funds carry a CIK** -- the SPDR S&P 500 trust and the Invesco QQQ trust.
  They genuinely file with the SEC, and none of it is company reporting. The
  same distinction Phase 13 drew between ``sec_identity`` and
  ``has_fundamentals`` applies here: having an SEC identity is not being a
  company.

That leaves **989 eligible CIKs**.

The policy is deliberately not tuned
------------------------------------
Each exclusion is a property that makes polling impossible or meaningless, not
a filter chosen to make a number look better. Nothing here excludes a company
for being quiet, foreign, small or uninteresting -- a foreign private issuer
whose 6-K filings cannot be classified is still polled, because the coverage
boundary belongs in the presentation layer where a reader can see it, not in a
universe that silently stopped asking.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

FUND_TYPES: Final[frozenset[str]] = frozenset({"ETF", "FUND", "ETN"})


class Exclusion(StrEnum):
    """Why a listing is not polled. Every value is a property of the listing."""

    NO_SEC_IDENTITY = "NO_SEC_IDENTITY"
    """No CIK. There is no SEC endpoint to ask."""
    NOT_AN_OPERATING_COMPANY = "NOT_AN_OPERATING_COMPANY"
    """A fund or note. It files, and none of it is company reporting."""
    DUPLICATE_REGISTRANT = "DUPLICATE_REGISTRANT"
    """Another listing of an issuer already in the universe."""
    AMBIGUOUS_IDENTITY = "AMBIGUOUS_IDENTITY"
    """One CIK claimed by more than one company row, or one company claiming
    more than one CIK. Refused rather than resolved -- the same rule identity
    resolution applies everywhere else."""


@dataclass(frozen=True, slots=True)
class ResearchTarget:
    """One registrant worth polling, named once however many listings it has."""

    company_id: int
    cik: str
    company_name: str
    listings: tuple[str, ...]

    @property
    def cik_number(self) -> int:
        return int(self.cik)


@dataclass(frozen=True, slots=True)
class ResearchUniverse:
    """The eligible registrants, and an account of everything left out."""

    targets: tuple[ResearchTarget, ...]
    excluded: dict[str, Exclusion]
    """Qualified listing name to the reason it is not polled."""
    total_listings: int
    total_companies: int

    @property
    def size(self) -> int:
        return len(self.targets)

    @property
    def by_cik(self) -> dict[str, ResearchTarget]:
        return {t.cik: t for t in self.targets}

    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for reason in self.excluded.values():
            counts[str(reason)] = counts.get(str(reason), 0) + 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_listings": self.total_listings,
            "total_companies": self.total_companies,
            "eligible_registrants": self.size,
            "excluded": self.reasons(),
        }


def build(candidates: Sequence[Any]) -> ResearchUniverse:
    """The research universe from a registry snapshot. Pure -- no network.

    One pass, and the order of the checks matters: identity ambiguity is
    resolved before deduplication, so a CIK claimed by two companies is refused
    outright rather than silently collapsing them into whichever listing
    happened to sort first.
    """
    companies_by_cik: dict[str, set[int]] = {}
    ciks_by_company: dict[int, set[str]] = {}
    for candidate in candidates:
        if not candidate.cik:
            continue
        key = str(candidate.cik).zfill(10)
        companies_by_cik.setdefault(key, set()).add(candidate.company_id)
        ciks_by_company.setdefault(candidate.company_id, set()).add(key)

    targets: dict[str, ResearchTarget] = {}
    excluded: dict[str, Exclusion] = {}
    for candidate in sorted(candidates, key=lambda c: (c.symbol, c.mic)):
        name = candidate.qualified
        if not candidate.cik:
            excluded[name] = Exclusion.NO_SEC_IDENTITY
            continue
        if str(candidate.asset_type) in FUND_TYPES:
            # Having an SEC identity is not being a company: the SPDR S&P 500
            # trust files, and none of it is company reporting.
            excluded[name] = Exclusion.NOT_AN_OPERATING_COMPANY
            continue
        key = str(candidate.cik).zfill(10)
        if len(companies_by_cik[key]) > 1 or len(ciks_by_company[candidate.company_id]) > 1:
            excluded[name] = Exclusion.AMBIGUOUS_IDENTITY
            continue
        held = targets.get(key)
        if held is not None:
            excluded[name] = Exclusion.DUPLICATE_REGISTRANT
            targets[key] = ResearchTarget(
                company_id=held.company_id,
                cik=held.cik,
                company_name=held.company_name,
                listings=(*held.listings, name),
            )
            continue
        targets[key] = ResearchTarget(
            company_id=candidate.company_id,
            cik=key,
            company_name=candidate.company_name,
            listings=(name,),
        )
    return ResearchUniverse(
        targets=tuple(targets[k] for k in sorted(targets)),
        excluded=excluded,
        total_listings=len(candidates),
        total_companies=len({c.company_id for c in candidates}),
    )
