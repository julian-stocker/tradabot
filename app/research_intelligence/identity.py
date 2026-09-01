"""Attaching a filing to a company, or refusing to.

Phase 13 measured what guessing an identity costs: ``DTE`` resolved to DTE
Energy rather than Deutsche Telekom, ``CNR`` to Core Natural Resources rather
than Canadian National -- four of twelve, each producing a complete and
confident report about the wrong business. Ingestion is the same problem seen
from the other side, and it gets the same answer.

CIK is the only key
-------------------
An SEC filing names a registrant by CIK. That is a stable identifier the SEC
assigns and never reuses, and it is the *only* thing consulted here. Not the
ticker, which is what mis-resolves; not the entity name, which invites fuzzy
matching; not the exchange. A CIK that does not map to exactly one company
Tradabot knows produces a :class:`~app.research_intelligence.schemas.
QuarantinedFiling` with the reason, and no event.

Cross-listing follows for free
------------------------------
Because the key is the company's CIK and events carry ``company_id``, one
filing produces one event for the issuer regardless of how many venues list
it. ``SAP.DE`` and ``SAP.US`` resolve to the same ``company_id`` in the
registry, so an SAP 6-K is one company event both listings share -- not two
events that would later have to be deduplicated by guessing they were the same.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.advisor.facts import company_key


class CompanyResolver:
    """CIK to Tradabot company identity, built from the instrument registry.

    One source of identity for the whole system: the same registry snapshot
    ``/check`` resolves against and the peer layer groups by.
    """

    def __init__(self, candidates: Sequence[Any]) -> None:
        by_cik: dict[str, set[int]] = {}
        names: dict[int, str] = {}
        for candidate in candidates:
            if not candidate.cik:
                continue
            key = str(candidate.cik).zfill(10)
            by_cik.setdefault(key, set()).add(candidate.company_id)
            names[candidate.company_id] = candidate.company_name
        self._by_cik = by_cik
        self._names = names

    @property
    def size(self) -> int:
        return len(self._by_cik)

    def resolve(self, cik: str | int) -> tuple[int | None, str | None]:
        """``(company_id, None)`` on success, ``(None, reason)`` on refusal.

        Refuses rather than picks when a CIK maps to more than one company.
        That should be impossible -- a CIK is one registrant -- so it would
        mean the registry holds two company rows for one issuer, and silently
        choosing between them would attach every future event to whichever
        happened to sort first.
        """
        key = str(cik).zfill(10)
        found = self._by_cik.get(key)
        if not found:
            return None, f"CIK {key} is not a company Tradabot knows"
        if len(found) > 1:
            ids = ", ".join(str(i) for i in sorted(found))
            return None, f"CIK {key} maps to several companies ({ids}); not resolved"
        return next(iter(found)), None

    def name(self, company_id: int) -> str:
        return self._names.get(company_id, "")

    @staticmethod
    def key_for(cik: str | int) -> str:
        """The fact-store company key, so events join to fundamentals."""
        return company_key(int(cik))
