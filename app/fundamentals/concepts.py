"""Which SEC XBRL concepts Tradabot ingests, and what each one means.

One map, one format
-------------------
This is the *only* declaration of the ingested concept set. The fact store it
produces is the same file :class:`~app.advisor.facts.FactStore` reads, with the
same columns, because a second SEC fact format is a second thing to keep
correct and the two would drift.

Aliases are explicit, substitution is not
-----------------------------------------
Several metrics have more than one acceptable concept -- a company that reports
``Revenues`` and one that reports
``RevenueFromContractWithCustomerExcludingAssessedTax`` are both reporting
revenue. Those are listed here as aliases of one metric.

What this map deliberately does **not** do is substitute incompatible concepts.
Nothing here quietly promotes, say, gross profit into revenue when revenue is
absent: an unavailable metric stays unavailable, and the Advisor renders it as
such. Choosing *between* aliases is a separate decision made downstream by
:func:`~app.advisor.facts._choose_concept`, which prefers whatever the most
recent filing used, so ingestion never has to guess.
"""

from __future__ import annotations

from typing import Final

CONCEPTS: Final[dict[str, tuple[str, ...]]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss",),
    "eps_diluted": ("EarningsPerShareDiluted",),
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "total_assets": ("Assets",),
    "total_liabilities": ("Liabilities",),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "short_term_debt": ("ShortTermBorrowings", "DebtCurrent"),
    "long_term_debt": ("LongTermDebtNoncurrent", "LongTermDebt"),
    "shares_diluted": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    "shares_outstanding": (
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
    ),
}
"""Metric -> accepted XBRL concepts, in declared preference order.

``shares_outstanding`` intentionally spans two *semantic families*: the us-gaap
period-end count and the dei cover-page count. They are ingested together and
separated downstream by :class:`~app.advisor.facts.ShareFamily`, because a
comparison across families is what produced Salesforce's phantom -12.6%
"buyback". Ingestion records what was filed; it does not decide comparability.
"""

METRIC_BY_CONCEPT: Final[dict[str, str]] = {
    concept: metric for metric, concepts in CONCEPTS.items() for concept in concepts
}

TAXONOMIES: Final[tuple[str, ...]] = ("us-gaap", "dei")
"""``dei`` is needed only for the cover-page share count, but omitting it would
silently drop that family rather than reporting it as missing."""

FACT_COLUMNS: Final[tuple[str, ...]] = (
    "symbol",
    "cik",
    "metric",
    "concept",
    "taxonomy",
    "unit",
    "value",
    "form",
    "filed",
    "accepted",
    "accession",
    "fy",
    "fp",
    "period_start",
    "period_end",
)
"""The persisted schema.

``filed`` is the SEC's filing date and remains the point-in-time key, unchanged
from the dataset every Advisor result so far was validated against. ``accepted``
is the acceptance timestamp from the submissions API -- the moment the document
actually became public, which can be after the close of the ``filed`` session.
It is recorded as provenance now; changing the visibility key to use it would
alter validated Advisor behaviour and is not done as a side effect of an
ingestion change.
"""

SCHEMA_VERSION: Final = "sec-facts-1"
"""Bumped only when :data:`FACT_COLUMNS` changes meaning, so a stale file on
disk can be recognised rather than misread."""
