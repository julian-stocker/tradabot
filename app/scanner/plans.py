"""Turning a score into a trade plan -- or refusing to.

**Signal quality is not trade quality.** This module exists because of one
situation: a score of 87 with strong resistance 0.3% overhead. The signal is
excellent and the trade is terrible, and a system that reports only the score
cannot tell the difference. Here it can, and says ``NO_TRADE``.

Everything is derived from market structure -- zones from
:mod:`app.scanner.levels`, which come from confirmed swings. No value is a
forecast. The target is "the next place price has repeatedly failed", not "where
we think it will go", and the wording throughout keeps that distinction because
the moment it blurs the whole thing becomes a prediction it cannot support.

Thresholds below are **engineering assumptions**. None was fitted: fitting three
parameters against 228 episodes would produce numbers that describe this dataset
and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from app.core.config import CostSettings
from app.costs.calculator import estimate_round_trip_cost
from app.scanner.enums import DataQuality
from app.scanner.horizons import TradingHorizon
from app.scanner.levels import BreakState, LevelMap, Zone

MIN_REWARD_RISK: Final = 1.5
"""Net reward:risk below which a setup is not worth taking.

An assumption, and a conventional one. At a 55% hit rate -- roughly the best the
phase-5.6 research measured for >=85 -- 1.5:1 is around break-even after costs,
so anything below it needs a hit rate this system has never demonstrated.
"""

MIN_HEADROOM_BPS: Final = 150.0
"""Minimum distance to the next resistance for a long to be worth entering.

**The rule that catches "score 87 under resistance".** 150 bps is about 1.5%:
below that the structural target is inside the noise, and the trade is paying a
round trip to capture a move smaller than the daily range.
"""

MAX_INVALIDATION_BPS: Final = 500.0
"""Beyond this the structural invalidation is too far to size around.

