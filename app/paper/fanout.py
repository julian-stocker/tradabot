"""One candidate, three accounts, three independent answers.

The experiment in one function
------------------------------
:func:`fan_out` takes a single :class:`~app.strategy.match_b.MatchBCandidate` and
asks each account what it can do with it. The candidate object is passed by
reference and never copied or adjusted, because the entire claim of the
experiment is that all three arms see *the same* opportunity and differ only in
what their capital permits.

Account role is metadata
------------------------
``CAPITAL_CONSTRAINT_ARM`` / ``TRANSITION_ARM`` /
``FULL_STRATEGY_REFERENCE_ARM`` exist for reporting. Nothing in the decision path
reads them, and a test asserts it: the moment a role changes a threshold, the
three accounts stop running the same strategy and the comparison means nothing.

Order of evaluation, and why refusals are the finding
-----------------------------------------------------
Each slot runs: account state → effective capital (no leverage) → risk-v1 → risk
gate → canonical sizing → **whole-share floor** → exposure and cash. Every
refusal is returned with its binding constraint rather than dropped, because
phase 12.7 measured PAPER_1K reaching only 27.4% of candidates and the 72.6% it
refuses — and *why* — is the result this experiment exists to produce.

The whole-share floor is applied **after** ranking. It is an execution
constraint, never a filter on the cross-section; filtering expensive symbols
before ranking would hand the small account a different alpha model.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from app.broker.paper_accounts import (
    ExperimentCapital,
    PaperAccountSlot,
    PaperAccountState,
    effective_capital,
)
from app.broker.paper_orders import WholeShareFeasibility, whole_share_feasibility
from app.core.config import CostSettings
from app.market_data.risk import ShortHorizonRisk
from app.paper.risk_gate import RiskDecision, evaluate_entry
from app.paper.sizing import ExecutionFractionality, size_position
from app.simulation.models import SimulationProfileConfig
from app.strategy.match_b import MatchBCandidate


class AccountRole(StrEnum):
    """What each arm is for. **Reporting metadata only.**"""

    CAPITAL_CONSTRAINT = "CAPITAL_CONSTRAINT_ARM"
    TRANSITION = "TRANSITION_ARM"
    FULL_STRATEGY_REFERENCE = "FULL_STRATEGY_REFERENCE_ARM"


ACCOUNT_ROLES: Final[dict[PaperAccountSlot, AccountRole]] = {
    PaperAccountSlot.PAPER_1K: AccountRole.CAPITAL_CONSTRAINT,
    PaperAccountSlot.PAPER_3K: AccountRole.TRANSITION,
    PaperAccountSlot.PAPER_10K: AccountRole.FULL_STRATEGY_REFERENCE,
}


class DecisionOutcome(StrEnum):
    """What one account concluded. Only the first is an intent to trade."""

    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    RISK_REJECTED = "RISK_REJECTED"
    RISK_UNAVAILABLE = "RISK_UNAVAILABLE"
    INSUFFICIENT_CAPITAL = "INSUFFICIENT_CAPITAL"
    WHOLE_SHARE_NOT_FEASIBLE = "WHOLE_SHARE_NOT_FEASIBLE"
    """One whole share does not fit the risk-derived allowance.

    The defining constraint of the small-capital arm, and the reason it gets its
    own outcome rather than being folded into ``INSUFFICIENT_CAPITAL``: the
    account may hold ample cash and still be unable to take a position, and
    conflating the two would make PAPER_1K's coverage unreadable.
    """

    EXPOSURE_LIMIT = "EXPOSURE_LIMIT"
    POSITION_ALREADY_OPEN = "POSITION_ALREADY_OPEN"
    ORDER_ALREADY_EXISTS = "ORDER_ALREADY_EXISTS"
    DATA_STALE = "DATA_STALE"
    ACCOUNT_UNAVAILABLE = "ACCOUNT_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class AccountDecision:
    """One account's answer, with everything needed to explain it later."""

    candidate_id: str
    slot: PaperAccountSlot
    role: AccountRole
    outcome: DecisionOutcome

    equity: Decimal
    cash: Decimal
    effective_capital: Decimal
    current_exposure: Decimal
    risk_budget: Decimal

    risk_available: bool
    risk_regime: str | None = None
    stop_distance: Decimal | None = None

    risk_sized_quantity: Decimal | None = None
    proposed_quantity: Decimal | None = None
    proposed_notional: Decimal | None = None
    whole_share_feasible: bool = False

    binding_constraint: str | None = None
    detail: str = ""

    @property
    def is_actionable(self) -> bool:
        return self.outcome is DecisionOutcome.READY_TO_SUBMIT


