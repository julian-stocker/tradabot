"""Paper-trading engine.

Drives the lifecycle::

    decision -> sized order -> fill -> position -> bar monitoring -> exit -> trade

Two entry points, matching the two things that happen in a market:

``open_from_decision``
    A signal produced a TRADE decision; try to act on it.
``process_bar``
    A candle closed; mark positions, check exits, snapshot equity.

Timing (the important part)
---------------------------
A signal computed on the close of bar *N* **cannot** execute at bar *N*'s prices.
That close was not observable while the bar was forming, and filling against it is
the single most flattering bug a paper trader can have.

This is enforced structurally, not by convention: :meth:`open_from_decision`
takes both the signal's bar timestamp and the execution timestamp, and raises
:class:`LookAheadError` if execution is not strictly later. There is no argument
combination that fills at or before the signal bar. See docs/simulation-timing.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.errors import TradabotError
from app.core.logging import get_logger
from app.db.models import Instrument, VirtualPortfolio, VirtualPosition, VirtualTrade
from app.domain.enums import (
    CandleAmbiguityPolicy,
    ExitReason,
    OrderRejectionReason,
    Side,
    TradeOutcome,
)
from app.domain.quotes import Quote
from app.market_data.calendars import TradingCalendar, get_trading_calendar
from app.paper.broker import OrderRequest, PaperBroker
from app.paper.execution import liquidation_value
from app.paper.exits import (
    BarPrices,
    derive_stop_and_target,
    evaluate_exit,
    holding_period_expired,
)
from app.paper.portfolio import PortfolioValuation, record_valuation, value_portfolio
from app.paper.repository import PaperTradingRepository
from app.paper.sessions import (
    SessionState,
    daily_loss_breached,
    holding_deadline,
    holding_period_expired_at,
    resolve_session,
)
from app.paper.sizing import size_position
from app.simulation.models import SimulationProfileConfig

logger = get_logger(__name__)

ZERO = Decimal(0)
_DAILY_LOSS_HALT = "max_daily_loss breached for this trading session"

BREAKEVEN_EPSILON = Decimal("0.005")
"""Net P&L within half a cent of zero counts as a breakeven rather than a
microscopic win or loss. Without it, outcome statistics are dominated by rounding."""


class LookAheadError(TradabotError):
    """An execution was attempted at or before the bar that produced the signal.

    Always a bug in the caller, never a market condition -- hence an exception
    rather than a rejection. A paper trader that filled here would report
    performance no live system could reproduce.
    """

    def __init__(self, signal_bar: datetime, execution_at: datetime) -> None:
        super().__init__(
            f"cannot execute at {execution_at.isoformat()} on a signal computed from "
            f"the bar closing at {signal_bar.isoformat()}: execution must be strictly "
            f"later. Filling on the signal bar uses a price that was not observable "
            f"when the decision was made."
        )


@dataclass(frozen=True, slots=True)
class EntryOutcome:
    """Result of attempting to act on a decision."""

    accepted: bool
    order_id: int | None
    position_id: int | None
    rejection: OrderRejectionReason | None
    detail: str


@dataclass(frozen=True, slots=True)
class BarOutcome:
    """Result of processing one candle for one profile."""

    profile_name: str
    positions_marked: int
    positions_closed: int
    valuation: PortfolioValuation
    closed_trades: tuple[VirtualTrade, ...] = ()
    """The trades this bar closed. Carried out rather than merely counted, so a
    caller can notify per portfolio without re-querying and guessing which rows
    are new."""


class PaperTradingEngine:
    """Runs one profile's paper-trading lifecycle.

    Holds no state between calls: everything lives in the database, so a restart
    mid-simulation loses nothing (Part U).
    """

    def __init__(
        self,
        repository: PaperTradingRepository,
        profile: SimulationProfileConfig,
        portfolio: VirtualPortfolio,
        *,
        ambiguity_policy: CandleAmbiguityPolicy = CandleAmbiguityPolicy.CONSERVATIVE,
        calendar: TradingCalendar | None = None,
    ) -> None:
        self._repository = repository
        self._profile = profile
        self._portfolio = portfolio
        self._policy = ambiguity_policy
        # Defaults to NYSE hours. A caller with an instrument in hand should pass
        # that venue's calendar; the default is documented rather than silent.
        self._calendar = calendar or get_trading_calendar("XNYS")
        self._broker = PaperBroker(repository.session, profile, portfolio)

    @property
    def broker(self) -> PaperBroker:
        return self._broker

    @property
    def portfolio(self) -> VirtualPortfolio:
        return self._portfolio

    # -- Entry -------------------------------------------------------------

    async def open_from_decision(
        self,
        *,
        instrument: Instrument,
        trade_decision_id: int,
        signal_id: int | None,
        signal_bar_timestamp: datetime,
        execution_timestamp: datetime,
        execution_price: Decimal,
        quote: Quote | None,
        atr: Decimal | None,
    ) -> EntryOutcome:
        """Attempt to open a position from an accepted trade decision.

        Args:
            instrument: what to trade. Must be tradable at ``execution_timestamp``.
            trade_decision_id: the decision that authorised this. Also the
                idempotency key, so replaying a signal cannot trade twice.
            signal_id: provenance for the position.
            signal_bar_timestamp: close of the bar the signal was computed from.
            execution_timestamp: when the order is placed. **Must be strictly
                after** ``signal_bar_timestamp``.
            execution_price: reference mid at execution. The *next* bar's price,
                never the signal bar's.
            quote: live top-of-book, if available.
            atr: ATR at signal time, for stop placement. ``None`` means no
                risk-based size is possible.

        Raises:
            LookAheadError: execution is not strictly after the signal bar.
        """
        if execution_timestamp <= signal_bar_timestamp:
            raise LookAheadError(signal_bar_timestamp, execution_timestamp)

        key = f"entry:{trade_decision_id}"

        if not instrument.is_tradable_at(execution_timestamp):
            return await self._rejected_entry(
                instrument,
                trade_decision_id,
                key,
                execution_timestamp,
                OrderRejectionReason.INSTRUMENT_NOT_TRADABLE,
                f"{instrument.symbol} was not tradable at {execution_timestamp.isoformat()}",
            )

        valuation = await self.value(timestamp=execution_timestamp, quotes={}, marks={})

        # The expected fill, not the mid: sizing against the mid systematically
        # overshoots by half a spread plus slippage.
        expected_fill = quote.ask if quote is not None else execution_price
        stop_loss, take_profit = derive_stop_and_target(
            entry_price=expected_fill,
            atr=atr,
            stop_loss_atr_multiple=self._profile.risk.stop_loss_atr_multiple,
            take_profit_r_multiple=self._profile.risk.take_profit_r_multiple,
        )

        sizing = size_position(
            profile=self._profile,
            equity=valuation.equity,
            available_cash=self._portfolio.cash,
            current_exposure=valuation.gross_exposure,
            entry_price=expected_fill,
            stop_loss=stop_loss,
        )
        if not sizing.is_tradable:
            return await self._rejected_entry(
                instrument,
                trade_decision_id,
                key,
                execution_timestamp,
                sizing.rejection or OrderRejectionReason.QUANTITY_TOO_SMALL,
                sizing.detail,
            )

        order = await self._broker.submit(
            OrderRequest(
                profile=self._profile,
                instrument_id=instrument.id,
                symbol=instrument.symbol,
                side=Side.LONG,
                quantity=sizing.quantity,
                idempotency_key=key,
                requested_at=execution_timestamp,
                quote=quote,
                reference_price=execution_price,
                trade_decision_id=trade_decision_id,
                signal_id=signal_id,
                stop_loss=stop_loss,
                take_profit=take_profit,
                max_holding_bars=self._profile.risk.max_holding_bars,
            ),
            valuation=valuation,
        )

        if order.rejection_reason is not None:
            return EntryOutcome(
                accepted=False,
                order_id=order.id,
                position_id=None,
                rejection=order.rejection_reason,
                detail=order.rejection_detail,
            )

        return EntryOutcome(
            accepted=True,
            order_id=order.id,
            position_id=order.position_id,
            rejection=None,
            detail=sizing.detail,
        )

    async def _rejected_entry(
        self,
        instrument: Instrument,
        trade_decision_id: int,
        key: str,
        timestamp: datetime,
        reason: OrderRejectionReason,
        detail: str,
    ) -> EntryOutcome:
        """Record a refusal that never reached the broker's own gates."""
        order = await self._broker.reject(
            OrderRequest(
                profile=self._profile,
                instrument_id=instrument.id,
                symbol=instrument.symbol,
                side=Side.LONG,
                quantity=ZERO,
                idempotency_key=key,
                requested_at=timestamp,
                trade_decision_id=trade_decision_id,
            ),
            reason,
            detail,
        )
        return EntryOutcome(
            accepted=False, order_id=order.id, position_id=None, rejection=reason, detail=detail
        )

    # -- Bar processing ----------------------------------------------------

    async def process_bar(
        self,
        *,
        instrument_id: int,
        bar: BarPrices,
        quote: Quote | None = None,
        advance_clock: bool = True,
    ) -> BarOutcome:
        """Mark positions against a candle, run exits, and snapshot equity.

        Idempotent per (position, bar): the exit order's key includes the bar
        timestamp, so replaying a candle cannot close the same position twice.

        Args:
            instrument_id: which instrument the bar belongs to.
            bar: the candle.
            quote: end-of-bar quote, used for bid-based marking.
            advance_clock: whether this bar advances the portfolio's bar counter.
                False when replaying, so holding periods are not double-counted.
        """
        positions = await self._repository.open_positions(
            _profile_id(self._profile), instrument_id=instrument_id
        )

        if advance_clock:
            self._portfolio.bars_processed += 1

        closed_trades: list[VirtualTrade] = []
        for position in positions:
            _update_excursions(position, bar)
            trade = await self._maybe_exit(position, bar, quote)
            if trade is not None:
                closed_trades.append(trade)
            else:
                _mark(position, bar, quote)

        valuation = await self.value(
            timestamp=bar.timestamp,
            quotes={instrument_id: quote} if quote is not None else {},
            marks={instrument_id: bar.close},
        )
        await self._persist_valuation(valuation)
        self._apply_risk_halt(valuation)

        return BarOutcome(
            profile_name=self._profile.name,
            positions_marked=len(positions),
            positions_closed=len(closed_trades),
            valuation=valuation,
            closed_trades=tuple(closed_trades),
        )

    async def _maybe_exit(
        self, position: VirtualPosition, bar: BarPrices, quote: Quote | None
    ) -> VirtualTrade | None:
        """Close ``position`` if this bar triggered an exit.

        Returns the resulting trade, or None if the position stays open. The
        trade rather than a boolean because the caller needs it to notify.
        """
        evaluation = evaluate_exit(
            side=position.side,
            bar=bar,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            policy=self._policy,
        )

        if evaluation.triggered and evaluation.exit_price is not None:
            return await self._close(
                position,
                exit_price=evaluation.exit_price,
                timestamp=bar.timestamp,
                reason=evaluation.reason or ExitReason.STOP_LOSS,
                quote=quote,
                gapped=evaluation.gapped,
                ambiguous=evaluation.ambiguous,
            )

        if self._holding_period_over(position, bar):
            # Time exits fill at the bar's close: the first price available once
            # the holding period is known to have elapsed.
            return await self._close(
                position,
                exit_price=bar.close,
                timestamp=bar.timestamp,
                reason=ExitReason.MAX_HOLDING_PERIOD,
                quote=quote,
            )

        return None

    def _holding_period_over(self, position: VirtualPosition, bar: BarPrices) -> bool:
        """Whether ``position`` has been held long enough to be closed on time.

        Two rules, and the earlier one wins:

        **Bars held** is exact for a contiguous replay and is the only meaningful
        rule intraday, where "a day" is not the unit anyone means.

        **Calendar deadline** covers what the counter cannot. The counter only
        advances on bars the engine actually sees, so a halted instrument, a data
        gap or a symbol that simply did not trade freezes it -- and a position with
        a 10-bar limit can sit open for months while its counter reads 3. Counting
        sessions from the entry timestamp instead makes the limit a fact about the
        market rather than about how much data happened to arrive, and it skips
        weekends and holidays, which is what "10 trading days" always meant.

        Taking whichever fires first is deliberate: a holding limit is a risk
        control, and the failure worth avoiding is a position outliving it.
        """
        risk = self._profile.risk
        bars_held = self._portfolio.bars_processed - position.entry_bar_index
        if holding_period_expired(bars_held=bars_held, max_holding_bars=risk.max_holding_bars):
            return True

        deadline = holding_deadline(
            calendar=self._calendar,
            entry=position.entry_timestamp,
            max_holding_days=risk.max_holding_bars,
        )
        return holding_period_expired_at(
            deadline=deadline, moment=bar.timestamp, calendar=self._calendar
        )

    async def close_position(
        self,
        position: VirtualPosition,
        *,
        exit_price: Decimal,
        timestamp: datetime,
        reason: ExitReason,
        quote: Quote | None = None,
    ) -> VirtualTrade | None:
        """Close a position explicitly -- signal reversal, simulation end, manual."""
        return await self._close(
            position, exit_price=exit_price, timestamp=timestamp, reason=reason, quote=quote
        )

    async def _close(
        self,
        position: VirtualPosition,
        *,
        exit_price: Decimal,
        timestamp: datetime,
        reason: ExitReason,
        quote: Quote | None,
        gapped: bool = False,
        ambiguous: bool = False,
    ) -> VirtualTrade | None:
        """Settle a position and write its trade record."""
        key = f"exit:{position.id}:{timestamp.isoformat()}"
        _order, closed = await self._broker.close_position(
            position=position,
            exit_price=exit_price,
            exit_timestamp=timestamp,
            exit_reason=reason,
            quote=quote,
            idempotency_key=key,
            gapped=gapped,
            ambiguous=ambiguous,
        )
        if closed.exit_price is None or closed.exit_timestamp is None:
            return None
        trade = await _build_trade(self._repository, closed, self._portfolio)
        return await self._repository.record_trade(trade)

    # -- Valuation ---------------------------------------------------------

    async def value(
        self,
        *,
        timestamp: datetime,
        quotes: dict[int, Quote],
        marks: dict[int, Decimal],
    ) -> PortfolioValuation:
        """Mark the portfolio to market without persisting anything."""
        positions = await self._repository.open_positions(_profile_id(self._profile))
        return value_portfolio(
            portfolio=self._portfolio,
            positions=positions,
            quotes=quotes,
            marks=marks,
            timestamp=timestamp,
        )

    async def _persist_valuation(self, valuation: PortfolioValuation) -> None:
        record_valuation(self._portfolio, valuation)
        await self._repository.record_snapshot(
            simulation_profile_id=_profile_id(self._profile), valuation=valuation
        )

    def _apply_risk_halt(self, valuation: PortfolioValuation) -> None:
        """Halt the portfolio if a drawdown or daily-loss limit was breached.

        Both halts are sticky and recorded on the portfolio, so a recovering
        equity curve does not quietly resume trading. Clearing a halt is a
        deliberate act.

        The daily-loss halt is the exception: it clears **by itself at the next
        trading session**, because that is what "daily" means. Everything else
        stays halted until someone looks at it.
        """
        session = self._roll_session(valuation)

        if session.is_new_session and self._portfolio.halted_reason == _DAILY_LOSS_HALT:
            # A new session resets the daily budget, and with it this halt only.
            self._portfolio.halted_reason = None
            logger.info(
                "daily-loss halt cleared at new session",
                profile=self._profile.name,
                session=str(session.session_date),
            )

        if self._portfolio.halted_reason is not None:
            return

        drawdown_limit = self._profile.risk.max_drawdown
        if valuation.drawdown < -float(drawdown_limit):
            self._portfolio.halted_reason = (
                f"max_drawdown breached: {valuation.drawdown:.4f} < -{float(drawdown_limit)}"
            )
            logger.warning(
                "portfolio halted",
                profile=self._profile.name,
                drawdown=str(valuation.drawdown),
            )
            return

        if daily_loss_breached(
            equity=valuation.equity,
            session_start_equity=session.start_equity,
            max_daily_loss=self._profile.risk.max_daily_loss,
        ):
            self._portfolio.halted_reason = _DAILY_LOSS_HALT
            logger.warning(
                "portfolio halted for the session",
                profile=self._profile.name,
                session=str(session.session_date),
                session_start_equity=str(session.start_equity),
                equity=str(valuation.equity),
            )

    def _roll_session(self, valuation: PortfolioValuation) -> SessionState:
        """Advance the portfolio's trading session and persist the new baseline.

        The daily-loss budget is measured against the *session's* opening equity,
        not a UTC calendar day. A US session runs past 20:00 UTC, so a
        midnight-UTC reset would split one trading day across two budgets.
        """
        session = resolve_session(
            calendar=self._calendar,
            moment=valuation.timestamp,
            current_session=self._portfolio.session_date,
            current_start_equity=self._portfolio.session_start_equity,
            equity=valuation.equity,
        )
        if session.is_new_session:
            self._portfolio.session_date = session.session_date
            self._portfolio.session_start_equity = session.start_equity
        return session


