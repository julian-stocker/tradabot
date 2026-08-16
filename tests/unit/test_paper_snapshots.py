"""Reading a paper account must stay reading.

The structural tests here are the load-bearing ones. The arithmetic can be
re-derived; the guarantee that a portfolio analysis cannot reach an execution
path is the property that has to hold on every future edit, including edits made
by someone who has not read this file.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

from app.broker import paper_snapshots
from app.broker.paper_accounts import PaperAccountRegistry, PaperAccountSlot
from app.broker.paper_snapshots import PaperAccountSnapshotReader
from app.portfolio_fit.accounts import (
    AccountSnapshot,
    PortfolioAccountReader,
    SnapshotPosition,
    safe_account_reference,
)

ADAPTER = Path("app/broker/paper_snapshots.py")
PROTOCOL = Path("app/portfolio_fit/accounts.py")

MUTATING_CALLS = (
    "submit_order",
    "cancel_order",
    "cancel_orders",
    "cancel_order_by_id",
    "replace_order",
    "close_position",
    "close_all_positions",
    "liquidate",
    "submit_exit",
    "PaperOrderSubmitter",
    "MarketOrderRequest",
    "LimitOrderRequest",
    "OrderRequest",
)

FAKE_ENV = {
    "ALPACA_PAPER_1K_API_KEY": "PKFAKE1K",
    "ALPACA_PAPER_1K_API_SECRET": "s1k",
    "ALPACA_PAPER_3K_API_KEY": "PKFAKE3K",
    "ALPACA_PAPER_3K_API_SECRET": "s3k",
    "ALPACA_PAPER_10K_API_KEY": "PKFAKE10K",
    "ALPACA_PAPER_10K_API_SECRET": "s10k",
}


class FakeAccount:
    def __init__(self, number="PA123", equity="3000", cash="3000", buying="12000"):
        self.account_number = number
        self.equity, self.cash, self.buying_power = equity, cash, buying
        self.status, self.currency = "ACTIVE", "USD"
        self.trading_blocked = False
        self.account_blocked = False


class FakeClient:
    """Records every method called, so a silent mutation cannot pass unnoticed."""

    def __init__(self, account=None, positions=(), orders=(), raises=False):
        self._account = account or FakeAccount()
        self._positions, self._orders, self._raises = positions, orders, raises
        self.calls: list[str] = []

    def get_account(self):
        self.calls.append("get_account")
        if self._raises:
            msg = "unauthorized"
            raise RuntimeError(msg)
        return self._account

    def get_all_positions(self):
        self.calls.append("get_all_positions")
        return list(self._positions)

    def get_orders(self):
        self.calls.append("get_orders")
        return list(self._orders)


def position(symbol="NVDA", qty="2", value="400", price="200", entry="150", pl="100"):
    return SimpleNamespace(
        symbol=symbol,
        qty=qty,
        market_value=value,
        current_price=price,
        avg_entry_price=entry,
        unrealized_pl=pl,
    )


def reader(clients=None, env=None):
    registry = PaperAccountRegistry.load(env=dict(env if env is not None else FAKE_ENV))
    return PaperAccountSnapshotReader(registry, clients=clients or {})


class TestCannotTrade:
    def test_the_adapter_never_names_a_mutating_call(self) -> None:
        """**The gate.** Reading an account must not be able to change it."""
        source = ADAPTER.read_text()
        body = source.split('"""', 2)[-1]
        for token in MUTATING_CALLS:
            assert token not in body, f"{ADAPTER} references {token}"

    def test_the_adapter_calls_only_the_three_read_methods(self) -> None:
        """**The gate.** Enumerated from the syntax tree, not from a promise."""
        tree = ast.parse(ADAPTER.read_text())
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        vendor = called & {
            name
            for name in called
            if name.startswith(("get_", "submit", "cancel", "close", "replace"))
        }
        assert vendor <= {"get_account", "get_all_positions", "get_orders",
                          "get_secret_value"}, f"unexpected vendor calls: {vendor}"

    def test_a_snapshot_read_touches_nothing_else(self) -> None:
        client = FakeClient(positions=[position()])
        snapshot = reader({PaperAccountSlot.PAPER_3K: client}).snapshot("PAPER_3K")
        assert snapshot.available
        assert set(client.calls) <= {"get_account", "get_all_positions", "get_orders"}

    def test_the_protocol_exposes_no_mutation(self) -> None:
        """**The gate.** The read-only contract has no write member to call."""
        members = {
            name
            for name, _ in inspect.getmembers(PortfolioAccountReader)
            if not name.startswith("_")
        }
        assert members == {"slots", "snapshot"}

    def test_the_analysis_package_defines_the_protocol_without_a_broker(self) -> None:
        tree = ast.parse(PROTOCOL.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(("app.broker", "alpaca")), (
                    f"{PROTOCOL} imports {name}"
                )

    def test_the_adapter_satisfies_the_read_only_protocol(self) -> None:
        assert isinstance(reader(), PortfolioAccountReader)


class TestAccountIsolation:
    def test_each_slot_returns_its_own_account(self) -> None:
        """**The gate.** One account's data must never answer for another's."""
        clients = {
            PaperAccountSlot.PAPER_1K: FakeClient(FakeAccount("PA111", "1000", "1000")),
            PaperAccountSlot.PAPER_3K: FakeClient(FakeAccount("PA333", "3000", "3000")),
            PaperAccountSlot.PAPER_10K: FakeClient(
                FakeAccount("PA101010", "10000", "10000")
            ),
        }
        snaps = reader(clients).snapshots()
        assert [snaps[s].equity for s in ("PAPER_1K", "PAPER_3K", "PAPER_10K")] == [
            1000.0,
            3000.0,
            10000.0,
        ]
        references = {s.account_reference for s in snaps.values()}
        assert len(references) == 3

    def test_a_missing_slot_never_falls_back_to_another(self) -> None:
        """**The gate.** An unconfigured account refuses; it does not borrow keys."""
        env = {k: v for k, v in FAKE_ENV.items() if "10K" not in k}
        clients = {PaperAccountSlot.PAPER_1K: FakeClient(FakeAccount("PA111"))}
        snapshot = reader(clients, env=env).snapshot("PAPER_10K")
        assert not snapshot.available
        assert snapshot.error == paper_snapshots.NOT_CONFIGURED
        assert snapshot.account_reference is None

    def test_one_failing_account_does_not_disturb_another(self) -> None:
        clients = {
            PaperAccountSlot.PAPER_1K: FakeClient(raises=True),
            PaperAccountSlot.PAPER_3K: FakeClient(FakeAccount("PA333", "3000")),
        }
        snaps = reader(clients).snapshots(("PAPER_1K", "PAPER_3K"))
        assert not snaps["PAPER_1K"].available
        assert snaps["PAPER_3K"].available
        assert snaps["PAPER_3K"].equity == 3000.0

    def test_the_reference_identifies_without_revealing(self) -> None:
        reference = safe_account_reference("PA1234567890")
        assert "1234567890" not in reference
        assert reference == safe_account_reference("PA1234567890")
        assert reference != safe_account_reference("PA0987654321")


class TestOrdinaryStates:
    def test_an_unknown_slot_is_a_state_not_a_crash(self) -> None:
        snapshot = reader().snapshot("PAPER_50K")
        assert not snapshot.available
        assert "UNKNOWN_SLOT" in (snapshot.error or "")

    def test_a_flat_account_is_valid(self) -> None:
        snapshot = reader({PaperAccountSlot.PAPER_1K: FakeClient()}).snapshot("PAPER_1K")
        assert snapshot.available
        assert snapshot.is_flat
        portfolio = snapshot.to_portfolio()
        assert portfolio.positions == ()
        assert portfolio.equity == portfolio.cash == snapshot.cash

    def test_leverage_is_withheld_and_reported(self) -> None:
        """Two of three accounts are margin accounts; the experiment uses none."""
        client = FakeClient(FakeAccount("PA333", equity="3000", cash="3000",
                                        buying="12000"))
        snapshot = reader({PaperAccountSlot.PAPER_3K: client}).snapshot("PAPER_3K")
        assert snapshot.broker_buying_power == 12000.0
        assert snapshot.usable_capital == 3000.0
        assert snapshot.leverage_withheld == 9000.0

    def test_positions_carry_price_and_cost_basis(self) -> None:
        client = FakeClient(positions=[position(qty="2", value="400", entry="150")])
        snapshot = reader({PaperAccountSlot.PAPER_3K: client}).snapshot("PAPER_3K")
        held = snapshot.positions[0]
        assert (held.symbol, held.quantity, held.market_value) == ("NVDA", 2.0, 400.0)
        assert held.cost_basis == 300.0
        portfolio = snapshot.to_portfolio()
        assert portfolio.positions[0].price == 200.0

    def test_an_unreported_cost_basis_stays_unknown(self) -> None:
        """A defaulted zero basis renders as a 100% gain, which is invented."""
        held = SnapshotPosition("AAA", 3.0, 300.0, price=100.0)
        assert held.cost_basis is None
        assert AccountSnapshot("PAPER_1K", "now", True,
                               positions=(held,)).to_portfolio().positions[0].unrealised is None
