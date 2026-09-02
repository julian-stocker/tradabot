"""Validated syntheses, keyed by everything that could change one.

The key answers a single question: *would a call made now produce a different
answer than the one stored?* Anything that could is in it -- the company and
listing, the ``as_of``, the packet hash, both schema versions, the provider, the
model, and a hash of the sampling configuration. A key missing any of those
serves output produced under conditions that no longer hold, which is worse than
no cache, because it is silent.

The packet hash alone would nearly do: it already covers the company, the date
and every piece of evidence. The rest are in the key anyway because the cost of
an over-specific key is one extra call and the cost of an under-specific one is
a synthesis attributed to a model that did not write it.

Only validated output is stored
-------------------------------
:meth:`SynthesisCache.put` takes a :class:`ValidatedResearchSynthesis` and
nothing else, so caching a rejected candidate is not a discipline anybody has to
remember -- it does not type-check. Raw and rejected responses live in
``synthesis_raw``, which no read path here touches.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.synthesis.contract import (
    SYNTHESIS_SCHEMA_VERSION,
    ClaimType,
    ModelMetadata,
    ResearchSynthesis,
    SynthesisClaim,
    SynthesisConfidence,
    ValidatedResearchSynthesis,
)
from app.synthesis.evidence import EvidencePacket
from app.synthesis.ledger import SynthesisLedger


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Every input that determines a synthesis, and the digest of all of them."""

    company_key: str
    listing: str | None
    as_of: str
    packet_hash: str
    provider: str
    model: str
    schema_version: str
    template_version: str
    config_hash: str

    @property
    def digest(self) -> str:
        payload = json.dumps(
            {
                "company_key": self.company_key,
                "listing": self.listing,
                "as_of": self.as_of,
                "packet_hash": self.packet_hash,
                "provider": self.provider,
                "model": self.model,
                "schema_version": self.schema_version,
                "template_version": self.template_version,
                "config_hash": self.config_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:32]


def config_hash(config: dict[str, Any]) -> str:
    """Digest of the material model configuration.

    Material means "could change the output": the output ceiling, the reasoning
    effort, any sampling parameter actually sent. A timeout is not material and
    is deliberately excluded, so retrying a slow call does not miss its own
    cache entry.
    """
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def key_for(
    packet: EvidencePacket,
    *,
    provider: str,
    model: str,
    template_version: str,
    config: dict[str, Any],
) -> CacheKey:
    return CacheKey(
        company_key=packet.identity.company_key,
        listing=packet.identity.listing,
        as_of=packet.as_of,
        packet_hash=packet.packet_hash,
        provider=provider,
        model=model,
        schema_version=SYNTHESIS_SCHEMA_VERSION,
        template_version=template_version,
        config_hash=config_hash(config),
    )


class SynthesisCache:
    """Reads and writes validated syntheses. Stores nothing else."""

    def __init__(self, ledger: SynthesisLedger) -> None:
        self._ledger = ledger

    def get(self, key: CacheKey) -> ValidatedResearchSynthesis | None:
        with self._ledger.connect() as conn:
            row = conn.execute(
                "SELECT validated_json FROM synthesis_cache WHERE cache_key = ?",
                (key.digest,),
            ).fetchone()
        if row is None:
            return None
        return _from_json(json.loads(row["validated_json"]))

    def put(self, key: CacheKey, validated: ValidatedResearchSynthesis) -> None:
        """Store a synthesis that has already passed the validator.

        The parameter type is the whole enforcement. There is no overload taking
        a candidate, a raw string or a dict.
        """
        with self._ledger.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO synthesis_cache (
                    cache_key, company_key, listing, as_of, packet_hash, provider,
                    model, schema_version, template_version, config_hash,
                    created_at, validated_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    key.digest,
                    key.company_key,
                    key.listing,
                    key.as_of,
                    key.packet_hash,
                    key.provider,
                    key.model,
                    key.schema_version,
                    key.template_version,
                    key.config_hash,
                    datetime.now(UTC).isoformat(),
                    json.dumps(validated.as_dict(), sort_keys=True),
                ),
            )


def _from_json(payload: dict[str, Any]) -> ValidatedResearchSynthesis:
    meta = payload.get("metadata")
    synthesis = ResearchSynthesis(
        company_key=payload["company_key"],
        as_of=payload["as_of"],
        summary=payload["summary"],
        claims=tuple(
            SynthesisClaim(
                claim_id=c["claim_id"],
                claim_type=ClaimType(c["claim_type"]),
                text=c["text"],
                evidence_ids=tuple(c["evidence_ids"]),
                temporal_scope=c["temporal_scope"],
                detail=c.get("detail"),
            )
            for c in payload["claims"]
        ),
        confidence=SynthesisConfidence(payload["confidence"]),
        limitations=tuple(payload["limitations"]),
        metadata=None
        if meta is None
        else ModelMetadata(
            provider=meta["provider"],
            model=meta["model"],
            schema_version=meta["schema_version"],
            packet_version=meta["packet_version"],
            packet_hash=meta["packet_hash"],
            template_version=meta["template_version"],
            temperature=meta["temperature"],
            response_hash=meta["response_hash"],
        ),
    )
    return ValidatedResearchSynthesis(
        synthesis=synthesis,
        packet_hash=payload["packet_hash"],
        validator_version=payload["validator_version"],
        checks_passed=tuple(payload["checks_passed"]),
    )
