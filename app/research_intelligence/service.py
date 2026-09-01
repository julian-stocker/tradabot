"""Ingestion: submissions payload in, stored events or quarantine out.

The whole pipeline is five steps and none of them reads a document:

    SEC submissions -> FilingRecord -> company identity -> classification
    -> ResearchEvent -> store

This is the only module that triggers a fetch, and it does so through an
injected client rather than a socket of its own -- so every other module is a
pure function of its inputs, and every test in this package runs offline
against fixtures.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.research_intelligence import extraction
from app.research_intelligence.identity import CompanyResolver
from app.research_intelligence.schemas import (
    EventKind,
    QuarantinedFiling,
    ResearchEvent,
)
from app.research_intelligence.sec import FilingRecord, SecFilingSource

logger = get_logger(__name__)

DEFAULT_FORMS: frozenset[str] = frozenset({"8-K", "6-K", "20-F", "40-F", "10-K", "10-Q"})
"""Forms worth ingesting. Only ``8-K`` carries item codes; the rest are
recorded so the coverage boundary is visible rather than absent."""

CLASSIFIABLE_FORMS: frozenset[str] = frozenset({"8-K"})


@dataclass(frozen=True, slots=True)
class IngestionReport:
    """What one ingestion run did. Counts, never a judgement."""

    company_id: int | None
    cik: str
    entity_name: str
    filings_examined: int = 0
    events_built: int = 0
    events_new: int = 0
    events_duplicate: int = 0
    classified: int = 0
    unclassified: int = 0
    administrative_only: int = 0
    amendments: int = 0
    quarantined: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "cik": self.cik,
            "entity_name": self.entity_name,
            "filings_examined": self.filings_examined,
            "events_built": self.events_built,
            "events_new": self.events_new,
            "events_duplicate": self.events_duplicate,
            "classified": self.classified,
            "unclassified": self.unclassified,
            "administrative_only": self.administrative_only,
            "amendments": self.amendments,
            "quarantined": self.quarantined,
            "by_kind": dict(self.by_kind),
            "detail": self.detail,
        }


class ResearchIngestionService:
    """Builds and stores research events for one company at a time.

    Args:
        source: documented SEC submissions access.
        resolver: CIK to Tradabot company identity.
        store: where events land.
    """

    def __init__(self, *, source: SecFilingSource, resolver: CompanyResolver, store: Any) -> None:
        self._source = source
        self._resolver = resolver
        self._store = store

    def ingest_company(
        self,
        cik: str | int,
        *,
        forms: frozenset[str] = DEFAULT_FORMS,
        since: str | None = None,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> IngestionReport:
        """Ingest one filer's recent filings. **Never raises.**"""
        fetched_at = (now or datetime.now(UTC)).isoformat()
        try:
            payload = self._source.submissions(cik)
        except Exception as exc:
            logger.warning("submissions unavailable", cik=str(cik), reason=type(exc).__name__)
            return IngestionReport(
                company_id=None,
                cik=str(cik).zfill(10),
                entity_name="",
                detail=f"submissions unavailable ({type(exc).__name__})",
            )

        company_id, refusal = self._resolver.resolve(payload.cik)
        records = [
            r
            for r in payload.filings
            if r.base_form in forms and (since is None or r.filing_date >= since)
        ]
        if limit is not None:
            records = records[:limit]

        if company_id is None:
            self._store.quarantine(
                [
                    QuarantinedFiling(
                        cik=payload.cik,
                        accession=r.accession,
                        form=r.form,
                        reason=refusal or "unresolved identity",
                        fetched_at=fetched_at,
                    )
                    for r in records
                ]
            )
            logger.info(
                "filings quarantined",
                cik=payload.cik,
                count=len(records),
                reason=refusal,
            )
            return IngestionReport(
                company_id=None,
                cik=payload.cik,
                entity_name=payload.entity_name,
                filings_examined=len(records),
                quarantined=len(records),
                detail=refusal,
            )

        company_key = CompanyResolver.key_for(payload.cik)
        events: list[ResearchEvent] = []
        counts: dict[str, int] = {}
        classified = unclassified = admin_only = amendments = 0
        for record in records:
            built = extraction.build(
                record,
                company_id=company_id,
                company_key=company_key,
                fetched_at=fetched_at,
            )
            if not built:
                admin_only += 1
                continue
            if record.is_amendment:
                amendments += 1
            for event in built:
                counts[str(event.event_kind)] = counts.get(str(event.event_kind), 0) + 1
                if event.event_kind is EventKind.UNCLASSIFIED_SEC_FILING:
                    unclassified += 1
                else:
                    classified += 1
            events.extend(built)

        new = self._store.upsert(events)
        self._link_amendments(events)
        return IngestionReport(
            company_id=company_id,
            cik=payload.cik,
            entity_name=payload.entity_name,
            filings_examined=len(records),
            events_built=len(events),
            events_new=new,
            events_duplicate=len(events) - new,
            classified=classified,
            unclassified=unclassified,
            administrative_only=admin_only,
            amendments=amendments,
            by_kind=counts,
        )

    def _link_amendments(self, events: Sequence[ResearchEvent]) -> None:
        """Relate an amendment to what it amends, only where SEC establishes it.

        SEC's submissions metadata marks a filing as an amendment through its
        form suffix and nothing else -- there is no field naming the amended
        accession. So the link is drawn only when the store already holds an
        event of the same kind, for the same company, published earlier: that
        much is a defensible inference from the registrant's own filing
        behaviour. Where it is not, both events stay and the relationship is
        simply unrecorded, which is honest and reconstructable later.
        """
        for event in events:
            if not event.amends_accession:
                continue
            prior = [
                candidate
                for candidate in self._store.events_for_company(
                    event.company_id, as_of=event.published_at
                )
                if candidate.event_kind is event.event_kind
                and candidate.accession != event.accession
                and candidate.published_at < event.published_at
            ]
            if len(prior) != 1:
                # Zero or several plausible originals: refuse to pick, exactly
                # as identity resolution refuses to pick a company.
                continue
            self._store.mark_superseded(
                prior[0].event_id, by=event.event_id, when=event.published_at
            )


def filings_of_interest(
    records: Sequence[FilingRecord], forms: frozenset[str]
) -> list[FilingRecord]:
    return [r for r in records if r.base_form in forms]
