"""The stop-vs-time-exit race, reconciliation, and slot isolation.

Every test here defends one invariant: **tradabot may never send a SELL that can
leave the account net short.** A protected position carries a live broker stop
that can fill at any moment, including while the cancel is in flight.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

from app.broker.paper_accounts import PaperAccountSlot
from app.broker.paper_orders import BrokerOrderStatus
from app.paper import lifecycle
from app.paper.lifecycle import (
    BrokerPositionView,
    ExitStage,
    ProtectiveOrderView,
    SlotState,
    SyncStatus,
    may_recover,
    plan_protected_exit,
    reconcile_slot,
)

SLOT = PaperAccountSlot.PAPER_3K


class FakeBroker:
    """A broker whose position and protection can change between the two reads."""

    def __init__(self, positions, protective=None, cancel_ok=True, raise_on=None):
        self._positions = list(positions)  # one entry per read
        self._protective = list(protective or [[], []])
        self._cancel_ok = cancel_ok
        self._raise_on = raise_on
        self.reads = 0
        self.protective_reads = 0
        self.cancelled: list[str] = []

    def position(self, symbol):
        if self._raise_on == "position":
            raise RuntimeError("timeout")
        if self._raise_on == "reread" and self.reads == 1:
            raise RuntimeError("timeout")
        value = self._positions[min(self.reads, len(self._positions) - 1)]
        self.reads += 1
        return None if value is None else BrokerPositionView(symbol, value, value)

    def protective_orders(self, symbol):
        if self._raise_on == "protective":
            raise RuntimeError("timeout")
        value = self._protective[min(self.protective_reads, len(self._protective) - 1)]
        self.protective_reads += 1
        return [
            ProtectiveOrderView(f"stop-{i}", symbol, q, BrokerOrderStatus.ACCEPTED)
            for i, q in enumerate(value)
        ]

    def cancel(self, order_id):
        if self._raise_on == "cancel":
            raise RuntimeError("timeout")
        if self._cancel_ok:
            self.cancelled.append(order_id)
        return self._cancel_ok


def plan(broker, qty="2"):
    return plan_protected_exit(broker, slot=SLOT, symbol="MSFT", intended_quantity=Decimal(qty))


class TestStopVersusTimeExitRace:
    def test_case_a_stop_cancelled_then_sell_the_confirmed_remainder(self) -> None:
        broker = FakeBroker([Decimal("2"), Decimal("2")], [[Decimal("2")], []])
        result = plan(broker)
        assert result.stage is ExitStage.READY_TO_SELL
        assert result.sell_quantity == Decimal("2")
        assert result.cancelled_orders == ("stop-0",)

    def test_case_b_stop_filled_before_the_first_read(self) -> None:
        """Common and safe: nothing to close, so nothing is sent."""
        broker = FakeBroker([None])
        result = plan(broker)
        assert result.stage is ExitStage.NOTHING_TO_CLOSE
        assert result.sell_quantity == 0

    def test_case_c_stop_fills_while_cancellation_is_pending(self) -> None:
        """**The gate.** The second read is what catches this."""
        broker = FakeBroker([Decimal("2"), None], [[Decimal("2")], []])
        result = plan(broker)
        assert result.stage is ExitStage.NOTHING_TO_CLOSE
        assert not result.may_sell
        assert "during cancellation" in result.detail

    def test_case_d_stop_partially_fills_and_the_sell_shrinks(self) -> None:
        """**The gate.** Selling the original 2 would short by 1."""
        broker = FakeBroker([Decimal("2"), Decimal("1")], [[Decimal("2")], []])
        result = plan(broker)
        assert result.stage is ExitStage.READY_TO_SELL
        assert result.sell_quantity == Decimal("1")

    def test_case_e_cancel_rejected_and_protection_still_working(self) -> None:
        broker = FakeBroker(
            [Decimal("2"), Decimal("2")], [[Decimal("2")], [Decimal("2")]], cancel_ok=False
        )
        result = plan(broker)
        assert result.stage is ExitStage.PROTECTION_CANCEL_REQUIRED
        assert not result.may_sell

    def test_case_f_cancel_raises_yielding_ambiguity(self) -> None:
        result = plan(FakeBroker([Decimal("2")], [[Decimal("2")]], raise_on="cancel"))
        assert result.stage is ExitStage.AMBIGUOUS
        assert not result.may_sell

    def test_case_g_reread_fails_and_nothing_is_sent(self) -> None:
        """**The gate.** Without the second read there is no safe quantity."""
        broker = FakeBroker([Decimal("2"), Decimal("2")], [[Decimal("2")], []], raise_on="reread")
        result = plan(broker)
        assert result.stage is ExitStage.AMBIGUOUS
        assert not result.may_sell

    def test_case_h_first_read_fails(self) -> None:
        assert plan(FakeBroker([Decimal("2")], raise_on="position")).stage is ExitStage.AMBIGUOUS

    def test_protective_read_failure_is_ambiguous(self) -> None:
        assert plan(FakeBroker([Decimal("2")], raise_on="protective")).stage is (
            ExitStage.AMBIGUOUS
        )

    def test_local_belief_never_widens_the_sell(self) -> None:
        """**The gate.** Broker quantity always wins."""
        broker = FakeBroker([Decimal("1"), Decimal("1")], [[], []])
        assert plan(broker, qty="5").sell_quantity == Decimal("1")

    def test_reserved_quantity_limits_the_sell(self) -> None:
        class Reserved(FakeBroker):
            def position(self, symbol):
                self.reads += 1
                return BrokerPositionView(symbol, Decimal("3"), Decimal("1"))

        assert plan(Reserved([Decimal("3")], [[], []])).sell_quantity == Decimal("1")

    def test_a_fractional_confirmed_quantity_is_refused(self) -> None:
        broker = FakeBroker([Decimal("1.5"), Decimal("1.5")], [[], []])
        assert plan(broker, qty="1.5").stage is ExitStage.BLOCKED_SHORT_RISK

    def test_no_path_ever_sells_more_than_the_broker_confirms(self) -> None:
        """The invariant, swept across every scenario above."""
        scenarios = [
            FakeBroker([Decimal("2"), Decimal("1")], [[Decimal("2")], []]),
            FakeBroker([Decimal("2"), None], [[Decimal("2")], []]),
            FakeBroker([None]),
            FakeBroker(
                [Decimal("2"), Decimal("2")], [[Decimal("2")], [Decimal("2")]], cancel_ok=False
            ),
        ]
        for broker in scenarios:
            result = plan(broker, qty="10")
            confirmed = broker._positions[-1] or Decimal(0)
            assert result.sell_quantity <= confirmed

    def test_the_module_cannot_submit_an_order(self) -> None:
        """**The gate.** Race logic and the order path must not drift together."""
        source = inspect.getsource(lifecycle).split('"""', 2)[-1].lower()
        for forbidden in ("submit_order", "marketorderrequest", "submit_entry"):
            assert forbidden not in source


