"""Short-horizon risk: how far a symbol may move over the next one to three days.

What this is, and the one sentence that defines it
---------------------------------------------------
A **rolling** estimate of movement magnitude over the next 1 or 3 trading days,
computed from the current information state and **recomputed as new bars
arrive**. It is not a forecast of a holding period, and it is not a direction.

A position held for twenty days does not get a twenty-day risk number at entry.
It gets a fresh one-to-three-day number every day it stays open. That is the
canonical behaviour, and it exists because phase 11.1 measured the alternative:
the same model calibrates to 4.06pp at one day and **8.82pp at twenty**, because
volatility mean-reverts. A twenty-day claim would be a number this project
cannot support.

Why only 1d and 3d
------------------
Measured out of sample on 2024-2026, with parameters frozen on 2020-2023:

===========  ===================  ==================
Horizon      Calibration error    Against the 5.00pp bar
===========  ===================  ==================
1 day        4.06pp               passes
3 days       4.12pp               passes
5 days       5.16pp               fails
10 days      7.18pp               fails
20 days      8.82pp               fails
===========  ===================  ==================

:data:`SUPPORTED_HORIZONS` is the enforcement, not a convention: asking this
module for a five-day band raises rather than returning a number nobody
validated.

The form, and why it is four constants
--------------------------------------
::

    band(horizon) = k(regime) x sqrt(horizon) x ATR%

Square-root time scaling is a random-walk property rather than a fitted shape.
Phase 11.1 compared it against a twenty-parameter alternative that bought only
0.32pp -- below the 0.50pp justification bar set in advance -- so the four-
constant form was kept.

What it deliberately cannot say
-------------------------------
There is no direction, no target, no expected price and no probability of
profit anywhere in :class:`ShortHorizonRisk`, and there is nowhere to put one.
Eight research phases found no stable directional information; a field that
implied otherwise would be the product contradicting its own evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.core.time import utc_now
from app.market_data.volatility import (
    ExpectedMovement,
    VolatilityRegime,
)

MODEL_VERSION: Final = "risk-v1"
"""Versioned separately from ``volatility-v1``.

volatility-v1 answers "how active is this symbol relative to its own history".
risk-v1 turns that into a calibrated movement band. They can change
independently, and a stored estimate must be traceable to both.
"""

SUPPORTED_HORIZONS: Final[tuple[int, ...]] = (1, 3)
"""The only horizons with validated calibration. Enforced, not documented."""

COVERAGE: Final = 0.80
"""What :attr:`ShortHorizonRisk.risk_band_1d` claims: four times in five, the
maximum excursion stayed inside it."""

STRESS_COVERAGE: Final = 0.95

# Fitted on 2020-2023 only and frozen. Validation on 2024-2026 was not consulted
# in choosing them -- that is the whole basis for the calibration numbers above.
K_TYPICAL: Final[dict[VolatilityRegime, float]] = {
    VolatilityRegime.LOW: 2.815396,
    VolatilityRegime.NORMAL: 2.468830,
    VolatilityRegime.HIGH: 2.203984,
    VolatilityRegime.EXTREME: 2.046702,
}
"""Median excursion, in ATR multiples. The "typical move" figure."""

K_BAND: Final[dict[VolatilityRegime, float]] = {
    VolatilityRegime.LOW: 4.311697,
    VolatilityRegime.NORMAL: 3.726623,
    VolatilityRegime.HIGH: 3.323977,
    VolatilityRegime.EXTREME: 3.117651,
}
"""The 80% band. ``k`` **falls** as the regime rises because ATR% is already
larger there -- volatility mean reversion, seen from the multiplier side."""

K_STRESS: Final[dict[VolatilityRegime, float]] = {
    VolatilityRegime.LOW: 6.591040,
    VolatilityRegime.NORMAL: 5.562523,
    VolatilityRegime.HIGH: 5.043565,
    VolatilityRegime.EXTREME: 4.856794,
}

GAP_ATR_MULTIPLE: Final[dict[VolatilityRegime, float]] = {
    VolatilityRegime.LOW: 1.579817,
    VolatilityRegime.NORMAL: 1.528677,
    VolatilityRegime.HIGH: 1.457506,
    VolatilityRegime.EXTREME: 1.493984,
}
"""80th-percentile overnight gap, in ATR multiples.

