"""The gate that refuses a call before it is made.

Two caps, for two different mistakes.

The **monthly cap** bounds money. It is checked against what a request could
cost at its ceiling, not what it will probably cost: a request is refused if
``estimate > remaining``, where the estimate charges the full configured output
allowance whether or not the model uses it. Estimating optimistically and
hoping the response is short is how a cap gets exceeded by the one call that
was not short.

The **per-run cap** bounds blast radius, and is the more important of the two.
A bug that loops over the universe at $0.017 a call takes $16.80 and four
minutes to become expensive, which is faster than anybody reads a log. The
default is one call per invocation. Raising it requires passing a number, and
the number has a ceiling.

Currency
--------
USD, because that is what the provider publishes and bills in, and the cap must
be denominated in the same unit as the invoice it is bounding.

**The authoritative guard is $10.00 per calendar month.** It is not a euro cap
expressed in dollars, and no safety property here rests on an exchange rate. An
earlier draft argued the dollar figure was "at most €10 for any EUR/USD at or
above parity"; that is a claim about the future value of a currency pair, which
is exactly the kind of thing this project refuses to assert anywhere else, and
it has no business load-bearing in a spending cap. The number was chosen
conservatively around the €10 design target of Phase 18.0 and stands on its own
in dollars.

No live rate is fetched. A budget that needs a network call has a failure mode
where nothing can be spent at all, and one that silently caches a rate is
worse. If the euro-denominated intent ever needs restating, this is a single
constant in a single file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

from app.synthesis.ledger import SynthesisLedger
from app.synthesis.pricing import ModelPricing

BUDGET_CURRENCY: Final = "USD"
MONTHLY_CAP_USD: Final = Decimal("10.00")
CAP_BASIS: Final = (
    "Authoritative guard: USD 10.00 per calendar month, the provider's own "
    "billing currency. Chosen conservatively against the EUR 10 design target "
    "of Phase 18.0. No FX rate is fetched, assumed, or relied upon."
)

DEFAULT_PER_RUN_CALLS: Final = 1
MAX_PER_RUN_CALLS: Final = 24
"""The ceiling on the ceiling. ``run_pilot`` may ask for a bounded batch; it may
not ask for an unbounded one, and 24 is the size of the frozen cohort."""


class BudgetDecision(StrEnum):
    ALLOWED = "ALLOWED"
    REFUSED_MONTHLY_CAP = "REFUSED_MONTHLY_CAP"
    REFUSED_RUN_CAP = "REFUSED_RUN_CAP"
    REFUSED_NO_PRICING = "REFUSED_NO_PRICING"


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Worst-case cost of one request, computed before it is sent."""

    input_tokens: int
    max_output_tokens: int
    input_usd: Decimal
    output_usd: Decimal

    @property
    def total_usd(self) -> Decimal:
        return self.input_usd + self.output_usd

    @property
    def display(self) -> str:
        return f"${self.total_usd.quantize(Decimal('0.000001'), ROUND_HALF_UP)}"


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """Whether one request may proceed, and the arithmetic behind the answer."""

    decision: BudgetDecision
    estimate: CostEstimate
    month: str
    spent_usd: Decimal
    remaining_usd: Decimal
    calls_made: int
    call_cap: int
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is BudgetDecision.ALLOWED


def current_month(now: datetime | None = None) -> str:
    """The calendar month a call belongs to, ``YYYY-MM``, in UTC.

    An explicit calendar month, not a rolling thirty days. A rolling window
    means the answer to "how much is left" depends on when you ask and can go
    up while you are not looking, which is not a property anybody wants in a
    spending cap.
    """
    return (now or datetime.now(UTC)).strftime("%Y-%m")


class CostGuard:
    """Refuses requests that would breach either cap. Never makes a call."""

    def __init__(
        self,
        *,
        ledger: SynthesisLedger,
        pricing: ModelPricing,
        monthly_cap_usd: Decimal = MONTHLY_CAP_USD,
        per_run_calls: int = DEFAULT_PER_RUN_CALLS,
    ) -> None:
        if per_run_calls < 1 or per_run_calls > MAX_PER_RUN_CALLS:
            raise ValueError(f"per_run_calls must be 1..{MAX_PER_RUN_CALLS}, got {per_run_calls}")
        self._ledger = ledger
        self._pricing = pricing
        self._cap = monthly_cap_usd
        self._call_cap = per_run_calls
        self._calls_made = 0

    @property
    def call_cap(self) -> int:
        return self._call_cap

    @property
    def calls_made(self) -> int:
        return self._calls_made

    def estimate(self, *, input_tokens: int, max_output_tokens: int) -> CostEstimate:
        """Worst case: every input token charged, every output token allowed.

        Output is charged at the *configured ceiling* rather than an expected
        length. The ceiling is what the provider is authorised to generate, so
        it is what the budget must be able to afford.
        """
        return CostEstimate(
            input_tokens=input_tokens,
            max_output_tokens=max_output_tokens,
            input_usd=self._pricing.cost_usd(input_tokens=input_tokens, output_tokens=0),
            output_usd=self._pricing.cost_usd(input_tokens=0, output_tokens=max_output_tokens),
        )

    def check(
        self,
        *,
        input_tokens: int,
        max_output_tokens: int,
        now: datetime | None = None,
    ) -> BudgetVerdict:
        """May this request be sent? Asked before every dispatch."""
        month = current_month(now)
        estimate = self.estimate(input_tokens=input_tokens, max_output_tokens=max_output_tokens)
        spent = self._ledger.month_spend_usd(month)
        remaining = self._cap - spent

        if self._calls_made >= self._call_cap:
            return self._verdict(
                BudgetDecision.REFUSED_RUN_CAP,
                estimate,
                month,
                spent,
                remaining,
                f"this run has made {self._calls_made} of {self._call_cap} permitted calls",
            )
        if estimate.total_usd > remaining:
            return self._verdict(
                BudgetDecision.REFUSED_MONTHLY_CAP,
                estimate,
                month,
                spent,
                remaining,
                f"{estimate.display} exceeds ${remaining} remaining of ${self._cap} for {month}",
            )
        return self._verdict(BudgetDecision.ALLOWED, estimate, month, spent, remaining, "")

    def _verdict(
        self,
        decision: BudgetDecision,
        estimate: CostEstimate,
        month: str,
        spent: Decimal,
        remaining: Decimal,
        detail: str,
    ) -> BudgetVerdict:
        return BudgetVerdict(
            decision=decision,
            estimate=estimate,
            month=month,
            spent_usd=spent,
            remaining_usd=remaining,
            calls_made=self._calls_made,
            call_cap=self._call_cap,
            detail=detail,
        )

    def note_dispatch(self) -> None:
        """Count a call that actually left the machine.

        Called by the adapter's caller after the guard allows a request and
        before the response is known. Counting on dispatch rather than on
        success is deliberate: ten timeouts are ten calls, and a run cap that
        only counted successes would not bound anything under the failure it
        exists to bound.
        """
        self._calls_made += 1