def _mark(position: VirtualPosition, bar: BarPrices, quote: Quote | None) -> None:
    """Update a position's mark and unrealised P&L.

    Marked at the **bid** when a quote exists: that is what the position could be
    liquidated at, and marking at the mid overstates equity by half a spread.
    """
    value = liquidation_value(quantity=position.quantity, quote=quote, mark_price=bar.close)
    position.current_mark_price = quote.bid if quote is not None else bar.close
    position.unrealized_pnl = value - (position.average_entry_price * position.quantity)


def _update_excursions(position: VirtualPosition, bar: BarPrices) -> None:
    """Track the best and worst prices seen, for MFE/MAE.

    Updated from the bar's high and low rather than its close, because the
    excursion is about how far the trade went *while open*, not where it happened
    to end each bar.
    """
    high = position.highest_price_seen
    low = position.lowest_price_seen
    position.highest_price_seen = bar.high if high is None else max(high, bar.high)
    position.lowest_price_seen = bar.low if low is None else min(low, bar.low)


async def _build_trade(
    repository: PaperTradingRepository,
    position: VirtualPosition,
    portfolio: VirtualPortfolio,
) -> VirtualTrade:
    """Build the immutable trade record for a closed position.

    Cost components are summed from the position's actual orders rather than
    carried forward as one aggregate. Keeping fees, spread and slippage separate
    all the way to the trade record is the whole point of itemising them: a
    single "costs" number cannot answer "is the spread or the fee killing the
    small portfolio?".
    """
    orders = await repository.orders_for_position(position.id)
    fees = sum((o.fees for o in orders), ZERO)
    spread = sum((o.spread_cost for o in orders), ZERO)
    slippage = sum((o.slippage_cost for o in orders), ZERO)

    exit_price = position.exit_price or ZERO
    entry_notional = position.average_entry_price * position.quantity
    net = position.realized_pnl
    # Derived, not recomputed: gross = net + costs holds exactly by construction.
    gross = net + fees + spread + slippage

    mfe = (
        (position.highest_price_seen - position.average_entry_price) * position.quantity
        if position.highest_price_seen is not None
        else None
    )
    mae = (
        (position.lowest_price_seen - position.average_entry_price) * position.quantity
        if position.lowest_price_seen is not None
        else None
    )

    return VirtualTrade(
        simulation_profile_id=position.simulation_profile_id,
        position_id=position.id,
        instrument_id=position.instrument_id,
        originating_signal_id=position.originating_signal_id,
        side=position.side,
        quantity=position.quantity,
        entry_timestamp=position.entry_timestamp,
        entry_price=position.average_entry_price,
        exit_timestamp=position.exit_timestamp or position.entry_timestamp,
        exit_price=exit_price,
        exit_reason=position.exit_reason or ExitReason.MANUAL,
        holding_bars=max(0, portfolio.bars_processed - position.entry_bar_index),
        gross_pnl=gross,
        total_fees=fees,
        total_spread_cost=spread,
        total_slippage_cost=slippage,
        net_pnl=net,
        net_return=float(net / entry_notional) if entry_notional else 0.0,
        max_favorable_excursion=mfe,
        max_adverse_excursion=mae,
        outcome=classify_outcome(net),
    )


def classify_outcome(net_pnl: Decimal) -> TradeOutcome:
    """Classify a trade on **net** P&L.

    After costs, not before. A trade that made 3 EUR gross and paid 4 EUR in fees
    is a loss, and calling it a win is how a cost-blind system convinces itself it
    is profitable.
    """
    if net_pnl > BREAKEVEN_EPSILON:
        return TradeOutcome.WIN
    if net_pnl < -BREAKEVEN_EPSILON:
        return TradeOutcome.LOSS
    return TradeOutcome.BREAKEVEN


def _profile_id(profile: SimulationProfileConfig) -> int:
    if profile.id is None:
        msg = f"profile {profile.name!r} must be persisted before trading"
        raise ValueError(msg)
    return profile.id
