"""Reading filing metadata from documented SEC endpoints.

One endpoint, already in use
----------------------------
``data.sec.gov/submissions/CIK##########.json`` is on SEC's official
`EDGAR Application Programming Interfaces` page and is the same endpoint
:mod:`app.fundamentals.client` already calls for entity name, SIC and
acceptance times. This module adds no second HTTP client: it extends the
existing :class:`~app.fundamentals.client.EdgarClient`, which already enforces
the declared ``User-Agent`` and the 0.11 s inter-request gap that keeps
Tradabot under SEC's stated ten-per-second ceiling.

Not used, deliberately
----------------------
``efts.sec.gov`` -- EDGAR full-text search -- returns useful JSON and is absent
from SEC's official API documentation, which describes only submissions,
companyconcept, companyfacts and frames. Phase 14.0 classified it as an
undocumented backing endpoint for a web UI, and nothing here needs it: the
documented submissions payload carries item codes, acceptance timestamps and
report dates directly. No search-result page is fetched or parsed.

What the payload gives, measured
--------------------------------
Per filing: ``accessionNumber``, ``form``, ``filingDate``, ``reportDate``,
``acceptanceDateTime``, ``items``, ``primaryDocument``,
``primaryDocDescription``, ``size``. The three timestamps are genuinely
different things and this module keeps them apart -- see
:class:`FilingRecord`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}"
"""Documented archive path. ``accession`` is the dash-stripped form."""

AMENDMENT_SUFFIX = "/A"


@dataclass(frozen=True, slots=True)
class FilingRecord:
    """One filing as SEC's metadata describes it. No document has been read."""

    cik: str
    accession: str
    form: str
    filing_date: str
    """The calendar day SEC recorded the filing. Not a publication instant."""
    acceptance: str | None
    """``acceptanceDateTime`` -- the moment the filing became public, to the
    second. The strongest publication timestamp SEC offers, and the one
    ``published_at`` uses. Falls back to ``filing_date`` only when absent."""
    report_date: str | None
    """The 8-K cover page's date of earliest event reported. The event's own
    date where the form carries one, and ``None`` where it does not -- never
    substituted with the filing date, which would silently assert that filing
    and occurrence coincided."""
    items: tuple[str, ...]
    primary_document: str
    primary_description: str
    size: int

    @property
    def is_amendment(self) -> bool:
        return self.form.endswith(AMENDMENT_SUFFIX)

    @property
    def base_form(self) -> str:
        """``8-K/A`` -> ``8-K``. The form the amendment amends."""
        return self.form[: -len(AMENDMENT_SUFFIX)] if self.is_amendment else self.form

    @property
    def published_at(self) -> str:
        return self.acceptance or self.filing_date

    @property
    def bare_accession(self) -> str:
        return self.accession.replace("-", "")

    def archive_url(self, document: str | None = None) -> str:
        base = ARCHIVE_URL.format(cik=int(self.cik), accession=self.bare_accession)
        return f"{base}/{document}" if document else base

    def as_dict(self) -> dict[str, Any]:
        return {
            "cik": self.cik,
            "accession": self.accession,
            "form": self.form,
            "filing_date": self.filing_date,
            "acceptance": self.acceptance,
            "report_date": self.report_date,
            "items": list(self.items),
            "primary_document": self.primary_document,
        }


@dataclass(frozen=True, slots=True)
class SubmissionsPayload:
    """The parsed submissions document for one filer."""

    cik: str
    entity_name: str
    sic: str
    sic_description: str
    tickers: tuple[str, ...]
    exchanges: tuple[str, ...]
    filings: tuple[FilingRecord, ...] = field(default_factory=tuple)


def _split_items(raw: str | None) -> tuple[str, ...]:
    """``"2.02,9.01"`` -> ``("2.02", "9.01")``. Order preserved, blanks dropped."""
    if not raw:
        return ()
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())


def parse_submissions(payload: dict[str, Any]) -> SubmissionsPayload:
    """Turn one submissions document into records. Pure -- no network.

    Reads only the inline ``filings.recent`` block. The overflow files hold
    older history and are not fetched here: this phase ingests a recent window,
    and pulling every historical page for every company would multiply request
    volume for filings nothing yet consumes.
    """
    recent = (payload.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    cik = str(payload.get("cik") or "").zfill(10)

    records: list[FilingRecord] = []
    for index, accession in enumerate(accessions):

        def at(key: str, i: int = index) -> Any:
            values = recent.get(key) or []
            return values[i] if i < len(values) else None

        records.append(
            FilingRecord(
                cik=cik,
                accession=str(accession),
                form=str(at("form") or ""),
                filing_date=str(at("filingDate") or ""),
                acceptance=str(at("acceptanceDateTime")) if at("acceptanceDateTime") else None,
                report_date=str(at("reportDate")) if at("reportDate") else None,
                items=_split_items(at("items")),
                primary_document=str(at("primaryDocument") or ""),
                primary_description=str(at("primaryDocDescription") or ""),
                size=int(at("size") or 0),
            )
        )
    return SubmissionsPayload(
        cik=cik,
        entity_name=str(payload.get("name") or ""),
        sic=str(payload.get("sic") or ""),
        sic_description=str(payload.get("sicDescription") or ""),
        tickers=tuple(str(t) for t in (payload.get("tickers") or [])),
        exchanges=tuple(str(e) for e in (payload.get("exchanges") or [])),
        filings=tuple(records),
    )


def select(
    payload: SubmissionsPayload,
    *,
    forms: frozenset[str],
    since: str | None = None,
    limit: int | None = None,
) -> Iterator[FilingRecord]:
    """Filings worth ingesting, newest first.

    ``forms`` matches the base form, so ``8-K`` also selects ``8-K/A`` -- an
    amendment to a filing Tradabot ingests is itself worth ingesting.
    """
    seen = 0
    for record in payload.filings:
        if record.base_form not in forms:
            continue
        if since and record.filing_date < since:
            continue
        yield record
        seen += 1
        if limit is not None and seen >= limit:
            return


class SecFilingSource:
    """Filing metadata for one company, from the documented submissions API.

    Args:
        client: an :class:`~app.fundamentals.client.EdgarClient`. Injected so
            tests supply a fixture-backed stub and never touch the network.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def submissions(self, cik: int | str) -> SubmissionsPayload:
        return parse_submissions(self._client.submissions(int(cik)))