def _refusal(
    candidate: MatchBCandidate,
    slot: PaperAccountSlot,
    capital: ExperimentCapital,
    state: PaperAccountState,
    risk_budget: Decimal,
    outcome: DecisionOutcome,
    detail: str,
    *,
    risk: ShortHorizonRisk | None = None,
    feasibility: WholeShareFeasibility | None = None,
    binding: str | None = None,
    exposure: Decimal = Decimal(0),
) -> AccountDecision:
    return AccountDecision(
        candidate_id=candidate.candidate_id,
        slot=slot,
        role=ACCOUNT_ROLES[slot],
        outcome=outcome,
        equity=capital.equity,
        cash=state.cash,
        effective_capital=capital.max_exposure,
        current_exposure=exposure,
        risk_budget=risk_budget,
        risk_available=risk is not None,
        risk_regime=risk.regime.value if risk else None,
        risk_sized_quantity=feasibility.fractional_shares if feasibility else None,
        whole_share_feasible=bool(feasibility and feasibility.feasible),
        binding_constraint=binding,
        detail=detail,
    )


def decide_for_account(  # noqa: PLR0911 -- one return per refusal reason; merging
    # any two would record the wrong binding constraint, and the constraints are
    # the experiment's primary result
    candidate: MatchBCandidate,
    *,
    slot: PaperAccountSlot,
    state: PaperAccountState,
    risk: ShortHorizonRisk | None,
    profile: SimulationProfileConfig,
    costs: CostSettings,
    current_exposure: Decimal = Decimal(0),
    has_open_position: bool = False,
    has_open_order: bool = False,
) -> AccountDecision:
    """Evaluate one candidate for one account. **Never raises for a refusal.**

    The candidate is read, never modified. Every path returns an
    :class:`AccountDecision`, so a slot that cannot act still produces a record
    explaining why.
    """
    capital = effective_capital(state)
    risk_budget = capital.equity * profile.risk.risk_per_trade
    price = candidate.reference_price

    def refuse(outcome: DecisionOutcome, detail: str, **kw: object) -> AccountDecision:
        return _refusal(
            candidate,
            slot,
            capital,
            state,
            risk_budget,
            outcome,
            detail,
            risk=risk,
            exposure=current_exposure,
            **kw,  # type: ignore[arg-type]
        )

    if not state.can_execute:
        return refuse(
            DecisionOutcome.ACCOUNT_UNAVAILABLE,
            f"account not executable (status={state.status}, error={state.error})",
        )
    if has_open_position:
        return refuse(DecisionOutcome.POSITION_ALREADY_OPEN, f"already long {candidate.symbol}")
    if has_open_order:
        return refuse(DecisionOutcome.ORDER_ALREADY_EXISTS, f"order open for {candidate.symbol}")
    if capital.max_exposure <= current_exposure:
        return refuse(
            DecisionOutcome.EXPOSURE_LIMIT,
            f"exposure {current_exposure} already at the {capital.max_exposure} cap",
            binding="MAX_TOTAL_EXPOSURE",
        )

    # --- risk-v1 and the frozen risk gate -------------------------------
    structural_stop = price - (
        Decimal(str(candidate.atr_pct))
        / Decimal(100)
        * price
        * (profile.risk.stop_loss_atr_multiple or Decimal(2))
    )
    gate = evaluate_entry(
        risk=risk,
        entry_price=price,
        structural_stop=structural_stop,
        risk_budget=risk_budget,
        costs=costs,
        allow_stale=False,
    )
    if gate.decision is RiskDecision.UNAVAILABLE:
        return refuse(DecisionOutcome.RISK_UNAVAILABLE, gate.detail)
    if not gate.permits_entry:
        outcome = (
            DecisionOutcome.DATA_STALE
            if gate.reason and gate.reason.value == "STALE_RISK_DATA"
            else DecisionOutcome.RISK_REJECTED
        )
        return refuse(outcome, gate.detail, binding=gate.reason.value if gate.reason else None)

    # --- canonical sizing, then the whole-share floor --------------------
    sizing = size_position(
        profile=profile,
        equity=capital.equity,
        available_cash=capital.usable_cash,
        current_exposure=current_exposure,
        entry_price=price,
        stop_loss=price - (gate.risk_distance or Decimal(0)),
        fractionality=ExecutionFractionality.FRACTIONAL_ALLOWED,
    )
    if not sizing.is_tradable:
        return refuse(
            DecisionOutcome.INSUFFICIENT_CAPITAL,
            sizing.detail,
            binding=sizing.rejection.value if sizing.rejection else None,
        )

    allowance = min(
        capital.equity * profile.risk.max_position_percent, capital.max_exposure - current_exposure
    )
    feasibility = whole_share_feasibility(
        risk_sized_quantity=sizing.quantity, price=price, max_notional=allowance
    )
    if not feasibility.feasible:
        return refuse(
            DecisionOutcome.WHOLE_SHARE_NOT_FEASIBLE,
            feasibility.shortfall_reason or "",
            feasibility=feasibility,
            binding="WHOLE_SHARE_FLOOR",
        )

    return AccountDecision(
        candidate_id=candidate.candidate_id,
        slot=slot,
        role=ACCOUNT_ROLES[slot],
        outcome=DecisionOutcome.READY_TO_SUBMIT,
        equity=capital.equity,
        cash=state.cash,
        effective_capital=capital.max_exposure,
        current_exposure=current_exposure,
        risk_budget=risk_budget,
        risk_available=True,
        risk_regime=gate.regime,
        stop_distance=gate.risk_distance,
        risk_sized_quantity=sizing.quantity,
        proposed_quantity=feasibility.whole_shares,
        proposed_notional=feasibility.whole_shares * price,
        whole_share_feasible=True,
        binding_constraint=sizing.constraint.value if sizing.constraint else None,
    )


