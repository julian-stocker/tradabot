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

IFRS_CONCEPTS: Final[dict[str, tuple[str, ...]]] = {
    # DIRECT: the IFRS tag means what the us-gaap tag means.
    "gross_profit": ("GrossProfit",),
    "net_income": ("ProfitLoss",),
    "eps_diluted": ("DilutedEarningsLossPerShare",),
    "cash": ("CashAndCashEquivalents",),
    # NORMALIZABLE: one canonical metric, several permitted presentations.
    "revenue": ("Revenue", "RevenueFromContractsWithCustomers"),
    "operating_cash_flow": ("CashFlowsFromUsedInOperatingActivities",),
    "total_assets": ("Assets",),
    "total_liabilities": ("Liabilities",),
    "equity": ("Equity",),
}
"""IFRS concepts Tradabot is willing to treat as canonical metrics.

Deliberately shorter than the us-gaap map. Phase 13.0 classified five metrics
AMBIGUOUS under IFRS and they are **absent here on purpose**:

``operating_income``
    IAS 1 does not define operating profit, so what sits above the line varies
    by issuer. ``ProfitLossFromOperatingActivities`` exists but is not
    comparable to ``OperatingIncomeLoss`` across filers.
``capex``
    SAP files one combined property, plant, equipment *and intangibles* line.
    Free cash flow derived from it is not like-for-like with a US filer.
``free_cash_flow``
    inherits the capex problem and the IAS 7 choice of where interest sits.
``total_debt``
    IFRS 16 lease liabilities routinely sit inside borrowings, which would move
    the net-cash/net-debt state without anything changing at the company.
``shares_outstanding``
    IFRS offers ``NumberOfSharesIssued``, which **includes treasury shares**.
    Using it as period-end shares outstanding would recreate the Salesforce
    phantom-buyback defect in a new taxonomy. Refusal is the correct answer
    until a real outstanding-share concept is verified per issuer.

Nothing here is wired into ingestion yet; that is the next phase's work.
"""

METRIC_BY_IFRS_CONCEPT: Final[dict[str, str]] = {
    concept: metric for metric, concepts in IFRS_CONCEPTS.items() for concept in concepts
}

IFRS_REFUSED: Final[frozenset[str]] = frozenset(
    {"operating_income", "capex", "free_cash_flow", "total_debt", "shares_outstanding"}
)
"""Metrics that stay UNAVAILABLE for an IFRS filer. A refusal, not a gap."""

METRIC_BY_CONCEPT: Final[dict[str, str]] = {
    concept: metric for metric, concepts in CONCEPTS.items() for concept in concepts
}


def metric_for(taxonomy: str, concept: str) -> str | None:
    """The canonical metric a concept maps to *within its taxonomy*, or None.

    Taxonomy-scoped on purpose. Resolving a concept name against a merged map
    would let an IFRS tag inherit a us-gaap meaning it does not have.
    """
    table = CONCEPTS_BY_TAXONOMY.get(taxonomy)
    if table is None:
        return None
    for metric, concepts in table.items():
        if concept in concepts:
            return metric
    return None


TAXONOMIES: Final[tuple[str, ...]] = ("us-gaap", "dei", "ifrs-full")
"""``dei`` is needed only for the cover-page share count, but omitting it would
silently drop that family rather than reporting it as missing.

``ifrs-full`` is what foreign private issuers file in their 20-F and 40-F. SAP
SE alone publishes 368 IFRS concepts through the same endpoint, with the same
``accn``/``filed``/``form`` provenance, so ingesting it needs no second pipeline
-- only a second concept map."""

CONCEPTS_BY_TAXONOMY: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "us-gaap": CONCEPTS,
    "dei": CONCEPTS,
    "ifrs-full": IFRS_CONCEPTS,
}
"""Which concept map applies to which taxonomy.

Separate maps rather than one merged dictionary, because a concept name can
mean different things in two taxonomies. ``GrossProfit`` happens to agree;
``Assets`` and ``Liabilities`` agree; nothing here relies on that being true in
general, and a concept absent from a taxonomy's map is simply not ingested."""

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
