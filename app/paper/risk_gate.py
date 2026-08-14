"""risk-v1 as an entry constraint. **It never decides to trade.**

Where this sits
---------------
The paper engine already sizes by risk: ``app.paper.sizing`` divides a risk
budget by a stop distance and caps the result by position limit, cash and
exposure. That arithmetic is not duplicated here and is not replaced.

What this module adds is the two things risk-v1 can honestly contribute:

1. a **floor** under the stop distance, so a position is never sized off a stop
   that sits inside ordinary noise;
2. a **gate** that can refuse or shrink an entry for explicit risk reasons.

Everything else about the trade -- whether the candidate is attractive, which way
it might go, when to leave -- is untouched, because eight research phases found
no evidence supporting any of it.

The boundary, made structural
-----------------------------
:class:`RiskGateDecision` carries no direction, no score and no opinion about
the candidate. Its rejection reasons are all statements about *arithmetic or
data quality* -- budget, practicality, staleness, cost share, allocation. There
is deliberately no reason code meaning "the risk model dislikes this trade",
because risk-v1 has no view on that and a field for it would invite one.

Why a floor rather than a stop
------------------------------
Phase 11.1 measured a stop at half the calibrated one-day band being touched
**33.7% of the time within a single session**. A stop tighter than
:meth:`ShortHorizonRisk.minimum_noise_distance` is therefore not a risk control;
it is a coin flip with extra steps. This module widens such a stop, and if the
wider stop makes the position impractical it says so -- it does **not** tighten
the stop back to make the trade fit, which would be sizing driving risk instead
of the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from app.core.config import CostSettings
from app.domain.quotes import Quote
from app.market_data.risk import ShortHorizonRisk
from app.paper.execution import estimate_round_trip_cost

MAX_COST_SHARE_OF_RISK: Final = Decimal("0.35")
"""How much of the declared risk budget transaction costs may consume.

Thirty-five percent.

**Phase 11.3 measured this binding, and often.** The 8.1% figure phase 11.2
quoted came from a 20 bps round trip with no flat per-order fee. With the
configured €1.00 order fee the round trip carries €2.00 of fixed cost whatever
the size, so the share is set by the risk *budget* rather than by the spread: at
a €2.50 budget (paper-1000 at 0.25% risk) cost is roughly 90% of the budget and
every candidate in the stream was refused; at a €25 budget (paper-10000 at the
same 0.25%) it is about 14% and nothing binds.

That is the cap working, not failing -- a trade whose fees consume most of what
you are willing to lose has no room left to be right. But it is a real
constraint on small accounts rather than the inert backstop this docstring
previously claimed, and the difference matters when reading why paper-100 never
trades.
"""

MIN_PRACTICAL_NOTIONAL: Final = Decimal("20")
"""Below this a position is not worth opening.

Matches ``app.market_data.risk.MIN_PRACTICAL_POSITION``. Phase 11.2 classified
paper-100 as LIMITED on exactly this boundary: at 0.25% risk it sizes to about
€10, and rounding that into existence would manufacture a trade the account
cannot really carry.

Phase 11.3 confirmed it against real candidates: at 0.25% and 0.5% risk every
paper-100 candidate is refused here, and at 1% and 2% it is refused on cost
share instead. Refused either way, and fractional shares change neither -- both
tests are on notional, which rounding cannot alter.
"""


class RiskDecision(StrEnum):
    """The gate's verdict. **None of these is a trading opinion.**"""

    ACCEPTABLE = "RISK_ACCEPTABLE"
    LIMITED = "RISK_LIMITED"
    """Permitted, but the stop was widened or the size will be capped."""

    REJECTED = "RISK_REJECTED"
    UNAVAILABLE = "RISK_UNAVAILABLE"
    """No usable risk estimate. Distinct from rejection: the trade was not
    judged and failed, it was never judged at all."""


