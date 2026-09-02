"""The one path from a packet to something renderable.

    packet -> cache -> cost guard -> provider -> validator -> validated
                 |          |            |           |
                 |          |            |           +-- rejected -> brief
                 |          |            +-- failed ------------------> brief
                 |          +-- refused -------------------------------> brief
                 +-- hit --> validated

There is no other route. The provider is called from here and nowhere else, the
validator runs on every candidate without exception, and every branch that is
not a validated synthesis ends at the deterministic brief. A caller cannot
accidentally skip a step by calling the adapter directly -- it can, and gets an
unvalidated candidate that no renderer accepts.

Failure is never partial
------------------------
A rejected candidate does not contribute its acceptable claims to the fallback.
The brief is rebuilt from the packet, deterministically, exactly as it would be
if no provider existed. Mixing a model's surviving sentences into a
deterministic document produces something with no author and no version, which
is precisely the artefact this design exists to prevent.

An empty packet costs nothing
-----------------------------
A packet with no evidence -- an ETF, a company with no coverage -- returns
``NOT_APPLICABLE`` before the cache is consulted. There is no synthesis to be
had, and paying a provider to confirm that is a way of testing the wrapper
rather than the model.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.core.logging import get_logger
from app.synthesis.brief import build_brief
from app.synthesis.budget import BudgetVerdict, CostGuard, current_month
from app.synthesis.cache import SynthesisCache, key_for
from app.synthesis.contract import (
    SYNTHESIS_SCHEMA_VERSION,
    ResearchSynthesis,
    ValidatedResearchSynthesis,
)
from app.synthesis.evidence import EvidencePacket
from app.synthesis.ledger import (
    STATUS_CACHE_HIT,
    STATUS_DISPATCHED,
    STATUS_NOT_APPLICABLE,
    STATUS_OK,
    STATUS_PROVIDER_FAILED,
    STATUS_REFUSED_BUDGET,
    STATUS_VALIDATOR_REJECTED,
    VERDICT_INVALID,
    VERDICT_VALID,
    CallRecord,
    SynthesisLedger,
)
from app.synthesis.openai_provider import OpenAIConfig
from app.synthesis.pricing import ModelPricing
from app.synthesis.provider import (
    REJECTED_BEFORE_INFERENCE,
    ProviderFailure,
    SynthesisProvider,
    build_request,
)
from app.synthesis.validator import SynthesisValidator, ValidationResult

logger = get_logger(__name__)


class Outcome(StrEnum):
    """What happened, at the granularity a pilot report needs."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    """No evidence to synthesise. No call, no cost."""
    CACHE_HIT = "CACHE_HIT"
    """An identical request was already validated. No call, no cost."""
    MODEL_VALIDATED = "MODEL_VALIDATED"
    REFUSED_BUDGET = "REFUSED_BUDGET"
    """A cap refused the request before dispatch. No call, no cost."""
    PROVIDER_FAILED = "PROVIDER_FAILED"
    VALIDATOR_REJECTED = "VALIDATOR_REJECTED"


OUTCOME_STATUS: dict[Outcome, str] = {
    Outcome.MODEL_VALIDATED: STATUS_OK,
    Outcome.VALIDATOR_REJECTED: STATUS_VALIDATOR_REJECTED,
    Outcome.PROVIDER_FAILED: STATUS_PROVIDER_FAILED,
    Outcome.REFUSED_BUDGET: STATUS_REFUSED_BUDGET,
    Outcome.CACHE_HIT: STATUS_CACHE_HIT,
    Outcome.NOT_APPLICABLE: STATUS_NOT_APPLICABLE,
}
"""Which ledger status each outcome is written under.

