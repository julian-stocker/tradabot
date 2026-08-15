"""Persistence for the forward experiment, and what a restart must not do.

The failures these prevent are all silent: a re-run that doubles the coverage
statistics, a resubmission that places a second BUY, a late order that pretends
to be the original forward hypothesis. None would raise; all would corrupt the
experiment.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.broker.paper_accounts import PaperAccountSlot
from app.paper.fanout import AccountDecision, AccountRole, DecisionOutcome
from app.paper.forward_repository import ForwardExperimentRepository
from app.strategy.match_b import EvaluationOutcome, MatchBCandidate, SessionEvaluation

SESSION = datetime(2026, 8, 13, tzinfo=UTC)
DECIDED = datetime(2026, 8, 13, 21, tzinfo=UTC)


def candidate(symbol="MSFT", cid="cand-1"):
    return MatchBCandidate(
        candidate_id=cid,
        strategy_version="match-b-v1",
        universe_version="original-52-v1",
        session=SESSION,
        data_cutoff=SESSION,
        decided_at=DECIDED,
        symbol=symbol,
        rank=0.95,
        sector="technology",
        sector_etf_return_20d=1.0,
        movement_to_cost=12.0,
        atr_pct=3.0,
        reference_price=Decimal("496.81"),
    )


def evaluation(outcome=EvaluationOutcome.CANDIDATES, cands=None, error=None):
    return SessionEvaluation(
        session=SESSION,
        outcome=outcome,
        universe_size=52,
        eligible_symbols=52,
        candidates=cands if cands is not None else (candidate(),),
        error=error,
    )


def decision(slot=PaperAccountSlot.PAPER_3K, outcome=DecisionOutcome.READY_TO_SUBMIT, cid="cand-1"):
    return AccountDecision(
        candidate_id=cid,
        slot=slot,
        role=AccountRole.TRANSITION,
        outcome=outcome,
        equity=Decimal("3000"),
        cash=Decimal("3000"),
        effective_capital=Decimal("3000"),
        current_exposure=Decimal("0"),
        risk_budget=Decimal("30"),
        risk_available=True,
        risk_regime="NORMAL_VOL",
        stop_distance=Decimal("15"),
        risk_sized_quantity=Decimal("2.4"),
        proposed_quantity=Decimal("2"),
        proposed_notional=Decimal("993.62"),
        whole_share_feasible=True,
    )


async def repo(session):
    return ForwardExperimentRepository(session)


class TestEvaluationPersistence:
    async def test_an_evaluation_is_written_once(self, session):
        """**The gate.** A restart must not double the record."""
        r = await repo(session)
        args = {
            "strategy_version": "match-b-v1",
            "universe_version": "original-52-v1",
            "universe_hash": "0ba6f7c7",
        }
        first, created_a = await r.record_evaluation(evaluation(), **args)
        second, created_b = await r.record_evaluation(evaluation(), **args)
        assert created_a is True
        assert created_b is False
        assert first.id == second.id

    async def test_no_opportunity_persists_explicitly(self, session):
        """A quiet day must be a stored fact, not an absent row."""
        r = await repo(session)
        row, _ = await r.record_evaluation(
            evaluation(EvaluationOutcome.NO_OPPORTUNITY, cands=()),
            strategy_version="match-b-v1",
            universe_version="original-52-v1",
            universe_hash="h",
        )
        assert row.outcome == "NO_OPPORTUNITY"
        assert row.candidate_count == 0
        assert row.error is None

    async def test_failed_persists_and_is_distinguishable(self, session):
        r = await repo(session)
        row, _ = await r.record_evaluation(
            evaluation(EvaluationOutcome.FAILED, cands=(), error="no rows"),
            strategy_version="match-b-v1",
            universe_version="original-52-v1",
            universe_hash="h",
        )
        assert row.outcome == "FAILED"
        assert row.error == "no rows"

    async def test_not_evaluated_is_the_absence_of_a_row(self, session):
        """**The gate.** The fourth state, and the only one with no record."""
        r = await repo(session)
        assert (
            await r.get_evaluation(
                strategy_version="match-b-v1", universe_version="original-52-v1", session=SESSION
            )
            is None
        )


class TestCandidatePersistence:
    async def test_candidates_are_not_duplicated_on_replay(self, session):
        r = await repo(session)
        ev, _ = await r.record_evaluation(
            evaluation(),
            strategy_version="match-b-v1",
            universe_version="original-52-v1",
            universe_hash="h",
        )
        await r.record_candidates(ev, (candidate(),))
        await r.record_candidates(ev, (candidate(),))
        assert len(await r.candidates_for(ev.id)) == 1

    async def test_provenance_round_trips(self, session):
        r = await repo(session)
        ev, _ = await r.record_evaluation(
            evaluation(),
            strategy_version="match-b-v1",
            universe_version="original-52-v1",
            universe_hash="h",
        )
        (row,) = await r.record_candidates(ev, (candidate(),))
        assert row.symbol == "MSFT"
        assert row.reference_price == Decimal("496.81")
        assert row.sector == "technology"


class TestAccountDecisionPersistence:
    async def test_one_decision_per_candidate_and_slot(self, session):
        """**The gate.** A re-fan after restart would double coverage stats."""
        r = await repo(session)
        _, created_a = await r.record_decision(decision(), decided_at=DECIDED)
        _, created_b = await r.record_decision(decision(), decided_at=DECIDED)
        assert created_a is True
        assert created_b is False
        assert len(await r.decisions_for_slot("PAPER_3K")) == 1

    async def test_slots_are_recorded_independently(self, session):
        r = await repo(session)
        for slot in PaperAccountSlot:
            await r.record_decision(decision(slot=slot), decided_at=DECIDED)
        for slot in PaperAccountSlot:
            assert len(await r.decisions_for_slot(slot.value)) == 1

    async def test_a_refusal_records_its_reason(self, session):
        r = await repo(session)
        row, _ = await r.record_decision(
            decision(outcome=DecisionOutcome.WHOLE_SHARE_NOT_FEASIBLE), decided_at=DECIDED
        )
        assert row.outcome == "WHOLE_SHARE_NOT_FEASIBLE"
        assert row.rejection_reason == "WHOLE_SHARE_NOT_FEASIBLE"

    async def test_an_actionable_decision_records_no_rejection(self, session):
        r = await repo(session)
        row, _ = await r.record_decision(decision(), decided_at=DECIDED)
        assert row.rejection_reason is None
        assert row.proposed_quantity == Decimal("2")


class TestOrderIntentAndRecovery:
    async def test_intent_is_written_before_submission(self, session):
        """**The gate.** An order at the broker that nothing locally knows about
        is unrecoverable; an unsent local intent is merely tidy-up."""
        r = await repo(session)
        row, created = await r.record_order_intent(
            client_order_id="paper:PAPER_3K:cand-1",
            slot="PAPER_3K",
            candidate_id="cand-1",
            symbol="MSFT",
            quantity=Decimal("2"),
            order_class="bracket",
            stop_price=Decimal("480"),
            target_price=Decimal("520"),
        )
        assert created
        assert row.status == "ORDER_READY"
        assert row.broker_order_id is None

    async def test_no_fabricated_broker_id_is_stored(self, session):
        r = await repo(session)
        row, _ = await r.record_order_intent(
            client_order_id="k",
            slot="PAPER_3K",
            candidate_id="c",
            symbol="MSFT",
            quantity=Decimal("1"),
            order_class="oto",
            stop_price=Decimal("1"),
            target_price=None,
        )
        assert row.broker_order_id is None

    async def test_a_retry_finds_the_existing_intent(self, session):
        """The recovery boundary: same client_order_id, no second row."""
        r = await repo(session)
        args = {
            "client_order_id": "paper:PAPER_3K:cand-1",
            "slot": "PAPER_3K",
            "candidate_id": "cand-1",
            "symbol": "MSFT",
            "quantity": Decimal("2"),
            "order_class": "bracket",
            "stop_price": Decimal("480"),
            "target_price": Decimal("520"),
        }
        await r.record_order_intent(**args)
        _, created = await r.record_order_intent(**args)
        assert created is False

    async def test_broker_truth_is_applied_without_inventing_a_fill(self, session):
        r = await repo(session)
        await r.record_order_intent(
            client_order_id="k",
            slot="PAPER_3K",
            candidate_id="c",
            symbol="MSFT",
            quantity=Decimal("2"),
            order_class="bracket",
            stop_price=Decimal("480"),
            target_price=Decimal("520"),
        )
        row = await r.apply_reconciliation(
            "k",
            broker_order_id="ord-9",
            status="PARTIALLY_FILLED",
            filled_quantity=Decimal("1"),
            filled_avg_price=Decimal("496"),
            protected_quantity=Decimal("1"),
        )
        assert row.broker_order_id == "ord-9"
        assert row.filled_quantity == Decimal("1")

    async def test_an_unprotected_fill_is_discoverable(self, session):
        """**The gate.** Filled quantity beyond protected quantity must never
        sit silently healthy."""
        r = await repo(session)
        await r.record_order_intent(
            client_order_id="k",
            slot="PAPER_3K",
            candidate_id="c",
            symbol="MSFT",
            quantity=Decimal("2"),
            order_class="bracket",
            stop_price=Decimal("480"),
            target_price=None,
        )
        await r.apply_reconciliation(
            "k",
            broker_order_id="ord-9",
            status="FILLED",
            filled_quantity=Decimal("2"),
            filled_avg_price=Decimal("496"),
            protected_quantity=Decimal("1"),
        )
        naked = await r.unprotected_positions("PAPER_3K")
        assert len(naked) == 1
        assert naked[0].filled_quantity > naked[0].protected_quantity

    async def test_one_slots_orders_are_invisible_to_another(self, session):
        r = await repo(session)
        await r.record_order_intent(
            client_order_id="a",
            slot="PAPER_1K",
            candidate_id="c",
            symbol="MSFT",
            quantity=Decimal("1"),
            order_class="oto",
            stop_price=Decimal("1"),
            target_price=None,
        )
        assert await r.open_orders_for_slot("PAPER_1K")
        assert await r.open_orders_for_slot("PAPER_10K") == []