Not a stop-loss policy -- the paper broker owns that. This only rejects plans
whose nearest structural failure point is so distant that risk is unmanageable.
"""

ENTRY_BAND_ATR: Final = 0.5
"""Half-width of an entry area, in ATRs. An area, never a price."""


class SetupType(StrEnum):
    """Why this is a trade, structurally."""

    BREAKOUT = "BREAKOUT"
    RETEST = "RETEST"
    PULLBACK = "PULLBACK"
    MOMENTUM_CONTINUATION = "MOMENTUM_CONTINUATION"
    NONE = "NONE"


class Tradeability(StrEnum):
    """What a human should do, separately from what the score says."""

    ACTIONABLE_BUY = "ACTIONABLE_BUY"
    WATCH = "WATCH"
    NO_TRADE = "NO_TRADE"
    EXIT = "EXIT"
    NOT_AVAILABLE = "NOT_AVAILABLE"


@dataclass(frozen=True, slots=True)
class TradePlan:
    """A structural plan for one symbol on one horizon.

    Every price field may be ``None``. Missing evidence is preferable to invented
    precision, and a plan with no target is a plan that says so rather than one
    that guesses.
    """

    symbol: str
    horizon: TradingHorizon
    direction: int
    generated_at: datetime
    tradeability: Tradeability
    setup_type: SetupType = SetupType.NONE

    market_data_timestamp: datetime | None = None
    market_price: float | None = None
    bid: float | None = None
    ask: float | None = None

    signal_score: float | None = None
    signal_confidence: float | None = None

    entry_zone_low: float | None = None
    entry_zone_high: float | None = None

    nearest_support: Zone | None = None
    nearest_resistance: Zone | None = None

    invalidation_price: float | None = None
    invalidation_reason: str = ""

    target_1: float | None = None
    target_2: float | None = None
    target_reason: str = ""

    estimated_upside_bps: float | None = None
    estimated_downside_bps: float | None = None
    gross_reward_risk: float | None = None
    net_reward_risk: float | None = None

    target_source: str = "NONE"
    exit_policy: str = "FIXED_TARGET"
    structure_state: str = "NORMAL"

    level_confidence: float | None = None
    plan_confidence: float | None = None
    data_quality: DataQuality | None = None

    reason_codes: tuple[str, ...] = ()
    risk_codes: tuple[str, ...] = ()

    @property
    def is_actionable(self) -> bool:
        return self.tradeability is Tradeability.ACTIONABLE_BUY

    @property
    def entry_reference(self) -> float | None:
        """Midpoint of the entry area, used for arithmetic only."""
        if self.entry_zone_low is None or self.entry_zone_high is None:
            return None
        return (self.entry_zone_low + self.entry_zone_high) / 2

    def as_dict(self) -> dict[str, Any]:
        """Flat, ML-ready. Distances in bps so they are comparable across prices."""
        return {
            "symbol": self.symbol,
            "horizon": self.horizon.value,
            "direction": self.direction,
            "tradeability": self.tradeability.value,
            "setup_type": self.setup_type.value,
            "market_price": self.market_price,
            "signal_score": self.signal_score,
            "signal_confidence": self.signal_confidence,
            "entry_zone_low": self.entry_zone_low,
            "entry_zone_high": self.entry_zone_high,
            "invalidation_price": self.invalidation_price,
            "invalidation_reason": self.invalidation_reason,
            "target_1": self.target_1,
            "target_2": self.target_2,
            "target_reason": self.target_reason,
            "target_source": self.target_source,
            "exit_policy": self.exit_policy,
            "structure_state": self.structure_state,
            "estimated_upside_bps": self.estimated_upside_bps,
            "estimated_downside_bps": self.estimated_downside_bps,
            "gross_reward_risk": self.gross_reward_risk,
            "net_reward_risk": self.net_reward_risk,
            "distance_to_support_bps": (
                self.nearest_support.distance_bps(self.market_price)
                if self.nearest_support and self.market_price
                else None
            ),
            "distance_to_resistance_bps": (
                self.nearest_resistance.distance_bps(self.market_price)
                if self.nearest_resistance and self.market_price
                else None
            ),
            "support_strength": self.nearest_support.strength if self.nearest_support else None,
            "resistance_strength": (
                self.nearest_resistance.strength if self.nearest_resistance else None
            ),
            "support_touch_count": (
                self.nearest_support.touch_count if self.nearest_support else None
            ),
            "resistance_touch_count": (
                self.nearest_resistance.touch_count if self.nearest_resistance else None
            ),
            "level_confidence": self.level_confidence,
            "plan_confidence": self.plan_confidence,
            "data_quality": self.data_quality.value if self.data_quality else None,
            "reason_codes": list(self.reason_codes),
            "risk_codes": list(self.risk_codes),
        }


@dataclass(slots=True)
class PlanInputs:
    """Everything needed to build a plan, gathered by the caller."""

    symbol: str
    horizon: TradingHorizon
    generated_at: datetime
    levels: LevelMap
    market_price: float
    atr: float
    direction: int = 1
    score: float | None = None
    confidence: float | None = None
    bid: float | None = None
    ask: float | None = None
    spread_bps: float | None = None
    break_state: BreakState = BreakState.NONE
    data_quality: DataQuality = DataQuality.OK
    market_data_timestamp: datetime | None = None
    costs: CostSettings | None = None
    allow_atr_fallback: bool = False
    """Research only. ATR targets measure distance, not observed resistance."""
    reason_codes: tuple[str, ...] = field(default_factory=tuple)


def build_plan(inputs: PlanInputs) -> TradePlan:
    """Construct a plan, or a documented refusal.

    Order matters: preconditions, then structure, then arithmetic, then policy.
    A plan that fails a precondition never reaches the arithmetic, so there is no
    path that produces a reward:risk from stale data.
    """
    refusal = _precondition_failure(inputs)
    if refusal is not None:
        return _refused(inputs, refusal)

    support = inputs.levels.nearest_support(inputs.market_price)
    resistance = inputs.levels.nearest_resistance(inputs.market_price)

    setup = _setup_type(inputs, support=support, resistance=resistance)
    entry_low, entry_high = _entry_zone(inputs, setup=setup, support=support)
    invalidation, invalidation_reason = _invalidation(inputs, support=support, setup=setup)

    structure_state = detect_price_discovery(inputs.levels, inputs.market_price)
    proposal = select_target(
        levels=inputs.levels,
        market_price=inputs.market_price,
        atr=inputs.atr,
        structure_state=structure_state,
        break_state=inputs.break_state,
        allow_atr_fallback=inputs.allow_atr_fallback,
    )
    target, target_reason = proposal.price, proposal.reason

    entry_reference = (entry_low + entry_high) / 2 if entry_low and entry_high else None
    upside, downside, gross, net = _reward_risk(
        entry=entry_reference,
        invalidation=invalidation,
        target=target,
        inputs=inputs,
    )

    risks: list[str] = []
    if resistance is not None and resistance.distance_bps(inputs.market_price) < MIN_HEADROOM_BPS:
        risks.append("resistance_overhead")
    if inputs.data_quality is DataQuality.STALE:
        risks.append("stale_data")

    if proposal.exit_policy is ExitPolicy.TRAILING_STRUCTURE:
        # A trailing setup has no fixed target and therefore no honest
        # reward:risk. Rejecting it for lacking one would reject exactly the
        # setups the signal model produces most (94.7% of >=85 are in price
        # discovery), so it gets its own policy instead of a fabricated ratio.
        tradeability = _trailing_tradeability(
            inputs=inputs, entry=entry_reference, invalidation=invalidation, risks=risks
        )
    else:
        tradeability = _tradeability(
            inputs=inputs,
            setup=setup,
            entry=entry_reference,
            invalidation=invalidation,
            target=target,
            net_reward_risk=net,
            resistance=resistance,
            risks=risks,
        )

    return TradePlan(
        symbol=inputs.symbol,
        horizon=inputs.horizon,
        direction=inputs.direction,
        generated_at=inputs.generated_at,
        tradeability=tradeability,
        setup_type=setup,
        market_data_timestamp=inputs.market_data_timestamp,
        market_price=inputs.market_price,
        bid=inputs.bid,
        ask=inputs.ask,
        signal_score=inputs.score,
        signal_confidence=inputs.confidence,
        entry_zone_low=entry_low,
        entry_zone_high=entry_high,
        nearest_support=support,
        nearest_resistance=resistance,
        invalidation_price=invalidation,
        invalidation_reason=invalidation_reason,
        target_1=target,
        target_reason=target_reason,
        target_source=proposal.source.value,
        exit_policy=proposal.exit_policy.value,
        structure_state=structure_state.value,
        estimated_upside_bps=upside,
        estimated_downside_bps=downside,
        gross_reward_risk=gross,
        net_reward_risk=net,
        level_confidence=_level_confidence(support, resistance),
        plan_confidence=_plan_confidence(inputs, support, resistance, net),
        data_quality=inputs.data_quality,
        reason_codes=inputs.reason_codes,
        risk_codes=tuple(risks),
    )


def _precondition_failure(inputs: PlanInputs) -> str | None:
    """Reasons a plan cannot be built at all.

    Checked before anything else so no arithmetic ever runs on bad inputs.
    """
    if inputs.market_price <= 0:
        return "no_market_price"
    if inputs.data_quality in (DataQuality.MISSING, DataQuality.INSUFFICIENT):
        return "insufficient_data"
    if not inputs.levels.support and not inputs.levels.resistance:
        return "no_levels"
    if inputs.spread_bps is not None and inputs.spread_bps > 100:  # noqa: PLR2004
        # Phase 4 recorded 883-1118 bps after hours. A plan priced off that
        # spread would have a cost estimate detached from reality.
        return "implausible_spread"
    if inputs.direction <= 0:
        return "not_bullish"
    return None


def _refused(inputs: PlanInputs, reason: str) -> TradePlan:
    return TradePlan(
        symbol=inputs.symbol,
        horizon=inputs.horizon,
        direction=inputs.direction,
        generated_at=inputs.generated_at,
        tradeability=Tradeability.NOT_AVAILABLE,
        market_price=inputs.market_price or None,
        signal_score=inputs.score,
        data_quality=inputs.data_quality,
        risk_codes=(reason,),
    )


def _setup_type(inputs: PlanInputs, *, support: Zone | None, resistance: Zone | None) -> SetupType:
    """What kind of trade this structurally is."""
    if inputs.break_state in (BreakState.RETEST_IN_PROGRESS, BreakState.RETEST_CONFIRMED):
        return SetupType.RETEST
    if inputs.break_state in (BreakState.BREAKOUT_CONFIRMED, BreakState.BREAKOUT_ATTEMPT):
        return SetupType.BREAKOUT
    if support is not None and support.distance_bps(inputs.market_price) > -200:  # noqa: PLR2004
        # Within 2% above support: price has pulled back toward a defended level.
        return SetupType.PULLBACK
    if resistance is not None:
        return SetupType.MOMENTUM_CONTINUATION
    return SetupType.NONE


def _entry_zone(
    inputs: PlanInputs, *, setup: SetupType, support: Zone | None
) -> tuple[float | None, float | None]:
    """Where entering makes structural sense -- **not** simply the last price.

    A breakout is entered around the level it broke; a pullback around the
    support it is testing. Using the current price for both would make the entry
    a description of when the scanner happened to run.
    """
    band = inputs.atr * ENTRY_BAND_ATR
    if band <= 0:
        return None, None

    if setup is SetupType.PULLBACK and support is not None:
        centre = support.upper_bound
    else:
        centre = inputs.market_price

    return round(centre - band, 4), round(centre + band, 4)


def _invalidation(
    inputs: PlanInputs, *, support: Zone | None, setup: SetupType
) -> tuple[float | None, str]:
    """Where the thesis is wrong -- structural, never a flat percentage.

    Deliberately **not** called a stop-loss. It is the price at which the reason
    for the trade stops being true; what a portfolio does about that is the paper
    broker's business.
    """
    if support is None:
        return None, ""
    buffer = inputs.atr * 0.25
    price = support.lower_bound - buffer
    reason = (
        "below the support that defines the retest"
        if setup is SetupType.RETEST
        else "below the nearest validated support"
    )
    return round(price, 4), reason


def _target(resistance: Zone | None) -> tuple[float | None, str]:
    """The next structural obstacle. **Not a price forecast.**

    Named a target *area* everywhere it is rendered: it is where price has
    repeatedly failed before, which is a fact about the past, not a claim about
    the future.
    """
    if resistance is None:
        return None, ""
    return round(resistance.lower_bound, 4), "next validated resistance zone"


def _reward_risk(
    *,
    entry: float | None,
    invalidation: float | None,
    target: float | None,
    inputs: PlanInputs,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Upside, downside and reward:risk, gross and net of modelled costs.

    Returns all-``None`` for a nonsensical geometry -- target below entry,
    invalidation above it, zero risk. Those are not low-quality plans, they are
    arithmetic that means nothing, and producing a number anyway would put a
    ratio on a channel that no trade could realise.
    """
    if entry is None or invalidation is None or target is None:
        return None, None, None, None
    if target <= entry or invalidation >= entry:
        return None, None, None, None

    risk = entry - invalidation
    reward = target - entry
    if risk <= 0 or reward <= 0:
        return None, None, None, None

    upside_bps = reward / entry * 10_000
    downside_bps = risk / entry * 10_000
    gross = reward / risk

    net = gross
    if inputs.costs is not None:
        cost = estimate_round_trip_cost(
            entry_mid=Decimal(str(entry)),
            exit_mid=Decimal(str(target)),
            quantity=Decimal(1),
            spread_bps=Decimal(str(inputs.spread_bps or inputs.costs.default_spread_bps)),
            settings=inputs.costs,
        )
        # Costs are paid out of the reward, not the risk: they reduce what a
        # winner returns and add to what a loser costs.
        net_reward = reward - float(cost.total_cost)
        net = net_reward / risk if net_reward > 0 else 0.0

    return (
        round(upside_bps, 1),
        round(downside_bps, 1),
        round(gross, 2),
        round(net, 2),
    )


