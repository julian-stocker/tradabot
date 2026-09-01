"""What a trajectory is, and the many ways this system declines to draw one.

A trajectory says *what has already happened* to a company's economics. It is
descriptive arithmetic over filings that were public at the time, and it is
deliberately incapable of saying anything else: there is no field for a
forecast, no field for a verdict, and no threshold anywhere that was chosen by
looking at what a share price did afterwards.

The refusals are the design
---------------------------
Ten years of filings look like a clean series and are not. Measured across the
989-company universe:

* a company's **revenue concept changes** -- the modern
  ``RevenueFromContractsWithCustomer...`` tag begins around 2018, so pinning one
  concept (which the fact store already does) caps most revenue history near
  eight years rather than the eighteen the raw rows suggest;
* **foreign private issuers file annually**, so SAP has *zero* quarterly
  observations and a trailing-twelve-month series cannot exist for it;
* **banks have no comparable revenue at all** -- JPMorgan's series ends in 2014
  and the fact store already marks it abandoned.

Each of those would produce a confident, wrong line through incomparable
numbers. So every one has a named status instead, and a trajectory with a
status other than ``AVAILABLE`` carries no numbers at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SeriesStatus(StrEnum):
    """Why a trajectory exists, or does not. Never a bare "unavailable"."""

    AVAILABLE = "AVAILABLE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    """Fewer observations than the shortest declared window needs."""
    GAPPED_SERIES = "GAPPED_SERIES"
    """The company stopped reporting the metric and resumed. The gap is not
    filled: interpolating one quarter of revenue invents a fact, and a
    trajectory drawn through it would be indistinguishable from a real one."""
    MIXED_BASIS = "MIXED_BASIS"
    """Trailing-twelve-month and fiscal-year points cannot share a line. A
    company that reports quarterly and then annually has two histories, not one
    changing history."""
    CURRENCY_CHANGE = "CURRENCY_CHANGE"
    """The reporting currency changed. Tradabot performs no conversion, so the
    older segment is a different quantity, not an earlier value."""
    TAXONOMY_DISCONTINUITY = "TAXONOMY_DISCONTINUITY"
    """The underlying XBRL concept changed and the fact store does not declare
    the two equivalent."""
    ABANDONED_SERIES = "ABANDONED_SERIES"
    """The company kept filing and stopped reporting this metric. Its last value
    is stale in a way a date alone would not reveal."""
    SECTOR_MODEL_REQUIRED = "SECTOR_MODEL_REQUIRED"
    """A bank's revenue, margin and free cash flow are not the industrial
    quantities of the same name. Refused rather than computed -- a naive
    cross-sector screen over this data returns REITs at 700% net margin."""
    NOT_APPLICABLE = "NOT_APPLICABLE"
    """A fund. It has no operations to have a trajectory."""
    UNAVAILABLE = "UNAVAILABLE"
    """The metric was never reported."""


class SeriesBasis(StrEnum):
    """What one point on the line is. Two bases never share a line."""

    TTM = "TTM"
    """Four contiguous quarters summed. The only basis for a flow metric."""
    ANNUAL = "ANNUAL"
    """One fiscal year. The only basis available to annual-only filers."""
    INSTANT = "INSTANT"
    """A balance-sheet or share-count observation at a period end."""


class Direction(StrEnum):
    """A description of what happened, in each metric's own vocabulary.

    Deliberately not ``IMPROVING``/``DECLINING``. A compressing margin is a
    fact; whether it is bad depends on why, and on a business judgement this
    system does not make. The words describe the number and stop.
    """

    EXPANDING = "EXPANDING"
    COMPRESSING = "COMPRESSING"
    INCREASING = "INCREASING"
    DECREASING = "DECREASING"
    STABLE = "STABLE"


@dataclass(frozen=True, slots=True)
class Observation:
    """One point, and enough provenance to audit it."""

    period_end: str
    value: float
    filed: str | None = None
    """When this observation became public. The reason a trajectory can be
    reconstructed as of a past date rather than merely recomputed today."""
    concept: str | None = None
    unit: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "period_end": self.period_end,
            "value": self.value,
            "filed": self.filed,
            "concept": self.concept,
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class Change:
    """Movement over one declared window, in the metric's natural unit."""

    window: str
    from_value: float
    to_value: float
    from_period: str
    to_period: str
    absolute: float
    """``to - from``. Percentage *points* for a margin, currency for an amount."""
    relative: float | None = None
    """Proportional change. ``None`` where a sign change or a zero base makes it
    meaningless -- earnings that went from negative to positive have no
    percentage growth, only a direction."""
    annualised: float | None = None
    """Compound annual rate over the window. Descriptive arithmetic about a
    period that has already happened, never a rate expected to continue."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "from_value": self.from_value,
            "to_value": self.to_value,
            "from_period": self.from_period,
            "to_period": self.to_period,
            "absolute": self.absolute,
            "relative": self.relative,
            "annualised": self.annualised,
        }


@dataclass(frozen=True, slots=True)
class MetricTrajectory:
    """One metric's history, or one reason there is none."""

    metric: str
    status: SeriesStatus
    basis: SeriesBasis | None = None
    unit: str = ""
    currency: str | None = None
    as_of: str = ""
    current: float | None = None
    observations: tuple[Observation, ...] = ()
    changes: dict[str, Change] = field(default_factory=dict)
    direction: Direction | None = None
    """Set only for the shortest declared window, and only when the metric has
    a declared tolerance. A direction over five years would describe a company
    that no longer exists in the same form."""
    percentile: float | None = None
    """Where the current value sits in this company's *own* history. Midrank,
    the same convention the peer layer uses. Says nothing about other companies
    and nothing about what happens next."""
    history_span: str | None = None
    """The period the percentile is drawn over, always stated, because a
    percentile of eight years and one of two years are different claims."""
    detail: str | None = None

    @property
    def available(self) -> bool:
        return self.status is SeriesStatus.AVAILABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "status": str(self.status),
            "basis": str(self.basis) if self.basis else None,
            "unit": self.unit,
            "currency": self.currency,
            "as_of": self.as_of,
            "current": self.current,
            "observations": len(self.observations),
            "first_period": self.observations[0].period_end if self.observations else None,
            "last_period": self.observations[-1].period_end if self.observations else None,
            "changes": {k: v.as_dict() for k, v in self.changes.items()},
            "direction": str(self.direction) if self.direction else None,
            "percentile": self.percentile,
            "history_span": self.history_span,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class CompanyTrajectory:
    """Every trajectory for one company as of one moment."""

    company_id: int | None
    company_key: str
    as_of: str
    metrics: dict[str, MetricTrajectory] = field(default_factory=dict)
    detail: str | None = None

    @property
    def available(self) -> tuple[MetricTrajectory, ...]:
        return tuple(t for t in self.metrics.values() if t.available)

    def get(self, metric: str) -> MetricTrajectory | None:
        return self.metrics.get(metric)

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "company_key": self.company_key,
            "as_of": self.as_of,
            "metrics": {k: v.as_dict() for k, v in self.metrics.items()},
            "detail": self.detail,
        }
