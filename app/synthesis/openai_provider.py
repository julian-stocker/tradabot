"""The only module in Tradabot permitted to import a model SDK.

Everything provider-specific is here: the client, the wire schema, the error
mapping and the sampling configuration. The rest of the system talks to
:class:`~app.synthesis.provider.SynthesisProvider`, which is a protocol, so
replacing OpenAI with something else is a new file rather than a search.

The SDK is imported inside the method that needs it
---------------------------------------------------
Not at module scope. Three things follow, and all three matter:

* ``app.synthesis`` stays importable with no SDK installed, so the whole
  package -- packet builder, validator, deterministic brief -- is testable on a
  machine that has never been near an API key.
* A missing SDK is a :class:`ProviderFailure`, not an ``ImportError`` at start-up
  that takes an unrelated command down with it.
* Installing the SDK is an operator step at the activation gate, separable from
  merging this code.

Unknown evidence identifiers are impossible, not discouraged
------------------------------------------------------------
``evidence_ids`` is typed in the wire schema as an enum of the identifiers this
packet actually contains. Under strict structured outputs the model cannot emit
a string outside that set, so a fabricated citation is a decoding impossibility
rather than something the validator catches afterwards. The validator checks it
anyway -- provider-side schema success confers no trust -- but the two mechanisms
fail independently, which is the point of having both.

Retries are off
---------------
Both ours and the SDK's. ``openai`` retries twice by default, silently, which
turns one budgeted request into three billed ones and makes a rate-limit
incident cost triple. ``max_retries=0`` is passed explicitly. For a manual pilot
with a human reading each result, re-running a slot is a decision somebody makes
rather than one the library makes at 2am.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Final

from app.core.logging import get_logger
from app.synthesis.contract import (
    MAX_CLAIM_CHARS,
    MAX_CLAIMS,
    MAX_SUMMARY_CHARS,
    SYNTHESIS_SCHEMA_VERSION,
    ClaimType,
    ModelMetadata,
    ResearchSynthesis,
    SynthesisClaim,
    SynthesisConfidence,
)
from app.synthesis.pricing import PILOT_MODEL, PILOT_PROVIDER
from app.synthesis.provider import ProviderFailure, ProviderResponse, SynthesisRequest

logger = get_logger(__name__)

API_KEY_ENV: Final = "OPENAI_API_KEY"
"""Read from the environment, never from a parameter, a config file in git or a
prompt. The value is never logged, never stored and never placed in a packet,
a synthesis, a ledger row or an exception message this code constructs."""

MAX_OUTPUT_TOKENS: Final = 900
REQUEST_TIMEOUT_SECONDS: Final = 60.0
MAX_RETRIES: Final = 0
MAX_LIMITATIONS: Final = 6
MAX_EVIDENCE_PER_CLAIM: Final = 6
SCHEMA_NAME: Final = "research_synthesis"


@dataclass(frozen=True, slots=True)
class OpenAIConfig:
    """Exactly what is sent, and what is deliberately not.

    ``temperature`` is ``None`` and stays ``None``. The selected model's page
    enumerates the features it supports -- structured outputs, function calling,
    prompt caching, streaming -- and no sampling parameter appears among them.
    Sending an undocumented parameter to find out whether it errors is a way to
    spend the pilot's budget on an experiment about the API rather than about
    the synthesis. The variance lever used instead is ``reasoning_effort``.

    ``reasoning_effort="none"`` makes the output ceiling mean what it says.
    Reasoning tokens are billed as output and counted against
    ``max_completion_tokens``, so any other setting makes a 900-token cap a cap
    on reasoning-plus-answer and turns a long deliberation into a truncated
    synthesis. With reasoning off, the ceiling bounds the answer, and the cost
    estimate computed before dispatch is exact rather than hopeful.
    """

    model: str = PILOT_MODEL
    max_output_tokens: int = MAX_OUTPUT_TOKENS
    reasoning_effort: str | None = "none"
    temperature: float | None = None
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    max_retries: int = MAX_RETRIES
    length_constraints_in_schema: bool = True
    """Whether ``maxLength``/``maxItems`` are sent inside the strict schema.

    Documented as supported. Kept switchable because the local validator
    enforces every one of these bounds regardless, so if a future model rejects
    them the correct response is to stop sending them, not to stop checking."""

    @property
    def material(self) -> dict[str, Any]:
        """The part of the configuration that could change the output.

        Feeds the cache key. ``timeout_seconds`` and ``max_retries`` are absent:
        waiting longer for the same request does not produce a different answer,
        and a cache key that moved when a timeout was tuned would discard
        everything the pilot had already paid for.
        """
        return {
            "model": self.model,
            "max_output_tokens": self.max_output_tokens,
            "reasoning_effort": self.reasoning_effort,
            "temperature": self.temperature,
            "schema_version": SYNTHESIS_SCHEMA_VERSION,
            "length_constraints": self.length_constraints_in_schema,
        }


EVIDENCE_SECTIONS: Final[tuple[str, ...]] = (
    "fundamentals",
    "trajectory",
    "own_history",
    "peer_context",
    "market_context",
    "developments",
    "primary_source",
)


def evidence_ids_in(evidence: dict[str, Any]) -> list[str]:
    """Identifiers present in the evidence payload, in packet order.

    Read from the serialised dictionary rather than from an
    :class:`~app.synthesis.evidence.EvidencePacket`, so the schema's enum is
    derived from exactly the bytes being sent. If the two ever disagreed, the
    constraint would be built from the packet the model did not receive.
    """
    # "id", not "evidence_id": that is the key `EvidenceItem.as_dict` emits, and
    # this function reads the payload rather than the object precisely so a
    # divergence between the two shows up here instead of in a constraint built
    # from a packet the model never received.
    return [str(item["id"]) for section in EVIDENCE_SECTIONS for item in evidence.get(section, [])]


def wire_schema(evidence: dict[str, Any], *, config: OpenAIConfig) -> dict[str, Any]:
    """The JSON Schema the provider is asked to satisfy, for this packet.

    Built per request rather than once, because the interesting constraint is
    packet-specific: ``evidence_ids`` enumerates the identifiers in *this*
    packet. There is no ``metadata`` object -- provenance is stamped locally
    from what was actually sent, never taken from the model's word for it -- and
    no field for anything the contract forbids.
    """
    ids = evidence_ids_in(evidence)
    if not ids:
        raise ValueError("cannot build a schema for a packet with no evidence")

    text: dict[str, Any] = {"type": "string"}
    summary: dict[str, Any] = {"type": "string"}
    claims: dict[str, Any] = {"type": "array"}
    limitations: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
    evidence_ids: dict[str, Any] = {
        "type": "array",
        "items": {"type": "string", "enum": ids},
    }
    if config.length_constraints_in_schema:
        text["maxLength"] = MAX_CLAIM_CHARS
        summary["maxLength"] = MAX_SUMMARY_CHARS
        claims["maxItems"] = MAX_CLAIMS
        limitations["maxItems"] = MAX_LIMITATIONS
        limitations["items"] = {"type": "string", "maxLength": MAX_CLAIM_CHARS}
        evidence_ids["minItems"] = 1
        evidence_ids["maxItems"] = MAX_EVIDENCE_PER_CLAIM

    claims["items"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["claim_id", "claim_type", "text", "evidence_ids", "temporal_scope"],
        "properties": {
            "claim_id": {"type": "string"},
            "claim_type": {"type": "string", "enum": [str(c) for c in ClaimType]},
            "text": text,
            "evidence_ids": evidence_ids,
            "temporal_scope": {"type": "string", "enum": ["PAST", "CURRENT"]},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["company_key", "as_of", "summary", "claims", "confidence", "limitations"],
        "properties": {
            "company_key": {"type": "string"},
            "as_of": {"type": "string"},
            "summary": summary,
            "claims": claims,
            "confidence": {"type": "string", "enum": [str(c) for c in SynthesisConfidence]},
            "limitations": limitations,
        },
    }


@dataclass
class OpenAISynthesisProvider:
    """One bounded call to one model. Never raises; returns a failure instead.

    Satisfies :class:`~app.synthesis.provider.SynthesisProvider` structurally.
    """

    config: OpenAIConfig = field(default_factory=OpenAIConfig)
    name: str = PILOT_PROVIDER
    model: str = PILOT_MODEL
    _client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.model = self.config.model

    # -- client ------------------------------------------------------------

    def _ensure_client(self) -> Any:
        """Build the SDK client on first use, from the environment only."""
        if self._client is not None:
            return self._client
        from openai import OpenAI  # noqa: PLC0415 -- see the module docstring

        self._client = OpenAI(
            timeout=self.config.timeout_seconds,
            max_retries=self.config.max_retries,
        )
        return self._client

    # -- the call ----------------------------------------------------------

    def synthesise(self, request: SynthesisRequest) -> ProviderResponse:
        """Send one request and return a candidate, or say why there is none."""
        try:
            payload = self._payload(request)
        except ValueError as exc:
            return ProviderResponse(failure=ProviderFailure.SCHEMA_VIOLATION, detail=str(exc))

        try:
            client = self._ensure_client()
        except ImportError:
            return ProviderResponse(
                failure=ProviderFailure.PROVIDER_OUTAGE,
                detail="the openai SDK is not installed",
            )
        except Exception as exc:
            return ProviderResponse(failure=_classify(exc), detail=_detail(exc))

        try:
            completion = client.chat.completions.create(**payload)
        except Exception as exc:
            failure = _classify(exc)
            logger.warning("synthesis provider call failed", failure=str(failure))
            return ProviderResponse(failure=failure, detail=_detail(exc))

        return self._interpret(completion, request)

    def _payload(self, request: SynthesisRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "max_completion_tokens": request.max_output_tokens,
            "messages": [
                {"role": "system", "content": request.contract},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"task": request.task, "evidence": request.evidence},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": SCHEMA_NAME,
                    "strict": True,
                    "schema": wire_schema(request.evidence, config=self.config),
                },
            },
        }
        if self.config.reasoning_effort is not None:
            payload["reasoning_effort"] = self.config.reasoning_effort
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        return payload

    def _interpret(self, completion: Any, request: SynthesisRequest) -> ProviderResponse:
        """Turn a raw completion into a candidate, or into a stated failure."""
        usage = getattr(completion, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", None)
        tokens_out = getattr(usage, "completion_tokens", None)

        choices = getattr(completion, "choices", None) or []
        if not choices:
            return ProviderResponse(
                failure=ProviderFailure.INVALID_JSON,
                detail="response contained no choices",
                input_tokens=tokens_in,
                output_tokens=tokens_out,
            )
        choice = choices[0]
        message = getattr(choice, "message", None)

        refusal = getattr(message, "refusal", None)
        if refusal:
            return ProviderResponse(
                failure=ProviderFailure.REFUSED,
                detail="model returned a refusal",
                input_tokens=tokens_in,
                output_tokens=tokens_out,
            )
        if getattr(choice, "finish_reason", None) == "length":
            return ProviderResponse(
                failure=ProviderFailure.TOKEN_OVERFLOW,
                detail=f"output reached the {request.max_output_tokens}-token ceiling",
                input_tokens=tokens_in,
                output_tokens=tokens_out,
            )

        content = getattr(message, "content", None)
        if not content:
            return ProviderResponse(
                failure=ProviderFailure.INVALID_JSON,
                detail="response contained no content",
                input_tokens=tokens_in,
                output_tokens=tokens_out,
            )
        return self._parse(content, request, tokens_in, tokens_out)

    def _parse(
        self,
        content: str,
        request: SynthesisRequest,
        tokens_in: int | None,
        tokens_out: int | None,
    ) -> ProviderResponse:
        """Parse structured content into a candidate. Still untrusted after."""
        raw_hash = hashlib.sha256(content.encode()).hexdigest()[:32]
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            return ProviderResponse(
                failure=ProviderFailure.INVALID_JSON,
                detail=f"content did not parse: {exc.msg}",
                raw_hash=raw_hash,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
            )
        try:
            candidate = _candidate_from(parsed, request=request, model=self.config.model)
        except (KeyError, TypeError, ValueError) as exc:
            return ProviderResponse(
                failure=ProviderFailure.SCHEMA_VIOLATION,
                detail=f"structured response did not match the contract: {exc}",
                raw_hash=raw_hash,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
            )
        return ProviderResponse(
            candidate=candidate,
            raw_hash=raw_hash,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
        )


def _candidate_from(
    payload: dict[str, Any], *, request: SynthesisRequest, model: str
) -> ResearchSynthesis:
    """Build a candidate from parsed JSON.

    Provenance is stamped from the request, not read from the payload. The model
    is not asked which packet it was given and would not be believed if it
    answered: ``packet_hash`` here is the hash of what was actually sent, which
    is what the validator compares against.
    """
    claims = tuple(
        SynthesisClaim(
            claim_id=str(c["claim_id"]),
            claim_type=ClaimType(c["claim_type"]),
            text=str(c["text"]),
            evidence_ids=tuple(str(e) for e in c["evidence_ids"]),
            temporal_scope=str(c["temporal_scope"]),
        )
        for c in payload["claims"]
    )
    return ResearchSynthesis(
        company_key=str(payload["company_key"]),
        as_of=str(payload["as_of"]),
        summary=str(payload["summary"]),
        claims=claims,
        confidence=SynthesisConfidence(payload["confidence"]),
        limitations=tuple(str(x) for x in payload["limitations"]),
        metadata=ModelMetadata(
            provider=PILOT_PROVIDER,
            model=model,
            packet_version=str(request.evidence.get("version", "")),
            packet_hash=request.packet_hash,
            template_version=request.template_version,
            temperature=None,
        ),
    )


_ERROR_MAP: Final[tuple[tuple[str, ProviderFailure], ...]] = (
    # Matched on the SDK's exception class name so the mapping is readable and
    # does not require the SDK to be importable in order to be tested.
    ("APITimeoutError", ProviderFailure.TIMEOUT),
    ("Timeout", ProviderFailure.TIMEOUT),
    ("RateLimitError", ProviderFailure.RATE_LIMITED),
    ("AuthenticationError", ProviderFailure.AUTHENTICATION),
    ("PermissionDeniedError", ProviderFailure.AUTHENTICATION),
    ("InsufficientQuotaError", ProviderFailure.QUOTA_EXCEEDED),
    ("BadRequestError", ProviderFailure.SCHEMA_VIOLATION),
    ("UnprocessableEntityError", ProviderFailure.SCHEMA_VIOLATION),
    ("NotFoundError", ProviderFailure.PROVIDER_OUTAGE),
    ("InternalServerError", ProviderFailure.PROVIDER_OUTAGE),
    ("APIConnectionError", ProviderFailure.PROVIDER_OUTAGE),
    ("APIStatusError", ProviderFailure.PROVIDER_OUTAGE),
    ("APIError", ProviderFailure.PROVIDER_OUTAGE),
)


QUOTA_MARKERS: Final[tuple[str, ...]] = (
    "insufficient_quota",
    "exceeded your current quota",
    "billing details",
    "billing_not_active",
)
"""Phrases that mean the account cannot pay, whatever class carried them.