def _tradeability(  # noqa: PLR0911 -- one return per verdict; merging them hides the reasons
    *,
    inputs: PlanInputs,
    setup: SetupType,  # noqa: ARG001 -- part of the signature so a future rule can use it
    entry: float | None,
    invalidation: float | None,
    target: float | None,
    net_reward_risk: float | None,
    resistance: Zone | None,
    risks: list[str],
) -> Tradeability:
    """**Where signal quality and trade quality are separated.**

    A qualified score is necessary and not sufficient. The additional
    requirements are all structural: somewhere to enter, somewhere the thesis
    breaks, somewhere to go, enough room to get there, and enough reward for the
    risk.

    Failing any of them yields WATCH -- the setup is interesting and the entry is
    not there yet -- rather than silence, because "wait" is useful information.
    """
    if entry is None or invalidation is None or target is None:
        return Tradeability.WATCH

    if resistance is not None:
        headroom = resistance.distance_bps(inputs.market_price)
        if headroom < MIN_HEADROOM_BPS:
            # The score-87-under-resistance case.
            return Tradeability.NO_TRADE

    if net_reward_risk is None or net_reward_risk < MIN_REWARD_RISK:
        return Tradeability.WATCH

    downside = (entry - invalidation) / entry * 10_000
    if downside > MAX_INVALIDATION_BPS:
        return Tradeability.NO_TRADE

    if inputs.break_state is BreakState.BREAKOUT_ATTEMPT:
        # Unconfirmed by construction: confirmation needs a bar that has not
        # happened. Waiting for it is the entire point of the distinction.
        return Tradeability.WATCH

    if "stale_data" in risks:
        return Tradeability.WATCH

    return Tradeability.ACTIONABLE_BUY


