"""Phase 10.2: does a 10-Q/10-K filing change what happens next?

The honesty problem this module is built around
------------------------------------------------
Phase 10.1 tested whether EPS *growth direction* predicted returns. It did not:
on 916 events the spread inverted between horizons and reversed every year. But
the matched baseline showed every bucket lifting at 5 days, including the one
whose earnings fell. That is an *event* effect, not a directional one -- and it
was noticed **after** looking at the results.

A hypothesis found by inspecting outcomes is worth testing and worth nothing
until it is tested properly. So everything here is frozen as a module constant
before any outcome was computed, and the primary target is deliberately
**magnitude, not direction**: magnitude is what the observation actually
suggested, and reaching for direction because it would be more useful is how the
previous six phases produced results that did not survive a larger sample.

What is frozen, and why each cutoff
-----------------------------------
* Horizons 1/3/5/10/20 sessions, exactly as briefed.
* Reaction buckets at the 30th and 70th percentiles, computed **within each
  calendar year's events** so the split is mechanical rather than chosen. Fixed
  quantiles rather than fixed percentages because a 3% move means something
  different for KO than for NVDA.
* A matched control must sit 10-60 sessions away from its event and carry no
  filing within +/-5 sessions.
* Alignment means same sign, nothing more.

The direction test (H2) is secondary and reported alongside its own null. Both
continuation and reversal are reported for every cell, because reporting only
the one that happened to work is the failure mode this project keeps finding in
its own earlier phases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

HORIZONS: Final[tuple[int, ...]] = (1, 3, 5, 10, 20)
"""Trading-day horizons, exactly as briefed. 10d is measured directly from bars
rather than from the outcome-label schema, which has no 10d member."""

# ---------------------------------------------------------------------------
# C: the frozen hypotheses
# ---------------------------------------------------------------------------
H0: Final = (
    "Post-filing return, range and volatility distributions do not materially "
    "differ from matched non-event windows."
)

H1: Final = (
    "10-Q/10-K acceptance is associated with a reproducible change in subsequent "
    "market behaviour. PRIMARY TARGET: magnitude and distribution, not direction."
)

H2: Final = (
    "The initial market reaction after publication contains information about "
    "subsequent continuation or reversal. SECONDARY, and reported against both "
    "the continuation and the reversal reading."
)

MATERIAL_MAGNITUDE_LIFT: Final = 0.15
"""How much larger event-window movement must be to count as material.

Fifteen percent relative to the matched control. Chosen before measuring, and
deliberately a *ratio* rather than a percentage-point threshold: absolute
movement differs by an order of magnitude between KO and NVDA, so a points
threshold would simply select the volatile names.
"""

MEANINGFUL_DIRECTIONAL_SPREAD: Final = 0.05
"""Five percentage points of positive-rate separation, identical to the floor
every phase since 6 has used. Restated here so this phase cannot quietly lower
it after seeing a 3pp result."""

MIN_EVENTS_FOR_CLAIM: Final = 100
"""Below this an effect is reported as INSUFFICIENT regardless of size.

Phase 10.1's 167-event result inverted at 916. That is the cautionary number
this constant exists to respect.
"""

MIN_EVENTS_PER_YEAR: Final = 25
"""Below this a calendar year is shown but excluded from the stability verdict."""

# ---------------------------------------------------------------------------
# I: initial-reaction buckets
# ---------------------------------------------------------------------------
REACTION_LOW_QUANTILE: Final = 0.30
REACTION_HIGH_QUANTILE: Final = 0.70
"""Where the initial reaction is split into negative / neutral / positive.

Broad thirds, computed within each calendar year's own event cross-section, so
the boundary is mechanical and cannot drift with the market. The brief forbids
optimising these for hit rate and they were fixed before any outcome was seen.
"""

REACTION_BUCKETS: Final[tuple[str, ...]] = (
    "STRONG_NEGATIVE",
    "NEUTRAL",
    "STRONG_POSITIVE",
)

# ---------------------------------------------------------------------------
# F: matched-control tolerances
# ---------------------------------------------------------------------------
CONTROL_MIN_GAP_SESSIONS: Final = 10
CONTROL_MAX_GAP_SESSIONS: Final = 60
"""How far a control observation must sit from its event.

