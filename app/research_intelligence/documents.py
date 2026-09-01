"""Finding the documents inside a filing, and choosing which ones are evidence.

Where the typed manifest actually lives
---------------------------------------
Three archive paths were measured, and only one is both authoritative and
cheap:

* ``index.json`` -- its ``type`` field is an **icon filename**
  (``text.gif``), not an exhibit type. Useless for selection.
* ``{accession}.txt`` -- the full submission, carrying correct SGML
  ``<TYPE>``/``<FILENAME>`` headers and, for one NVIDIA 8-K, **802 KB** of
  document bodies to reach them. SEC does not honour HTTP Range, so there is
  no way to read only the head.
* ``{accession}-index-headers.html`` -- the same SGML header block, **5.9 KB**,
  carrying every document's type, sequence, filename and description. This is
  the one used.

All three are ordinary Archives paths. ``efts.sec.gov`` is not touched, and no
search-result page is parsed.

Selection refuses rather than guesses
-------------------------------------
An Item 2.02 filing routinely carries more than one EX-99 exhibit: NVIDIA's
carries EX-99.1 (the press release) and EX-99.2 (CFO commentary). The
description fields say only ``EX-99.1`` and ``EX-99.2``, so **filing metadata
does not establish which one is the earnings release**.

Rather than assume the numbering, every EX-99 exhibit is treated as a candidate
evidence document and facts are attributed to whichever document states them.
Two documents agreeing corroborate; two documents disagreeing about the same
metric and period is a conflict the fact layer refuses. Nothing here decides
that ``.1`` means "the important one".
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Final

from app.research_intelligence.schemas import (
    DocumentRole,
    EventKind,
    ResearchDocument,
)
from app.research_intelligence.sec import FilingRecord

EXTRACTION_VERSION: Final = "15.1.0"
"""Stamped on every document and fact. A parser upgrade must be visible in
provenance rather than silently rewriting historical evidence."""

_DOCUMENT = re.compile(
    r"<DOCUMENT>\s*<TYPE>([^\n<]*)\s*<SEQUENCE>([^\n<]*)\s*<FILENAME>([^\n<]*)"
    r"(?:\s*<DESCRIPTION>([^\n<]*))?",
    re.IGNORECASE,
)

_XBRL_TYPES = ("EX-101", "EX-100", "XML", "JSON", "ZIP", "EXCEL", "GRAPHIC")

EVIDENCE_EXHIBIT = re.compile(r"^EX-99(\.\d+)?$", re.IGNORECASE)
"""Exhibit types that can carry narrative disclosure. EX-99 is SEC's slot for
additional exhibits -- press releases, presentations, commentary. XBRL
linkbases and graphics are excluded because they carry no prose to cite."""


def _role(document_type: str) -> DocumentRole:
    upper = document_type.upper()
    if EVIDENCE_EXHIBIT.match(upper):
        return DocumentRole.EXHIBIT
    if any(upper.startswith(x) for x in _XBRL_TYPES):
        return DocumentRole.XBRL if upper != "GRAPHIC" else DocumentRole.GRAPHIC
    if upper.startswith("EX-"):
        return DocumentRole.EXHIBIT
    return DocumentRole.PRIMARY


def document_id(accession: str, sequence: int, filename: str) -> str:
    """Stable identity for one document inside one filing.

    Accession plus sequence is unique within EDGAR and never time-varying, so
    re-reading the manifest regenerates the same id and the store absorbs the
    write.
    """
    basis = f"{accession}|{sequence}|{filename}"
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def parse_manifest(
    header_html: str,
    *,
    record: FilingRecord,
    company_id: int,
) -> list[ResearchDocument]:
    """Every document in a filing, from the SGML header block.

    ``header_html`` is untrusted source content and is treated as data: it is
    unescaped and pattern-matched, never evaluated, and the filenames it yields
    are used only to build ``.../Archives/...`` URLs under the filing's own
    accession directory.
    """
    text = html.unescape(header_html)
    documents: list[ResearchDocument] = []
    for match in _DOCUMENT.finditer(text):
        doc_type = match.group(1).strip()
        raw_sequence = match.group(2).strip()
        filename = match.group(3).strip()
        description = (match.group(4) or "").strip() or None
        if not filename or not doc_type:
            continue
        try:
            sequence = int(raw_sequence)
        except ValueError:
            continue
        documents.append(
            ResearchDocument(
                document_id=document_id(record.accession, sequence, filename),
                company_id=company_id,
                cik=record.cik,
                accession=record.accession,
                document_type=doc_type,
                role=_role(doc_type),
                filename=filename,
                sequence=sequence,
                description=description,
                source_url=record.archive_url(filename),
                published_at=record.published_at,
                extraction_version=EXTRACTION_VERSION,
            )
        )
    return documents


EVIDENCE_KINDS: Final[frozenset[EventKind]] = frozenset(
    {
        EventKind.EARNINGS_RELEASE,
        EventKind.MANAGEMENT_CHANGE,
        EventKind.MATERIAL_AGREEMENT,
        EventKind.DEBT_EVENT,
        EventKind.M_AND_A,
        EventKind.ACCOUNTING_RESTATEMENT,
        EventKind.IMPAIRMENT,
        EventKind.AUDITOR_CHANGE,
        EventKind.BANKRUPTCY_OR_RECEIVERSHIP,
        EventKind.CONTROL_CHANGE,
        EventKind.CYBERSECURITY_INCIDENT,
        EventKind.EXIT_OR_DISPOSAL_COSTS,
        EventKind.LISTING_RULE_MATTER,
        EventKind.UNREGISTERED_EQUITY_SALE,
    }
)
"""Event kinds worth attaching document evidence to. ``PERIODIC_REPORT`` and
``UNCLASSIFIED_SEC_FILING`` are excluded: a 10-Q's own text is already the
canonical fact store's source, and an unclassified filing has no established
claim for a document to support."""


def citable(documents: list[ResearchDocument]) -> list[ResearchDocument]:
    """The documents worth keeping a record of.

    XBRL linkbases and graphics are dropped rather than stored. They carry no
    prose to cite, so no evidence can ever point at one -- and a single 40-F was
    measured listing **333** documents, almost all of them XBRL. Persisting
    those would make the document table an order of magnitude larger than the
    evidence it exists to support.
    """
    return [d for d in documents if d.role in (DocumentRole.PRIMARY, DocumentRole.EXHIBIT)]


def select_documents(
    documents: list[ResearchDocument], kind: EventKind
) -> tuple[list[ResearchDocument], str]:
    """Candidate evidence documents for one event kind, and the rule applied.

    Returns EX-99 exhibits in filing sequence, falling back to the primary
    filing document when the filing carries none. Deliberately a *list*: the
    filing does not say which exhibit is the earnings release, so the answer is
    every exhibit that could be, not a guess at one.
    """
    if kind not in EVIDENCE_KINDS:
        return [], "event kind carries no citable narrative document"
    exhibits = sorted(
        (d for d in documents if EVIDENCE_EXHIBIT.match(d.document_type.upper())),
        key=lambda d: d.sequence,
    )
    if exhibits:
        return exhibits, f"{len(exhibits)} EX-99 exhibit(s), in filing sequence"
    primary = sorted(
        (d for d in documents if d.role is DocumentRole.PRIMARY),
        key=lambda d: d.sequence,
    )
    if primary:
        return primary[:1], "no EX-99 exhibit; primary filing document"
    return [], "filing lists no citable document"
