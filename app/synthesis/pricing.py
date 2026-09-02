"""What a provider charges, checked against its own published page.

Kept apart from the design documents on purpose. ``docs/research-synthesis.md``
describes a boundary that does not change; a price changes without warning, and
a number copied into prose is a number nobody will ever re-check. Everything
here carries the date it was read and the URL it was read from, so a stale
figure is visible rather than merely wrong.

Money is :class:`~decimal.Decimal`, never ``float``. A budget that refuses a
call is an accounting decision, and binary floating point is the wrong tool for
one -- ``0.1 + 0.2`` deciding whether a request is affordable is not a defect
anybody would find twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

PRICING_CHECKED: Final = "2026-09-02"
"""The date every price below was read from its official source."""

MTOK: Final = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """One model's published rates and the properties the pilot depends on.

    ``structured_outputs`` and ``documented_temperature`` are recorded because
    they were *checked*, not assumed. The second is false for the selected
    model: its page lists the features it supports and sampling parameters are
    not among them, so the adapter does not send one.
    """

    provider: str
    model: str
    input_usd_per_mtok: Decimal
    cached_input_usd_per_mtok: Decimal | None
    output_usd_per_mtok: Decimal
    context_tokens: int
    max_output_tokens: int
    structured_outputs: bool
    documented_temperature: bool
    knowledge_cutoff: str
    source_url: str
    checked: str = PRICING_CHECKED

    def cost_usd(self, *, input_tokens: int, output_tokens: int) -> Decimal:
        """Exact cost of one call at these rates, in USD.

        No rounding here. The ledger stores the full quotient and rounding
        happens once, at display, so twenty-four sub-cent calls do not each
        round to zero and sum to nothing.
        """
        return (
            Decimal(input_tokens) * self.input_usd_per_mtok
            + Decimal(output_tokens) * self.output_usd_per_mtok
        ) / MTOK


_OPENAI_PRICING: Final = "https://developers.openai.com/api/docs/pricing"

CATALOGUE: Final[dict[str, ModelPricing]] = {
    # The three current-generation OpenAI models that could plausibly serve this
    # task, recorded so the size decision can be re-argued against real numbers
    # rather than re-researched.
    "gpt-5.6-luna": ModelPricing(
        provider="openai",
        model="gpt-5.6-luna",
        input_usd_per_mtok=Decimal("0.20"),
        cached_input_usd_per_mtok=Decimal("0.02"),
        output_usd_per_mtok=Decimal("1.20"),
        context_tokens=1_050_000,
        max_output_tokens=128_000,
        structured_outputs=True,
        documented_temperature=False,
        knowledge_cutoff="2026-02-16",
        source_url=_OPENAI_PRICING,
    ),
    "gpt-5.6-terra": ModelPricing(
        provider="openai",
        model="gpt-5.6-terra",
        input_usd_per_mtok=Decimal("2.00"),
        cached_input_usd_per_mtok=Decimal("0.20"),
        output_usd_per_mtok=Decimal("12.00"),
        context_tokens=1_050_000,
        max_output_tokens=128_000,
        structured_outputs=True,
        documented_temperature=False,
        knowledge_cutoff="2026-02-16",
        source_url=_OPENAI_PRICING,
    ),
    "gpt-5.6-sol": ModelPricing(
        provider="openai",
        model="gpt-5.6-sol",
        input_usd_per_mtok=Decimal("4.00"),
        cached_input_usd_per_mtok=Decimal("0.40"),
        output_usd_per_mtok=Decimal("20.00"),
        context_tokens=1_050_000,
        max_output_tokens=128_000,
        structured_outputs=True,
        documented_temperature=False,
        knowledge_cutoff="2026-02-16",
        source_url=_OPENAI_PRICING,
    ),
}

PILOT_PROVIDER: Final = "openai"
PILOT_MODEL: Final = "gpt-5.6-terra"
"""The one model this pilot uses.

Not the cheapest and not the largest. The packet is small enough that the
price difference across the three above is $0.36 over the whole cohort, so cost
does not discriminate between them; what discriminates is the failure this
experiment exists to detect. The question being asked is whether *a model* can
read bounded evidence without importing what it already knows about Apple, and
a null result from the cheapest tier would not answer it. Establishing that the
mid tier can, and then testing downward, is the order that yields an answer at
each step. See ``docs/synthesis-provider.md``.
"""


def pricing_for(model: str) -> ModelPricing:
    """Rates for a model, or a refusal naming what is known.

    An unknown model is an error rather than a default, because the fallback
    for "I do not know what this costs" cannot be "assume it is cheap".
    """
    try:
        return CATALOGUE[model]
    except KeyError:
        known = ", ".join(sorted(CATALOGUE))
        raise KeyError(f"no checked pricing for {model!r}; known: {known}") from None