def _level_confidence(support: Zone | None, resistance: Zone | None) -> float | None:
    present = [zone.confidence for zone in (support, resistance) if zone is not None]
    return round(sum(present) / len(present), 4) if present else None


def _plan_confidence(
    inputs: PlanInputs,
    support: Zone | None,
    resistance: Zone | None,
    net_reward_risk: float | None,
) -> float | None:
    """How much the plan as a whole rests on solid evidence.

    Distinct from the signal's own confidence: a high-confidence signal at a
    location with one weak level is a low-confidence *plan*.
    """
    levels = _level_confidence(support, resistance)
    if levels is None:
        return None
    quality = 1.0 if inputs.data_quality is DataQuality.OK else 0.6
    geometry = min(1.0, (net_reward_risk or 0.0) / (MIN_REWARD_RISK * 2))
    return round(0.5 * levels + 0.3 * quality + 0.2 * geometry, 4)


# ---------------------------------------------------------------------------
# Price discovery and target strategies (phase 5.7B)
# ---------------------------------------------------------------------------
class TargetSource(StrEnum):
    """Where a target came from -- always visible, never implied.

    A structural target and an ATR projection are different kinds of claim. The
    first says "price has repeatedly failed here"; the second says "this is one
    volatility unit away". Rendering both as "Target" would let the reader treat
    an arithmetic convenience as evidence.
    """

    STRUCTURAL_RESISTANCE = "STRUCTURAL_RESISTANCE"
    MEASURED_MOVE = "MEASURED_MOVE"
    ATR_1_0 = "ATR_1_0"
    ATR_1_5 = "ATR_1_5"
    ATR_2_0 = "ATR_2_0"
    NONE_TRAILING_STRUCTURE = "NONE_TRAILING_STRUCTURE"
    NONE = "NONE"

    @property
    def is_fixed(self) -> bool:
        return self not in {TargetSource.NONE_TRAILING_STRUCTURE, TargetSource.NONE}

    @property
    def is_evidence_based(self) -> bool:
        """Whether the target rests on observed price behaviour.

        Only the first two do. ATR targets are a research fallback: they measure
        distance, not a place anyone defended.
        """
        return self in {TargetSource.STRUCTURAL_RESISTANCE, TargetSource.MEASURED_MOVE}


