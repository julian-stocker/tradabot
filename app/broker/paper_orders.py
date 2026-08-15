"""Submitting and reconciling orders on an Alpaca **paper** account.

The only module in tradabot that can place an order of any kind. It exists
because :mod:`app.market_data.providers.alpaca` deliberately cannot -- that
module's contract is market data, and its docstring promises "no order type is
imported here and none is reachable from this client". Keeping submission in one
small, separately-tested file is what makes that promise checkable.

Everything here is paper
------------------------
:class:`~alpaca.trading.client.TradingClient` is constructed with ``paper=True``
and the endpoint is re-checked before every submission. There is no argument,
setting or code path in this module that reaches ``api.alpaca.markets``.

Fills are not assumed
---------------------
A submitted order is **not** a position. Alpaca returns ``NEW`` or
``PENDING_NEW`` long before anything trades, and a market order placed outside
market hours may sit for hours. :func:`reconcile` reads the broker's own view
back, and :attr:`OrderReconciliation.opens_position` is the single place that
decides whether local state may move -- which happens only on a real filled
quantity, never on a submission acknowledgement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any, Final

from app.broker.paper_accounts import (
    ExperimentCapital,
    PaperAccountCredentials,
    PaperAccountRegistry,
    PaperAccountSlot,
    PaperAccountState,
    PaperExecutionRefusedError,
    assert_may_submit,
    classify_endpoint,
)
from app.core.logging import get_logger

logger = get_logger(__name__)


class BrokerOrderStatus(StrEnum):
    """Alpaca's lifecycle, normalised. Anything unrecognised is ``UNKNOWN``.

    ``UNKNOWN`` is a real member rather than an exception because a broker adding
    a status must not crash reconciliation -- but it must also never be treated
    as a fill, which :attr:`OrderReconciliation.opens_position` guarantees.
    """

    NEW = "NEW"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


TERMINAL_STATUSES: Final[frozenset[BrokerOrderStatus]] = frozenset(
    {
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.CANCELED,
        BrokerOrderStatus.REJECTED,
        BrokerOrderStatus.EXPIRED,
    }
)

_STATUS_ALIASES: Final[dict[str, BrokerOrderStatus]] = {
    "new": BrokerOrderStatus.NEW,
    "pending_new": BrokerOrderStatus.NEW,
    "accepted": BrokerOrderStatus.ACCEPTED,
    "partially_filled": BrokerOrderStatus.PARTIALLY_FILLED,
    "filled": BrokerOrderStatus.FILLED,
    "canceled": BrokerOrderStatus.CANCELED,
    "cancelled": BrokerOrderStatus.CANCELED,
    "pending_cancel": BrokerOrderStatus.CANCELED,
    "rejected": BrokerOrderStatus.REJECTED,
    "expired": BrokerOrderStatus.EXPIRED,
}


def normalise_status(raw: object) -> BrokerOrderStatus:
    """Map a broker status onto the enum, defaulting to ``UNKNOWN``."""
    value = str(getattr(raw, "value", raw) or "").strip().lower()
    return _STATUS_ALIASES.get(value, BrokerOrderStatus.UNKNOWN)


@dataclass(frozen=True, slots=True)
class PaperOrderRequest:
    """One intended paper order. Long only; there is no side field to set."""

    slot: PaperAccountSlot
    symbol: str
    quantity: Decimal
    decision_id: int
    idempotency_key: str
    notional: Decimal
    fractional: bool


@dataclass(frozen=True, slots=True)
class OrderReconciliation:
    """The broker's view of one order, read back rather than assumed."""

    slot: PaperAccountSlot
    broker_order_id: str
    decision_id: int
    symbol: str
    side: str
    requested_quantity: Decimal
    order_type: str
    submitted_at: datetime | None
    status: BrokerOrderStatus
    filled_quantity: Decimal
    filled_avg_price: Decimal | None
    filled_at: datetime | None
    rejection_reason: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_partial(self) -> bool:
        return self.status is BrokerOrderStatus.PARTIALLY_FILLED or (
            0 < self.filled_quantity < self.requested_quantity
        )

    @property
    def opens_position(self) -> bool:
        """Whether local portfolio state may now record a position.

        **The one place this decision is made.** Requires a real filled quantity
        and a real fill price -- an acknowledgement, a pending order and a
        rejection all return False. Opening a local position on submission is how
        a portfolio comes to believe it owns something it never bought.
        """
        return (
            self.filled_quantity > 0
            and self.filled_avg_price is not None
            and self.filled_avg_price > 0
            and self.status in {BrokerOrderStatus.FILLED, BrokerOrderStatus.PARTIALLY_FILLED}
        )

    @property
    def economic_notional(self) -> Decimal:
        """What actually traded, at the price it actually traded."""
        if not self.opens_position or self.filled_avg_price is None:
            return Decimal(0)
        return self.filled_quantity * self.filled_avg_price


