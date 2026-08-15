"""The order path. Every test here uses a fake client; nothing reaches Alpaca.

The dangerous failure in this module is not a crash — it is a submission
acknowledgement being mistaken for a fill, or one account's order being sent
through another account's client. Both produce a portfolio that quietly believes
something untrue.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.broker import paper_orders
from app.broker.paper_accounts import (
    PaperAccountRegistry,
    PaperAccountSlot,
    PaperAccountState,
    PaperExecutionRefusedError,
    effective_capital,
)
from app.broker.paper_orders import (
    BrokerOrderStatus,
    PaperOrderRequest,
    PaperOrderSubmitter,
    normalise_status,
)

FAKE_ENV = {
    "ALPACA_PAPER_1K_API_KEY": "PK1",
    "ALPACA_PAPER_1K_API_SECRET": "s1",
    "ALPACA_PAPER_3K_API_KEY": "PK3",
    "ALPACA_PAPER_3K_API_SECRET": "s3",
    "ALPACA_PAPER_10K_API_KEY": "PK10",
    "ALPACA_PAPER_10K_API_SECRET": "s10",
    "ALPACA_PAPER_EXECUTION_ENABLED": "true",
}


class FakeOrder:
    def __init__(self, status="filled", filled_qty="1", price="100", reason=None):
        self.id = "ord-1"
        self.symbol = "LMT"
        self.side = "buy"
        self.order_type = "market"
        self.status = status
        self.filled_qty = filled_qty
        self.filled_avg_price = price
        self.submitted_at = datetime(2026, 8, 17, 14, tzinfo=UTC)
        self.filled_at = datetime(2026, 8, 17, 14, 1, tzinfo=UTC)
        self.rejected_reason = reason


class FakeClient:
    def __init__(self, order=None, raises=None):
        self.order = order or FakeOrder()
        self.raises = raises
        self.submitted: list[object] = []

    def submit_order(self, request):
        if self.raises:
            raise self.raises
        self.submitted.append(request)
        return self.order

    def get_order_by_id(self, order_id):
        return self.order


def registry(enabled=True):
    env = dict(FAKE_ENV)
    env["ALPACA_PAPER_EXECUTION_ENABLED"] = "true" if enabled else "false"
    return PaperAccountRegistry.load(env=env)


def state(slot=PaperAccountSlot.PAPER_1K, equity="1000"):
    return PaperAccountState(
        slot=slot,
        account_number="PA1",
        is_paper=True,
        status="ACTIVE",
        currency="USD",
        cash=Decimal(equity),
        equity=Decimal(equity),
        buying_power=Decimal(equity),
        trading_blocked=False,
        account_blocked=False,
        open_positions=0,
        open_orders=0,
    )


def request(slot=PaperAccountSlot.PAPER_1K, qty="1", fractional=False, notional="100"):
    return PaperOrderRequest(
        slot=slot,
        symbol="LMT",
        quantity=Decimal(qty),
        decision_id=1,
        idempotency_key=f"entry:{slot.value}:1",
        notional=Decimal(notional),
        fractional=fractional,
    )


def submitter(slot=PaperAccountSlot.PAPER_1K, client=None, enabled=True):
    return PaperOrderSubmitter(registry=registry(enabled), slot=slot, client=client or FakeClient())


def send(sub, req, **kw):
    base = {
        "state": state(sub.slot),
        "capital": effective_capital(state(sub.slot)),
        "risk_permits": True,
        "already_submitted": False,
        "asset_fractionable": True,
    }
    base.update(kw)
    return sub.submit_entry(req, **base)


# ---------------------------------------------------------------------------
# A submission is not a fill
# ---------------------------------------------------------------------------
class TestFillsAreNotAssumed:
    @pytest.mark.parametrize(
        ("status", "filled", "opens"),
        [
            ("new", "0", False),
            ("pending_new", "0", False),
            ("accepted", "0", False),
            ("partially_filled", "0.4", True),
            ("filled", "1", True),
            ("canceled", "0", False),
            ("rejected", "0", False),
            ("expired", "0", False),
        ],
    )
    def test_only_a_real_fill_may_open_a_position(
        self, status: str, filled: str, opens: bool
    ) -> None:
        """**The gate.** Opening on acknowledgement is how a portfolio comes to
        believe it owns something it never bought."""
        client = FakeClient(FakeOrder(status=status, filled_qty=filled))
        result = send(submitter(client=client), request())
        assert result.opens_position is opens

    def test_a_fill_without_a_price_cannot_open_a_position(self) -> None:
        client = FakeClient(FakeOrder(status="filled", filled_qty="1", price=None))
        assert not send(submitter(client=client), request()).opens_position

    def test_an_unknown_status_is_never_treated_as_a_fill(self) -> None:
        client = FakeClient(FakeOrder(status="some_new_alpaca_state", filled_qty="1"))
        result = send(submitter(client=client), request())
        assert result.status is BrokerOrderStatus.UNKNOWN
        assert not result.opens_position

    def test_a_partial_fill_is_flagged_and_sized_by_what_traded(self) -> None:
        client = FakeClient(FakeOrder(status="partially_filled", filled_qty="0.4", price="100"))
        result = send(submitter(client=client), request(qty="1"))
        assert result.is_partial
        assert result.economic_notional == Decimal("40")

    def test_a_rejection_carries_its_reason_and_opens_nothing(self) -> None:
        client = FakeClient(FakeOrder(status="rejected", filled_qty="0", reason="insufficient"))
        result = send(submitter(client=client), request())
        assert result.rejection_reason == "insufficient"
        assert result.is_terminal
        assert not result.opens_position

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("FILLED", BrokerOrderStatus.FILLED),
            ("cancelled", BrokerOrderStatus.CANCELED),
            ("", BrokerOrderStatus.UNKNOWN),
            (None, BrokerOrderStatus.UNKNOWN),
        ],
    )
    def test_status_normalisation(self, raw: object, expected: BrokerOrderStatus) -> None:
        assert normalise_status(raw) == expected


# ---------------------------------------------------------------------------
# Isolation and the gates
# ---------------------------------------------------------------------------
class TestSubmissionGates:
    def test_a_submitter_refuses_another_slots_request(self) -> None:
        """**The gate.** Structural isolation: the slot is bound, not passed."""
        sub = submitter(PaperAccountSlot.PAPER_1K)
        with pytest.raises(PaperExecutionRefusedError, match="refusing to cross accounts"):
            send(sub, request(slot=PaperAccountSlot.PAPER_3K))

    def test_execution_disabled_refuses_before_any_client_call(self) -> None:
        client = FakeClient()
        with pytest.raises(PaperExecutionRefusedError, match="disabled"):
            send(submitter(client=client, enabled=False), request())
        assert client.submitted == []

    def test_a_risk_rejection_never_reaches_the_broker(self) -> None:
        client = FakeClient()
        with pytest.raises(PaperExecutionRefusedError, match="risk gate"):
            send(submitter(client=client), request(), risk_permits=False)
        assert client.submitted == []

    def test_a_leveraged_notional_never_reaches_the_broker(self) -> None:
        client = FakeClient()
        with pytest.raises(PaperExecutionRefusedError, match="no-leverage cap"):
            send(submitter(client=client), request(notional="5000"))
        assert client.submitted == []

    def test_a_duplicate_decision_never_reaches_the_broker(self) -> None:
        client = FakeClient()
        with pytest.raises(PaperExecutionRefusedError, match="already used"):
            send(submitter(client=client), request(), already_submitted=True)
        assert client.submitted == []

    def test_a_fractional_order_on_a_non_fractionable_asset_is_refused(self) -> None:
        """Capability is verified, never discovered by sending an order."""
        client = FakeClient()
        with pytest.raises(PaperExecutionRefusedError, match="not fractionable"):
            send(
                submitter(client=client),
                request(qty="0.5", fractional=True),
                asset_fractionable=False,
            )
        assert client.submitted == []

    def test_a_fractional_order_on_a_fractionable_asset_is_permitted(self) -> None:
        client = FakeClient(FakeOrder(filled_qty="0.5"))
        result = send(submitter(client=client), request(qty="0.5", fractional=True))
        assert result.opens_position
        assert len(client.submitted) == 1

    def test_the_idempotency_key_becomes_the_client_order_id(self) -> None:
        """So a retry is deduplicated by the broker, not only by tradabot."""
        client = FakeClient()
        send(submitter(client=client), request())
        assert client.submitted[0].client_order_id == "entry:PAPER_1K:1"

    def test_every_order_is_a_long_day_market_order(self) -> None:
        """GTC would let an order fill on information the decision never saw."""
        client = FakeClient()
        send(submitter(client=client), request())
        sent = client.submitted[0]
        assert str(sent.side).lower().endswith("buy")
        assert str(sent.time_in_force).lower().endswith("day")


# ---------------------------------------------------------------------------
# Paper only
# ---------------------------------------------------------------------------
class TestPaperOnly:
    def test_the_client_is_always_constructed_for_paper(self) -> None:
        source = inspect.getsource(paper_orders._client)
        assert "paper=True" in source

    def test_no_live_endpoint_appears_anywhere(self) -> None:
        source = inspect.getsource(paper_orders)
        assert "https://api.alpaca.markets" not in source

    def test_a_sell_only_ever_reduces_a_long_position(self) -> None:
        """**The gate.** The SELL path exists now, but it may only close.

        There is no short entry: ``submit_entry`` is BUY-only, and
        ``submit_exit`` refuses a quantity it was not handed after broker
        confirmation. Together those are the two halves of "never net short".
        """
        entry = inspect.getsource(PaperOrderSubmitter.submit_entry)
        assert "OrderSide.BUY" in entry
        assert "OrderSide.SELL" not in entry

        exit_source = inspect.getsource(PaperOrderSubmitter.submit_exit)
        assert "OrderSide.SELL" in exit_source
        assert "stop_loss" not in exit_source  # nothing left to protect

    def test_the_endpoint_is_rechecked_immediately_before_sending(self) -> None:
        source = inspect.getsource(PaperOrderSubmitter.submit_entry)
        assert "classify_endpoint" in source

    def test_options_data_cannot_influence_an_order(self) -> None:
        source = inspect.getsource(paper_orders).lower()
        for forbidden in ("implied_volatility", "option_surface", "iv_30d"):
            assert forbidden not in source