class ExitPolicy(StrEnum):
    FIXED_TARGET = "FIXED_TARGET"
    TRAILING_STRUCTURE = "TRAILING_STRUCTURE"


class MarketStructureState(StrEnum):
    """Where price sits relative to its own recorded history."""

    NORMAL = "NORMAL"
    PRICE_DISCOVERY = "PRICE_DISCOVERY"
    """No validated resistance above, **within the available lookback**.

    Deliberately not "all-time high". The dataset holds at most six years and the
    hourly series far less, so a claim about all time is one the data cannot
    support. The wording everywhere is "within the available data window".
    """


PRICE_DISCOVERY_WORDING: Final = "price discovery within available lookback"
"""User-facing phrasing. Never 'all-time high' -- see :class:`MarketStructureState`."""

ATR_MULTIPLES: Final[dict[TargetSource, float]] = {
    TargetSource.ATR_1_0: 1.0,
    TargetSource.ATR_1_5: 1.5,
    TargetSource.ATR_2_0: 2.0,
}
"""A small fixed research set. **Not a continuous parameter to be optimised.**"""

MIN_RANGE_ATR_FOR_MEASURED_MOVE: Final = 1.5
"""A pre-breakout range must be at least this tall (in ATRs) to project.

Below it the "range" is noise, and projecting noise produces a target that looks
structural and is not. This is what stops a measured move being manufactured for
an arbitrary momentum setup.
"""