class TestSlotReconciliation:
    def test_matching_state_is_in_sync(self) -> None:
        r = reconcile_slot(
            slot=SLOT,
            broker_positions={"MSFT": Decimal("2")},
            local_positions={"MSFT": Decimal("2")},
        )
        assert r.status is SyncStatus.IN_SYNC
        assert r.slot_state is SlotState.ACTIVE
        assert r.slot_state.may_open_new_positions

    def test_a_position_the_broker_holds_but_we_do_not_know_is_ambiguous(self) -> None:
        """**The gate.** Nothing would ever close it."""
        r = reconcile_slot(slot=SLOT, broker_positions={"MSFT": Decimal("2")}, local_positions={})
        assert r.status is SyncStatus.AMBIGUOUS
        assert r.slot_state is SlotState.RECONCILIATION_REQUIRED
        assert not r.slot_state.may_open_new_positions

    def test_an_unprotected_position_freezes_the_slot(self) -> None:
        r = reconcile_slot(
            slot=SLOT,
            broker_positions={"MSFT": Decimal("2")},
            local_positions={"MSFT": Decimal("2")},
            unprotected_symbols=("MSFT",),
        )
        assert r.status is SyncStatus.AMBIGUOUS
        assert r.unprotected == 1

    def test_an_unreachable_broker_is_an_error_not_a_guess(self) -> None:
        r = reconcile_slot(
            slot=SLOT, broker_positions={}, local_positions={}, broker_reachable=False
        )
        assert r.status is SyncStatus.ERROR
        assert r.slot_state is SlotState.BROKER_UNAVAILABLE

    def test_a_quantity_mismatch_is_recoverable_and_freezes_entries(self) -> None:
        r = reconcile_slot(
            slot=SLOT,
            broker_positions={"MSFT": Decimal("1")},
            local_positions={"MSFT": Decimal("2")},
        )
        assert r.status is SyncStatus.RECOVERABLE
        assert not r.slot_state.may_open_new_positions

    def test_freezing_one_slot_says_nothing_about_another(self) -> None:
        """**The gate.** No cross-slot contamination, no rerouting."""
        broken = reconcile_slot(
            slot=PaperAccountSlot.PAPER_1K, broker_positions={"X": Decimal("1")}, local_positions={}
        )
        healthy = reconcile_slot(
            slot=PaperAccountSlot.PAPER_10K, broker_positions={}, local_positions={}
        )
        assert not broken.slot_state.may_open_new_positions
        assert healthy.slot_state.may_open_new_positions

    def test_reconciliation_never_liquidates(self) -> None:
        # It receives already-read state and holds no broker handle, so it is
        # structurally incapable of acting -- stronger than scanning for words.
        params = set(inspect.signature(reconcile_slot).parameters)
        assert "broker" not in params
        source = inspect.getsource(reconcile_slot)
        for forbidden in ("broker.", "submit", "cancel("):
            assert forbidden not in source

    def test_recovery_requires_two_consecutive_clean_reconciliations(self) -> None:
        """One successful poll is not evidence; it would flap."""
        clean = reconcile_slot(slot=SLOT, broker_positions={}, local_positions={})
        assert not may_recover(clean, consecutive_clean=1)
        assert may_recover(clean, consecutive_clean=2)

    def test_a_dirty_reconciliation_never_recovers(self) -> None:
        dirty = reconcile_slot(slot=SLOT, broker_positions={"X": Decimal("1")}, local_positions={})
        assert not may_recover(dirty, consecutive_clean=99)
