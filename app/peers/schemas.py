"""What a peer comparison is, and what it refuses to be.

Tradabot has no validated predictive evidence, so this layer describes a
company's *position* among comparable companies and stops there. It produces no
composite score, no ranking of which company is preferable, and no vocabulary
that could be read as a recommendation. "Higher than 78% of comparable peers"
is a fact about a distribution; "better than its peers" is a claim about the
future, and nothing here supports one.

The distinction the whole design rests on
-----------------------------------------
A percentile is only meaningful if the values being ranked are genuinely
comparable. Two failure modes make them not comparable, and both already have
owners elsewhere in the codebase:

* **Units.** A price-to-sales built from a USD price and DKK revenue is a unit
  error, refused by :func:`~app.instruments.registry.valuation_allowed` and,
  since phase 13.7, by :class:`~app.advisor.service.MarketIdentity` inside the
  report itself. This layer consumes those already-gated figures and never
  rebuilds a ratio the Advisor declined to produce.
* **Semantics.** An operating margin means something for a manufacturer and
  nothing for a bank, which is why the Advisor refuses generic analysis for
  financial-sector issuers. This layer refuses the whole comparison for them
  rather than inventing a bank model.

So a metric reaches a percentile only if the Advisor was willing to compute it
for the subject *and* for enough peers. Everything else is stated as absent
with a reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PeerBasis(StrEnum):
    """How the peer group was defined. Always reported, never implicit."""

    SIC_4 = "SIC_4"
    """The SEC's four-digit industry classification -- the most specific
    grouping available from data Tradabot already holds."""
    SIC_3 = "SIC_3"
    """The three-digit industry group, one level up the same official
    hierarchy. Used only when the four-digit group is too small."""


class PeerOutcome(StrEnum):
    """Why a comparison did or did not happen."""

    AVAILABLE = "AVAILABLE"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    """Fewer comparable companies than the pre-declared minimum. The peer
    definition is never widened past the declared hierarchy to escape this."""
    NO_CLASSIFICATION = "NO_CLASSIFICATION"
    """The company carries no SIC code, so no industry peer set can be formed.
    True of funds and of issuers Tradabot could not verify against EDGAR."""
    SECTOR_MODEL_REQUIRED = "SECTOR_MODEL_REQUIRED"
    """A financial-sector issuer. Deposits and borrowings are the business
    rather than a way of funding it, so the generic metrics this layer compares
    describe nothing -- the same refusal the Advisor already makes."""
    NO_COMPARABLE_METRIC = "NO_COMPARABLE_METRIC"
    """A peer group exists but no single metric cleared the minimum."""


@dataclass(frozen=True, slots=True)
class PeerMember:
    """One company considered for a peer group, included or not."""

    company_id: int
    symbol: str
    name: str
    included: bool
    reason: str | None = None
    """Why this company was excluded, when it was. ``None`` when included."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "symbol": self.symbol,
            "name": self.name,
            "included": self.included,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PeerGroup:
    """The comparison universe, stated in full.

    Carried on every result so the set is inspectable. A percentile whose
    universe is hidden is a number the reader cannot argue with, which is the
    opposite of what this layer is for.
    """

    basis: PeerBasis
    code: str
    label: str
    as_of: str
    members: tuple[PeerMember, ...]
    subject_taxonomy: str | None = None
    peer_taxonomy: str | None = None
    """The accounting taxonomy most of the peers file under.

    Recorded because an industry group is assembled from what a company *does*,
    not from how it reports. SAP's ifrs-full gross margin ranked against
    fifty-four us-gaap filers is a defensible comparison of two percentages and
    is not quite a like-for-like one, since the taxonomies draw the cost-of-
    revenue line differently. Stating it is the honest middle ground between
    refusing international comparison entirely and pretending the difference is
    not there."""

    @property
    def mixed_taxonomy(self) -> bool:
        """Whether the subject files under a different taxonomy than its peers."""
        return (
            self.subject_taxonomy is not None
            and self.peer_taxonomy is not None
            and self.subject_taxonomy != self.peer_taxonomy
        )

    @property
    def included(self) -> tuple[PeerMember, ...]:
        return tuple(m for m in self.members if m.included)

    @property
    def excluded(self) -> tuple[PeerMember, ...]:
        return tuple(m for m in self.members if not m.included)

    @property
    def size(self) -> int:
        """Peers available to compare against, excluding the subject itself."""
        return len(self.included)

    def as_dict(self) -> dict[str, Any]:
        return {
            "basis": str(self.basis),
            "code": self.code,
            "label": self.label,
            "as_of": self.as_of,
            "size": self.size,
            "subject_taxonomy": self.subject_taxonomy,
            "peer_taxonomy": self.peer_taxonomy,
            "mixed_taxonomy": self.mixed_taxonomy,
            "members": [m.as_dict() for m in self.members],
        }