def fan_out(
    candidate: MatchBCandidate,
    *,
    states: dict[PaperAccountSlot, PaperAccountState],
    risks: dict[PaperAccountSlot, ShortHorizonRisk | None],
    profiles: dict[PaperAccountSlot, SimulationProfileConfig],
    costs: CostSettings,
    exposures: dict[PaperAccountSlot, Decimal] | None = None,
    open_positions: dict[PaperAccountSlot, bool] | None = None,
    open_orders: dict[PaperAccountSlot, bool] | None = None,
) -> dict[PaperAccountSlot, AccountDecision]:
    """Deliver one candidate to every configured slot, independently.

    A slot missing state, or raising, produces an ``ACCOUNT_UNAVAILABLE``
    decision rather than aborting the fan-out: one account's outage must never
    stop the other two from being evaluated, and it must never reroute a
    decision.
    """
    decisions: dict[PaperAccountSlot, AccountDecision] = {}
    for slot in PaperAccountSlot:
        state = states.get(slot)
        profile = profiles.get(slot)
        if state is None or profile is None:
            decisions[slot] = _refusal(
                candidate,
                slot,
                ExperimentCapital(slot, Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(0)),
                _unavailable_state(slot),
                Decimal(0),
                DecisionOutcome.ACCOUNT_UNAVAILABLE,
                "no account state or profile",
            )
            continue
        decisions[slot] = decide_for_account(
            candidate,
            slot=slot,
            state=state,
            risk=risks.get(slot),
            profile=profile,
            costs=costs,
            current_exposure=(exposures or {}).get(slot, Decimal(0)),
            has_open_position=(open_positions or {}).get(slot, False),
            has_open_order=(open_orders or {}).get(slot, False),
        )
    return decisions


def _unavailable_state(slot: PaperAccountSlot) -> PaperAccountState:
    return PaperAccountState(
        slot=slot,
        account_number="",
        is_paper=False,
        status="UNKNOWN",
        currency="",
        cash=Decimal(0),
        equity=Decimal(0),
        buying_power=Decimal(0),
        trading_blocked=True,
        account_blocked=True,
        open_positions=0,
        open_orders=0,
        error="unconfigured",
    )
