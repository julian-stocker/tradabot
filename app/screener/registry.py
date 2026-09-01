"""Every dimension a screen may filter on, declared once.

A metric appears here only if some existing service already owns it and already
knows when to refuse it. The screener computes no trailing-twelve-month sum, no
margin, no compound rate, no percentile and no valuation ratio -- it reads them
off the layer that does, and a structural test forbids the formulas appearing
here at all. A second implementation of operating margin would agree with the
first for about a quarter and then quietly stop.

What is deliberately absent
---------------------------
**Peer percentiles.** They are safe and they are validated, and one company's
peer position costs an ``AdvisorReport`` for every member of its group.
Measured at **613 ms per company -- ten minutes across the universe**. Making
that interactive means materialising a second cross-sectional table, which is a
larger decision than this phase should take on its own. Deferred, with the
number, rather than shipped slow or shipped approximate.

**Absolute free cash flow, EPS and net debt as trajectories.** Excluded in
Phase 16 for reasons that have not changed: scale dominance, sign crossings and
an unvalidated denominator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from app.screener.schemas import Cost, Operator, Scope

NUMERIC: Final[tuple[Operator, ...]] = (
    Operator.GTE,
    Operator.LTE,
    Operator.GT,
    Operator.LT,
)
EXACT: Final[tuple[Operator, ...]] = (Operator.EQ, Operator.NEQ)


@dataclass(frozen=True, slots=True)
class ScreenMetric:
    """One screenable dimension and the rules that keep it honest."""

    key: str
    label: str
    unit: str
    scope: Scope
    cost: Cost
    operators: tuple[Operator, ...]
    source: str
    """The service that owns the number. Named so a reader can find the
    definition rather than infer it from the metric's name."""
    financial_ok: bool = False
    """Whether a financial-sector issuer may be tested on it. Almost nothing is:
    a bank's revenue and margins are not the industrial quantities of the same
    name. Share count and market position are, and are marked so."""
    description: str = ""

    def supports(self, operator: Operator) -> bool:
        return operator in self.operators


def _history(
    key: str, label: str, unit: str, description: str, *, financial: bool = False
) -> ScreenMetric:
    return ScreenMetric(
        key=key,
        label=label,
        unit=unit,
        scope=Scope.COMPANY,
        cost=Cost.HISTORY,
        operators=NUMERIC,
        source="CompanyHistoryService",
        financial_ok=financial,
        description=description,
    )


_TRAJECTORY_METRICS = ("revenue", "gross_margin", "operating_margin", "fcf_margin")

METRICS: Final[dict[str, ScreenMetric]] = {}


def _register(metric: ScreenMetric) -> None:
    METRICS[metric.key] = metric


# --- current level, read off Phase 16 trajectories -------------------------
for _key, _label, _unit in (
    ("revenue", "Revenue", "CURRENCY"),
    ("gross_margin", "Gross margin", "PERCENT"),
    ("operating_margin", "Operating margin", "PERCENT"),
    ("fcf_margin", "FCF margin", "PERCENT"),
):
    _register(_history(_key, _label, _unit, f"Current {_label.lower()}, trailing twelve months"))
_register(
    _history(
        "share_count",
        "Shares outstanding",
        "CURRENCY",
        "Current shares outstanding",
        financial=True,
    )
)

# --- movement over a declared window ---------------------------------------
for _key, _label in (
    ("revenue", "Revenue"),
    ("gross_margin", "Gross margin"),
    ("operating_margin", "Operating margin"),
    ("fcf_margin", "FCF margin"),
):
    for _window in ("1y", "3y", "5y"):
        if _key == "revenue":
            _register(
                _history(
                    f"revenue_cagr_{_window}",
                    f"Revenue CAGR {_window}",
                    "PERCENT",
                    f"Compound annual revenue growth over {_window}. Descriptive "
                    f"arithmetic about a period that has already happened.",
                )
            )
        else:
            _register(
                _history(
                    f"{_key}_change_{_window}",
                    f"{_label} change {_window}",
                    "PERCENTAGE_POINTS",
                    f"Movement in {_label.lower()} over {_window}, in percentage points.",
                )
            )
for _window in ("1y", "3y", "5y"):
    _register(
        _history(
            f"share_count_change_{_window}",
            f"Share count change {_window}",
            "PERCENT",
            f"Proportional change in shares outstanding over {_window}.",
            financial=True,
        )
    )

# --- position within the company's own history -----------------------------
for _key, _label in (
    ("gross_margin", "Gross margin"),
    ("operating_margin", "Operating margin"),
    ("fcf_margin", "FCF margin"),
):
    _register(
        _history(
            f"{_key}_own_percentile",
            f"{_label} own-history percentile",
            "PERCENTILE",
            f"Where the current {_label.lower()} sits in this company's own recorded "
            f"range. Not a comparison to other companies.",
        )
    )
