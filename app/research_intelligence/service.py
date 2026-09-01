"""The two pipelines, and the only place either of them fetches anything.

Classification -- metadata only, no document read:

    SEC submissions -> FilingRecord -> company identity -> classification
    -> ResearchEvent -> store

Evidence -- documents read, and mostly refused:

    FilingRecord -> document manifest -> selected exhibits -> verified content
    -> cited facts -> store

They are separate services because they answer separate questions and fail
separately: an unreachable exhibit must not cost a filing its classification,
which the metadata already established. Both take their HTTP client by
injection rather than opening a socket of their own, so every other module in
this package is a pure function of its inputs and every test runs offline
against fixtures.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.research_intelligence import content, extraction, facts
from app.research_intelligence import documents as docs
from app.research_intelligence.identity import CompanyResolver
from app.research_intelligence.schemas import (
    EventKind,
    EvidenceStatus,
    QuarantinedFiling,
    ResearchDocument,
    ResearchEvent,
    ResearchFact,
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


@dataclass(frozen=True, slots=True)
class EvidenceReport:
    """What one filing's evidence pass did. Counts and reasons, never a verdict."""

    accession: str
    status: EvidenceStatus
    selection_rule: str = ""
    documents_listed: int = 0
    documents_selected: int = 0
    documents_fetched: int = 0
    documents_cached: int = 0
    bytes_fetched: int = 0
    facts_extracted: int = 0
    facts_new: int = 0
    refusals_by_status: dict[str, int] = field(default_factory=dict)
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "accession": self.accession,
            "status": str(self.status),
            "selection_rule": self.selection_rule,
            "documents_listed": self.documents_listed,
            "documents_selected": self.documents_selected,
            "documents_fetched": self.documents_fetched,
            "documents_cached": self.documents_cached,
            "bytes_fetched": self.bytes_fetched,
            "facts_extracted": self.facts_extracted,
            "facts_new": self.facts_new,
            "refusals_by_status": dict(self.refusals_by_status),
            "detail": self.detail,
        }


