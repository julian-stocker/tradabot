"""The full lifecycle and restart matrix, run against the production service.

Broker doubles stand in for Alpaca; every other component — repository, order
builder, race logic, holding clock — is the real one. A parallel fake pipeline
would prove nothing about what actually ships.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.broker.paper_accounts import PaperAccountSlot
from app.broker.paper_orders import BrokerOrderStatus
from app.paper.forward_repository import ForwardExperimentRepository
from app.paper.forward_service import (
    ActionOutcome,
    ForwardPaperService,
    entry_key,
    exit_key,
    may_open_local_position,
)
from app.paper.lifecycle import BrokerPositionView, SlotState, SyncStatus

SLOT = PaperAccountSlot.PAPER_3K
FRIDAY_FILL = datetime(2026, 8, 7, 14, tzinfo=UTC)
EXPIRED_NOW = datetime(2026, 8, 12, 20, tzinfo=UTC)  # 3 sessions later
STILL_HOLDING = datetime(2026, 8, 10, 20, tzinfo=UTC)  # 1 session later


class FakeOrder:
    def __init__(self, status="filled", qty="2", price="500", oid="ord-1"):
        self.id = oid
        self.symbol = "MSFT"
        self.side = "buy"
        self.order_type = "market"
        self.status = status
        self.qty = qty
        self.filled_qty = qty
        self.filled_avg_price = price
        self.submitted_at = FRIDAY_FILL
        self.filled_at = FRIDAY_FILL
        self.rejected_reason = None


class FakeLookup:
    """Broker-side lookup. Distinguishes 'no such order' from 'cannot ask'."""

    def __init__(self, existing=None, raises=False):
        self.existing = existing or {}
        self.raises = raises
        self.calls = 0

    def find_by_client_order_id(self, client_order_id):
        self.calls += 1
        if self.raises:
            raise RuntimeError("broker unreachable")
        return self.existing.get(client_order_id)


class FakeSubmitter:
    def __init__(self, reconciliation=None, raises=None):
        self.reconciliation = reconciliation
        self.raises = raises
        self.entries: list[str] = []
        self.exits: list[tuple[str, Decimal]] = []

    def submit_entry(self, request, **kw):
        if self.raises:
            raise self.raises
        self.entries.append(request.idempotency_key)
        return self.reconciliation

    def submit_exit(self, *, slot, symbol, quantity, client_order_id):
        if self.raises:
            raise self.raises
        self.exits.append((client_order_id, quantity))
        return self.reconciliation


class FakeBroker:
    def __init__(self, positions=None, protective=None):
        self._positions = list(positions or [Decimal("2"), Decimal("2")])
        self._protective = list(protective or [[], []])
        self.reads = 0
        self.protective_reads = 0

    def position(self, symbol):
        value = self._positions[min(self.reads, len(self._positions) - 1)]
        self.reads += 1
        return None if value is None else BrokerPositionView(symbol, value, value)

    def protective_orders(self, symbol):
        self.protective_reads += 1
        return []

    def cancel(self, order_id):
        return True


def reconciliation(status=BrokerOrderStatus.FILLED, filled="2", price="500"):
    from app.broker.paper_orders import OrderReconciliation

    return OrderReconciliation(
        slot=SLOT,
        broker_order_id="ord-1",
        decision_id=0,
        symbol="MSFT",
        side="buy",
        requested_quantity=Decimal("2"),
        order_type="market",
        submitted_at=FRIDAY_FILL,
        status=status,
        filled_quantity=Decimal(filled),
        filled_avg_price=Decimal(price) if price else None,
        filled_at=FRIDAY_FILL,
    )


def service(session, *, enabled=False, lookup=None, submitter=None, broker=None):
    return ForwardPaperService(
        slot=SLOT,
        repository=ForwardExperimentRepository(session),
        submitter=submitter or FakeSubmitter(reconciliation()),
        broker=broker or FakeBroker(),
        lookup=lookup or FakeLookup(),
        execution_enabled=enabled,
    )


async def enter(svc, candidate_id="cand-1", quantity="2"):
    return await svc.prepare_entry(
        candidate_id=candidate_id,
        symbol="MSFT",
        quantity=Decimal(quantity),
        stop_price=Decimal("480"),
        target_price=Decimal("560"),
        state=None,
        capital=None,
    )


# ---------------------------------------------------------------------------
# Entry wiring and the execution flag
# ---------------------------------------------------------------------------
class TestEntryWiring:
    async def test_execution_disabled_reaches_order_ready_and_sends_nothing(self, session):
        """**The gate.** The default configuration constructs and persists only."""
        submitter = FakeSubmitter(reconciliation())
        result = await enter(service(session, submitter=submitter))
        assert result.outcome is ActionOutcome.ORDER_READY
        assert submitter.entries == []

    async def test_intent_is_persisted_before_any_broker_call(self, session):
        svc = service(session)
        await enter(svc)
        row = await svc.repository.get_order(entry_key(SLOT, "cand-1"))
        assert row is not None
        assert row.status == "ORDER_READY"
        assert row.broker_order_id is None

    async def test_a_fractional_quantity_is_refused_before_persistence(self, session):
        svc = service(session, enabled=True)
        result = await svc.prepare_entry(
            candidate_id="c",
            symbol="MSFT",
            quantity=Decimal("1.5"),
            stop_price=Decimal("480"),
            target_price=None,
            state=None,
            capital=None,
        )
        assert result.outcome is ActionOutcome.REFUSED
        assert "whole-share" in result.detail

    async def test_a_frozen_slot_never_enters(self, session):
        svc = service(session, enabled=True)
        svc.slot_state = SlotState.RECONCILIATION_REQUIRED
        result = await enter(svc)
        assert result.outcome is ActionOutcome.FROZEN
        assert svc.submitter.entries == []


# ---------------------------------------------------------------------------
# The recovery boundary
# ---------------------------------------------------------------------------
class TestClientOrderIdRecovery:
    async def test_an_order_already_at_the_broker_is_recovered_not_resubmitted(self, session):
        """**The gate.** Restart case 5: submitted, broker id never persisted."""
        key = entry_key(SLOT, "cand-1")
        submitter = FakeSubmitter(reconciliation())
        svc = service(
            session,
            enabled=True,
            submitter=submitter,
            lookup=FakeLookup({key: FakeOrder()}),
        )
        result = await enter(svc)
        assert result.outcome is ActionOutcome.RECOVERED
        assert submitter.entries == []
        row = await svc.repository.get_order(key)
        assert row.broker_order_id == "ord-1"
        assert row.status == "FILLED"

    async def test_the_lookup_runs_before_every_submission_not_only_after_a_crash(
        self, session
    ) -> None:
        """A restart cannot tell whether it crashed before or after the call."""
        lookup = FakeLookup()
        await enter(service(session, enabled=True, lookup=lookup))
        assert lookup.calls == 1

    async def test_an_unanswerable_lookup_freezes_rather_than_resubmitting(self, session):
        """**The gate.** Resubmitting on an unanswered question is a duplicate BUY."""
        submitter = FakeSubmitter(reconciliation())
        svc = service(session, enabled=True, submitter=submitter, lookup=FakeLookup(raises=True))
        result = await enter(svc)
        assert result.outcome is ActionOutcome.FROZEN
        assert submitter.entries == []
        assert not svc.may_open_positions()

    async def test_a_retry_reuses_the_same_client_order_id(self, session):
        svc = service(session)
        first = await enter(svc)
        second = await enter(svc)
        assert first.client_order_id == second.client_order_id

    async def test_exit_keys_are_distinct_from_entry_keys(self, session):
        assert entry_key(SLOT, "c") != exit_key(SLOT, "c", "MAX_HOLDING_PERIOD")
        assert exit_key(SLOT, "c", "STOP_LOSS") != exit_key(SLOT, "c", "MAX_HOLDING_PERIOD")


# ---------------------------------------------------------------------------
# Partial fills and position opening
# ---------------------------------------------------------------------------
class TestPartialFillSafety:
    @pytest.mark.parametrize(
        ("status", "filled", "protected"),
        [
            (BrokerOrderStatus.NEW, "0", "0"),
            (BrokerOrderStatus.ACCEPTED, "0", "0"),
            (BrokerOrderStatus.PARTIALLY_FILLED, "1", "1"),
            (BrokerOrderStatus.FILLED, "2", "2"),
            (BrokerOrderStatus.REJECTED, "0", "0"),
        ],
    )
    async def test_protection_tracks_the_filled_quantity_never_the_request(
        self, session, status, filled, protected
    ) -> None:
        """**The gate.** Recording the request would claim protection over
        shares that were never bought."""
        key = entry_key(SLOT, "cand-1")
        svc = service(
            session,
            enabled=True,
            submitter=FakeSubmitter(reconciliation(status, filled, "500")),
        )
        await enter(svc)
        row = await svc.repository.get_order(key)
        assert row.filled_quantity == Decimal(filled)
        assert row.protected_quantity == Decimal(protected)
        assert row.protected_quantity <= row.filled_quantity

    async def test_an_under_protected_fill_is_discoverable(self, session):
        svc = service(session)
        await enter(svc)
        await svc.repository.apply_reconciliation(
            entry_key(SLOT, "cand-1"),
            broker_order_id="o",
            status="FILLED",
            filled_quantity=Decimal("2"),
            filled_avg_price=Decimal("500"),
            protected_quantity=Decimal("1"),
        )
        assert len(await svc.repository.unprotected_positions(SLOT.value)) == 1

    @pytest.mark.parametrize(
        ("status", "filled", "price", "protected", "opens"),
        [
            (BrokerOrderStatus.FILLED, "2", "500", "2", True),
            (BrokerOrderStatus.PARTIALLY_FILLED, "1", "500", "1", True),
            (BrokerOrderStatus.FILLED, "2", "500", "1", False),  # under-protected
            (BrokerOrderStatus.ACCEPTED, "0", "500", "0", False),
            (BrokerOrderStatus.FILLED, "2", None, "2", False),  # no fill price
        ],
    )
    def test_a_local_position_opens_only_on_safe_broker_truth(
        self, status, filled, price, protected, opens
    ) -> None:
        """**The gate.** Never on an acknowledgement, never under-protected."""
        assert (
            may_open_local_position(reconciliation(status, filled, price), Decimal(protected))
            is opens
        )


# ---------------------------------------------------------------------------
# Exit
# ---------------------------------------------------------------------------
class TestExitWiring:
    async def close(
        self, session, *, now=EXPIRED_NOW, enabled=False, broker=None, submitter=None, quantity="2"
    ):
        svc = service(session, enabled=enabled, broker=broker, submitter=submitter)
        result = await svc.close_on_expiry(
            candidate_id="cand-1",
            symbol="MSFT",
            entry_filled_at=FRIDAY_FILL,
            now=now,
            local_quantity=Decimal(quantity),
        )
        return svc, result

    async def test_a_position_inside_its_horizon_is_left_alone(self, session):
        _, result = await self.close(session, now=STILL_HOLDING)
        assert result.outcome is ActionOutcome.NOTHING_TO_DO
        assert "1/3 sessions" in result.detail

    async def test_expiry_reaches_order_ready_with_execution_disabled(self, session):
        submitter = FakeSubmitter(reconciliation())
        _, result = await self.close(session, submitter=submitter)
        assert result.outcome is ActionOutcome.ORDER_READY
        assert submitter.exits == []

    async def test_the_sell_quantity_comes_from_the_broker_not_local_belief(self, session):
        """**The gate.** Local says 5; the broker confirms 1."""
        broker = FakeBroker([Decimal("1"), Decimal("1")])
        submitter = FakeSubmitter(reconciliation(filled="1"))
        _, result = await self.close(
            session, enabled=True, broker=broker, submitter=submitter, quantity="5"
        )
        assert result.quantity == Decimal("1")
        assert submitter.exits == [(exit_key(SLOT, "cand-1", "MAX_HOLDING_PERIOD"), Decimal("1"))]

    async def test_a_position_closed_by_its_stop_sends_no_sell(self, session):
        """Restart case 16: the protective stop already closed it."""
        submitter = FakeSubmitter(reconciliation())
        _, result = await self.close(
            session, enabled=True, broker=FakeBroker([None]), submitter=submitter
        )
        assert result.outcome is ActionOutcome.NOTHING_TO_DO
        assert submitter.exits == []

    async def test_exit_intent_is_persisted_before_submission(self, session):
        svc, _ = await self.close(session)
        row = await svc.repository.get_order(exit_key(SLOT, "cand-1", "MAX_HOLDING_PERIOD"))
        assert row is not None
        assert row.status == "ORDER_READY"

    async def test_an_exit_already_at_the_broker_is_not_resubmitted(self, session):
        """Restart case 14: SELL submitted, broker id never persisted."""
        key = exit_key(SLOT, "cand-1", "MAX_HOLDING_PERIOD")
        submitter = FakeSubmitter(reconciliation())
        svc = service(
            session, enabled=True, submitter=submitter, lookup=FakeLookup({key: FakeOrder()})
        )
        result = await svc.close_on_expiry(
            candidate_id="cand-1",
            symbol="MSFT",
            entry_filled_at=FRIDAY_FILL,
            now=EXPIRED_NOW,
            local_quantity=Decimal("2"),
        )
        assert result.outcome is ActionOutcome.RECOVERED
        assert submitter.exits == []

    async def test_an_unknown_holding_age_freezes_and_sells_nothing(self, session):
        class Broken:
            def session_date_for(self, moment):
                raise RuntimeError("no calendar")

        import app.strategy.holding_clock as clock

        original = clock.get_trading_calendar
        clock.get_trading_calendar = lambda _: Broken()  # type: ignore[assignment]
        try:
            submitter = FakeSubmitter(reconciliation())
            _svc, result = await self.close(session, enabled=True, submitter=submitter)
        finally:
            clock.get_trading_calendar = original  # type: ignore[assignment]
        assert result.outcome is ActionOutcome.FROZEN
        assert submitter.exits == []


# ---------------------------------------------------------------------------
# Reconciliation, freeze and recovery
# ---------------------------------------------------------------------------
class TestSlotStateWiring:
    async def test_a_clean_slot_stays_active(self, session):
        svc = service(session)
        status = await svc.reconcile(broker_positions={}, local_positions={})
        assert status is SyncStatus.IN_SYNC
        assert svc.may_open_positions()

    async def test_an_unknown_broker_position_freezes_entries(self, session):
        svc = service(session)
        status = await svc.reconcile(broker_positions={"MSFT": Decimal("2")}, local_positions={})
        assert status is SyncStatus.AMBIGUOUS
        assert not svc.may_open_positions()
        assert any(e[0] == "PAPER_RECONCILIATION_ERROR" for e in svc.events)

    async def test_an_unreachable_broker_freezes_entries(self, session):
        svc = service(session)
        status = await svc.reconcile(
            broker_positions={}, local_positions={}, broker_reachable=False
        )
        assert status is SyncStatus.ERROR
        assert not svc.may_open_positions()

    async def test_recovery_needs_two_consecutive_clean_runs(self, session):
        """**The gate.** One clean poll would flap."""
        svc = service(session)
        await svc.reconcile(broker_positions={"MSFT": Decimal("2")}, local_positions={})
        assert not svc.may_open_positions()
        await svc.reconcile(broker_positions={}, local_positions={})
        assert not svc.may_open_positions()
        await svc.reconcile(broker_positions={}, local_positions={})
        assert svc.may_open_positions()
        assert any(e[0] == "PAPER_SLOT_RECOVERED" for e in svc.events)

    async def test_an_unprotected_position_freezes_the_slot(self, session):
        svc = service(session)
        await enter(svc)
        await svc.repository.apply_reconciliation(
            entry_key(SLOT, "cand-1"),
            broker_order_id="o",
            status="FILLED",
            filled_quantity=Decimal("2"),
            filled_avg_price=Decimal("500"),
            protected_quantity=Decimal("0"),
        )
        status = await svc.reconcile(
            broker_positions={"MSFT": Decimal("2")}, local_positions={"MSFT": Decimal("2")}
        )
        assert status is SyncStatus.AMBIGUOUS
        assert not svc.may_open_positions()

    async def test_an_unknown_broker_order_state_freezes(self, session):
        svc = service(
            session,
            enabled=True,
            submitter=FakeSubmitter(reconciliation(BrokerOrderStatus.UNKNOWN, "0", "500")),
        )
        await enter(svc)
        assert not svc.may_open_positions()


# ---------------------------------------------------------------------------
# Restart: replaying every step must never duplicate
# ---------------------------------------------------------------------------
class TestRestartMatrix:
    async def test_replaying_an_entry_never_produces_a_second_order_row(self, session):
        svc = service(session)
        for _ in range(3):
            await enter(svc)
        rows = await svc.repository.open_orders_for_slot(SLOT.value)
        assert len([r for r in rows if r.client_order_id == entry_key(SLOT, "cand-1")]) == 1

    async def test_replaying_an_exit_never_produces_a_second_order_row(self, session):
        svc = service(session)
        for _ in range(3):
            await svc.close_on_expiry(
                candidate_id="cand-1",
                symbol="MSFT",
                entry_filled_at=FRIDAY_FILL,
                now=EXPIRED_NOW,
                local_quantity=Decimal("2"),
            )
        key = exit_key(SLOT, "cand-1", "MAX_HOLDING_PERIOD")
        rows = await svc.repository.open_orders_for_slot(SLOT.value)
        assert len([r for r in rows if r.client_order_id == key]) == 1

    async def test_a_restart_after_a_broker_fill_recovers_rather_than_resubmits(self, session):
        """Restart case 18: DB write failed after the broker BUY succeeded."""
        key = entry_key(SLOT, "cand-1")
        svc = service(session)
        await enter(svc)  # ORDER_READY persisted, nothing sent

        submitter = FakeSubmitter(reconciliation())
        restarted = ForwardPaperService(
            slot=SLOT,
            repository=ForwardExperimentRepository(session),
            submitter=submitter,
            broker=FakeBroker(),
            lookup=FakeLookup({key: FakeOrder()}),
            execution_enabled=True,
        )
        result = await enter(restarted)
        assert result.outcome is ActionOutcome.RECOVERED
        assert submitter.entries == []

    async def test_the_holding_clock_survives_a_restart(self, session):
        """Restart case 10: age comes from timestamps, not process state."""
        await TestExitWiring().close(session, now=EXPIRED_NOW)
        _restarted, result = await TestExitWiring().close(session, now=EXPIRED_NOW)
        assert result.outcome is not ActionOutcome.NOTHING_TO_DO

    async def test_one_slots_orders_never_appear_in_another(self, session):
        svc = service(session)
        await enter(svc)
        other = ForwardPaperService(
            slot=PaperAccountSlot.PAPER_10K,
            repository=ForwardExperimentRepository(session),
            submitter=FakeSubmitter(reconciliation()),
            broker=FakeBroker(),
            lookup=FakeLookup(),
        )
        assert await other.repository.open_orders_for_slot("PAPER_10K") == []