class RiskRejectionReason(StrEnum):
    """Why an entry was refused. Every one is arithmetic or data quality."""

    IMPRACTICAL_SIZE = "IMPRACTICAL_SIZE"
    COST_EXCEEDS_RISK_SHARE = "COST_EXCEEDS_RISK_SHARE"
    STALE_RISK_DATA = "STALE_RISK_DATA"
    NO_RISK_ESTIMATE = "NO_RISK_ESTIMATE"
    ALLOCATION_CAP = "ALLOCATION_CAP"
    RISK_BUDGET_EXHAUSTED = "RISK_BUDGET_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class RiskGateDecision:
    """What the risk layer permits for one candidate entry.

    Carries a stop *distance*, never a stop *price*, and never a quantity: the
    engine's existing sizer owns quantity, and handing it a price here would put
    two modules in charge of the same number.
    """

    decision: RiskDecision
    reason: RiskRejectionReason | None = None
    detail: str = ""

    risk_distance: Decimal | None = None
    """The distance to use, after applying the noise floor."""

    structural_distance: Decimal | None = None
    """What the caller proposed, before the floor."""

    noise_floor: Decimal | None = None
    tighter_than_noise: bool = False
    """True when the proposal was inside the floor and had to be widened.

    Recorded rather than silently corrected: a strategy that keeps producing
    sub-noise stops is telling you something about itself.
    """

    estimated_cost: Decimal | None = None
    """Modelled round-trip cost, from the same model execution charges."""

    intended_notional: Decimal | None = None
    """What the budget buys at :attr:`risk_distance`, before sizing's own caps."""

    regime: str | None = None
    risk_band_1d: Decimal | None = None
    """The 80% one-day band, in percent. Carried so a later breach audit can ask
    whether a loss exceeded what the model warned about, without re-deriving the
    model state from bars that may since have been re-adjusted."""

    expected_move_1d: Decimal | None = None
    expected_move_3d: Decimal | None = None
    gap_component: Decimal | None = None
    risk_model_version: str | None = None

    @property
    def permits_entry(self) -> bool:
        return self.decision in (RiskDecision.ACCEPTABLE, RiskDecision.LIMITED)


def _as_decimal(value: float) -> Decimal:
    return Decimal(str(round(value, 6)))


def evaluate_entry(
    *,
    risk: ShortHorizonRisk | None,
    entry_price: Decimal,
    structural_stop: Decimal | None,
    risk_budget: Decimal,
    costs: CostSettings,
    quote: Quote | None = None,
    allow_stale: bool = False,
    enforce_cost_share: bool = True,
    max_cost_share: Decimal = MAX_COST_SHARE_OF_RISK,
) -> RiskGateDecision:
    """Decide what the risk layer permits, before the engine sizes anything.

    Args:
        risk: the rolling short-horizon estimate, or ``None`` when volatility-v1
            had too little history. ``None`` yields ``UNAVAILABLE``, not a
            rejection -- the difference matters when reading why nothing traded.
        structural_stop: the stop the engine derived from ATR, or ``None``.
        risk_budget: currency the profile is willing to lose on this trade.
        costs: the fee, spread and slippage settings **paper execution itself
            uses**. The gate models cost from these rather than accepting a
            figure, so the number that constrains a position and the number
            actually charged cannot drift apart.
        quote: live quote, when one is available, for a measured half-spread
            instead of the configured default.
        allow_stale: whether a stale estimate may still gate an entry. Default
            False: a risk number computed from bars that stopped arriving is
            not a risk number.
        enforce_cost_share: whether the cost backstop may reject. Exposed so a
            replay can measure what the backstop actually changes; production
            leaves it on.
        max_cost_share: the ceiling itself, defaulting to the production
            constant. A parameter rather than a second gate: phase 12.2 needs to
            sweep 10/15/20/25/35% to find the capital at which execution becomes
            economic, and forking the gate to do that would put two cost rules
            in the codebase where there must be exactly one.

    Returns a decision. A refusal is an outcome, never an exception -- the
    engine records it and moves on to the next candidate.
    """
    if risk is None:
        return RiskGateDecision(
            decision=RiskDecision.UNAVAILABLE,
            reason=RiskRejectionReason.NO_RISK_ESTIMATE,
            detail="volatility-v1 produced no estimate; too little stored history",
        )

    if risk.stale and not allow_stale:
        return RiskGateDecision(
            decision=RiskDecision.REJECTED,
            reason=RiskRejectionReason.STALE_RISK_DATA,
            detail=f"risk estimate is stale (bar {risk.bar_timestamp.isoformat()})",
            regime=risk.regime.value,
            risk_model_version=risk.model_version,
        )

    # The floor is a percentage of price, so it becomes a distance here.
    floor = _as_decimal(risk.minimum_noise_distance(1)) / Decimal(100) * entry_price
    proposed = (
        entry_price - structural_stop
        if structural_stop is not None and 0 < structural_stop < entry_price
        else None
    )
    distance = max(proposed, floor) if proposed is not None else floor
    tighter = proposed is not None and proposed < floor

    common = {
        "risk_distance": distance,
        "structural_distance": proposed,
        "noise_floor": floor,
        "tighter_than_noise": tighter,
        "regime": risk.regime.value,
        "risk_band_1d": _as_decimal(risk.risk_band_1d),
        "expected_move_1d": _as_decimal(risk.expected_move_1d),
        "expected_move_3d": _as_decimal(risk.expected_move_3d),
        "gap_component": _as_decimal(risk.overnight_gap_pct),
        "risk_model_version": risk.model_version,
    }

    if risk_budget <= 0:
        return RiskGateDecision(
            decision=RiskDecision.REJECTED,
            reason=RiskRejectionReason.RISK_BUDGET_EXHAUSTED,
            detail=f"risk budget is {risk_budget}",
            **common,  # type: ignore[arg-type]
        )

    # The notional the budget buys at this distance. Sizing will recompute it
    # under its own caps; this only asks whether it is worth opening at all.
    notional = risk_budget / (distance / entry_price)
    estimated_cost = estimate_round_trip_cost(settings=costs, notional=notional, quote=quote)
    common["estimated_cost"] = estimated_cost
    common["intended_notional"] = notional

    if notional < MIN_PRACTICAL_NOTIONAL:
        return RiskGateDecision(
            decision=RiskDecision.REJECTED,
            reason=RiskRejectionReason.IMPRACTICAL_SIZE,
            detail=(
                f"risk budget {risk_budget} at a {distance} stop sizes to "
                f"{notional:.2f}, below the {MIN_PRACTICAL_NOTIONAL} practical minimum"
            ),
            **common,  # type: ignore[arg-type]
        )

    if enforce_cost_share and estimated_cost > risk_budget * max_cost_share:
        return RiskGateDecision(
            decision=RiskDecision.REJECTED,
            reason=RiskRejectionReason.COST_EXCEEDS_RISK_SHARE,
            detail=(
                f"estimated cost {estimated_cost} is "
                f"{estimated_cost / risk_budget:.0%} of the risk budget"
            ),
            **common,  # type: ignore[arg-type]
        )

    return RiskGateDecision(
        decision=RiskDecision.LIMITED if tighter else RiskDecision.ACCEPTABLE,
        detail=(f"stop widened from {proposed} to the {floor} noise floor" if tighter else ""),
        **common,  # type: ignore[arg-type]
    )


