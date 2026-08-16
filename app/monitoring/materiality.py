"""When a change is big enough to mention.

Every threshold in this file is **declared**, and every one of them is written
down in one place so the question "why did this alert?" always has an answer
that is a constant with a comment rather than a branch buried in a detector.

How these numbers were chosen
-----------------------------
Three sources, in order of preference:

**Borrowed from an existing calibration.** Portfolio weight, sector and
correlation thresholds are imported from :mod:`app.portfolio_fit`, which
measured them against the real distribution of equity pair correlations. A
second copy here would drift from that calibration silently.

**Anchored to a measured distribution.** Volume and volatility ratios are set at
percentiles of their own observed distribution across the universe, so "unusual"
means "rare", measured, rather than a round number that felt right.

**Structural.** A filing either appeared or it did not; a position is either
held or it is not; a data store is either readable or it is not. These need no
threshold, only a classification of how much each matters.

What is *not* a source
----------------------
No threshold here is fitted to a forward return, a hit rate, or any outcome. It
would be easy to tune "unusual volume" until it preceded profitable moves, and
that would be alpha research wearing a monitoring costume -- Phase 12.25
established that no such relationship in this data survives out-of-sample, and
nothing in this phase revisits that. These thresholds answer "is this rare or
large?", never "is this good?".
"""

from __future__ import annotations

from typing import Final

from app.monitoring.schemas import EventKind, Materiality
from app.portfolio_fit import (
    CORRELATION_PERCENTILES,
    MATERIAL_WEIGHT_SHIFT,
    SECTOR_HEAVY,
)

# ---------------------------------------------------------------- market
REGIME_TREND_BAND: Final = 0.02
"""How far the benchmark must sit from its 200-day average before the regime is
called trending either way. A band, not a line: an index oscillating within 2%
of its average would otherwise flip regime every few sessions and report each
flip as news."""

REGIME_MIN_SESSIONS_IN_STATE: Final = 3
"""Sessions a new regime must persist before it is announced. The cost of this
is a three-day delay on a real turn; the benefit is not announcing the two-day
head-fakes, which are far more numerous."""

SECTOR_MOVE_5D: Final = 0.05
SECTOR_MOVE_5D_SIGNIFICANT: Final = 0.08
"""Five-session sector moves. A 5% week in a diversified sector basket is
roughly a two-standard-deviation move at typical sector volatility."""

VOLUME_RATIO_NOTABLE: Final = 2.5
VOLUME_RATIO_SIGNIFICANT: Final = 4.0
"""Session volume over the trailing 20-session median. Percentile-anchored
against the observed distribution across the universe; see
``reports/phase12_36/materiality_calibration.json``."""

VOLATILITY_RATIO_NOTABLE: Final = 1.6
VOLATILITY_RATIO_SIGNIFICANT: Final = 2.2
"""Twenty-session realised volatility over the trailing one-year figure. A ratio
rather than a level, because 40% annualised is ordinary for one stock and
extraordinary for another."""

RELATIVE_STRENGTH_BAND: Final = 0.10
"""Twelve-month return minus the benchmark's. Crossing zero is the transition
that matters, but a name oscillating around parity would report constantly, so
the crossing must clear this band on the far side."""

# ---------------------------------------------------------------- company
FUNDAMENTAL_CHANGE_NOTABLE: Final = 0.10
FUNDAMENTAL_CHANGE_SIGNIFICANT: Final = 0.25
"""Relative change in a trailing-twelve-month figure between observations.
Below 10% is ordinary quarterly drift in a rolling four-quarter sum."""

VALUATION_CONFIRM_PASSES: Final = 2
"""Company passes a new valuation band must hold before it is reported.

The band is a percentile of the company's own price-to-sales history, so a name
sitting near a boundary crosses it on ordinary price movement and crosses back
days later. Replaying 120 sessions produced 172 band changes across 52
companies without this; the great majority were that oscillation rather than a
company becoming durably cheaper or dearer. Same principle as the market
regime: confirm, then report.
"""

FUNDAMENTAL_METRICS: Final[frozenset[str]] = frozenset(
    {
        "revenue_ttm",
        "eps_ttm",
        "operating_income_ttm",
        "gross_margin",
        "operating_margin",
        "free_cash_flow",
    }
)
"""Which carried figures count as *fundamentals*.

Market capitalisation and price-to-sales are deliberately excluded. Both move
with the share price, so watching them here would report a 10% price move as a
change in the business -- which it is not, and which
:attr:`~app.monitoring.schemas.EventKind.VALUATION_STATE_CHANGE` already covers
from the right angle.
"""