class EvidenceService:
    """Filing -> documents -> verified content -> cited facts.

    The one place in this package that fetches a document, and it fetches only
    URLs it built itself from SEC filing metadata: the manifest under the
    filing's own accession directory, and the filenames that manifest lists.
    The client refuses anything outside the EDGAR archive prefix, so a filename
    crafted inside a document cannot redirect a request.

    Args:
        client: an :class:`~app.fundamentals.client.EdgarClient`.
        store: an :class:`~app.research_intelligence.store.EventStore`.
        extraction_version: stamped on every document and fact written.
    """

    def __init__(
        self,
        *,
        client: Any,
        store: Any,
        extraction_version: str = docs.EXTRACTION_VERSION,
    ) -> None:
        self._client = client
        self._store = store
        self._version = extraction_version

    def collect(
        self,
        record: FilingRecord,
        *,
        events: Sequence[ResearchEvent],
        company_id: int,
        refresh: bool = False,
        now: datetime | None = None,
    ) -> EvidenceReport:
        """Attach evidence to one filing's events. **Never raises.**

        Args:
            refresh: refetch documents already retrieved. Off by default
                because EDGAR archive documents are immutable once accepted, so
                the stored hash is the answer and a second request would only
                spend SEC's rate budget confirming it.
        """
        fetched_at = (now or datetime.now(UTC)).isoformat()
        if not events:
            return EvidenceReport(
                record.accession,
                EvidenceStatus.NO_RELEVANT_EXHIBIT,
                detail="filing produced no event",
            )
        kind = events[0].event_kind
        if kind not in docs.EVIDENCE_KINDS:
            # Checked before the manifest is fetched, not after. Measured on the
            # live store, 86% of accessions are periodic reports or unclassified
            # foreign filings -- reading their manifests only to discard them
            # would have spent six requests in seven on nothing, against a rate
            # limit shared with every other SEC consumer in the system.
            return EvidenceReport(
                record.accession,
                EvidenceStatus.NO_RELEVANT_EXHIBIT,
                selection_rule="event kind carries no citable narrative document",
            )
        known = self._store.documents_for_accession(record.accession)
        if known and not refresh:
            # The manifest of an accepted filing does not change. Reading it
            # again on every pass would make an incremental run cost one request
            # per filing forever, for an answer already on disk.
            return self._from_documents(known, kind=kind, events=events, fetched_at=fetched_at)
        try:
            raw, _ = self._client.archive_document(record.manifest_url)
        except Exception as exc:
            logger.warning(
                "manifest unavailable", accession=record.accession, reason=type(exc).__name__
            )
            return EvidenceReport(
                record.accession,
                EvidenceStatus.CONTENT_FETCH_FAILED,
                detail=f"manifest unavailable ({type(exc).__name__})",
            )

        listed = docs.citable(
            docs.parse_manifest(raw.decode("utf-8", "ignore"), record=record, company_id=company_id)
        )
        self._store.upsert_documents(listed)
        return self._from_documents(
            listed, kind=kind, events=events, fetched_at=fetched_at, refresh=refresh
        )

    def _from_documents(
        self,
        listed: Sequence[ResearchDocument],
        *,
        kind: EventKind,
        events: Sequence[ResearchEvent],
        fetched_at: str,
        refresh: bool = False,
    ) -> EvidenceReport:
        """Retrieve and read the documents a filing lists."""
        accession = listed[0].accession if listed else ""
        selected, rule = docs.select_documents(list(listed), kind)
        if not selected:
            return EvidenceReport(
                accession,
                EvidenceStatus.NO_RELEVANT_EXHIBIT,
                selection_rule=rule,
                documents_listed=len(listed),
            )

        fetched = cached = total_bytes = 0
        collected: list[ResearchFact] = []
        refusals: dict[str, int] = {}
        status = EvidenceStatus.OK
        for document in selected:
            stored = self._store.document(document.document_id)
            if not refresh and stored is not None and stored.retrieved:
                cached += 1
                continue
            updated, text = self._retrieve(document, fetched_at=fetched_at, prior=stored)
            self._store.update_document(updated)
            if updated.status is not EvidenceStatus.OK:
                status = updated.status
                continue
            fetched += 1
            total_bytes += updated.raw_size or 0
            for event in events:
                outcome = facts.extract(
                    text,
                    document=updated,
                    event_id=event.event_id,
                    extraction_version=self._version,
                )
                collected.extend(outcome.facts)
                for refusal in outcome.refusals:
                    key = str(refusal.status)
                    refusals[key] = refusals.get(key, 0) + 1

        new = self._store.upsert_facts(collected)
        return EvidenceReport(
            accession=accession,
            status=status,
            selection_rule=rule,
            documents_listed=len(listed),
            documents_selected=len(selected),
            documents_fetched=fetched,
            documents_cached=cached,
            bytes_fetched=total_bytes,
            facts_extracted=len(collected),
            facts_new=new,
            refusals_by_status=refusals,
        )

    def _retrieve(
        self,
        document: ResearchDocument,
        *,
        fetched_at: str,
        prior: ResearchDocument | None,
    ) -> tuple[ResearchDocument, str]:
        """Fetch one document and verify it is what it was last time."""
        try:
            raw, content_type = self._client.archive_document(document.source_url)
        except Exception as exc:
            logger.warning(
                "document unavailable",
                document=document.filename,
                accession=document.accession,
                reason=type(exc).__name__,
            )
            return (
                replace(
                    document,
                    fetched_at=fetched_at,
                    status=EvidenceStatus.CONTENT_FETCH_FAILED,
                    extraction_version=self._version,
                ),
                "",
            )
        if not content.supported(content_type):
            return replace(
                document,
                fetched_at=fetched_at,
                content_type=content_type,
                raw_size=len(raw),
                status=EvidenceStatus.UNSUPPORTED_CONTENT_TYPE,
                extraction_version=self._version,
            ), ""

        digest = content.content_hash(raw)
        if prior is not None and prior.content_hash and prior.content_hash != digest:
            # An EDGAR archive document changed under a stable URL. The prior
            # hash is kept, because the facts already cited were extracted from
            # it; the new bytes are not read until someone has looked.
            logger.warning(
                "archive content changed",
                document=document.filename,
                accession=document.accession,
            )
            return replace(
                prior,
                fetched_at=fetched_at,
                status=EvidenceStatus.CHANGED_SOURCE_CONTENT,
            ), ""

        text = content.to_text(raw, content_type)
        return replace(
            document,
            fetched_at=fetched_at,
            content_type=content_type,
            content_hash=digest,
            raw_size=len(raw),
            text_length=len(text),
            status=EvidenceStatus.OK,
            extraction_version=self._version,
        ), text
