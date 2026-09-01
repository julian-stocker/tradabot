"""Putting a disclosed figure in proportion, or saying why it cannot be.

A number alone is not context. *"Revenue of $46.7 billion"* means one thing at
NVIDIA and something else entirely at a company whose whole year is smaller than
that. So a disclosed amount is expressed against the same company's own
canonical history -- and only when four things hold at once.

The four conditions
-------------------
1. **The figure is an absolute amount.** Percentages and per-share figures have
   no magnitude to scale; a margin is already a ratio.
2. **The periods are commensurable.** A flow over a quarter can be read as a
   share of a trailing year. A balance-sheet instant cannot: dividing a stock by
   a flow produces a number with no meaning, and it would look like a
   percentage.
3. **The currencies match, exactly.** Phase 13's rule, unchanged and not
   softened here: Tradabot performs no conversion, so a figure in one currency
   is never divided by a comparator in another. ``UNKNOWN`` never matches
   anything, including itself.
4. **The comparator was on file at the time.** ``as_of`` is the event's own
   publication moment, so the proportion is the one a reader could have
   computed that day. Using the latest figure available now would answer a
   question about August with September's filing and give no sign of it.

Any of them failing produces a :class:`~...schemas.ContextStatus` naming which,
never a ratio with a caveat attached.

Not a judgement
---------------
The output is a share of a comparator and nothing more. There is no threshold
above which a figure becomes "large", because that word would be doing work the
arithmetic does not support -- and no field in which a direction could be
recorded.
"""

from __future__ import annotations

from typing import Any, Final

from app.research_intelligence.facts import Unit
from app.research_intelligence.schemas import (
    UNKNOWN_CURRENCY,
    ContextStatus,
    FiscalPeriod,
    MagnitudeContext,
    ResearchFact,
)

COMPARATOR: Final = "trailing twelve months, same metric"

FLOW_PERIODS: Final[frozenset[FiscalPeriod]] = frozenset(
    {
        FiscalPeriod.QUARTER,
        FiscalPeriod.YEAR_TO_DATE,
        FiscalPeriod.YEAR,
        FiscalPeriod.TRAILING_TWELVE_MONTHS,
    }
)
"""Periods measuring activity over a span. ``INSTANT`` is excluded because it
measures a position at a moment, and the two do not divide."""

_PER_SHARE = "/shares"


def _comparator_currency(result: Any) -> str:
    """The currency the canonical comparator is denominated in.

    Read from the XBRL unit the fact was filed with -- ``USD``, ``DKK`` --
    rather than assumed, because the same store holds companies reporting in a
    dozen currencies and the unit is the only field that says which.
    """
    for provenance in getattr(result, "provenance", ()):
        unit = str(getattr(provenance, "unit", ""))
        if unit and _PER_SHARE not in unit:
            return unit
    return UNKNOWN_CURRENCY


def magnitude(
    fact: ResearchFact,
    *,
    store: Any,
    symbol: str,
    as_of: str,
) -> MagnitudeContext:
    """Express ``fact`` as a share of the company's own trailing-year figure.

    Args:
        store: a :class:`~app.advisor.facts.FactStore`. Injected, so this
            module holds no data of its own and no test needs one.
        symbol: the company key the fact store is indexed by. A *company* key,
            never a listing symbol -- the Phase 13 defect was reading one
            company's facts under another's listing.
        as_of: the event's publication moment. Trimmed to a date, because the
            fact store's point-in-time filter is day-grained.
    """
    if fact.unit != Unit.CURRENCY:
        return MagnitudeContext(
            status=ContextStatus.NO_ESTABLISHED_AMOUNT,
            metric=fact.metric,
            detail=f"{fact.unit.lower().replace('_', ' ')} carries no absolute magnitude",
        )
    if fact.currency == UNKNOWN_CURRENCY:
        return MagnitudeContext(
            status=ContextStatus.UNKNOWN_CURRENCY,
            metric=fact.metric,
            detail="the document did not establish a currency",
        )
    if fact.fiscal_period not in FLOW_PERIODS:
        return MagnitudeContext(
            status=ContextStatus.INCOMPATIBLE_PERIOD,
            metric=fact.metric,
            detail=f"{fact.fiscal_period} is not a flow over a period",
        )

    result = store.ttm(symbol, fact.metric, as_of[:10])
    comparator_value = getattr(result, "value", None)
    if comparator_value in (None, 0):
        return MagnitudeContext(
            status=ContextStatus.NO_PIT_COMPARATOR,
            metric=fact.metric,
            comparator=COMPARATOR,
            detail=f"no usable {fact.metric} on file as of {as_of[:10]} "
            f"({getattr(result, 'status', 'MISSING')})",
        )

    currency = _comparator_currency(result)
    if currency != fact.currency:
        return MagnitudeContext(
            status=ContextStatus.CURRENCY_MISMATCH,
            metric=fact.metric,
            comparator=COMPARATOR,
            detail=f"document states {fact.currency}, canonical history is {currency}; "
            f"Tradabot performs no conversion",
        )

    return MagnitudeContext(
        status=ContextStatus.COMPUTED,
        metric=fact.metric,
        comparator=COMPARATOR,
        comparator_value=float(comparator_value),
        ratio=float(fact.value) / float(comparator_value),
        detail=f"{fact.currency} {fact.value:,.0f} against {fact.metric} of "
        f"{float(comparator_value):,.0f} known at {as_of[:10]}",
    )