An explicit table rather than two vocabularies that happen to match. They did
not match once -- the service wrote ``VALIDATOR_REJECTED`` while the ledger
charged ``REJECTED`` -- and the result was a month of rejected responses
reporting zero spend. A test walks this map against
:data:`~app.synthesis.ledger.BILLABLE_STATUSES` so the two cannot drift again.
"""


NO_CALL_OUTCOMES = frozenset({Outcome.NOT_APPLICABLE, Outcome.CACHE_HIT, Outcome.REFUSED_BUDGET})
"""Outcomes reached without a request leaving the machine. Each must cost zero,
and a test asserts the ledger agrees."""


@dataclass(frozen=True, slots=True)
class SynthesisOutcome:
    """One packet's result, including everything the pilot needs to score it."""

    outcome: Outcome
    packet_hash: str
    company_key: str
    as_of: str
    validated: ValidatedResearchSynthesis | None = None
    fallback: ResearchSynthesis | None = None
    candidate: ResearchSynthesis | None = None
    failure: ProviderFailure | None = None
    validation: ValidationResult | None = None
    budget: BudgetVerdict | None = None
    call: CallRecord | None = None
    provider_latency_ms: int | None = None
    detail: str = ""

    @property
    def renderable(self) -> ValidatedResearchSynthesis | None:
        """The only thing anything downstream may show. ``None`` means the
        deterministic brief is what the reader gets."""
        return self.validated


