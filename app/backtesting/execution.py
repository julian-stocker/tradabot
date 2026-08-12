"""What each portfolio would actually have made. **This is the trade outcome.**

Separate from :mod:`app.research.labels` because the two answer different
questions and routinely disagree. A signal the market vindicated by 40 bps is a
loss in a 100 EUR portfolio whose round trip costs 2 EUR on a 30 EUR position --
667 bps of friction. Reporting only the market outcome would call that signal
good; reporting only the trade outcome would hide that the *signal* was right and
the *account size* was wrong. Phase 5 keeps both.

Sequential, not independent
---------------------------
Each portfolio is walked forward in time with real state: cash, open positions,
concurrent-position limits. Sizing every signal against the starting capital in
isolation would ignore the two constraints that actually bind a small account --
it cannot hold five positions at once, and a loss shrinks the next position --
and would produce an equity curve that no sequence of trades could have produced.

Reused, not reimplemented
-------------------------
``derive_stop_and_target``, ``size_position`` and ``evaluate_exit`` are the
production functions, imported directly. A separate backtest copy of the risk
rules would drift, and then the backtest would be measuring the copy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from app.core.logging import get_logger
from app.core.time import utc_now
from app.db.models import Candle, SignalEvaluation, TradeOutcome
from app.domain.enums import (
    CandleAmbiguityPolicy,
    CostBasis,
    ExitReason,
    OrderRejectionReason,
    Side,
)
from app.paper.exits import BarPrices, derive_stop_and_target, evaluate_exit
from app.paper.sizing import size_position
from app.research.costs import COST_MODEL_VERSION, historical_round_trip
from app.research.horizons import LABEL_POLICY_VERSION
from app.scanner.enums import SessionPhase
from app.simulation.models import SimulationProfileConfig

logger = get_logger(__name__)

ZERO: Final = Decimal(0)


@dataclass(slots=True)
class PortfolioState:
    """A portfolio being walked forward through history."""

    profile: SimulationProfileConfig
    cash: Decimal
    equity: Decimal
    peak_equity: Decimal
    open_positions: int = 0
    trades: list[TradeOutcome] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    max_drawdown: Decimal = ZERO

    @classmethod
    def start(cls, profile: SimulationProfileConfig) -> PortfolioState:
        capital = profile.initial_capital
        return cls(profile=profile, cash=capital, equity=capital, peak_equity=capital)

    def record_equity(self, moment: datetime) -> None:
        self.equity_curve.append((moment, self.equity))
        self.peak_equity = max(self.peak_equity, self.equity)
        if self.peak_equity > 0:
            drawdown = (self.equity - self.peak_equity) / self.peak_equity
            self.max_drawdown = min(self.max_drawdown, drawdown)


@dataclass(frozen=True, slots=True)
class SimulatedTrade:
    """One completed (or rejected) execution attempt."""

    executed: bool
    rejection_reason: str | None = None
    entry_timestamp: datetime | None = None
    entry_price: Decimal | None = None
    exit_timestamp: datetime | None = None
    exit_price: Decimal | None = None
    quantity: Decimal | None = None
    exit_reason: ExitReason | None = None
    gross_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    spread_cost: Decimal = ZERO
    slippage_cost: Decimal = ZERO
    net_pnl: Decimal = ZERO
    net_return: float | None = None
    holding_period_seconds: float | None = None
    modelled_spread_bps: float | None = None
    ambiguous_exit: bool = False


def simulate_entry(
    *,
    evaluation: SignalEvaluation,
    state: PortfolioState,
    entry_bar: Candle | None,
    future_bars: list[Candle],
    session: SessionPhase,
    ambiguity_policy: CandleAmbiguityPolicy = CandleAmbiguityPolicy.CONSERVATIVE,
) -> SimulatedTrade:
    """Attempt one entry and follow it to its exit.

    ``entry_bar`` is the first bar **after** the signal instant, and the fill is
    its open. This is the execution convention in one line: a signal computed
    from a bar that closed at T could not have been acted on before T, so the
    earliest honest fill is the next bar's open. Filling at the signal bar's own
    close would book a trade at a price that was only knowable at the moment the
    opportunity ended.

    A rejection is a result, not a failure -- "insufficient capital" is precisely
    the finding a 100 EUR portfolio is there to produce.
    """
    if entry_bar is None:
        return SimulatedTrade(executed=False, rejection_reason="NO_EXECUTABLE_BAR")

    if state.equity <= ZERO:
        # **Ruin is terminal.** An unleveraged cash account cannot trade its way
        # out of zero, and a simulator that keeps going produces negative equity
        # -- which is not a bad result, it is an impossible one. The first
        # 100 EUR benchmark ran past insolvency and finished at -4.02 EUR,
        # because a flat exit fee was still being charged against cash that no
        # longer existed.
        return SimulatedTrade(executed=False, rejection_reason="ACCOUNT_RUINED")

    if state.open_positions >= state.profile.risk.max_open_positions:
        return SimulatedTrade(
            executed=False, rejection_reason=OrderRejectionReason.MAX_OPEN_POSITIONS.value
        )

    reference = entry_bar.open
    atr = _atr_from(evaluation, reference)
    stop_loss, take_profit = derive_stop_and_target(
        entry_price=reference,
        atr=atr,
        stop_loss_atr_multiple=state.profile.risk.stop_loss_atr_multiple,
        take_profit_r_multiple=state.profile.risk.take_profit_r_multiple,
    )

    sizing = size_position(
        profile=state.profile,
        equity=state.equity,
        available_cash=state.cash,
        current_exposure=ZERO,
        entry_price=reference,
        stop_loss=stop_loss,
    )
    if not sizing.is_tradable:
        reason = sizing.rejection or OrderRejectionReason.QUANTITY_TOO_SMALL
        return SimulatedTrade(executed=False, rejection_reason=reason.value)

    exit_bar, exit_price, exit_reason, ambiguous = _walk_to_exit(
        bars=future_bars,
        stop_loss=stop_loss,
        take_profit=take_profit,
        max_holding_bars=state.profile.risk.max_holding_bars,
        policy=ambiguity_policy,
    )
    if exit_bar is None or exit_price is None:
        return SimulatedTrade(executed=False, rejection_reason="NO_EXIT_WITHIN_WINDOW")

    cost, spread = historical_round_trip(
        entry_mid=reference,
        exit_mid=exit_price,
        quantity=sizing.quantity,
        settings=state.profile.costs.to_cost_settings(),
        volatility=_metric(evaluation.volatility_metrics, "volatility"),
        relative_volume=_metric(evaluation.volume_metrics, "relative_volume"),
        session=session,
        side=Side.LONG,
    )

    notional = reference * sizing.quantity
    net_return = float(cost.net_pnl / notional) if notional > 0 else None

    return SimulatedTrade(
        executed=True,
        entry_timestamp=entry_bar.timestamp,
        entry_price=reference,
        exit_timestamp=exit_bar.timestamp,
        exit_price=exit_price,
        quantity=sizing.quantity,
        exit_reason=exit_reason,
        gross_pnl=cost.gross_pnl,
        fees=cost.breakdown.fee_cost,
        spread_cost=cost.breakdown.spread_cost,
        slippage_cost=cost.breakdown.slippage_cost,
        net_pnl=cost.net_pnl,
        net_return=net_return,
        holding_period_seconds=(exit_bar.timestamp - entry_bar.timestamp).total_seconds(),
        modelled_spread_bps=spread.spread_bps,
        ambiguous_exit=ambiguous,
    )


def _walk_to_exit(
    *,
    bars: list[Candle],
    stop_loss: Decimal | None,
    take_profit: Decimal | None,
    max_holding_bars: int | None,
    policy: CandleAmbiguityPolicy,
) -> tuple[Candle | None, Decimal | None, ExitReason | None, bool]:
    """Follow the position bar by bar until something closes it.

    Uses the production :func:`~app.paper.exits.evaluate_exit`, so gaps, intrabar
    touches and same-bar ambiguity are resolved by exactly the rules the live
    paper broker applies -- including the conservative assumption that the stop
    came first when a single candle spanned both levels.
    """
    if not bars:
        return None, None, None, False

    for index, bar in enumerate(bars, start=1):
        evaluation = evaluate_exit(
            side=Side.LONG,
            bar=BarPrices(
                timestamp=bar.timestamp,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
            ),
            stop_loss=stop_loss,
            take_profit=take_profit,
            policy=policy,
        )
        if evaluation.triggered and evaluation.exit_price is not None:
            return bar, evaluation.exit_price, evaluation.reason, evaluation.ambiguous
        if max_holding_bars is not None and index >= max_holding_bars:
            return bar, bar.close, ExitReason.MAX_HOLDING_PERIOD, False

    # The window ended with the position still open: close at the last available
    # price rather than discarding the trade, and label it so the reason is not
    # mistaken for a rule-based exit.
    last = bars[-1]
    return last, last.close, ExitReason.MAX_HOLDING_PERIOD, False


def _atr_from(evaluation: SignalEvaluation, price: Decimal) -> Decimal | None:
    """Recover an absolute ATR from the stored percentage.

    The evaluation records ``atr_pct``; sizing needs it in price units. Returns
    ``None`` when absent, which propagates to a rejection rather than to an
    invented stop distance -- the stop is the sizing denominator, and guessing it
    would produce arbitrary position sizes.
    """
    atr_pct = _metric(evaluation.volatility_metrics, "atr_pct")
    if atr_pct is None or atr_pct <= 0:
        return None
    return price * Decimal(str(atr_pct)) / Decimal(100)


def _metric(payload: dict[str, Any] | None, key: str) -> float | None:
    if not payload:
        return None
    value = payload.get(key)
    return float(value) if isinstance(value, int | float) else None


def to_trade_outcome(
    *,
    trade: SimulatedTrade,
    evaluation: SignalEvaluation,
    profile: SimulationProfileConfig,
    profile_id: int,
    backtest_run_id: int | None,
    session: SessionPhase,
    spread_quality: str,
) -> TradeOutcome:
    """Persist one execution attempt, cost provenance included.

    ``cost_basis`` is always ``MODELLED``: this database holds no historical
    quotes, so no backtested cost here was ever observed. Recording it on every
    row is what stops a later report from presenting a modelled spread as a
    measured one.
    """
    return TradeOutcome(
        evaluation_id=evaluation.id,
        backtest_run_id=backtest_run_id,
        simulation_profile_id=profile_id,
        profile_key=profile.name,
        executed=trade.executed,
        rejection_reason=trade.rejection_reason,
        entry_timestamp=trade.entry_timestamp,
        entry_price=trade.entry_price,
        exit_timestamp=trade.exit_timestamp,
        exit_price=trade.exit_price,
        quantity=trade.quantity,
        exit_reason=trade.exit_reason.value if trade.exit_reason else None,
        gross_pnl=trade.gross_pnl if trade.executed else None,
        fees=trade.fees if trade.executed else None,
        spread_cost=trade.spread_cost if trade.executed else None,
        slippage_cost=trade.slippage_cost if trade.executed else None,
        net_pnl=trade.net_pnl if trade.executed else None,
        net_return=trade.net_return,
        holding_period_seconds=trade.holding_period_seconds,
        modelled_spread_bps=trade.modelled_spread_bps,
        cost_basis=CostBasis.MODELLED.value,
        spread_quality=spread_quality,
        session_phase=session.value,
        cost_model_version=COST_MODEL_VERSION,
        label_policy_version=LABEL_POLICY_VERSION,
        computed_at=utc_now(),
    )
