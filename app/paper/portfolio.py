"""Portfolio accounting and mark-to-market valuation.

The invariant everything here maintains::

    equity = cash + Σ(liquidation value of open positions)

Cash is a stored ledger balance, not a figure derived from replaying orders. That
choice is deliberate: recomputing a balance from an event log is slow and
fragile -- one unhandled event type and the balance silently drifts, with nothing
to compare against. Here, the balance moves inside the same transaction as the
event that moved it, so the two cannot disagree.

Equity and unrealised P&L are *not* stored on the portfolio row: they depend on
current market prices and would be stale the instant they were written. They are
computed on demand and captured into a :class:`PortfolioSnapshot` when a
particular valuation is worth keeping.

Valuation is at the **bid**, not the mid. A holder cannot realise the mid -- they
must sell into the bid -- so mid-marking overstates equity by half a spread per
position, flattering both the equity curve and the drawdown.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.db.models import VirtualPortfolio, VirtualPosition
from app.domain.quotes import Quote
from app.paper.execution import liquidation_value

ZERO = Decimal(0)
MONEY_EXPONENT = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class PortfolioValuation:
    """A point-in-time view of one portfolio.

    Every figure needed to answer "what is this worth, and how did it get here",
    computed together so they cannot disagree with one another.
    """

    timestamp: datetime
    cash: Decimal
    positions_value: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    open_position_count: int
    gross_exposure: Decimal
    net_exposure: Decimal
    peak_equity: Decimal
    drawdown: float
    """Fraction below peak equity, <= 0. Dimensionless, hence float."""
    max_drawdown: float

    @property
    def exposure_ratio(self) -> Decimal:
        return self.gross_exposure / self.equity if self.equity > 0 else ZERO


def value_portfolio(
    *,
    portfolio: VirtualPortfolio,
    positions: Sequence[VirtualPosition],
    quotes: dict[int, Quote],
    marks: dict[int, Decimal],
    timestamp: datetime,
) -> PortfolioValuation:
    """Mark a portfolio to market.

    Args:
        portfolio: the stored ledger row.
        positions: currently open positions.
        quotes: live quotes by instrument id. Preferred, because the bid is the
            price a holder could actually realise.
        marks: fallback prices by instrument id, used when no quote exists.
        timestamp: valuation time.

    Returns:
        A :class:`PortfolioValuation`. Nothing is written -- persistence is the
        caller's job, inside its own transaction.

    Note the valuation excludes exit costs. They belong to an exit that has not
    happened, so equity is optimistic by roughly one exit fee per open position.
    Documented rather than silently patched with an arbitrary reserve.
    """
    positions_value = ZERO
    unrealized = ZERO

    for position in positions:
        quote = quotes.get(position.instrument_id)
        mark = marks.get(position.instrument_id) or position.current_mark_price
        if mark is None and quote is None:
            # No price at all: fall back to the entry price. This values the
            # position at cost rather than dropping it from equity entirely,
            # which would look like a loss that never happened.
            mark = position.average_entry_price
        reference = mark if mark is not None else position.average_entry_price

        value = liquidation_value(quantity=position.quantity, quote=quote, mark_price=reference)
        positions_value += value
        unrealized += value - (position.average_entry_price * position.quantity)

    equity = _q(portfolio.cash + positions_value)
    peak_equity = max(portfolio.peak_equity, equity)
    drawdown = _drawdown(equity=equity, peak_equity=peak_equity)
    max_drawdown = min(portfolio.max_drawdown, drawdown)

    return PortfolioValuation(
        timestamp=timestamp,
        cash=_q(portfolio.cash),
        positions_value=_q(positions_value),
        equity=equity,
        realized_pnl=_q(portfolio.realized_pnl),
        unrealized_pnl=_q(unrealized),
        open_position_count=len(positions),
        # Long-only today, so gross and net exposure coincide. They are reported
        # separately because they diverge the moment shorts exist, and a caller
        # that has only ever seen one number will use the wrong one.
        gross_exposure=_q(positions_value),
        net_exposure=_q(positions_value),
        peak_equity=_q(peak_equity),
        drawdown=drawdown,
        max_drawdown=max_drawdown,
    )


def _drawdown(*, equity: Decimal, peak_equity: Decimal) -> float:
    """Fractional distance below the running peak, as a non-positive number.

    Zero peak equity yields zero drawdown rather than a division error: a
    portfolio that never had value cannot have fallen from it.
    """
    if peak_equity <= 0:
        return 0.0
    return min(0.0, float((equity - peak_equity) / peak_equity))


def apply_entry(
    portfolio: VirtualPortfolio,
    *,
    cash_delta: Decimal,
    fee: Decimal,
    spread_cost: Decimal,
    slippage_cost: Decimal,
) -> None:
    """Apply an entry fill to the ledger.

    ``cash_delta`` is negative for a buy and already includes the fee -- see
    :attr:`~app.paper.execution.FillPricing.cash_delta`, which is the single place
    cash movement is derived. Spread and slippage are *not* subtracted again here:
    they are already inside the fill price. Double-counting them is the easiest
    mistake in this module, so they are accumulated for reporting only.
    """
    portfolio.cash = _q(portfolio.cash + cash_delta)
    portfolio.total_fees = _q(portfolio.total_fees + fee)
    portfolio.total_spread_cost = _q(portfolio.total_spread_cost + spread_cost)
    portfolio.total_slippage_cost = _q(portfolio.total_slippage_cost + slippage_cost)


def apply_exit(
    portfolio: VirtualPortfolio,
    *,
    cash_delta: Decimal,
    fee: Decimal,
    spread_cost: Decimal,
    slippage_cost: Decimal,
    realized_pnl: Decimal,
    is_win: bool,
    is_loss: bool,
) -> None:
    """Apply an exit fill to the ledger and record the trade outcome."""
    portfolio.cash = _q(portfolio.cash + cash_delta)
    portfolio.total_fees = _q(portfolio.total_fees + fee)
    portfolio.total_spread_cost = _q(portfolio.total_spread_cost + spread_cost)
    portfolio.total_slippage_cost = _q(portfolio.total_slippage_cost + slippage_cost)
    portfolio.realized_pnl = _q(portfolio.realized_pnl + realized_pnl)
    portfolio.trade_count += 1
    if is_win:
        portfolio.winning_trades += 1
    elif is_loss:
        portfolio.losing_trades += 1


def record_valuation(portfolio: VirtualPortfolio, valuation: PortfolioValuation) -> None:
    """Persist the path-dependent parts of a valuation onto the ledger row.

    Peak equity and maximum drawdown are path-dependent: they cannot be recovered
    later from a list of closed trades, because they depend on mark-to-market
    movement while positions were open. So they are carried forward here on every
    valuation.
    """
    portfolio.peak_equity = valuation.peak_equity
    portfolio.max_drawdown = valuation.max_drawdown
    portfolio.last_valued_at = valuation.timestamp


def _q(value: Decimal) -> Decimal:
    return value.quantize(MONEY_EXPONENT)