@dataclass(frozen=True, slots=True)
class TargetProposal:
    """One strategy's answer, or its refusal."""

    source: TargetSource
    price: float | None
    reason: str
    exit_policy: ExitPolicy = ExitPolicy.FIXED_TARGET


def detect_price_discovery(levels: LevelMap, market_price: float) -> MarketStructureState:
    """Is there any validated resistance above the market, in what we hold?"""
    return (
        MarketStructureState.NORMAL
        if levels.nearest_resistance(market_price) is not None
        else MarketStructureState.PRICE_DISCOVERY
    )


def structural_target(levels: LevelMap, market_price: float) -> TargetProposal:
    """The next place price has repeatedly failed. The preferred target."""
    zone = levels.nearest_resistance(market_price)
    if zone is None:
        return TargetProposal(
            TargetSource.NONE, None, "no validated resistance in the available lookback"
        )
    return TargetProposal(
        TargetSource.STRUCTURAL_RESISTANCE, zone.lower_bound, "next validated resistance zone"
    )


def measured_move_target(levels: LevelMap, market_price: float, atr: float) -> TargetProposal:
    """Project the height of the range price just broke out of.

    Valid only when a genuine range existed *before* the breakout: a support and
    a resistance zone both below the market, separated by at least
    :data:`MIN_RANGE_ATR_FOR_MEASURED_MOVE` ATRs. Both zones are built from
    swings confirmed before the decision bar, so the projection uses nothing the
    trader could not have measured at the time.
    """
    below_resistance = [z for z in levels.resistance if z.upper_bound <= market_price]
    support = levels.nearest_support(market_price)
    if not below_resistance or support is None:
        return TargetProposal(TargetSource.NONE, None, "no completed range to project")

    breakout_level = max(below_resistance, key=lambda z: z.upper_bound)
    height = breakout_level.midpoint - support.midpoint
    if atr <= 0 or height <= atr * MIN_RANGE_ATR_FOR_MEASURED_MOVE:
        return TargetProposal(TargetSource.NONE, None, "pre-breakout range too small to project")

    return TargetProposal(
        TargetSource.MEASURED_MOVE,
        round(breakout_level.upper_bound + height, 4),
        "range height projected from the broken level",
    )


def atr_target(market_price: float, atr: float, source: TargetSource) -> TargetProposal:
    """A distance, not a level. Research fallback only."""
    multiple = ATR_MULTIPLES.get(source)
    if multiple is None or atr <= 0:
        return TargetProposal(TargetSource.NONE, None, "no ATR available")
    return TargetProposal(
        source,
        round(market_price + atr * multiple, 4),
        f"{multiple:g} ATR from market -- a distance, not observed resistance",
    )