class RiskFlag(StrEnum):
    """Descriptive state of an open position's risk. **Issues no SELL.**

    Inputs for a future position-management phase, which does not exist. A flag
    is a statement about volatility, not an instruction: ``RISK_EXTREME`` means
    the symbol moved into its own top decile, and says nothing about whether the
    position should be held.
    """

    STABLE = "RISK_STABLE"
    INCREASED = "RISK_INCREASED"
    DECREASED = "RISK_DECREASED"
    EXTREME = "RISK_EXTREME"
    DATA_STALE = "RISK_DATA_STALE"


MATERIAL_RISK_CHANGE: Final = Decimal("0.20")
"""Relative change in the one-day band that counts as a move worth flagging.

Twenty percent. Below that the band wanders with ordinary ATR drift and a flag
on every tick would train a reader to ignore it.
"""


def flag_against_band(
    *, entry_band_1d: Decimal | None, current_risk: ShortHorizonRisk | None
) -> RiskFlag:
    """Flag a position from its **persisted** entry band.

    The rolling recompute runs against rows in a database, where the entry-time
    :class:`ShortHorizonRisk` object is long gone. Comparing against the stored
    band rather than re-deriving it also means the comparison cannot silently
    change when historical bars are re-adjusted for a split.

    Purely descriptive. Cannot close a position and returns no action --
    :class:`RiskFlag` has no member that means "exit".
    """
    if current_risk is None or current_risk.stale:
        return RiskFlag.DATA_STALE
    if current_risk.regime.value == "EXTREME_VOL":
        return RiskFlag.EXTREME
    if entry_band_1d is None or entry_band_1d <= 0:
        return RiskFlag.STABLE

    change = (Decimal(str(current_risk.risk_band_1d)) - entry_band_1d) / entry_band_1d
    if change >= MATERIAL_RISK_CHANGE:
        return RiskFlag.INCREASED
    if change <= -MATERIAL_RISK_CHANGE:
        return RiskFlag.DECREASED
    return RiskFlag.STABLE


def flag_position(
    *, entry_risk: ShortHorizonRisk, current_risk: ShortHorizonRisk | None
) -> RiskFlag:
    """Compare a position's current risk against its risk at entry.

    A thin adapter over :func:`flag_against_band` so there is one rule, not two.
    """
    return flag_against_band(
        entry_band_1d=Decimal(str(entry_risk.risk_band_1d)), current_risk=current_risk
    )