def _decimal(value: object, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except Exception:
        return Decimal(default)


def _client(credentials: PaperAccountCredentials) -> Any:
    from alpaca.trading.client import TradingClient  # noqa: PLC0415

    return TradingClient(
        api_key=credentials.api_key.get_secret_value(),
        secret_key=credentials.api_secret.get_secret_value(),
        paper=True,
    )


class PaperOrderSubmitter:
    """Places long paper orders for one slot, and only for that slot.

    Bound to a single :class:`PaperAccountSlot` at construction. There is no
    method that takes a slot argument, so a caller cannot route one account's
    order into another's client by passing the wrong parameter -- the isolation
    is structural rather than a runtime check.
    """

    def __init__(
        self,
        *,
        registry: PaperAccountRegistry,
        slot: PaperAccountSlot,
        client: Any = None,
    ) -> None:
        self._registry = registry
        self._slot = slot
        self._client = client or _client(registry.credentials(slot))

    @property
    def slot(self) -> PaperAccountSlot:
        return self._slot

    def submit_entry(
        self,
        request: PaperOrderRequest,
        *,
        state: PaperAccountState,
        capital: ExperimentCapital,
        risk_permits: bool,
        already_submitted: bool,
        asset_fractionable: bool,
    ) -> OrderReconciliation:
        """Place one long market order, after every gate.

        Raises:
            PaperExecutionRefusedError: if any gate fails. There is no partial
                success: the order is either fully authorised or not sent.
        """
        if request.slot is not self._slot:
            msg = (
                f"submitter is bound to {self._slot.value} but the request names "
                f"{request.slot.value}; refusing to cross accounts"
            )
            raise PaperExecutionRefusedError(msg)

        assert_may_submit(
            registry=self._registry,
            state=state,
            risk_permits=risk_permits,
            quantity=request.quantity,
            idempotency_key=request.idempotency_key,
            already_submitted=already_submitted,
            side="LONG",
            notional=request.notional,
            capital=capital,
        )
        if request.fractional and not asset_fractionable:
            msg = (
                f"{self._slot.value}: {request.symbol} is not fractionable and the "
                f"sized quantity {request.quantity} is not whole"
            )
            raise PaperExecutionRefusedError(msg)
        if classify_endpoint(self._registry.base_url) != "PAPER":
            msg = "endpoint is not paper"  # re-checked immediately before sending
            raise PaperExecutionRefusedError(msg)

        from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: PLC0415
        from alpaca.trading.requests import MarketOrderRequest  # noqa: PLC0415

        order = self._client.submit_order(
            MarketOrderRequest(
                symbol=request.symbol,
                qty=float(request.quantity),
                side=OrderSide.BUY,
                # DAY, never GTC: an order that outlives the session it was
                # decided in would fill on information the decision never saw.
                time_in_force=TimeInForce.DAY,
                client_order_id=request.idempotency_key,
            )
        )
        logger.info(
            "paper order submitted",
            slot=self._slot.value,
            symbol=request.symbol,
            quantity=str(request.quantity),
            decision_id=request.decision_id,
        )
        return self._to_reconciliation(order, request)

    def submit_exit(
        self,
        *,
        slot: PaperAccountSlot,
        symbol: str,
        quantity: Decimal,
        client_order_id: str,
    ) -> OrderReconciliation:
        """Close a long position. **Only ever reduces.**

        The quantity must already have been confirmed against the broker by
        :func:`~app.paper.lifecycle.plan_protected_exit` — this method does not
        re-derive it, and there is no path here that sizes a sell from local
        belief. A plain DAY market SELL: no protective legs, because there is
        nothing left to protect once the position is gone.

        Raises:
            PaperExecutionRefusedError: on a cross-slot request, a fractional or
                non-positive quantity, a disabled execution flag, or a
                non-paper endpoint.
        """
        if slot is not self._slot:
            msg = (
                f"submitter is bound to {self._slot.value} but the exit names "
                f"{slot.value}; refusing to cross accounts"
            )
            raise PaperExecutionRefusedError(msg)
        if quantity <= 0 or quantity != quantity.to_integral_value():
            msg = f"{self._slot.value}: refusing to sell {quantity} of {symbol}"
            raise PaperExecutionRefusedError(msg)
        if not self._registry.execution_enabled:
            msg = "paper execution is disabled; set the execution flag explicitly to enable it"
            raise PaperExecutionRefusedError(msg)
        if classify_endpoint(self._registry.base_url) != "PAPER":
            msg = f"endpoint {self._registry.base_url!r} is not positively identified as paper"
            raise PaperExecutionRefusedError(msg)

        from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: PLC0415
        from alpaca.trading.requests import MarketOrderRequest  # noqa: PLC0415

        order = self._client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=float(quantity),
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id,
            )
        )
        logger.info(
            "paper exit submitted",
            slot=self._slot.value,
            symbol=symbol,
            quantity=str(quantity),
        )
        return self._to_reconciliation(
            order,
            PaperOrderRequest(
                slot=slot,
                symbol=symbol,
                quantity=quantity,
                decision_id=0,
                idempotency_key=client_order_id,
                notional=Decimal(0),
                fractional=False,
            ),
        )

    def reconcile(self, broker_order_id: str, request: PaperOrderRequest) -> OrderReconciliation:
        """Re-read one order from the broker. Never infers a fill."""
        order = self._client.get_order_by_id(broker_order_id)
        return self._to_reconciliation(order, request)

    def _to_reconciliation(self, order: Any, request: PaperOrderRequest) -> OrderReconciliation:
        filled_price = getattr(order, "filled_avg_price", None)
        return OrderReconciliation(
            slot=self._slot,
            broker_order_id=str(getattr(order, "id", "") or ""),
            decision_id=request.decision_id,
            symbol=str(getattr(order, "symbol", request.symbol)),
            side=str(getattr(getattr(order, "side", ""), "value", getattr(order, "side", "buy"))),
            requested_quantity=request.quantity,
            order_type=str(
                getattr(
                    getattr(order, "order_type", ""),
                    "value",
                    getattr(order, "order_type", "market"),
                )
            ),
            submitted_at=getattr(order, "submitted_at", None),
            status=normalise_status(getattr(order, "status", None)),
            filled_quantity=_decimal(getattr(order, "filled_qty", 0)),
            filled_avg_price=_decimal(filled_price) if filled_price is not None else None,
            filled_at=getattr(order, "filled_at", None),
            rejection_reason=(str(getattr(order, "rejected_reason", "") or "") or None),
        )