Far enough that post-filing drift has decayed, close enough that the company and
the market regime are still recognisably the same. Both ends matter: an
unbounded window would match a 2020 event against 2026.
"""

FILING_EXCLUSION_SESSIONS: Final = 5
"""A control may not sit within this many sessions of *any* filing.

Without it a "non-event" window routinely lands just before the next quarter's
10-Q, which would put event behaviour on both sides of the comparison and bias
the test toward finding nothing.
"""

VOLATILITY_REGIME_MUST_MATCH: Final = True
"""Controls must share the event's pre-event volatility-v1 regime.

The single most important match: filings cluster in earnings season, and
volatility clusters generally, so an unmatched control would compare a
high-volatility event window against a calm control and attribute the difference
to the filing.
"""

# ---------------------------------------------------------------------------
# J: the confirmation matrix
# ---------------------------------------------------------------------------
CONFIRMATION_CELLS: Final[tuple[str, ...]] = (
    "stock_only",
    "stock_and_market",
    "stock_and_sector",
    "stock_and_market_and_sector",
)
"""Four cells, pre-registered. Alignment means the reference moved the same
direction as the stock's initial reaction over the same bar -- nothing more
elaborate, and no new market model."""

# ---------------------------------------------------------------------------
# K: extension buckets
# ---------------------------------------------------------------------------
EXTENSION_BUCKETS: Final[tuple[tuple[str, float, float], ...]] = (
    ("extended_down (< -2 ATR)", -99.0, -2.0),
    ("normal (-2 to +2 ATR)", -2.0, 2.0),
    ("extended_up (> +2 ATR)", 2.0, 99.0),
)
"""Broad and symmetric, reusing ``dist_ema20_atr``. Three buckets rather than
five because this is descriptive: the question is whether extremes behave
differently, not where an entry threshold sits."""

COST_SCENARIOS: Final[tuple[tuple[str, float], ...]] = (
    ("0 bps", 0.0),
    ("20 bps", 0.20),
    ("50 bps", 0.50),
)


@dataclass(frozen=True, slots=True)
class ComparisonLedger:
    """A count of every comparison made, so the report cannot cherry-pick.

    The brief requires reporting how many comparisons were run. Tracking it in
    a structure rather than counting by hand afterwards is the difference
    between a stated number and a remembered one.
    """

    primary: int = 0
    exploratory: int = 0

    def with_primary(self, n: int) -> ComparisonLedger:
        return ComparisonLedger(self.primary + n, self.exploratory)

    def with_exploratory(self, n: int) -> ComparisonLedger:
        return ComparisonLedger(self.primary, self.exploratory + n)

    @property
    def total(self) -> int:
        return self.primary + self.exploratory


def classify_magnitude(*, events: int, lift: float | None, sign_stable: bool) -> str:
    """Verdict for the primary (magnitude) question, applied mechanically."""
    if events < MIN_EVENTS_FOR_CLAIM or lift is None:
        return "PROMISING_BUT_INSUFFICIENT"
    if abs(lift) < MATERIAL_MAGNITUDE_LIFT:
        return "NO_EVENT_INFORMATION"
    if not sign_stable:
        return "REGIME_DEPENDENT"
    return "ROBUST_EVENT_MAGNITUDE_INFORMATION"


def classify_direction(*, events: int, spread: float | None, sign_stable: bool) -> str:
    """Verdict for the secondary (direction) question."""
    if events < MIN_EVENTS_FOR_CLAIM or spread is None:
        return "PROMISING_BUT_INSUFFICIENT"
    if abs(spread) < MEANINGFUL_DIRECTIONAL_SPREAD:
        return "NO_STABLE_DIRECTIONAL_INFORMATION"
    if not sign_stable:
        return "REGIME_DEPENDENT"
    return "ROBUST_DIRECTIONAL_INFORMATION"
