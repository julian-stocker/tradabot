"""The frozen 24-slot cohort, and the only thing that runs it.

Two properties matter more than anything else in this file.

**The cohort is a literal.** Eight companies at three dates, written out. There
is no query that produces this list, no filter over a universe and no argument
that widens it. A pilot whose input came from a database would, on the day
somebody passed the wrong flag, become a 989-company batch; a pilot whose input
is twenty-four hard-coded pairs cannot.

**Nothing here can reach a universe.** This module imports the synthesis
service and nothing else from the application. It does not know what a screener
is, cannot list companies and receives packets through a callback the caller
supplies. A structural test asserts the import graph, because the guarantee is
about what this file *can* do, not about what it currently does.

Freezing
--------
Section 34 of the phase brief: template, model, temperature, schema and
validator are fixed before the first scored call, and a material change means a
new cohort rather than a patched one. :data:`COHORT_VERSION` is what a scored
response is stamped with, so two generations cannot be silently averaged.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from app.core.logging import get_logger
from app.synthesis.budget import MAX_PER_RUN_CALLS
from app.synthesis.evidence import EvidencePacket
from app.synthesis.service import NO_CALL_OUTCOMES, Outcome, SynthesisOutcome, SynthesisService

logger = get_logger(__name__)

COHORT_VERSION: Final = "18.1.0"

AS_OF_DATES: Final[tuple[str, ...]] = ("2022-09-01", "2024-09-01", "2026-09-01")
"""Three dates that produce materially different evidence for the same company.

Verified rather than assumed: rebuilding AAPL, MSFT and NVDA at these dates in
Phase 18.0 produced three distinct packet hashes each, and no packet carries
evidence filed after its own date."""


@dataclass(frozen=True, slots=True)
class PilotSlot:
    """One company at one date. The unit of the experiment."""

    symbol: str
    as_of: str
    note: str = ""


PILOT_COHORT: Final[tuple[PilotSlot, ...]] = tuple(
    PilotSlot(symbol=symbol, as_of=as_of, note=note)
    for symbol, note in (
        ("AAPL", "does it go beyond restating margin and share-count trajectory"),
        ("MSFT", "does it select the operating context that carries information"),
        ("NVDA", "extreme trajectory plus a supplied period-based conflict"),
        ("AMD", "volatile series; is change interpreted or merely reported"),
        ("KO", "mature and stable, without calling it defensive or high quality"),
        ("JPM", "financial refusals respected rather than filled from memory"),
        ("SAP.DE", "annual IFRS reporting, currency and source limitations"),
        ("SPY", "not a company; expected to yield no synthesis and no call"),
    )
    for as_of in AS_OF_DATES
)
"""Twenty-four slots. Twenty-one can produce a call; the three SPY slots carry
no evidence and are refused before the cache is consulted."""

PacketSource = Callable[[str, str], EvidencePacket | None]
"""``(symbol, as_of) -> packet``. Supplied by the caller so this module never
holds a fact store, a registry or anything that could enumerate companies."""


@dataclass(frozen=True, slots=True)
class SlotResult:
    slot: PilotSlot
    outcome: SynthesisOutcome | None
    skipped: str = ""


@dataclass(frozen=True, slots=True)
class PilotRun:
    """What one invocation did, in the terms the pilot report needs."""

    cohort_version: str
    started_at: str
    finished_at: str
    slots_planned: int
    results: tuple[SlotResult, ...]

    def _count(self, outcome: Outcome) -> int:
        return sum(
            1 for r in self.results if r.outcome is not None and r.outcome.outcome is outcome
        )

    @property
    def calls_attempted(self) -> int:
        return sum(
            1
            for r in self.results
            if r.outcome is not None and r.outcome.outcome not in NO_CALL_OUTCOMES
        )

    @property
    def not_applicable(self) -> int:
        return self._count(Outcome.NOT_APPLICABLE)

    @property
    def cache_hits(self) -> int:
        return self._count(Outcome.CACHE_HIT)

    @property
    def validated(self) -> int:
        return self._count(Outcome.MODEL_VALIDATED)

    @property
    def rejected(self) -> int:
        return self._count(Outcome.VALIDATOR_REJECTED)

    @property
    def provider_failures(self) -> int:
        return self._count(Outcome.PROVIDER_FAILED)

    @property
    def refused_budget(self) -> int:
        return self._count(Outcome.REFUSED_BUDGET)

    @property
    def spend_usd(self) -> Decimal:
        return sum(
            (r.outcome.call.billed_usd for r in self.results if r.outcome and r.outcome.call),
            Decimal(0),
        )

    @property
    def input_tokens(self) -> int:
        return sum(
            r.outcome.call.actual_input_tokens or 0
            for r in self.results
            if r.outcome and r.outcome.call
        )

    @property
    def output_tokens(self) -> int:
        return sum(
            r.outcome.call.actual_output_tokens or 0
            for r in self.results
            if r.outcome and r.outcome.call
        )


def run_pilot(
    slots: Sequence[PilotSlot],
    *,
    service: SynthesisService,
    packets: PacketSource,
    max_calls: int,
) -> PilotRun:
    """Run an explicit list of slots, at most ``max_calls`` of them.

    ``slots`` is required and has no default. Calling this with the whole cohort
    is a thing somebody types; there is no way to arrive here having asked for
    "everything" by omission.

    ``max_calls`` bounds calls that actually dispatch, and is separate from the
    cost guard's own per-run cap -- two independent counters, because the
    interesting bug is the one that defeats a single check. Slots beyond the
    bound are recorded as skipped rather than dropped, so the report says what
    was not covered instead of implying the cohort was complete.
    """
    if not slots:
        raise ValueError("run_pilot requires an explicit list of slots")
    if max_calls < 1 or max_calls > MAX_PER_RUN_CALLS:
        raise ValueError(f"max_calls must be 1..{MAX_PER_RUN_CALLS}, got {max_calls}")

    started = datetime.now(UTC).isoformat()
    results: list[SlotResult] = []
    dispatched = 0

    for slot in slots:
        packet = packets(slot.symbol, slot.as_of)
        if packet is None:
            results.append(SlotResult(slot=slot, outcome=None, skipped="no packet"))
            continue
        if not packet.items:
            outcome = service.synthesise(packet)
            results.append(SlotResult(slot=slot, outcome=outcome))
            continue
        if dispatched >= max_calls:
            results.append(
                SlotResult(slot=slot, outcome=None, skipped=f"run cap of {max_calls} reached")
            )
            logger.info("pilot slot skipped", symbol=slot.symbol, as_of=slot.as_of)
            continue

        outcome = service.synthesise(packet)
        if outcome.outcome not in NO_CALL_OUTCOMES:
            dispatched += 1
        results.append(SlotResult(slot=slot, outcome=outcome))

    return PilotRun(
        cohort_version=COHORT_VERSION,
        started_at=started,
        finished_at=datetime.now(UTC).isoformat(),
        slots_planned=len(slots),
        results=tuple(results),
    )