# ---------------------------------------------------------------------------
# Whole-share feasibility and broker-native protection
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WholeShareFeasibility:
    """Whether an account can buy at least one whole share within its caps.

    Applied **after** match-b-v1 has ranked the full 52-symbol universe, never
    before. Filtering expensive symbols out of the cross-section would give the
    smaller account a different alpha model, and the whole experiment is that
    all three accounts see the same opportunities.
    """

    feasible: bool
    whole_shares: Decimal
    fractional_shares: Decimal
    max_notional: Decimal
    price: Decimal

    @property
    def shortfall_reason(self) -> str | None:
        if self.feasible:
            return None
        return (
            f"one share at {self.price} exceeds the {self.max_notional} position "
            f"allowance (risk-sized {self.fractional_shares:.4f} shares)"
        )


def whole_share_feasibility(
    *, risk_sized_quantity: Decimal, price: Decimal, max_notional: Decimal
) -> WholeShareFeasibility:
    """Floor a risk-derived quantity to whole shares. **Never rounds up.**

    Rounding up by one share would breach whichever cap produced the quantity --
    and on an expensive instrument in a small account, one share *is* the breach.
    """
    whole = risk_sized_quantity.to_integral_value(rounding=ROUND_DOWN)
    return WholeShareFeasibility(
        feasible=whole >= 1 and whole * price <= max_notional,
        whole_shares=max(Decimal(0), whole),
        fractional_shares=risk_sized_quantity,
        max_notional=max_notional,
        price=price,
    )