MATERIAL_FORMS: Final[frozenset[str]] = frozenset({"10-K", "20-F", "40-F"})
NOTABLE_FORMS: Final[frozenset[str]] = frozenset({"10-Q", "8-K", "6-K"})
"""Annual reports restate the most; quarterlies and current reports matter less
but still matter. Everything else -- ownership forms, prospectuses -- is
routine, and there are a great many of them."""

# ---------------------------------------------------------------- portfolio
WEIGHT_SHIFT: Final = MATERIAL_WEIGHT_SHIFT
SECTOR_SHIFT: Final = MATERIAL_WEIGHT_SHIFT
SECTOR_HEAVY_LEVEL: Final = SECTOR_HEAVY
CORRELATION_BANDS: Final = CORRELATION_PERCENTILES
CASH_SHIFT_NOTABLE: Final = 0.10
CASH_SHIFT_SIGNIFICANT: Final = 0.25
"""Cash as a fraction of equity. A ten-point move is a real change in posture;
anything smaller is the market moving the denominator."""

# ---------------------------------------------------------------- cooldown
DEFAULT_COOLDOWN_HOURS: Final = 24
COOLDOWN_HOURS: Final[dict[EventKind, int]] = {
    # A regime is a slow-moving fact. Repeating it daily would be the loudest
    # and least informative thing this engine could do.
    EventKind.MARKET_REGIME_CHANGE: 24 * 7,
    EventKind.SECTOR_MOVE: 24 * 3,
    # Three days, matching volatility. A stock in the news trades heavily for
    # several sessions; the first is the observation, the rest are follow-through
    # and reporting each one is how a channel teaches people to skim it.
    EventKind.UNUSUAL_VOLUME: 24 * 3,
    EventKind.UNUSUAL_VOLATILITY: 24 * 3,
    EventKind.RELATIVE_STRENGTH_CHANGE: 24 * 14,
    EventKind.VALUATION_STATE_CHANGE: 24 * 7,
    EventKind.COMPANY_CONFIDENCE_CHANGE: 24 * 7,
    EventKind.FUNDAMENTAL_CHANGE: 24 * 7,
    # A new filing is a discrete fact that never repeats: the deduplication key
    # is the accession, so a cooldown would only ever suppress a genuinely new
    # document filed within a day of the last.
    EventKind.NEW_SEC_FILING: 0,
    EventKind.POSITION_ADDED: 0,
    EventKind.POSITION_REMOVED: 0,
    EventKind.DATA_HEALTH_CHANGE: 6,
}
"""How long the same deduplication key stays quiet after being reported.

Deduplication alone is not enough. A stock whose volume ratio hovers around the
threshold produces a *new* key each time it crosses back and forth, so without a
cooldown it would report every other session while nothing meaningful changed.
"""


def band(value: float, notable: float, significant: float) -> Materiality:
    """Map a magnitude to a materiality band.

    Anything below ``notable`` is ROUTINE: observed, recorded, not announced.
    """
    magnitude = abs(value)
    if magnitude >= significant:
        return Materiality.SIGNIFICANT
    if magnitude >= notable:
        return Materiality.NOTABLE
    return Materiality.ROUTINE


def cooldown_hours(kind: EventKind) -> int:
    return COOLDOWN_HOURS.get(kind, DEFAULT_COOLDOWN_HOURS)


def as_dict() -> dict[str, object]:
    """Every threshold, for the run's provenance record."""
    return {
        "regime_trend_band": REGIME_TREND_BAND,
        "regime_min_sessions_in_state": REGIME_MIN_SESSIONS_IN_STATE,
        "sector_move_5d": [SECTOR_MOVE_5D, SECTOR_MOVE_5D_SIGNIFICANT],
        "volume_ratio": [VOLUME_RATIO_NOTABLE, VOLUME_RATIO_SIGNIFICANT],
        "volatility_ratio": [VOLATILITY_RATIO_NOTABLE, VOLATILITY_RATIO_SIGNIFICANT],
        "relative_strength_band": RELATIVE_STRENGTH_BAND,
        "fundamental_change": [
            FUNDAMENTAL_CHANGE_NOTABLE,
            FUNDAMENTAL_CHANGE_SIGNIFICANT,
        ],
        "weight_shift": WEIGHT_SHIFT,
        "sector_shift": SECTOR_SHIFT,
        "sector_heavy_level": SECTOR_HEAVY_LEVEL,
        "correlation_bands": dict(CORRELATION_BANDS),
        "cash_shift": [CASH_SHIFT_NOTABLE, CASH_SHIFT_SIGNIFICANT],
        "cooldown_hours": {str(k): v for k, v in COOLDOWN_HOURS.items()},
        "default_cooldown_hours": DEFAULT_COOLDOWN_HOURS,
        "borrowed_from_portfolio_fit": [
            "weight_shift",
            "sector_shift",
            "sector_heavy_level",
            "correlation_bands",
        ],
        "fitted_to_forward_returns": False,
    }