@dataclass(frozen=True, slots=True)
class MetricComparison:
    """Where one company sits among its peers on one metric.

    ``percentile`` is the share of peers the company's value exceeds, by the
    midrank convention in :mod:`app.peers.statistics`. It is a position, not a
    grade: a high price-to-earnings percentile means the shares carry a higher
    multiple than most peers, and says nothing about what happens next.
    """

    metric: str
    label: str
    value: float
    percentile: float
    median: float
    p25: float
    p75: float
    peer_count: int
    unit: str
    """``PERCENT`` or ``MULTIPLE`` -- decides rendering, never meaning."""
    higher_is_not_better: bool = True
    """Always true, and named to be read. No metric here carries a direction:
    a high margin percentile is not an endorsement and a high valuation
    percentile is not a warning. Kept as a field so any future consumer that
    wants to sort by 'good' has to confront that the data does not support it."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "value": self.value,
            "percentile": self.percentile,
            "median": self.median,
            "p25": self.p25,
            "p75": self.p75,
            "peer_count": self.peer_count,
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class MetricRefusal:
    """A metric that was considered and not compared, and why."""

    metric: str
    label: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "label": self.label, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class PeerComparison:
    """One company's cross-sectional position, or an explicit refusal."""

    company_id: int | None
    symbol: str
    as_of: str
    outcome: PeerOutcome
    group: PeerGroup | None = None
    comparisons: tuple[MetricComparison, ...] = ()
    refusals: tuple[MetricRefusal, ...] = ()
    detail: str | None = None

    @property
    def available(self) -> bool:
        return self.outcome is PeerOutcome.AVAILABLE and bool(self.comparisons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of": self.as_of,
            "outcome": str(self.outcome),
            "detail": self.detail,
            "group": self.group.as_dict() if self.group else None,
            "comparisons": [c.as_dict() for c in self.comparisons],
            "refusals": [r.as_dict() for r in self.refusals],
        }


class MetricFamily(StrEnum):
    """What kind of statement a metric supports.

    Separate from :attr:`MetricSpec.unit`, which decides rendering. Revenue
    growth and a gross margin are both percentages and are not the same kind of
    claim, so a sentence generated from the unit called growth a margin.
    """

    GROWTH = "GROWTH"
    PROFITABILITY = "PROFITABILITY"
    VALUATION = "VALUATION"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One comparable dimension: where the value comes from and how it reads."""

    key: str
    label: str
    family: MetricFamily
    unit: str
    section: str | None
    """Advisor section the value is read from, or ``None`` when the peer layer
    derives it (revenue growth is the only such case, and it is a same-company
    year-on-year ratio with no cross-entity or cross-currency exposure)."""
    field: str
    positive_only: bool = False
    """Whether a non-positive value makes the metric undefined rather than low.
    A price-to-earnings on negative earnings is not a cheap company."""


V1_METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        "revenue_growth_ttm_yoy",
        "Revenue growth",
        MetricFamily.GROWTH,
        "PERCENT",
        None,
        "revenue_growth_ttm_yoy",
    ),
    MetricSpec(
        "gross_margin",
        "Gross margin",
        MetricFamily.PROFITABILITY,
        "PERCENT",
        "PROFITABILITY",
        "gross_margin",
    ),
    MetricSpec(
        "operating_margin",
        "Operating margin",
        MetricFamily.PROFITABILITY,
        "PERCENT",
        "PROFITABILITY",
        "operating_margin",
    ),
    MetricSpec(
        "fcf_margin",
        "FCF margin",
        MetricFamily.PROFITABILITY,
        "PERCENT",
        "CASH GENERATION",
        "fcf_margin",
    ),
    MetricSpec(
        "pe_ttm",
        "P/E",
        MetricFamily.VALUATION,
        "MULTIPLE",
        "VALUATION",
        "pe_ttm",
        positive_only=True,
    ),
    MetricSpec(
        "ps_ttm",
        "P/S",
        MetricFamily.VALUATION,
        "MULTIPLE",
        "VALUATION",
        "ps_ttm",
        positive_only=True,
    ),
    MetricSpec(
        "p_fcf",
        "P/FCF",
        MetricFamily.VALUATION,
        "MULTIPLE",
        "VALUATION",
        "p_fcf",
        positive_only=True,
    ),
)
"""The V1 set: one growth dimension, three margins, three multiples.

Every one is **dimensionless**, which is not a coincidence. A margin and a
multiple are ratios of two figures in one currency, so they survive a peer group
containing issuers that report in EUR, CAD and DKK. Absolute monetary
comparisons -- revenue, net debt, market capitalisation -- would need a
same-currency peer set, and Tradabot performs no conversion.

Deliberately absent from V1:

``eps_growth``
    Per-share figures move with the share count, and the Advisor already flags
    ``SPLIT_ADJUSTMENT_REQUIRED`` where the as-reported count is unreliable.
``net_debt`` / leverage
    Refused entirely for IFRS filers, because borrowing concepts vary by filer
    and routinely include lease liabilities. A peer group mixing taxonomies
    would compare figures that are not the same measurement.
``dilution``
    Categorical rather than continuous, and refused for IFRS filers, which
    report shares issued rather than shares outstanding.
"""