_register(
    _history(
        "share_count_own_percentile",
        "Share count own-history percentile",
        "PERCENTILE",
        "Where the current share count sits in this company's own range.",
        financial=True,
    )
)
# Revenue's own-history percentile is deliberately absent: measured across the
# universe its median is 98 and it sits at or above the 95th for 60% of
# companies, because an absolute quantity that trends upward is nearly always at
# its own record. It would filter almost nothing.

# --- valuation and market, from the Advisor --------------------------------
for _key, _label, _unit, _fin in (
    # A bank's *earnings* are a comparable quantity, so P/E is meaningful for
    # one. Its revenue and free cash flow are not: Bank of America was measured
    # yielding a price-to-sales ratio of 3.58x against a "sales" figure that
    # does not mean what it means for an industrial company.
    ("pe_ttm", "P/E", "MULTIPLE", True),
    ("ps_ttm", "P/S", "MULTIPLE", False),
    ("p_fcf", "P/FCF", "MULTIPLE", False),
):
    _register(
        ScreenMetric(
            _key,
            _label,
            _unit,
            Scope.LISTING,
            Cost.ADVISOR,
            NUMERIC,
            "AdvisorService",
            financial_ok=_fin,
            description=f"{_label} on trailing twelve months. Refused where the listing's "
            f"price and the company's filings are in different currencies.",
        )
    )
for _key, _label, _unit, _fin in (
    ("relative_strength_252d", "1Y relative strength", "PERCENT", True),
    ("distance_from_ma200", "Distance from 200-day average", "PERCENT", True),
    ("drawdown_from_252d_high", "Below 52-week high", "PERCENT", True),
):
    _register(
        ScreenMetric(
            _key,
            _label,
            _unit,
            Scope.LISTING,
            Cost.ADVISOR,
            NUMERIC,
            "AdvisorService",
            financial_ok=_fin,
            description=f"{_label} for this listing's own price series. Never "
            f"borrowed from another venue.",
        )
    )

# --- classification and disclosure -----------------------------------------
_register(
    ScreenMetric(
        "sic",
        "SIC code",
        "TEXT",
        Scope.COMPANY,
        Cost.REGISTRY,
        (*EXACT, Operator.GTE, Operator.LTE),
        "InstrumentRegistry",
        financial_ok=True,
        description="The SEC's own classification. Prefix matching: 73 selects all of SIC 73xx.",
    )
)
_register(
    ScreenMetric(
        "has_current_development",
        "Has a current SEC development",
        "BOOL",
        Scope.COMPANY,
        Cost.DEVELOPMENTS,
        EXACT,
        "CurrentDevelopmentsService",
        financial_ok=True,
        description="Whether a classified SEC filing is current under Phase 15 "
        "freshness. Says nothing about what it implies.",
    )
)
_register(
    ScreenMetric(
        "development_kind",
        "Current development kind",
        "TEXT",
        Scope.COMPANY,
        Cost.DEVELOPMENTS,
        EXACT,
        "CurrentDevelopmentsService",
        financial_ok=True,
        description="An SEC event kind currently on file, e.g. MANAGEMENT_CHANGE. "
        "A category the registrant filed under, not a signal.",
    )
)
_register(
    ScreenMetric(
        "development_materiality",
        "Current development materiality",
        "TEXT",
        Scope.COMPANY,
        Cost.DEVELOPMENTS,
        EXACT,
        "CurrentDevelopmentsService",
        financial_ok=True,
        description="Highest attention band among current filings: ROUTINE, "
        "NOTABLE, SIGNIFICANT, CRITICAL. Attention, never direction.",
    )
)


def get(key: str) -> ScreenMetric | None:
    return METRICS.get(key)


def keys() -> tuple[str, ...]:
    return tuple(sorted(METRICS))


def by_cost() -> dict[Cost, tuple[str, ...]]:
    out: dict[Cost, list[str]] = {}
    for key, metric in METRICS.items():
        out.setdefault(metric.cost, []).append(key)
    return {cost: tuple(sorted(names)) for cost, names in out.items()}


def describe() -> list[dict[str, Any]]:
    return [
        {
            "key": m.key,
            "label": m.label,
            "unit": m.unit,
            "scope": str(m.scope),
            "cost": str(m.cost),
            "source": m.source,
            "operators": [str(o) for o in m.operators],
            "financial_ok": m.financial_ok,
            "description": m.description,
        }
        for m in sorted(METRICS.values(), key=lambda x: (str(x.cost), x.key))
    ]
