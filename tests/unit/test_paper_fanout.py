"""One candidate, three accounts. The arms must differ only by capital."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal

from app.broker.paper_accounts import PaperAccountSlot, PaperAccountState
from app.core.config import CostSettings
from app.market_data.risk import assess
from app.market_data.volatility import ExpectedMovement, VolatilityRegime
from app.paper import fanout
from app.paper.fanout import (
    ACCOUNT_ROLES,
    AccountRole,
    DecisionOutcome,
    decide_for_account,
    fan_out,
)
from app.simulation.models import BrokerCostConfig, RiskConfig, SimulationProfileConfig
from app.strategy.match_b import MatchBCandidate

NOW = datetime(2026, 8, 10, 20, tzinfo=UTC)
COSTS = CostSettings(order_fee=Decimal("0"), default_spread_bps=Decimal("10"))


def candidate(symbol="MSFT", price="500", atr=3.0):
    return MatchBCandidate(
        candidate_id="cand-1",
        strategy_version="match-b-v1",
        universe_version="original-52-v1",
        session=NOW,
        data_cutoff=NOW,
        decided_at=NOW,
        symbol=symbol,
        rank=0.95,
        sector="technology",
        sector_etf_return_20d=1.0,
        movement_to_cost=12.0,
        atr_pct=atr,
        reference_price=Decimal(price),
    )


def state(slot, equity="1000", cash=None, ok=True):
    value = Decimal(equity)
    return PaperAccountState(
        slot=slot,
        account_number="PA1",
        is_paper=ok,
        status="ACTIVE" if ok else "UNKNOWN",
        currency="USD",
        cash=Decimal(cash) if cash else value,
        equity=value,
        buying_power=value * 4,
        trading_blocked=not ok,
        account_blocked=not ok,
        open_positions=0,
        open_orders=0,
        error=None if ok else "APIError",
    )


def profile(slot, equity="1000"):
    return SimulationProfileConfig(
        id=0,
        name=slot.value,
        initial_capital=Decimal(equity),
        currency="USD",
        risk=RiskConfig(
            name=slot.value,
            risk_per_trade=Decimal("0.01"),
            max_position_percent=Decimal("0.30"),
            max_total_exposure=Decimal("1.0"),
            max_open_positions=3,
            max_daily_loss=Decimal("1.0"),
            max_drawdown=Decimal("1.0"),
            min_signal_score=75.0,
            min_confidence=0.5,
            stop_loss_atr_multiple=Decimal("2.0"),
            take_profit_r_multiple=Decimal("2.5"),
            max_holding_bars=3,
            require_stop_loss=True,
        ),
        costs=BrokerCostConfig(
            name=slot.value,
            order_fee=Decimal("0"),
            variable_fee_rate=Decimal("0"),
            slippage_spread_multiple=Decimal("0.5"),
            default_spread_bps=Decimal("10"),
            min_order_notional=Decimal("0"),
        ),
    )


def risk(atr=3.0):
    return assess(
        ExpectedMovement(
            symbol="MSFT",
            calculated_at=NOW,
            bar_timestamp=NOW,
            regime=VolatilityRegime.NORMAL,
            percentile=0.5,
            atr_pct=atr,
            recent_range_pct=atr * 2,
        ),
        now=NOW,
    )


EQUITIES = {
    PaperAccountSlot.PAPER_1K: "1000",
    PaperAccountSlot.PAPER_3K: "3000",
    PaperAccountSlot.PAPER_10K: "10000",
}


def run(cand=None, **over):
    cand = cand or candidate()
    return fan_out(
        cand,
        states=over.get("states", {s: state(s, EQUITIES[s]) for s in PaperAccountSlot}),
        risks=over.get("risks", dict.fromkeys(PaperAccountSlot, risk())),
        profiles={s: profile(s, EQUITIES[s]) for s in PaperAccountSlot},
        costs=COSTS,
        **{k: v for k, v in over.items() if k in {"exposures", "open_positions", "open_orders"}},
    )


class TestSameCandidateEverywhere:
    def test_all_three_slots_receive_the_identical_candidate(self) -> None:
        """**The gate.** No slot may alter what it was offered."""
        cand = candidate()
        decisions = run(cand)
        assert set(decisions) == set(PaperAccountSlot)
        assert {d.candidate_id for d in decisions.values()} == {cand.candidate_id}

    def test_roles_are_assigned_as_registered(self) -> None:
        assert ACCOUNT_ROLES[PaperAccountSlot.PAPER_1K] is AccountRole.CAPITAL_CONSTRAINT
        assert ACCOUNT_ROLES[PaperAccountSlot.PAPER_3K] is AccountRole.TRANSITION
        assert ACCOUNT_ROLES[PaperAccountSlot.PAPER_10K] is AccountRole.FULL_STRATEGY_REFERENCE

    def test_no_decision_path_reads_the_account_role(self) -> None:
        """**The gate.** A role that changed a threshold would end the experiment."""
        # The only permitted use is stamping the role onto the record for
        # reporting. Any *conditional* on it would mean the arms diverged.
        source = inspect.getsource(decide_for_account)
        for forbidden in ("if role", "role ==", "role is AccountRole", "ACCOUNT_ROLES[slot] =="):
            assert forbidden not in source
        assert source.count("role=ACCOUNT_ROLES[slot]") == 1

    def test_no_per_slot_alpha_constant_exists(self) -> None:
        source = inspect.getsource(fanout)
        for forbidden in ("PAPER_1K:", "if slot is PaperAccountSlot.PAPER_1K"):
            assert source.count(forbidden) <= 1


class TestCapitalDrivenDifferences:
    def test_a_five_hundred_dollar_share_is_infeasible_only_for_the_small_account(self) -> None:
        """The capital-constraint arm, demonstrated."""
        decisions = run(candidate(price="500"))
        assert (
            decisions[PaperAccountSlot.PAPER_1K].outcome is DecisionOutcome.WHOLE_SHARE_NOT_FEASIBLE
        )
        assert decisions[PaperAccountSlot.PAPER_10K].outcome is DecisionOutcome.READY_TO_SUBMIT

    def test_whole_share_infeasibility_is_its_own_outcome(self) -> None:
        """**The gate.** The account holds ample cash and still cannot trade —
        folding this into INSUFFICIENT_CAPITAL would make coverage unreadable."""
        d = run(candidate(price="500"))[PaperAccountSlot.PAPER_1K]
        assert d.outcome is DecisionOutcome.WHOLE_SHARE_NOT_FEASIBLE
        assert d.cash == Decimal("1000")
        assert d.binding_constraint == "WHOLE_SHARE_FLOOR"
        assert d.risk_sized_quantity is not None

    def test_a_cheap_share_is_feasible_for_every_account(self) -> None:
        decisions = run(candidate(price="20"))
        assert all(d.outcome is DecisionOutcome.READY_TO_SUBMIT for d in decisions.values())

    def test_quantity_scales_with_capital_and_is_always_whole(self) -> None:
        decisions = run(candidate(price="20"))
        quantities = [decisions[s].proposed_quantity for s in PaperAccountSlot]
        assert all(q == q.to_integral_value() for q in quantities)
        assert quantities[0] < quantities[1] < quantities[2]

    def test_margin_buying_power_never_raises_the_proposal(self) -> None:
        d = run(candidate(price="20"))[PaperAccountSlot.PAPER_10K]
        assert d.effective_capital == Decimal("10000")
        assert d.proposed_notional <= Decimal("10000")


class TestRefusalsAreRecorded:
    def test_an_unavailable_account_does_not_stop_the_others(self) -> None:
        """**The gate.** One outage must never reroute or block a decision."""
        states = {s: state(s, EQUITIES[s]) for s in PaperAccountSlot}
        states[PaperAccountSlot.PAPER_3K] = state(PaperAccountSlot.PAPER_3K, "3000", ok=False)
        decisions = run(candidate(price="20"), states=states)
        assert decisions[PaperAccountSlot.PAPER_3K].outcome is DecisionOutcome.ACCOUNT_UNAVAILABLE
        assert decisions[PaperAccountSlot.PAPER_1K].is_actionable
        assert decisions[PaperAccountSlot.PAPER_10K].is_actionable

    def test_missing_risk_is_unavailable_not_rejected(self) -> None:
        decisions = run(candidate(price="20"), risks=dict.fromkeys(PaperAccountSlot, None))
        assert all(d.outcome is DecisionOutcome.RISK_UNAVAILABLE for d in decisions.values())

    def test_an_existing_position_blocks_only_that_slot(self) -> None:
        decisions = run(candidate(price="20"), open_positions={PaperAccountSlot.PAPER_1K: True})
        assert decisions[PaperAccountSlot.PAPER_1K].outcome is DecisionOutcome.POSITION_ALREADY_OPEN
        assert decisions[PaperAccountSlot.PAPER_10K].is_actionable

    def test_an_existing_order_blocks_only_that_slot(self) -> None:
        decisions = run(candidate(price="20"), open_orders={PaperAccountSlot.PAPER_3K: True})
        assert decisions[PaperAccountSlot.PAPER_3K].outcome is DecisionOutcome.ORDER_ALREADY_EXISTS

    def test_exhausted_exposure_refuses_with_its_constraint(self) -> None:
        decisions = run(
            candidate(price="20"), exposures={PaperAccountSlot.PAPER_1K: Decimal("1000")}
        )
        d = decisions[PaperAccountSlot.PAPER_1K]
        assert d.outcome is DecisionOutcome.EXPOSURE_LIMIT
        assert d.binding_constraint == "MAX_TOTAL_EXPOSURE"

    def test_every_refusal_records_capital_context(self) -> None:
        """So coverage can be explained afterwards, not merely counted."""
        d = run(candidate(price="500"))[PaperAccountSlot.PAPER_1K]
        assert d.equity == Decimal("1000")
        assert d.risk_budget == Decimal("10.00")
        assert d.role is AccountRole.CAPITAL_CONSTRAINT


def test_the_fanout_places_no_orders_and_reads_no_options_data() -> None:
    source = inspect.getsource(fanout).lower()
    for forbidden in ("submit_order", "implied_volatility", "option_surface", "webhook"):
        assert forbidden not in source
