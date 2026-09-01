"""What may be shown to a synthesis layer, and what each item actually is.

A model given a flat bag of numbers will treat them all alike. The distinction
this file exists to preserve is between a figure a company filed, a figure
Tradabot derived from filings, a category the registrant selected, and a reason
something is missing -- because a synthesis that cannot tell those apart will
eventually assert the fourth as if it were the first.

Interpretation is not an evidence class
---------------------------------------
There is deliberately no ``INTERPRETATION`` member below. Interpretation is
what a synthesis *produces*, and it lives in
:mod:`app.synthesis.contract` where it carries evidence references and a claim
type. Nothing a model emits can be written back here: the packet is built from
owning services and is immutable, so there is no path by which an opinion
becomes an evidence item.

Absence is evidence
-------------------
An omission carries a reason, never a bare null. "Operating margin is missing"
and "operating margin is not a comparable quantity for a bank" lead a reader --
human or model -- to opposite conclusions, and only one of them is true.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

PACKET_VERSION = "18.0.0"
"""Bumped when the packet's shape or selection rules change. Part of the hash,
so a synthesis produced against an older contract can never be mistaken for one
produced against this."""


class EvidenceClass(StrEnum):
    """What kind of thing an evidence item is. Never collapsed."""

    PRIMARY_SOURCE_FACT = "PRIMARY_SOURCE_FACT"
    """A figure a filing stated outright, with the sentence that states it."""
    CANONICAL_FINANCIAL_FACT = "CANONICAL_FINANCIAL_FACT"
    """An as-reported XBRL fact from the point-in-time fact store."""
    DERIVED_METRIC = "DERIVED_METRIC"
    """Arithmetic over canonical facts -- a margin, a trailing-twelve-month sum.
    Deterministic and reproducible, but not something anyone filed."""
    HISTORICAL_TRAJECTORY = "HISTORICAL_TRAJECTORY"
    """Movement of a derived metric over a declared window. Describes the past
    and carries no claim about what follows."""
    PEER_CONTEXT = "PEER_CONTEXT"
    """A cross-sectional position within a declared industry group."""
    MARKET_CONTEXT = "MARKET_CONTEXT"
    """Price-derived, and therefore about one listing rather than the company."""
    CURRENT_DEVELOPMENT = "CURRENT_DEVELOPMENT"
    """A classified SEC filing. The category is the registrant's own choice of
    item code, not a judgement about the disclosure."""
    SOURCE_LIMITATION = "SOURCE_LIMITATION"
    """Something the source regime cannot establish -- a foreign filer's 6-K
    carrying no item codes. Distinct from a company having nothing to report."""
    REFUSAL = "REFUSAL"
    """A deliberate decision not to compute something, with its reason."""


class OmissionReason(StrEnum):
    """Why something a reader might expect is not here."""

    NOT_AVAILABLE = "NOT_AVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    REFUSED = "REFUSED"
    STALE = "STALE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    SOURCE_LIMITATION = "SOURCE_LIMITATION"
    NO_CURRENT_EVENTS = "NO_CURRENT_EVENTS"
    NO_COVERAGE = "NO_COVERAGE"
    SECTOR_MODEL_REQUIRED = "SECTOR_MODEL_REQUIRED"
    CURRENCY_BOUNDARY = "CURRENCY_BOUNDARY"
    NO_MARKET_DATA = "NO_MARKET_DATA"


class ConflictType(StrEnum):
    """How two evidence items can disagree."""

    VALUE_MISMATCH = "VALUE_MISMATCH"
    """Two sources give different numbers for what should be one quantity."""
    PERIOD_MISMATCH = "PERIOD_MISMATCH"
    BASIS_MISMATCH = "BASIS_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    CONTEXT_MISSING = "CONTEXT_MISSING"
    """A raw metric exists while the context that makes it comparable does not."""


class ConflictStatus(StrEnum):
    """Whether a disagreement has a deterministic explanation.

    ``UNRESOLVED`` is a first-class outcome. A synthesis may describe an
    unresolved conflict and may not decide it: choosing the more convenient
    number is exactly the failure the whole evidence chain exists to prevent.
    """

    UNRESOLVED = "UNRESOLVED"
    EXPLAINED_BY_BASIS = "EXPLAINED_BY_BASIS"
    EXPLAINED_BY_PERIOD = "EXPLAINED_BY_PERIOD"
    EXPLAINED_BY_CURRENCY = "EXPLAINED_BY_CURRENCY"
    EXPLAINED_BY_SOURCE_PRIORITY = "EXPLAINED_BY_SOURCE_PRIORITY"


SOURCE_PRIORITY: dict[EvidenceClass, int] = {
    EvidenceClass.PRIMARY_SOURCE_FACT: 5,
    EvidenceClass.CANONICAL_FINANCIAL_FACT: 4,
    EvidenceClass.DERIVED_METRIC: 3,
    EvidenceClass.HISTORICAL_TRAJECTORY: 3,
    EvidenceClass.PEER_CONTEXT: 2,
    EvidenceClass.MARKET_CONTEXT: 1,
    EvidenceClass.CURRENT_DEVELOPMENT: 4,
    EvidenceClass.SOURCE_LIMITATION: 0,
    EvidenceClass.REFUSAL: 0,
}
"""Which source is closer to the filing when two agree about the same quantity.