Checked before the class map, and that ordering is the point. An exhausted
balance arrives as HTTP 429 -- the same status as a rate limit -- so the SDK
raises ``RateLimitError`` for both. Classifying by type alone would report an
unpayable account as "rate limited", which reads as *wait and retry* when the
truth is *this will never succeed until somebody adds a payment method*. These
phrases are specific to the quota message; an ordinary rate limit says "Rate
limit reached ... please try again in", and matches none of them.
"""


QUOTA_CODES: Final[frozenset[str]] = frozenset({"insufficient_quota", "billing_not_active"})
"""Documented ``error.code`` values that mean the account cannot pay."""


def _error_code(exc: BaseException) -> str:
    """The provider's own error code, if the SDK surfaced one.

    Preferred over reading the message. OpenAI errors carry a structured
    ``code`` and a parsed ``body``; prose is what is left when neither is
    available, and prose is the part most likely to be reworded.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return str(error["code"])
    return ""


def _classify(exc: BaseException) -> ProviderFailure:
    """Map an SDK exception to a stated failure.

    Authentication and quota get their own members rather than collapsing into
    ``PROVIDER_OUTAGE``. They are the two failures that will never succeed on
    retry and that a person must fix, and a pilot report that called a missing
    API key an outage would send somebody to look at a status page.

    Quota is decided before the class map, and that ordering is the whole point.
    An exhausted balance and ordinary throttling both arrive as HTTP 429, so the
    SDK raises ``RateLimitError`` for both; classifying by type alone would
    report an unpayable account as "rate limited", which reads as *wait and
    retry* when the truth is *this never succeeds until somebody adds a payment
    method*. The structured error code is consulted first and the message only
    as a fallback.
    """
    if _error_code(exc) in QUOTA_CODES:
        return ProviderFailure.QUOTA_EXCEEDED
    if any(marker in str(exc).lower() for marker in QUOTA_MARKERS):
        return ProviderFailure.QUOTA_EXCEEDED
    names = {cls.__name__ for cls in type(exc).__mro__}
    for name, failure in _ERROR_MAP:
        if name in names:
            return failure
    return ProviderFailure.PROVIDER_OUTAGE


def _detail(exc: BaseException) -> str:
    """Only the exception's class name ever leaves this module.

    Provider error text can echo request headers as well as request content.
    Rather than scan an arbitrary message for a key shape and trust the pattern,
    nothing but the type name is propagated -- so a credential cannot reach a
    log, a ledger row or a pilot report by way of an error string.
    """
    return type(exc).__name__