def trailing_target() -> TargetProposal:
    """No fixed target, and no pretence of one.

    Entry and invalidation are defined; the exit is a rule applied to bars as
    they arrive. Inventing a price here would be exactly the fabrication this
    whole module refuses.
    """
    return TargetProposal(
        TargetSource.NONE_TRAILING_STRUCTURE,
        None,
        "no fixed target; exit trails bullish structure",
        exit_policy=ExitPolicy.TRAILING_STRUCTURE,
    )


def select_target(
    *,
    levels: LevelMap,
    market_price: float,
    atr: float,
    structure_state: MarketStructureState,
    break_state: BreakState,
    allow_atr_fallback: bool = False,
) -> TargetProposal:
    """Deterministic precedence. **Not target shopping.**

    Fixed before any outcome was examined, and it never picks whichever strategy
    yields the best reward:risk -- that would be selecting the target to justify
    the trade, which is how a backtest talks itself into anything.

    1. structural resistance, when it exists;
    2. a measured move, for a confirmed breakout out of a real range;
    3. trailing structure, in price discovery;
    4. an ATR distance only when explicitly enabled for research.
    """
    structural = structural_target(levels, market_price)
    if structural.price is not None:
        return structural

    if break_state.is_confirmed_break:
        measured = measured_move_target(levels, market_price, atr)
        if measured.price is not None:
            return measured

    if allow_atr_fallback:
        return atr_target(market_price, atr, TargetSource.ATR_1_5)

    if structure_state is MarketStructureState.PRICE_DISCOVERY:
        return trailing_target()

    return TargetProposal(TargetSource.NONE, None, "no defensible target")


MAX_TRAILING_INVALIDATION_BPS: Final = 400.0
"""Trailing setups need a *tighter* invalidation than fixed-target ones.

There is no target to bound the trade, so the only thing limiting loss is where
the thesis breaks. A distant invalidation on an unbounded trade is an unbounded
risk, which is why this is stricter than :data:`MAX_INVALIDATION_BPS`.
"""


def _trailing_tradeability(
    *,
    inputs: PlanInputs,
    entry: float | None,
    invalidation: float | None,
    risks: list[str],
) -> Tradeability:
    """Conservative policy for setups with no fixed target.

    **Predefined before any trailing outcome was examined.** Deliberately strict:
    the risk of this path is that "no resistance above" becomes a reason to buy
    anything making a new high, so it requires a *confirmed* break rather than an
    attempt, clean data, and a close invalidation.
    """
    if entry is None or invalidation is None:
        return Tradeability.WATCH

    downside = (entry - invalidation) / entry * 10_000
    if downside > MAX_TRAILING_INVALIDATION_BPS:
        return Tradeability.NO_TRADE

    if inputs.break_state is not BreakState.BREAKOUT_CONFIRMED:
        # A new high without a confirmed break is momentum, not a setup.
        return Tradeability.WATCH

    if inputs.data_quality is not DataQuality.OK or "stale_data" in risks:
        return Tradeability.WATCH

    return Tradeability.ACTIONABLE_BUY


# ---------------------------------------------------------------------------
# Retest entry (phase 5.7C)
# ---------------------------------------------------------------------------
MIN_EPISODES_FOR_LIVE_BUY: Final = 30
"""Historical actionable episodes required before BUY may go live.

**Declared before the retest experiment was run**, so it cannot be relaxed once
the answer is known. This is a product-safety floor, not a statistical claim: 30
episodes proves nothing, but below it a live BUY feed would be firing on a
pattern nobody has ever seen behave.

50 would be preferable. If the reconstruction returns fewer than 30, the correct
outcome is ``INSUFFICIENT_SAMPLE`` and BUY stays disabled.
"""

PREFERRED_EPISODES_FOR_LIVE_BUY: Final = 50

RETEST_PROXIMITY_ATR: Final = 0.5
"""How close to the broken zone counts as a retest touch.

Price rarely returns exactly into the zone; requiring an exact re-entry would
miss most real retests. Half an ATR is the same tolerance the zone width itself
uses.
"""


