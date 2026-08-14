"""Phase 11: turning volatility-v1 into a calibrated expected-movement engine.

The question this phase is allowed to ask
-----------------------------------------
Seven phases have now failed to find stable direction. One result survived all
of them: volatility persists, and volatility-v1 measures it. So this phase asks
only *how far*, never *which way* -- and every constant below exists to keep that
boundary from eroding.

The distinction that makes the whole thing safe
-----------------------------------------------
An expected-movement band is a statement about **magnitude around the current
price**. It is not a target, not a forecast and not a probability of an increase.
A band of 6% at 80% confidence means "four times in five the price stayed within
6% of here" -- symmetric, directionless, and falsifiable by counting.

Turning that into a price target would require knowing the sign, which is the one
thing this project has established it does not know.

What is frozen before measuring
-------------------------------
volatility-v1 itself is frozen: its ATR period, percentile window and regime
boundaries are untouched by this phase, and its constants live in
``app.market_data.volatility``. Everything here is the *evaluation* scaffolding,
declared in advance so a band cannot be widened until it looks calibrated.
"""

from __future__ import annotations

from enum import StrEnum
from itertools import pairwise
from typing import Final

HORIZONS: Final[tuple[int, ...]] = (1, 3, 5, 10, 20)
"""Trading-day horizons, exactly as briefed."""

COVERAGE_LEVELS: Final[tuple[float, ...]] = (0.50, 0.60, 0.70, 0.80, 0.90, 0.95)
"""Bands to publish and test. A claimed 80% band must contain 80% of outcomes."""

MAX_CALIBRATION_ERROR: Final = 0.05
"""How far actual coverage may sit from claimed coverage and still be ROBUST.

Five percentage points, out of sample. Chosen before measuring. A band claiming
80% that delivers 68% is not "slightly optimistic" -- it under-warns by a fifth,
and the person relying on it sized a position from the wrong number.
"""

MIN_OBSERVATIONS_PER_CELL: Final = 200
"""Below this a regime/horizon cell is reported but not classified."""

ATR_MULTIPLES: Final[tuple[float, ...]] = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)
"""Pre-declared stop distances, in multiples of the symbol's own ATR%.

Multiples of ATR rather than fixed percentages, for the same reason the regimes
are percentile-based: 2% is a scratch for TSLA and a large move for KO. These
are **not** optimised -- the question is how often normal noise touches them, not
which one makes money.
"""

RISK_BUDGETS: Final[tuple[float, ...]] = (0.25, 0.50, 1.00, 2.00)
"""Hypothetical per-trade risk, as a percent of equity. Nothing is recommended."""

PORTFOLIO_SIZES: Final[tuple[float, ...]] = (100.0, 1_000.0, 10_000.0)

ROUND_TRIP_COST_PCT: Final = 0.20
"""Assumed round-trip friction, in percent. Twenty basis points, matching the
middle scenario every previous phase has costed against."""

MIN_PRACTICAL_POSITION_EUR: Final = 20.0
"""Below this a position is not worth opening: at 20 bps round trip a €20
position pays 4 cents in cost, and any smaller position is dominated by
rounding and minimum increments."""


def band_is_monotone(bands: dict[float, float]) -> bool:
    """A wider claimed coverage must never produce a narrower band.

    The single most important structural property of a set of published bands.
    If a 90% band were narrower than an 80% one, the numbers would be internally
    contradictory and no amount of good average calibration would rescue them.
    """
    levels = sorted(bands)
    return all(bands[a] <= bands[b] for a, b in pairwise(levels))


def calibration_error(claimed: float, actual: float) -> float:
    """Signed error, in coverage fraction. Negative means the band under-covers."""
    return actual - claimed


def classify_expected_movement(
    *,
    observations: int,
    mean_absolute_error: float | None,
    worst_year_error: float | None,
) -> str:
    """Verdict for the expected-movement engine, applied mechanically.

    ``worst_year_error`` is what separates ROBUST from REGIME_DEPENDENT: pooled
    calibration averages a year that over-covers against one that under-covers
    and reports neither. A risk band that is only right on average is not a risk
    band -- it is wrong in exactly the years someone would consult it.
    """
    if observations < MIN_OBSERVATIONS_PER_CELL or mean_absolute_error is None:
        return "PROMISING_BUT_INSUFFICIENT"
    if mean_absolute_error > MAX_CALIBRATION_ERROR * 2:
        return "POORLY_CALIBRATED"
    if worst_year_error is not None and worst_year_error > MAX_CALIBRATION_ERROR * 2:
        return "REGIME_DEPENDENT"
    if mean_absolute_error > MAX_CALIBRATION_ERROR:
        return "PROMISING_BUT_INSUFFICIENT"
    return "ROBUST"