@dataclass(frozen=True, slots=True)
class ProtectedEntryPlan:
    """An entry and its protection, as one atomic broker request.

    Alpaca's bracket class attaches the stop and target legs to the entry, so
    the broker holds the protection the moment the entry fills. That removes the
    ``ENTRY_FILLED_UNPROTECTED`` window entirely -- the state that made phase
    12.5 refuse unattended operation, because a stop living only in a Python
    process disappears when the machine sleeps.

    Both prices come from the canonical
    :func:`~app.paper.exits.derive_stop_and_target`, widened by risk-v1's noise
    floor. Nothing here chooses or tunes a level.
    """

    slot: PaperAccountSlot
    symbol: str
    quantity: Decimal
    stop_price: Decimal
    take_profit_price: Decimal | None
    idempotency_key: str
    candidate_id: str

    @property
    def is_whole_share(self) -> bool:
        return self.quantity == self.quantity.to_integral_value()


def build_protected_entry(plan: ProtectedEntryPlan) -> Any:
    """Construct the atomic bracket/OTO request. Does **not** send it.

    Raises:
        PaperExecutionRefusedError: on a fractional quantity. Alpaca supports
            fractional quantities for plain market orders only -- the SDK will
            happily *construct* a fractional bracket and the server refuses it,
            so the guard has to live here or an unprotected position is the
            result.
    """
    if not plan.is_whole_share:
        msg = (
            f"{plan.slot.value}: {plan.quantity} of {plan.symbol} is fractional; "
            f"Alpaca supports protective legs on whole-share orders only"
        )
        raise PaperExecutionRefusedError(msg)
    if plan.stop_price <= 0:
        msg = f"{plan.slot.value}: refusing an entry with stop price {plan.stop_price}"
        raise PaperExecutionRefusedError(msg)

    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce  # noqa: PLC0415
    from alpaca.trading.requests import (  # noqa: PLC0415
        MarketOrderRequest,
        StopLossRequest,
        TakeProfitRequest,
    )

    legs: dict[str, Any] = {"stop_loss": StopLossRequest(stop_price=float(plan.stop_price))}
    order_class = OrderClass.OTO
    if plan.take_profit_price is not None and plan.take_profit_price > plan.stop_price:
        legs["take_profit"] = TakeProfitRequest(limit_price=float(plan.take_profit_price))
        order_class = OrderClass.BRACKET

    return MarketOrderRequest(
        symbol=plan.symbol,
        qty=float(plan.quantity),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=order_class,
        client_order_id=plan.idempotency_key,
        **legs,
    )