class RetestState(StrEnum):
    """Where a broken-resistance setup stands in its retest sequence.

    The sequence exists because of a measured tension: by the time a breakout
    confirms, price has usually travelled 525 bps (median) from validated
    support, so the structural invalidation is too distant to size around.
    Waiting for price to come back to the broken level puts entry next to
    support again -- which is the whole hypothesis this phase tests.
    """

    NONE = "NONE"
    WATCH_FOR_RETEST = "WATCH_FOR_RETEST"
    """Breakout confirmed; price has not returned yet."""
    RETEST_IN_PROGRESS = "RETEST_IN_PROGRESS"
    """Price has touched the broken zone."""
    RETEST_CONFIRMED = "RETEST_CONFIRMED"
    """Touched and reclaimed: the level is acting as support."""
    FAILED_RETEST = "FAILED_RETEST"
    """Price closed decisively back below the zone. The thesis is wrong."""

    @property
    def is_entry_ready(self) -> bool:
        return self is RetestState.RETEST_CONFIRMED


@dataclass(frozen=True, slots=True)
class RetestContext:
    """The retest sequence for one broken zone, reconstructed causally."""

    state: RetestState
    zone: Zone | None = None
    breakout_timestamp: datetime | None = None
    retest_first_touch: datetime | None = None
    retest_confirmation_timestamp: datetime | None = None

    @property
    def retest_zone_low(self) -> float | None:
        return self.zone.lower_bound if self.zone else None

    @property
    def retest_zone_high(self) -> float | None:
        return self.zone.upper_bound if self.zone else None


def track_retest(*, zone: Zone, bars: list[Any], atr: float) -> RetestContext:
    """Walk bars forward from a breakout and label the retest sequence.

    **Strictly causal.** Each bar is examined in order and the state advances
    only on information that bar carried; nothing looks ahead. The returned state
    is what a trader watching live would have known at the last bar.

    A retest requires three things in order: a confirmed break above the zone,
    a return to within :data:`RETEST_PROXIMITY_ATR` of it, and a subsequent close
    back above it. A close decisively below ends the sequence as ``FAILED_RETEST``
    -- broken resistance that does not hold is not support.
    """
    tolerance = atr * RETEST_PROXIMITY_ATR
    broke_at: datetime | None = None
    touched_at: datetime | None = None
    confirmed_at: datetime | None = None
    state = RetestState.NONE

    for index, bar in enumerate(bars):
        close = float(bar.close)
        low = float(bar.low)

        if state is RetestState.NONE:
            # Confirmation needs two consecutive closes above: one close is an
            # attempt, and treating it as a break would import the look-ahead
            # `classify_break` exists to avoid.
            if close > zone.upper_bound and index > 0:
                previous = float(bars[index - 1].close)
                if previous > zone.upper_bound:
                    state = RetestState.WATCH_FOR_RETEST
                    broke_at = bar.timestamp
            continue

        if state is RetestState.WATCH_FOR_RETEST:
            if close < zone.lower_bound - tolerance:
                state = RetestState.FAILED_RETEST
            elif low <= zone.upper_bound + tolerance:
                state = RetestState.RETEST_IN_PROGRESS
                touched_at = bar.timestamp
            continue

        if state is RetestState.RETEST_IN_PROGRESS:
            if close < zone.lower_bound - tolerance:
                state = RetestState.FAILED_RETEST
            elif close > zone.upper_bound:
                state = RetestState.RETEST_CONFIRMED
                confirmed_at = bar.timestamp
            continue

        if state is RetestState.RETEST_CONFIRMED and close < zone.lower_bound - tolerance:
            state = RetestState.FAILED_RETEST

    return RetestContext(
        state=state,
        zone=zone,
        breakout_timestamp=broke_at,
        retest_first_touch=touched_at,
        retest_confirmation_timestamp=confirmed_at,
    )


def retest_invalidation(zone: Zone, atr: float) -> tuple[float, str]:
    """Below the reclaimed zone, plus a volatility buffer.

    Structural, not a percentage: the thesis is "this level now acts as support",
    so the thesis is wrong exactly when price closes below it. The buffer is a
    quarter ATR, matching the ordinary invalidation rule.
    """
    return (
        round(zone.lower_bound - atr * 0.25, 4),
        "below the reclaimed breakout zone (role-reversal support)",
    )