def position_size(
    *,
    equity: float,
    risk_budget_pct: float,
    stop_distance_pct: float,
    allow_leverage: bool = False,
) -> float:
    """Position notional such that a stop-out costs exactly the risk budget.

    The arithmetic is deliberately trivial and deliberately capped: without the
    leverage clamp a tight stop produces a position larger than the account,
    which is where a sizing formula silently becomes a margin call.

    Raises:
        ValueError: on a non-positive stop distance. A zero-width stop implies
            an infinite position, and returning ``inf`` would let that reach a
            caller instead of stopping here.
    """
    if stop_distance_pct <= 0:
        msg = f"stop distance must be positive, got {stop_distance_pct}"
        raise ValueError(msg)
    notional = (equity * risk_budget_pct / 100) / (stop_distance_pct / 100)
    return notional if allow_leverage else min(notional, equity)


def risk_at_stop(*, notional: float, stop_distance_pct: float) -> float:
    """What a stop-out actually costs. The inverse of :func:`position_size`."""
    return notional * stop_distance_pct / 100


# ---------------------------------------------------------------------------
# Phase 11.1: can a small horizon-aware form beat the frozen bar?
# ---------------------------------------------------------------------------
DEVELOPMENT_MAX_YEAR: Final = 2023
"""Parameters may be fitted on 2020-2023 and nowhere else.

Chronological, not random. A shuffled split would let 2025 inform a parameter
used to score 2024, which is the quiet form of leakage that makes a calibration
look better than it is.
"""

VALIDATION_MIN_YEAR: Final = 2024

PRIMARY_COVERAGE: Final = 0.80
"""The band the acceptance gate is judged on. 90% and 95% are reported beside
it, but adding a level after seeing results would be three chances at one bar."""

CELL_MIN_VALIDATION_OBS: Final = 30
"""Minimum validation observations before a symbol/regime/horizon cell counts."""


class BandModel(StrEnum):
    """The three pre-registered representations. **No fourth may be added.**

    Each is a statement about where volatility structure lives. Reporting the
    winner of three declared candidates is a result; reporting the winner of
    however many were tried until one passed is not.
    """

    REGIME_ONLY = "k(regime) x ATR%"
    """Four parameters, horizon-agnostic. The literal Phase-11 baseline form,
    and expected to fail across horizons -- one band cannot be right at both one
    day and twenty."""

    REGIME_AND_HORIZON = "k(regime, horizon) x ATR%"
    """Twenty parameters. Maximum flexibility inside the no-per-symbol rule, and
    the yardstick for whether the parsimonious form gives anything away."""

    SQRT_HORIZON = "k(regime) x ATR% x sqrt(horizon)"
    """Four parameters with square-root time scaling.

    Motivated rather than fitted: under a random walk, dispersion grows with the
    square root of time. Phase 11 measured k/sqrt(h) as flat to three decimals
    for HIGH_VOL and drifting about 21% for LOW_VOL, so this is expected to be
    close but not exact -- and the size of that gap is precisely what decides
    whether twenty parameters are worth paying for.
    """


POOL_HIGH_AND_EXTREME: Final = False
"""Whether HIGH_VOL and EXTREME_VOL share one parameter.

Pre-registered as a **comparison**, not a decision: EXTREME_VOL is the smallest
regime (8,404 observations) and the honest question is whether its own parameter
survives out of sample or is noise that pooling would remove. Both are measured
and reported; this constant records that the default was to keep them separate.
"""

COMPLEXITY_JUSTIFICATION_PP: Final = 0.50
"""How much calibration a model must buy to justify extra parameters.

Half a percentage point. Twenty parameters beating four by 0.05pp is not an
improvement worth carrying into production -- it is a fitted artefact with a
maintenance cost.
"""