Used to *describe* a disagreement, never to hide one. Precedence explains why
one number was placed first; it does not license discarding the other, and a
conflict whose explanation is only "this source ranks higher" stays
``EXPLAINED_BY_SOURCE_PRIORITY`` with both values visible."""


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where one evidence item came from, in enough detail to check it."""

    source: str
    """The owning service -- ``FactStore``, ``CompanyHistoryService``, ...."""
    concept: str | None = None
    unit: str | None = None
    period: str | None = None
    filed: str | None = None
    accession: str | None = None
    document: str | None = None
    url: str | None = None
    content_sha256: str | None = None
    status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in _asdict(self).items() if v is not None}


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One citable thing, with an identifier a claim can point at.

    ``evidence_id`` is short and stable by construction -- ``traj.revenue.3y``,
    ``dev.0001045810-26-000073`` -- so a claim can reference it and a validator
    can check the reference without the model needing to invent or echo a hash.
    """

    evidence_id: str
    evidence_class: EvidenceClass
    label: str
    value: Any = None
    unit: str | None = None
    currency: str | None = None
    period: str | None = None
    text: str | None = None
    """A quoted excerpt, where the evidence is a sentence in a filing. Data,
    never instruction -- see :mod:`app.synthesis.contract`."""
    provenance: Provenance | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.evidence_id,
            "class": str(self.evidence_class),
            "label": self.label,
        }
        for key, value in (
            ("value", self.value),
            ("unit", self.unit),
            ("currency", self.currency),
            ("period", self.period),
            ("text", self.text),
            ("detail", self.detail),
        ):
            if value is not None:
                out[key] = value
        if self.provenance is not None:
            out["provenance"] = self.provenance.as_dict()
        return out


@dataclass(frozen=True, slots=True)
class Omission:
    """Something absent, and why. Never a bare null."""

    key: str
    label: str
    reason: OmissionReason
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {"key": self.key, "label": self.label, "reason": str(self.reason)}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True, slots=True)
class EvidenceConflict:
    """Two evidence items that disagree, and whether anything explains it."""

    conflict_id: str
    evidence_a: str
    evidence_b: str
    conflict_type: ConflictType
    status: ConflictStatus
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.conflict_id,
            "a": self.evidence_a,
            "b": self.evidence_b,
            "type": str(self.conflict_type),
            "status": str(self.status),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Freshness:
    """How current each part of the packet is, separately.

    Deliberately not one timestamp. Fundamentals arrive quarterly, prices
    daily, research ingestion every four hours, and a single "last updated"
    would take its value from the fastest of them and imply it of the rest.
    """

    fundamentals_as_of: str | None = None
    market_as_of: str | None = None
    research_ingestion: str | None = None
    developments_as_of: str | None = None
    peer_as_of: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in _asdict(self).items() if v is not None}


@dataclass(frozen=True, slots=True)
class PacketIdentity:
    """Who the packet is about, keeping company and listing apart."""

    company_id: int | None
    company_key: str
    company_name: str
    cik: str | None
    sic: str | None
    sic_description: str | None
    listing: str | None
    """The listing any market or valuation evidence belongs to. ``None`` when
    the packet carries none -- the packet never names a listing it did not use."""
    listing_reason: str | None = None
    reporting_currency: str | None = None
    quote_currency: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in _asdict(self).items() if v is not None}


@dataclass(frozen=True, slots=True)
class EvidencePacket:
    """Everything a synthesis may see about one company at one moment.

    Company-centric and immutable. Assembled by
    :mod:`app.synthesis.packet` from the owning services; nothing is fetched
    here, and nothing outside those services can be added to it.
    """

    identity: PacketIdentity
    as_of: str
    version: str = PACKET_VERSION
    fundamentals: tuple[EvidenceItem, ...] = ()
    trajectory: tuple[EvidenceItem, ...] = ()
    own_history: tuple[EvidenceItem, ...] = ()
    peer_context: tuple[EvidenceItem, ...] = ()
    market_context: tuple[EvidenceItem, ...] = ()
    developments: tuple[EvidenceItem, ...] = ()
    primary_source: tuple[EvidenceItem, ...] = ()
    omissions: tuple[Omission, ...] = ()
    conflicts: tuple[EvidenceConflict, ...] = ()
    freshness: Freshness = field(default_factory=Freshness)
    limitations: tuple[str, ...] = ()
    """Standing boundaries that are true of every packet -- no validated
    predictive evidence, no historical event study. Stated so a synthesis
    cannot treat their absence as licence."""

    @property
    def items(self) -> tuple[EvidenceItem, ...]:
        return (
            *self.fundamentals,
            *self.trajectory,
            *self.own_history,
            *self.peer_context,
            *self.market_context,
            *self.developments,
            *self.primary_source,
        )

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(item.evidence_id for item in self.items)

    def item(self, evidence_id: str) -> EvidenceItem | None:
        return next((i for i in self.items if i.evidence_id == evidence_id), None)

    def as_dict(self) -> dict[str, Any]:
        """The model-facing form. Ordered, so the hash is stable."""
        return {
            "version": self.version,
            "as_of": self.as_of,
            "identity": self.identity.as_dict(),
            "fundamentals": [i.as_dict() for i in self.fundamentals],
            "trajectory": [i.as_dict() for i in self.trajectory],
            "own_history": [i.as_dict() for i in self.own_history],
            "peer_context": [i.as_dict() for i in self.peer_context],
            "market_context": [i.as_dict() for i in self.market_context],
            "developments": [i.as_dict() for i in self.developments],
            "primary_source": [i.as_dict() for i in self.primary_source],
            "omissions": [o.as_dict() for o in self.omissions],
            "conflicts": [c.as_dict() for c in self.conflicts],
            "freshness": self.freshness.as_dict(),
            "limitations": list(self.limitations),
        }

    def serialise(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def packet_hash(self) -> str:
        """Identity of exactly this evidence. Any change produces a new hash,
        which is what makes a cached synthesis safe to invalidate."""
        return hashlib.sha256(self.serialise().encode()).hexdigest()[:32]

    @property
    def size_bytes(self) -> int:
        return len(self.serialise().encode())


def _asdict(obj: Any) -> dict[str, Any]:
    return {f: getattr(obj, f) for f in obj.__slots__}