Carried separately rather than folded into the band because the two are not
interchangeable to anyone placing a stop. Measured against the calibrated 1-day
band, the gap component is 37% of it in LOW_VOL and **50% in EXTREME_VOL**.
"""

EXTREME_UNDER_COVERAGE_NOTE: Final = (
    "EXTREME_VOL under-covers at 1 day: the 80% band delivered 74.3% in "
    "validation. Treat EXTREME bands as a floor, not a bound."
)
"""A measured limitation, stated rather than fixed.

Re-fitting EXTREME_VOL to close this gap would mean tuning on the validation
period, which is the one thing the frozen-parameter discipline forbids.
"""

MIN_PRACTICAL_POSITION: Final = 20.0
"""Below this a position is not worth opening in euros or dollars."""


class UnsupportedHorizonError(ValueError):
    """Raised for a horizon with no validated calibration.

    Deliberately an exception rather than a wider band: 5d, 10d and 20d were
    measured and failed, so producing a number for them would present an
    uncalibrated estimate in the same shape as a calibrated one.
    """


@dataclass(frozen=True, slots=True)
class ShortHorizonRisk:
    """Movement magnitude over the next 1-3 sessions. **No direction anywhere.**

    Every field is a magnitude around the current price, symmetric by
    construction. There is no ``target``, ``direction`` or ``probability``, and
    a test asserts their absence so a future edit cannot add one quietly.
    """

    symbol: str
    calculated_at: datetime
    bar_timestamp: datetime
    regime: VolatilityRegime
    percentile: float
    atr_pct: float

    expected_move_1d: float
    expected_move_3d: float
    risk_band_1d: float
    risk_band_3d: float
    stress_move_1d: float
    stress_move_3d: float
    overnight_gap_pct: float

    stale: bool
    model_version: str = MODEL_VERSION
    volatility_model_version: str = "volatility-v1"

    @property
    def data_quality(self) -> str:
        """``OK`` or ``STALE``. A stale estimate may be shown, never relied on."""
        return "STALE" if self.stale else "OK"

    @property
    def gap_share_of_band(self) -> float:
        """How much of the one-day band could arrive before the market opens."""
        return self.overnight_gap_pct / self.risk_band_1d if self.risk_band_1d else 0.0

    def band(self, horizon: int) -> float:
        """The calibrated band for a supported horizon.

        Raises:
            UnsupportedHorizonError: for 5, 10, 20 or anything else.
        """
        if horizon == 1:
            return self.risk_band_1d
        if horizon == 3:  # noqa: PLR2004
            return self.risk_band_3d
        msg = (
            f"horizon {horizon}d has no validated calibration; "
            f"supported horizons are {SUPPORTED_HORIZONS}"
        )
        raise UnsupportedHorizonError(msg)

    def minimum_noise_distance(self, horizon: int = 1) -> float:
        """The distance below which a stop sits inside ordinary noise.

        **Not a stop recommendation.** It is the lower bound a future position
        engine should treat as "this will be touched by normal movement" --
        phase 11.1 measured a stop at half the 1-day band being touched 34% of
        the time within a single session. Choosing the actual stop belongs to a
        position-management phase that does not exist yet.
        """
        return self.band(horizon)

    def summary(self) -> str:
        """One line, magnitude only."""
        mark = "  [STALE]" if self.stale else ""
        return (
            f"{self.symbol}: {self.regime.value} "
            f"({self.percentile * 100:.0f}th pct)  "
            f"1d ~{self.expected_move_1d:.1f}% (80% band {self.risk_band_1d:.1f}%)  "
            f"3d ~{self.expected_move_3d:.1f}% (80% band {self.risk_band_3d:.1f}%){mark}"
        )


def assess(movement: ExpectedMovement, *, now: datetime | None = None) -> ShortHorizonRisk:
    """Turn a volatility-v1 estimate into calibrated short-horizon risk.

    Pure arithmetic over the supplied estimate: no database, no provider, and no
    access to anything later than the bar the estimate was built from. Staleness
    is inherited rather than recomputed, so a stale input cannot produce a
    fresh-looking risk claim.
    """
    moment = now or utc_now()
    regime = movement.regime
    atr = movement.atr_pct

    def scaled(table: dict[VolatilityRegime, float], horizon: int) -> float:
        return float(table[regime] * (horizon**0.5) * atr)

    return ShortHorizonRisk(
        symbol=movement.symbol,
        calculated_at=moment,
        bar_timestamp=movement.bar_timestamp,
        regime=regime,
        percentile=movement.percentile,
        atr_pct=atr,
        expected_move_1d=scaled(K_TYPICAL, 1),
        expected_move_3d=scaled(K_TYPICAL, 3),
        risk_band_1d=scaled(K_BAND, 1),
        risk_band_3d=scaled(K_BAND, 3),
        stress_move_1d=scaled(K_STRESS, 1),
        stress_move_3d=scaled(K_STRESS, 3),
        overnight_gap_pct=GAP_ATR_MULTIPLE[regime] * atr,
        stale=movement.is_stale(now=moment),
        volatility_model_version=movement.model_version,
    )


@dataclass(frozen=True, slots=True)
class PositionSizing:
    """What a risk budget permits. **Arithmetic only -- decides nothing.**"""

    equity: float
    risk_budget_pct: float
    risk_amount: float
    stop_distance_pct: float
    max_position_value: float
    capital_required: float
    cost: float
    leverage_capped: bool
    practical: bool

    @property
    def cost_share_of_risk(self) -> float:
        return self.cost / self.risk_amount if self.risk_amount else 0.0

    @property
    def allocation_pct(self) -> float:
        return self.max_position_value / self.equity * 100 if self.equity else 0.0


def size_position(
    *,
    equity: float,
    risk_budget_pct: float,
    stop_distance_pct: float,
    round_trip_cost_pct: float = 0.20,
    max_allocation_pct: float = 100.0,
    fractional_shares: bool = True,
    price: float | None = None,
) -> PositionSizing:
    """The largest position whose stop-out costs exactly the risk budget.

    Args:
        stop_distance_pct: how far the stop sits from entry, in percent. A
            caller may use :meth:`ShortHorizonRisk.minimum_noise_distance`, but
            the choice is the caller's -- this function does not pick a stop.
        fractional_shares: when False the notional is rounded down to whole
            shares, which is what makes small accounts fail in practice rather
            than in theory.

    Raises:
        ValueError: on a non-positive stop distance or equity. A zero-width stop
            implies an infinite position; returning ``inf`` would let that reach
            a caller.
    """
    if stop_distance_pct <= 0:
        msg = f"stop distance must be positive, got {stop_distance_pct}"
        raise ValueError(msg)
    if equity <= 0:
        msg = f"equity must be positive, got {equity}"
        raise ValueError(msg)

    risk_amount = equity * risk_budget_pct / 100
    uncapped = risk_amount / (stop_distance_pct / 100)
    ceiling = equity * max_allocation_pct / 100
    notional = min(uncapped, ceiling)

    if not fractional_shares and price and price > 0:
        notional = (notional // price) * price

    return PositionSizing(
        equity=equity,
        risk_budget_pct=risk_budget_pct,
        risk_amount=risk_amount,
        stop_distance_pct=stop_distance_pct,
        max_position_value=notional,
        capital_required=notional,
        cost=notional * round_trip_cost_pct / 100,
        leverage_capped=uncapped > ceiling,
        practical=notional >= MIN_PRACTICAL_POSITION,
    )


@dataclass(frozen=True, slots=True)
class PositionRisk:
    """Current risk on a hypothetical open position. **Suggests no action.**

    Exists so a future portfolio engine has a defined shape to consume. It
    reports what changed; deciding what to do about it is a phase that has not
    happened.
    """

    symbol: str
    entry_price: float
    current_price: float
    entry_regime: VolatilityRegime
    risk: ShortHorizonRisk
    risk_budget_amount: float

    @property
    def unrealised_pct(self) -> float:
        return (self.current_price / self.entry_price - 1) * 100 if self.entry_price else 0.0

    @property
    def regime_changed(self) -> bool:
        return self.risk.regime is not self.entry_regime

    @property
    def regime_transition(self) -> str | None:
        """``"NORMAL_VOL -> HIGH_VOL"``, or ``None`` when unchanged."""
        if not self.regime_changed:
            return None
        return f"{self.entry_regime.value} -> {self.risk.regime.value}"

    @property
    def next_session_risk_amount(self) -> float:
        """What the 80% band implies in currency on the current position."""
        return self.current_price * self.risk.risk_band_1d / 100

    def summary(self) -> str:
        transition = f"  regime {self.regime_transition}" if self.regime_changed else ""
        return (
            f"{self.symbol}: {self.unrealised_pct:+.2f}% unrealised, "
            f"next-session 80% band ±{self.risk.risk_band_1d:.1f}%{transition}"
        )
