"""PaperBroker: a virtual counterparty backed by the database.

Implements :class:`~app.broker.protocols.Broker`. No real broker, no real money,
no order routing -- and no path to any of those.

Design
------
The broker owns three things and nothing else:

1. **Execution gates.** Can this portfolio actually place this order *right now*?
   These are live-state questions (cash, open positions, drawdown, quote
   freshness), distinct from the signal-level questions
   :func:`~app.simulation.decisions.evaluate_decision` already answered.
2. **Fill pricing.** Delegated to :mod:`app.paper.execution`, which crosses the
   spread and applies slippage.
3. **Bookkeeping.** Order, position and cash mutations, all inside one caller
   transaction.

It does *not* decide what to trade, when to exit, or how large a position should
be. Those live in the decision engine, the exit engine and the sizing module
respectively, so each is testable without a database.

Atomicity
---------
Every method mutates through the caller's session and never commits. "Order
filled + cash reduced + position created" must be one transaction, and only the
caller knows where its transaction boundary is. A broker that committed on its
own would make partial failure possible and rollback tests impossible.

Rejections are returned, not raised
-----------------------------------
"Rejected: insufficient cash" is an outcome to record, not an exceptional
condition. Exceptions are reserved for genuine faults -- a malformed request, an
unsupported order type.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.backtesting.models import Fill, Order, Position
from app.broker.protocols import AccountState, OrderState, OrderStatus
from app.core.logging import get_logger
from app.db.models import VirtualOrder, VirtualPortfolio, VirtualPosition
from app.domain.enums import (
    OrderRejectionReason,
    OrderType,
    PositionStatus,
    Side,
)
from app.domain.quotes import Quote
from app.paper.execution import FillPricing, price_fill
from app.paper.portfolio import PortfolioValuation, apply_entry, apply_exit
from app.simulation.models import SimulationProfileConfig

logger = get_logger(__name__)

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """An order to place, with everything the broker needs to price and gate it.

    Richer than :class:`~app.backtesting.models.Order` because a paper broker
    needs provenance (which decision, which position) and an idempotency key that
    the backtesting value object has no use for.
    """

    profile: SimulationProfileConfig
    instrument_id: int
    symbol: str
    side: Side
    quantity: Decimal
    idempotency_key: str
    requested_at: datetime
    order_type: OrderType = OrderType.MARKET

    quote: Quote | None = None
    reference_price: Decimal | None = None
    trade_decision_id: int | None = None
    position_id: int | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    signal_id: int | None = None
    max_holding_bars: int | None = None

    def to_order(self) -> Order:
        """Adapt to the shared value object the ``Broker`` protocol speaks."""
        return Order(
            symbol=self.symbol,
            side=self.side,
            quantity=self.quantity,
            created_at=self.requested_at,
            reason=f"decision={self.trade_decision_id}" if self.trade_decision_id else "",
        )


class PaperBroker:
    """A database-backed virtual broker for one simulation profile."""

    def __init__(
        self,
        session: AsyncSession,
        profile: SimulationProfileConfig,
        portfolio: VirtualPortfolio,
    ) -> None:
        self._session = session
        self._profile = profile
        self._portfolio = portfolio

    @property
    def name(self) -> str:
        return "paper"

    @property
    def portfolio(self) -> VirtualPortfolio:
        return self._portfolio

    # -- Broker protocol ---------------------------------------------------

    async def place_order(self, order: Order) -> OrderState:
        """Protocol-shaped entry point.

        The protocol's :class:`Order` carries no quote, no instrument id and no
        idempotency key, so this cannot actually execute. It exists to satisfy the
        interface and to fail loudly rather than guess.

        Use :meth:`submit` for real work.
        """
        msg = (
            "PaperBroker.place_order requires the richer OrderRequest (quote, "
            "instrument id, idempotency key). Call PaperBroker.submit instead; "
            "this method exists only to satisfy the Broker protocol signature."
        )
        raise NotImplementedError(msg)

    async def cancel_order(self, order_id: str) -> OrderState:
        """Cancel a resting order.

        Market orders fill or reject synchronously, so there is never anything
        resting to cancel. Kept honest: it raises for unknown ids and refuses to
        "cancel" an already-terminal order rather than silently succeeding.
        """
        row = await self._get_order_row(order_id)
        if row.status in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED):
            msg = f"order {order_id} is already {row.status.value} and cannot be cancelled"
            raise ValueError(msg)
        row.status = OrderStatus.CANCELLED
        await self._session.flush()
        return _to_order_state(row, self._profile)

    async def get_order(self, order_id: str) -> OrderState:
        return _to_order_state(await self._get_order_row(order_id), self._profile)

    async def get_positions(self) -> Sequence[Position]:
        """Open positions, as the protocol's value object."""
        rows = await self._open_positions()
        return [
            Position(
                symbol=str(row.instrument_id),
                side=row.side,
                quantity=row.quantity,
                entry_price=row.average_entry_price,
                entry_time=row.entry_timestamp,
                stop_loss=row.stop_loss,
                take_profit=row.take_profit,
            )
            for row in rows
        ]

    async def get_account_state(self) -> AccountState:
        """Cash, equity and exposure, marked at last known prices."""
        rows = await self._open_positions()
        exposure = sum(
            ((r.current_mark_price or r.average_entry_price) * r.quantity for r in rows), ZERO
        )
        return AccountState(
            cash=self._portfolio.cash,
            equity=self._portfolio.cash + exposure,
            currency=self._portfolio.currency,
            open_positions=len(rows),
            total_exposure=exposure,
            as_of=self._portfolio.last_valued_at or self._portfolio.created_at,
        )

    # -- Real entry points -------------------------------------------------

    async def submit(self, request: OrderRequest, *, valuation: PortfolioValuation) -> VirtualOrder:
        """Place an order: gate it, price it, and record the result.

        Idempotent on ``request.idempotency_key`` -- resubmitting returns the
        existing order untouched rather than trading twice.

        Args:
            request: what to trade and with what context.
            valuation: current portfolio valuation, for the risk gates. Passed in
                rather than computed here so the broker does no market data I/O.

        Returns:
            The persisted order, ``FILLED`` or ``REJECTED``.
        """
        existing = await self._find_by_key(request.idempotency_key)
        if existing is not None:
            logger.debug("order already exists for key", key=request.idempotency_key)
            return existing

        if request.order_type is not OrderType.MARKET:
            return await self.reject(
                request,
                OrderRejectionReason.UNSUPPORTED_ORDER_TYPE,
                f"{request.order_type.value} orders are not implemented",
            )

        is_entry = request.position_id is None
        if is_entry:
            rejection = await self._gate_entry(request, valuation)
            if rejection is not None:
                return await self.reject(request, *rejection)

        pricing = price_fill(
            side=request.side,
            quantity=request.quantity,
            settings=self._profile.costs.to_cost_settings(),
            quote=request.quote,
            reference_price=request.reference_price,
        )

        if is_entry and -pricing.cash_delta > self._portfolio.cash:
            return await self.reject(
                request,
                OrderRejectionReason.INSUFFICIENT_CASH,
                (
                    f"fill would cost {-pricing.cash_delta} but only "
                    f"{self._portfolio.cash} is available"
                ),
            )

        return await self._fill(request, pricing)

    # -- Gates -------------------------------------------------------------

    async def _gate_entry(  # noqa: PLR0911 -- one return per gate; which gate fires is the result
        self, request: OrderRequest, valuation: PortfolioValuation
    ) -> tuple[OrderRejectionReason, str] | None:
        """Execution-time checks. Returns a rejection, or None to proceed.

        Ordered cheapest-first, and each is a *live portfolio state* question that
        the decision engine could not have answered.
        """
        risk = self._profile.risk

        if not self._profile.enabled:
            return (
                OrderRejectionReason.PROFILE_DISABLED,
                f"profile {self._profile.name!r} is disabled",
            )

        if self._portfolio.halted_reason:
            return (
                OrderRejectionReason.MAX_DRAWDOWN,
                f"portfolio is halted: {self._portfolio.halted_reason}",
            )

        if request.side is not Side.LONG:
            return (
                OrderRejectionReason.SHORT_NOT_SUPPORTED,
                "short positions are not implemented; tradabot refuses rather than "
                "simulating them approximately",
            )

        if request.quote is not None and _quote_is_stale(
            request.quote, request.requested_at, risk.max_quote_age_seconds
        ):
            age = (request.requested_at - request.quote.timestamp).total_seconds()
            return (
                OrderRejectionReason.STALE_QUOTE,
                f"quote is {age:.0f}s old, limit is {risk.max_quote_age_seconds}s",
            )

        if valuation.drawdown < -float(risk.max_drawdown):
            return (
                OrderRejectionReason.MAX_DRAWDOWN,
                (
                    f"drawdown {valuation.drawdown:.2%} exceeds the "
                    f"{float(risk.max_drawdown):.2%} limit"
                ),
            )

        if valuation.open_position_count >= risk.max_open_positions:
            return (
                OrderRejectionReason.MAX_OPEN_POSITIONS,
                (
                    f"{valuation.open_position_count} positions open, limit is "
                    f"{risk.max_open_positions}"
                ),
            )

        if not risk.allow_pyramiding and await self._has_open_position(request.instrument_id):
            return (
                OrderRejectionReason.POSITION_ALREADY_OPEN,
                "a position is already open in this instrument and pyramiding is disabled",
            )

        exposure_limit = valuation.equity * risk.max_total_exposure
        if valuation.gross_exposure >= exposure_limit:
            return (
                OrderRejectionReason.MAX_EXPOSURE,
                (f"exposure {valuation.gross_exposure} is at the limit ({exposure_limit})"),
            )

        if request.quantity <= 0:
            return (
                OrderRejectionReason.QUANTITY_TOO_SMALL,
                f"quantity {request.quantity} is not positive",
            )

        return None

    # -- Mutation ----------------------------------------------------------

    async def _fill(self, request: OrderRequest, pricing: FillPricing) -> VirtualOrder:
        """Record a filled order and move cash, positions and P&L accordingly."""
        order = VirtualOrder(
            simulation_profile_id=_require_id(self._profile),
            instrument_id=request.instrument_id,
            trade_decision_id=request.trade_decision_id,
            position_id=request.position_id,
            idempotency_key=request.idempotency_key,
            side=request.side,
            order_type=request.order_type,
            status=OrderStatus.FILLED,
            quantity=request.quantity,
            requested_at=request.requested_at,
            filled_at=request.requested_at,
            requested_price=pricing.mid_price,
            executed_price=pricing.fill_price,
            touch_price=pricing.touch_price,
            fees=pricing.fee,
            spread_cost=pricing.spread_cost,
            slippage_cost=pricing.slippage_cost,
            used_live_quote=pricing.used_quote,
        )
        self._session.add(order)

        if request.position_id is None:
            position = VirtualPosition(
                simulation_profile_id=_require_id(self._profile),
                instrument_id=request.instrument_id,
                originating_signal_id=request.signal_id,
                originating_trade_decision_id=request.trade_decision_id,
                side=request.side,
                status=PositionStatus.OPEN,
                quantity=request.quantity,
                average_entry_price=pricing.fill_price,
                entry_timestamp=request.requested_at,
                entry_bar_index=self._portfolio.bars_processed,
                current_mark_price=pricing.fill_price,
                unrealized_pnl=ZERO,
                stop_loss=request.stop_loss,
                take_profit=request.take_profit,
                maximum_holding_until_bar=(
                    self._portfolio.bars_processed + request.max_holding_bars
                    if request.max_holding_bars is not None
                    else None
                ),
                highest_price_seen=pricing.fill_price,
                lowest_price_seen=pricing.fill_price,
                entry_costs=pricing.total_cost,
                entry_fee=pricing.fee,
            )
            self._session.add(position)
            await self._session.flush()
            order.position_id = position.id

            apply_entry(
                self._portfolio,
                cash_delta=pricing.cash_delta,
                fee=pricing.fee,
                spread_cost=pricing.spread_cost,
                slippage_cost=pricing.slippage_cost,
            )
            logger.info(
                "entry filled",
                profile=self._profile.name,
                symbol=request.symbol,
                quantity=str(request.quantity),
                price=str(pricing.fill_price),
            )

        await self._session.flush()
        return order

    async def reject(
        self, request: OrderRequest, reason: OrderRejectionReason, detail: str
    ) -> VirtualOrder:
        """Persist a refusal.

        Stored as an order so "what did this portfolio try to do, and why could it
        not" remains answerable.
        """
        order = VirtualOrder(
            simulation_profile_id=_require_id(self._profile),
            instrument_id=request.instrument_id,
            trade_decision_id=request.trade_decision_id,
            position_id=request.position_id,
            idempotency_key=request.idempotency_key,
            side=request.side,
            order_type=request.order_type,
            status=OrderStatus.REJECTED,
            quantity=request.quantity,
            requested_at=request.requested_at,
            requested_price=request.reference_price,
            rejection_reason=reason,
            rejection_detail=detail[:500],
        )
        self._session.add(order)
        await self._session.flush()
        logger.info(
            "order rejected", profile=self._profile.name, reason=reason.value, detail=detail
        )
        return order

    async def close_position(
        self,
        *,
        position: VirtualPosition,
        exit_price: Decimal,
        exit_timestamp: datetime,
        exit_reason: object,
        quote: Quote | None,
        idempotency_key: str,
        gapped: bool = False,
        ambiguous: bool = False,
    ) -> tuple[VirtualOrder, VirtualPosition]:
        """Close an open position and settle the cash.

        The exit fill is priced from the *bid* side with slippage, exactly like any
        other sell -- except that ``exit_price`` (a stop or target level) overrides
        the mid, because the level is where the trade happened.

        Idempotent: a repeated call with the same key returns the existing order
        without closing twice. Replaying a candle is therefore safe.
        """
        existing = await self._find_by_key(idempotency_key)
        if existing is not None:
            return existing, position

        pricing = price_fill(
            side=Side.SHORT,
            quantity=position.quantity,
            settings=self._profile.costs.to_cost_settings(),
            quote=None,
            reference_price=exit_price,
        )

        order = VirtualOrder(
            simulation_profile_id=_require_id(self._profile),
            instrument_id=position.instrument_id,
            position_id=position.id,
            idempotency_key=idempotency_key,
            side=Side.SHORT,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            quantity=position.quantity,
            requested_at=exit_timestamp,
            filled_at=exit_timestamp,
            requested_price=exit_price,
            executed_price=pricing.fill_price,
            touch_price=pricing.touch_price,
            fees=pricing.fee,
            spread_cost=pricing.spread_cost,
            slippage_cost=pricing.slippage_cost,
            used_live_quote=quote is not None,
        )
        self._session.add(order)

        # Realised P&L is defined as **cash that actually moved**: what came back
        # on the exit minus what went out on the entry. That is the only
        # definition that cannot double-count, because spread and slippage are
        # already inside the fill prices.
        #
        #   entry outflow = entry_fill x qty + entry_fee
        #   exit inflow   = exit_fill  x qty - exit_fee
        #
        # Gross P&L (the mid-to-mid move) is then derived as realised + costs,
        # which makes `gross - costs == net` true by construction rather than by
        # two independent calculations happening to agree.
        entry_outflow = position.average_entry_price * position.quantity + position.entry_fee
        exit_inflow = pricing.fill_price * position.quantity - pricing.fee
        realized = exit_inflow - entry_outflow
        total_costs = position.entry_costs + pricing.total_cost

        position.status = PositionStatus.CLOSED
        position.exit_price = pricing.fill_price
        position.exit_timestamp = exit_timestamp
        position.exit_reason = exit_reason  # type: ignore[assignment]
        position.exit_was_gap = gapped
        position.exit_was_ambiguous = ambiguous
        position.exit_costs = pricing.total_cost
        position.realized_pnl = realized
        position.unrealized_pnl = ZERO
        position.current_mark_price = pricing.fill_price

        apply_exit(
            self._portfolio,
            cash_delta=pricing.cash_delta,
            fee=pricing.fee,
            spread_cost=pricing.spread_cost,
            slippage_cost=pricing.slippage_cost,
            realized_pnl=realized,
            is_win=realized > 0,
            is_loss=realized < 0,
        )
        await self._session.flush()
        logger.info(
            "position closed",
            profile=self._profile.name,
            reason=str(exit_reason),
            realized=str(realized),
            total_costs=str(total_costs),
        )
        return order, position

    # -- Queries -----------------------------------------------------------

    async def _open_positions(self) -> Sequence[VirtualPosition]:
        stmt = select(VirtualPosition).where(
            VirtualPosition.simulation_profile_id == _require_id(self._profile),
            VirtualPosition.status == PositionStatus.OPEN,
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def _has_open_position(self, instrument_id: int) -> bool:
        stmt = select(VirtualPosition.id).where(
            VirtualPosition.simulation_profile_id == _require_id(self._profile),
            VirtualPosition.instrument_id == instrument_id,
            VirtualPosition.status == PositionStatus.OPEN,
        )
        return (await self._session.execute(stmt)).first() is not None

    async def _find_by_key(self, key: str) -> VirtualOrder | None:
        stmt = select(VirtualOrder).where(VirtualOrder.idempotency_key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _get_order_row(self, order_id: str) -> VirtualOrder:
        row = await self._session.get(VirtualOrder, int(order_id))
        if row is None:
            msg = f"no virtual order with id {order_id}"
            raise KeyError(msg)
        return row


def _quote_is_stale(quote: Quote, now: datetime, max_age_seconds: int) -> bool:
    """Whether a quote is too old to price against.

    A quote from the future is also refused: it means a clock or wiring problem,
    and pricing against it would be look-ahead.
    """
    if quote.timestamp > now:
        return True
    return quote.timestamp < now - timedelta(seconds=max_age_seconds)


def _require_id(profile: SimulationProfileConfig) -> int:
    if profile.id is None:
        msg = f"profile {profile.name!r} must be persisted before trading"
        raise ValueError(msg)
    return profile.id


def _to_order_state(row: VirtualOrder, profile: SimulationProfileConfig) -> OrderState:
    """Adapt a stored order to the protocol's value object."""
    order = Order(
        symbol=str(row.instrument_id),
        side=row.side,
        quantity=row.quantity,
        created_at=row.requested_at,
        reason=f"profile={profile.name}",
    )
    fills: tuple[Fill, ...] = ()
    if row.status is OrderStatus.FILLED and row.executed_price is not None:
        fills = (
            Fill(
                symbol=str(row.instrument_id),
                side=row.side,
                quantity=row.quantity,
                price=row.executed_price,
                timestamp=row.filled_at or row.requested_at,
                fee=row.fees,
                mid_at_fill=row.requested_price,
            ),
        )
    return OrderState(
        order_id=str(row.id),
        order=order,
        status=row.status,
        submitted_at=row.requested_at,
        fills=fills,
        reject_reason=row.rejection_detail or None,
    )
