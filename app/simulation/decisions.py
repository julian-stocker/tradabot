"""Trade-decision evaluation.

A **pure function** from (signal, profile, quote) to a decision. No database, no
portfolio state, no order placement -- that is the phase 3 paper-trading engine.
What lives here is the part that must exist for a decision record to mean
anything: position sizing and the gates the sizing feeds.

Why this belongs in phase 2 rather than phase 3
-----------------------------------------------
The whole point of multi-profile simulation is that the *same* signal produces
different verdicts for different portfolios. That claim is only testable if
something actually computes the verdicts. Without it, ``TradeDecision`` would be
a table nothing writes to.

The gate order matters
----------------------
Cheap conviction checks run before expensive economic ones, and the economic
gates run **at the profile's actual position size**. A signal rejected for a low
score is rejected for every portfolio; a signal rejected because a 1.00 EUR fee
eats a 5 EUR position is rejected only by the small ones. Recording *which* gate
fired is what makes the decision log analysable later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.time import utc_now
from app.costs.calculator import estimate_round_trip_cost
from app.costs.models import BPS, RoundTripCost
from app.domain.enums import Classification, DecisionReason, Side, TradeDecisionType
from app.domain.quotes import Quote
from app.signals.models import SignalResult
from app.simulation.models import SimulationProfileConfig

ZERO = Decimal(0)
QUANTITY_EXPONENT = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class TradeDecision:
    """One profile's verdict on one signal, with the economics behind it.

    Everything needed to answer "why?" months later is on the record, computed at
    this profile's size. Nothing here is recomputed at read time, because the
    profile may have changed since.
    """

    profile_name: str
    symbol: str
    decided_at: datetime
    decision: TradeDecisionType
    reason: DecisionReason
    reason_detail: str
    side: Side | None

    signal_score: float
    signal_classification: Classification
    signal_confidence: float
    expected_move_bps: Decimal

    reference_price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    spread_bps: Decimal

    available_capital: Decimal
    position_quantity: Decimal
    position_notional: Decimal
    estimated_fees: Decimal
    estimated_spread_cost: Decimal
    estimated_slippage: Decimal
    estimated_total_cost: Decimal
    cost_bps_at_size: Decimal
    net_edge_bps_at_size: Decimal

    @property
    def is_trade(self) -> bool:
        return self.decision is TradeDecisionType.TRADE


def evaluate_decision(  # noqa: PLR0911 -- one early return per gate; see below
    *,
    signal: SignalResult,
    profile: SimulationProfileConfig,
    quote: Quote | None = None,
    available_capital: Decimal | None = None,
    now: datetime | None = None,
) -> TradeDecision:
    """Decide whether ``profile`` would act on ``signal``.

    The many early returns are the point, not an accident: each is one gate, and
    which gate fires *is* the recorded outcome. Collapsing them into nested
    conditionals or a single exit would obscure exactly the information the
    decision log exists to capture.

    Args:
        signal: the signal to evaluate. Shared across profiles unchanged.
        profile: the portfolio's capital, risk appetite and cost assumptions.
        quote: live top-of-book, when available. Falls back to the signal's
            spread, then to the profile's configured default.
        available_capital: current free capital. Defaults to the profile's
            initial capital, which is correct only for a portfolio with no open
            positions -- the phase 3 engine will supply the real figure.
        now: decision timestamp, injectable for deterministic tests.

    Returns:
        A :class:`TradeDecision`, whether the verdict is TRADE or SKIP. A skip is
        a recorded observation, not an absence of one.
    """
    decided_at = now or utc_now()
    capital = available_capital if available_capital is not None else profile.initial_capital
    spread_bps = _resolve_spread_bps(signal, quote, profile)
    risk = profile.risk

    context = _Context(
        signal=signal,
        profile=profile,
        decided_at=decided_at,
        capital=capital,
        spread_bps=spread_bps,
        quote=quote,
    )

    # --- Gate 1: is the profile even running? ---------------------------
    if not profile.enabled:
        return context.skip(
            DecisionReason.PROFILE_DISABLED,
            f"Profile {profile.name!r} is disabled.",
        )

    # --- Gate 2: conviction (capital-independent) ------------------------
    if signal.classification is Classification.NEUTRAL:
        return context.skip(
            DecisionReason.CLASSIFICATION_NEUTRAL,
            "Signal is NEUTRAL; there is no direction to act on.",
        )

    if abs(Decimal(str(signal.score))) < risk.min_signal_score:
        return context.skip(
            DecisionReason.SCORE_BELOW_THRESHOLD,
            f"|score| {abs(signal.score):.1f} is below the {risk.name} threshold "
            f"of {risk.min_signal_score}.",
        )

    if Decimal(str(signal.confidence)) < risk.min_confidence:
        return context.skip(
            DecisionReason.CONFIDENCE_BELOW_THRESHOLD,
            f"Confidence {signal.confidence:.2f} is below the {risk.name} threshold "
            f"of {risk.min_confidence}.",
        )

    side = Side.LONG if signal.classification.direction > 0 else Side.SHORT
    if side is Side.SHORT and not risk.allow_short:
        return context.skip(
            DecisionReason.SHORT_NOT_PERMITTED,
            f"Signal is bearish but the {risk.name} profile does not permit shorts.",
        )

    # --- Gate 3: sizing (where capital starts to matter) -----------------
    notional = min(capital, profile.max_position_notional)
    if notional <= 0 or signal.reference_price <= 0:
        return context.skip(
            DecisionReason.INSUFFICIENT_CAPITAL,
            f"No capital available to size a position (capital={capital}).",
        )

    quantity = (notional / signal.reference_price).quantize(QUANTITY_EXPONENT)
    if quantity <= 0:
        return context.skip(
            DecisionReason.INSUFFICIENT_CAPITAL,
            f"Position size rounds to zero: {notional} {profile.currency} at "
            f"{signal.reference_price} per unit.",
        )

    actual_notional = quantity * signal.reference_price
    if actual_notional < profile.costs.min_order_notional:
        return context.skip(
            DecisionReason.POSITION_BELOW_MIN_NOTIONAL,
            f"Position {actual_notional:.2f} {profile.currency} is below the broker "
            f"minimum of {profile.costs.min_order_notional}.",
            quantity=quantity,
            notional=actual_notional,
        )

    # --- Gate 4: economics at THIS size ----------------------------------
    cost = estimate_round_trip_cost(
        entry_mid=signal.reference_price,
        exit_mid=signal.reference_price,
        quantity=quantity,
        spread_bps=spread_bps,
        settings=profile.costs.to_cost_settings(),
        side=side,
    )
    cost_bps = cost.total_cost_bps
    net_edge_bps = signal.net_edge.expected_move_bps - cost_bps

    if risk.require_positive_net_edge and net_edge_bps <= 0:
        return context.skip(
            DecisionReason.NEGATIVE_NET_EDGE,
            f"Expected move {signal.net_edge.expected_move_bps:.1f} bps does not cover "
            f"the {cost_bps:.1f} bps round-trip cost of a "
            f"{actual_notional:.2f} {profile.currency} position "
            f"(net {net_edge_bps:.1f} bps).",
            quantity=quantity,
            notional=actual_notional,
            cost=cost,
            side=side,
        )

    return context.trade(
        side=side,
        quantity=quantity,
        notional=actual_notional,
        cost=cost,
        detail=(
            f"{signal.classification.value} at score {signal.score:.1f}; "
            f"{actual_notional:.2f} {profile.currency} position keeps "
            f"{net_edge_bps:.1f} bps of net edge after {cost_bps:.1f} bps of costs."
        ),
    )


@dataclass(frozen=True, slots=True)
class _Context:
    """Shared inputs, so each exit path does not restate them."""

    signal: SignalResult
    profile: SimulationProfileConfig
    decided_at: datetime
    capital: Decimal
    spread_bps: Decimal
    quote: Quote | None

    def skip(
        self,
        reason: DecisionReason,
        detail: str,
        *,
        quantity: Decimal = ZERO,
        notional: Decimal = ZERO,
        cost: RoundTripCost | None = None,
        side: Side | None = None,
    ) -> TradeDecision:
        return self._build(
            decision=TradeDecisionType.SKIP,
            reason=reason,
            detail=detail,
            side=side,
            quantity=quantity,
            notional=notional,
            cost=cost,
        )

    def trade(
        self,
        *,
        side: Side,
        quantity: Decimal,
        notional: Decimal,
        cost: RoundTripCost,
        detail: str,
    ) -> TradeDecision:
        return self._build(
            decision=TradeDecisionType.TRADE,
            reason=DecisionReason.ACCEPTED,
            detail=detail,
            side=side,
            quantity=quantity,
            notional=notional,
            cost=cost,
        )

    def _build(
        self,
        *,
        decision: TradeDecisionType,
        reason: DecisionReason,
        detail: str,
        side: Side | None,
        quantity: Decimal,
        notional: Decimal,
        cost: RoundTripCost | None,
    ) -> TradeDecision:
        cost_bps = cost.total_cost_bps if cost is not None else ZERO
        expected_move = self.signal.net_edge.expected_move_bps
        return TradeDecision(
            profile_name=self.profile.name,
            symbol=self.signal.symbol,
            decided_at=self.decided_at,
            decision=decision,
            reason=reason,
            reason_detail=detail[:500],
            side=side,
            signal_score=self.signal.score,
            signal_classification=self.signal.classification,
            signal_confidence=self.signal.confidence,
            expected_move_bps=expected_move,
            reference_price=self.signal.reference_price,
            bid=self.quote.bid if self.quote is not None else None,
            ask=self.quote.ask if self.quote is not None else None,
            spread_bps=self.spread_bps,
            available_capital=self.capital,
            position_quantity=quantity,
            position_notional=notional,
            estimated_fees=cost.breakdown.fee_cost if cost is not None else ZERO,
            estimated_spread_cost=cost.breakdown.spread_cost if cost is not None else ZERO,
            estimated_slippage=cost.breakdown.slippage_cost if cost is not None else ZERO,
            estimated_total_cost=cost.total_cost if cost is not None else ZERO,
            cost_bps_at_size=cost_bps.quantize(Decimal("0.0001")),
            net_edge_bps_at_size=(expected_move - cost_bps).quantize(Decimal("0.0001")),
        )


def _resolve_spread_bps(
    signal: SignalResult, quote: Quote | None, profile: SimulationProfileConfig
) -> Decimal:
    """Spread to charge this decision.

    A live quote wins, because it is the only measured value. Otherwise the
    signal's own spread (which may itself be a fallback), and finally the
    profile's configured default. Each step down is a step further from
    observation, which is why the resolution order is explicit rather than a
    silent ``or``.
    """
    if quote is not None:
        return Decimal(str(quote.spread_bps))
    if signal.spread_bps > 0:
        return signal.spread_bps
    return profile.costs.default_spread_bps


def cost_bps_for_notional(
    *, notional: Decimal, price: Decimal, spread_bps: Decimal, profile: SimulationProfileConfig
) -> Decimal:
    """Round-trip cost in bps for a position of ``notional`` at ``price``.

    Exposed for analysis and testing: it is the cleanest way to show that the
    identical signal and broker produce very different economics at 50 EUR and
    5000 EUR.
    """
    if price <= 0 or notional <= 0:
        return ZERO
    quantity = notional / price
    cost = estimate_round_trip_cost(
        entry_mid=price,
        exit_mid=price,
        quantity=quantity,
        spread_bps=spread_bps,
        settings=profile.costs.to_cost_settings(),
    )
    return (cost.total_cost / notional * BPS).quantize(Decimal("0.0001"))