class SynthesisService:
    """Orchestrates one bounded synthesis. Owns the ordering, nothing else."""

    def __init__(
        self,
        *,
        provider: SynthesisProvider,
        guard: CostGuard,
        cache: SynthesisCache,
        ledger: SynthesisLedger,
        pricing: ModelPricing,
        config: OpenAIConfig,
        validator: SynthesisValidator | None = None,
    ) -> None:
        self._provider = provider
        self._guard = guard
        self._cache = cache
        self._ledger = ledger
        self._pricing = pricing
        self._config = config
        self._validator = validator or SynthesisValidator()

    def synthesise(
        self, packet: EvidencePacket, *, now: datetime | None = None
    ) -> SynthesisOutcome:
        """Produce a validated synthesis, or say why there is not one."""
        stamp = now or datetime.now(UTC)
        base: dict[str, Any] = {
            "packet_hash": packet.packet_hash,
            "company_key": packet.identity.company_key,
            "as_of": packet.as_of,
        }

        if not packet.items:
            return SynthesisOutcome(
                outcome=Outcome.NOT_APPLICABLE,
                detail="packet carries no evidence",
                **base,
            )

        key = key_for(
            packet,
            provider=self._provider.name,
            model=self._provider.model,
            template_version=self._template_version(packet),
            config=self._config.material,
        )
        cached = self._cache.get(key)
        if cached is not None:
            return SynthesisOutcome(outcome=Outcome.CACHE_HIT, validated=cached, **base)

        fallback = build_brief(packet)
        request = build_request(packet, max_output_tokens=self._config.max_output_tokens)
        verdict = self._guard.check(
            input_tokens=request.approximate_input_tokens,
            max_output_tokens=request.max_output_tokens,
            now=stamp,
        )
        call = self._pre_dispatch_record(packet, request, verdict, stamp)

        if not verdict.allowed:
            refused = _with(
                call, status=OUTCOME_STATUS[Outcome.REFUSED_BUDGET], billed_usd=Decimal(0)
            )
            self._ledger.record(refused)
            return SynthesisOutcome(
                outcome=Outcome.REFUSED_BUDGET,
                fallback=fallback,
                budget=verdict,
                call=refused,
                detail=verdict.detail,
                **base,
            )

        self._ledger.record(call)
        self._guard.note_dispatch()
        started = time.monotonic()
        response = self._provider.synthesise(request)
        latency_ms = int((time.monotonic() - started) * 1000)

        billed, actual = self._billed(
            verdict, response.input_tokens, response.output_tokens, response.failure
        )

        if not response.ok or response.candidate is None:
            failed = _with(
                call,
                status=OUTCOME_STATUS[Outcome.PROVIDER_FAILED],
                failure=None if response.failure is None else str(response.failure),
                billed_usd=billed,
                actual_input_tokens=response.input_tokens,
                actual_output_tokens=response.output_tokens,
                actual_usd=actual,
                latency_ms=latency_ms,
            )
            self._ledger.record(failed)
            return SynthesisOutcome(
                outcome=Outcome.PROVIDER_FAILED,
                fallback=fallback,
                failure=response.failure,
                budget=verdict,
                call=failed,
                provider_latency_ms=latency_ms,
                detail=response.detail or "",
                **base,
            )

        result = self._validator.validate(response.candidate, packet=packet)
        self._ledger.record_raw(
            raw_id=uuid.uuid4().hex,
            call_id=call.call_id,
            stored_at=stamp.isoformat(),
            company_key=packet.identity.company_key,
            as_of=packet.as_of,
            packet_hash=packet.packet_hash,
            verdict=VERDICT_VALID if result.valid else VERDICT_INVALID,
            failure=None if result.valid else str(result.failures[0].reason),
            raw_response=response.raw_hash or "",
            candidate=response.candidate.as_dict(),
            validated=None if result.validated is None else result.validated.as_dict(),
        )

        if not result.valid or result.validated is None:
            rejected = _with(
                call,
                status=OUTCOME_STATUS[Outcome.VALIDATOR_REJECTED],
                failure=str(result.failures[0].reason) if result.failures else None,
                billed_usd=billed,
                actual_input_tokens=response.input_tokens,
                actual_output_tokens=response.output_tokens,
                actual_usd=actual,
                latency_ms=latency_ms,
            )
            self._ledger.record(rejected)
            return SynthesisOutcome(
                outcome=Outcome.VALIDATOR_REJECTED,
                fallback=fallback,
                candidate=response.candidate,
                validation=result,
                budget=verdict,
                call=rejected,
                provider_latency_ms=latency_ms,
                **base,
            )

        self._cache.put(key, result.validated)
        ok = _with(
            call,
            status=OUTCOME_STATUS[Outcome.MODEL_VALIDATED],
            billed_usd=billed,
            actual_input_tokens=response.input_tokens,
            actual_output_tokens=response.output_tokens,
            actual_usd=actual,
            latency_ms=latency_ms,
        )
        self._ledger.record(ok)
        return SynthesisOutcome(
            outcome=Outcome.MODEL_VALIDATED,
            validated=result.validated,
            fallback=fallback,
            candidate=response.candidate,
            validation=result,
            budget=verdict,
            call=ok,
            provider_latency_ms=latency_ms,
            **base,
        )

    # -- accounting --------------------------------------------------------

    def _billed(
        self,
        verdict: BudgetVerdict,
        tokens_in: int | None,
        tokens_out: int | None,
        failure: ProviderFailure | None = None,
    ) -> tuple[Decimal, Decimal | None]:
        """What to charge this call, and what it actually cost if that is known.

        Three cases, in order.

        Reported usage wins. When the provider says how many tokens it consumed,
        that is the truth and both figures are it -- including for a response the
        validator went on to reject, which was still generated and still billed.

        A failure the provider rejected before inference costs nothing, and is
        recorded as costing nothing. See
        :data:`~app.synthesis.provider.REJECTED_BEFORE_INFERENCE`.

        Everything else charges the pre-call estimate. A timeout may or may not
        have produced tokens; a connection dropped mid-stream certainly may
        have. Reserving the estimate is the only direction that does not leak,
        and a month of timeouts is exactly the month in which the cap has to
        hold. Reconciliation replaces the reservation rather than adding to it,
        because the row is rewritten under its own ``call_id``.
        """
        if tokens_in is not None and tokens_out is not None:
            actual = self._pricing.cost_usd(input_tokens=tokens_in, output_tokens=tokens_out)
            return actual, actual
        if failure is not None and failure in REJECTED_BEFORE_INFERENCE:
            return Decimal(0), Decimal(0)
        return verdict.estimate.total_usd, None

    def _pre_dispatch_record(
        self,
        packet: EvidencePacket,
        request: Any,
        verdict: BudgetVerdict,
        stamp: datetime,
    ) -> CallRecord:
        return CallRecord(
            call_id=uuid.uuid4().hex,
            requested_at=stamp.isoformat(),
            month=current_month(stamp),
            provider=self._provider.name,
            model=self._provider.model,
            company_key=packet.identity.company_key,
            as_of=packet.as_of,
            packet_hash=packet.packet_hash,
            template_version=request.template_version,
            schema_version=SYNTHESIS_SCHEMA_VERSION,
            estimated_input_tokens=request.approximate_input_tokens,
            max_output_tokens=request.max_output_tokens,
            estimated_usd=verdict.estimate.total_usd,
            billed_usd=verdict.estimate.total_usd,
            status=STATUS_DISPATCHED,
        )

    def _template_version(self, packet: EvidencePacket) -> str:
        return build_request(packet, max_output_tokens=1).template_version


def _with(call: CallRecord, **changes: Any) -> CallRecord:
    from dataclasses import replace  # noqa: PLC0415 -- one call site

    return replace(call, **changes)
