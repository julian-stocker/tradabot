"""Orchestration: replay, then execute, then measure.

Three passes rather than one loop, because they answer three different questions
and the boundaries between them are the ones phase 5 exists to keep clean:

1. **Replay** writes observations (X) using only what was knowable at each
   instant.
2. **Execution** walks each portfolio forward over those observations and records
   what it would have made (trade outcomes).
3. **Metrics** summarise, separating signal statistics from portfolio statistics.

Labelling (Y) is a fourth, independent pass owned by
:mod:`app.research.service`, and it is independent on purpose: labels mature as
future data arrives, so they cannot be a step inside a run that completes once.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backtesting.engine import BacktestConfig, HistoricalReplay, ReplayStats
from app.backtesting.execution import (
    PortfolioState,
    simulate_entry,
    to_trade_outcome,
)
from app.backtesting.feed import HistoricalDataFeed
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import Instrument, SignalEvaluation
from app.db.session import session_scope
from app.market_data.calendars import get_trading_calendar
from app.research.quality import classify_spread
from app.research.repository import TradeOutcomeRepository
from app.scanner.enums import SessionPhase
from app.simulation.models import SimulationProfileConfig
from app.simulation.repository import SimulationProfileRepository

logger = get_logger(__name__)

EXIT_WINDOW_BARS = 200
"""How far forward the exit walk may look for a stop, target or holding limit."""


@dataclass(slots=True)
class PortfolioResult:
    """One portfolio's outcome over a backtest."""

    profile_key: str
    initial_capital: Decimal
    ending_equity: Decimal
    attempted: int = 0
    executed: int = 0
    rejected: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: Decimal = Decimal(0)
    total_costs: Decimal = Decimal(0)
    net_pnl: Decimal = Decimal(0)
    max_drawdown: Decimal = Decimal(0)
    holding_seconds: list[float] = field(default_factory=list)
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    ruined: bool = False
    """Equity reached zero: the account was wiped out, not merely unprofitable."""

    @property
    def win_rate(self) -> float | None:
        closed = self.wins + self.losses
        return self.wins / closed if closed else None

    @property
    def net_return(self) -> float:
        return float(self.net_pnl / self.initial_capital) if self.initial_capital else 0.0

    @property
    def average_holding_hours(self) -> float | None:
        if not self.holding_seconds:
            return None
        return sum(self.holding_seconds) / len(self.holding_seconds) / 3600.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile_key,
            "initial_capital": float(self.initial_capital),
            "ending_equity": float(self.ending_equity),
            "attempted": self.attempted,
            "executed": self.executed,
            "rejected": self.rejected,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "gross_pnl": float(self.gross_pnl),
            "total_costs": float(self.total_costs),
            "net_pnl": float(self.net_pnl),
            "net_return": self.net_return,
            "max_drawdown": float(self.max_drawdown),
            "average_holding_hours": self.average_holding_hours,
            "ruined": self.ruined,
            "rejection_reasons": dict(sorted(self.rejection_reasons.items())),
        }


async def run_backtest(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    config: BacktestConfig,
    *,
    simulate_portfolios: bool = True,
) -> tuple[int, ReplayStats, list[PortfolioResult]]:
    """Replay, then optionally execute against every enabled portfolio."""
    replay = HistoricalReplay(factory, settings)
    run, stats = await replay.run(config)

    results: list[PortfolioResult] = []
    if simulate_portfolios:
        started = time.perf_counter()
        results = await simulate_all_portfolios(factory, settings, run_id=run.id, config=config)
        logger.info(
            "portfolio simulation complete",
            run_id=run.id,
            portfolios=len(results),
            seconds=round(time.perf_counter() - started, 1),
        )

    return run.id, stats, results


async def simulate_all_portfolios(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    run_id: int,
    config: BacktestConfig,
) -> list[PortfolioResult]:
    """Walk each portfolio independently over the run's qualified observations.

    Independently is the point (part W). The same signal is offered to 100, 1000
    and 10,000 EUR, and each accepts or rejects it on its own terms -- a flat
    order fee that is negligible at 10,000 can make the identical trade
    unprofitable at 100 before the market moves at all.
    """
    calendar = get_trading_calendar(settings.market_data.default_exchange)

    async with session_scope(factory) as session:
        profiles = await SimulationProfileRepository(session).list_profiles(enabled_only=True)
        observations = await _qualified_observations(session, run_id=run_id)
        instruments = await _instrument_map(session, observations)

    results: list[PortfolioResult] = []
    for profile in profiles:
        result = await _simulate_one(
            factory,
            profile=profile,
            observations=observations,
            instruments=instruments,
            run_id=run_id,
            config=config,
            calendar=calendar,
        )
        results.append(result)
    return results


async def _simulate_one(
    factory: async_sessionmaker[AsyncSession],
    *,
    profile: SimulationProfileConfig,
    observations: list[SignalEvaluation],
    instruments: dict[int, Instrument],
    run_id: int,
    config: BacktestConfig,
    calendar: Any,
) -> PortfolioResult:
    state = PortfolioState.start(profile)
    result = PortfolioResult(
        profile_key=profile.name,
        initial_capital=profile.initial_capital,
        ending_equity=profile.initial_capital,
    )

    # Positions the portfolio is holding, as (exit_timestamp, net_pnl). A signal
    # is only offered capacity once the trades that were open at its instant have
    # actually closed -- without this the book is always empty, the concurrent
    # -position limit never binds, and every signal looks affordable.
    open_book: list[tuple[datetime, Decimal]] = []

    async with session_scope(factory) as session:
        # The point-in-time feed, used here for its *forward* primitive only.
        # Feature computation never touches it -- that path goes through
        # FeatureService, which is bounded by `as_of`.
        feed = HistoricalDataFeed(
            session,
            instrument_ids={row.symbol: row.id for row in instruments.values()},
        )
        outcomes = TradeOutcomeRepository(session)

        for evaluation in observations:
            instrument = instruments.get(evaluation.instrument_id)
            if instrument is None:
                continue

            # Settle everything that closed before this instant, then measure
            # capacity. Realising P&L on exit rather than on entry is what makes
            # the equity curve a sequence that could actually have happened.
            open_book = _settle_due(state, open_book, until=evaluation.evaluated_at)
            state.open_positions = len(open_book)

            entry_bar, future_bars = await feed.execution_window(
                instrument_id=evaluation.instrument_id,
                timeframe=config.primary_timeframe,
                after=evaluation.evaluated_at,
                bars=EXIT_WINDOW_BARS,
            )
            assessment = classify_spread(
                spread_bps=evaluation.spread_bps,
                observed_at=evaluation.evaluated_at,
                quote_age_seconds=evaluation.quote_age_seconds,
                calendar=calendar,
            )
            trade = simulate_entry(
                evaluation=evaluation,
                state=state,
                entry_bar=entry_bar,
                future_bars=future_bars,
                session=SessionPhase(evaluation.session_phase),
            )

            result.attempted += 1
            if trade.executed:
                result.executed += 1
                result.gross_pnl += trade.gross_pnl
                result.net_pnl += trade.net_pnl
                result.total_costs += trade.fees + trade.spread_cost + trade.slippage_cost
                if trade.net_pnl > 0:
                    result.wins += 1
                elif trade.net_pnl < 0:
                    result.losses += 1
                if trade.holding_period_seconds is not None:
                    result.holding_seconds.append(trade.holding_period_seconds)
                if trade.exit_timestamp is not None:
                    open_book.append((trade.exit_timestamp, trade.net_pnl))
                else:  # pragma: no cover -- an executed trade always has an exit
                    state.cash += trade.net_pnl
                    state.equity += trade.net_pnl
            else:
                result.rejected += 1
                reason = trade.rejection_reason or "UNKNOWN"
                result.rejection_reasons[reason] = result.rejection_reasons.get(reason, 0) + 1

            await outcomes.upsert(
                to_trade_outcome(
                    trade=trade,
                    evaluation=evaluation,
                    profile=profile,
                    profile_id=_profile_id(profile),
                    backtest_run_id=run_id,
                    session=SessionPhase(evaluation.session_phase),
                    spread_quality=assessment.quality.value,
                )
            )

    # Floor terminal equity at zero. A cash account cannot owe money, so a
    # slightly negative figure is a modelling artefact rather than a result; the
    # solvency guard in `simulate_entry` stops new trades once it is reached.
    # Settle whatever was still open when the window ended. Leaving these
    # unrealised would report an equity curve that stops mid-trade and quietly
    # omits the open positions' P&L from the final number.
    _settle_due(state, open_book, until=None)

    result.ending_equity = max(state.equity, Decimal(0))
    result.max_drawdown = state.max_drawdown
    result.ruined = state.equity <= Decimal(0)
    return result


def _settle_due(
    state: PortfolioState,
    book: list[tuple[datetime, Decimal]],
    *,
    until: datetime | None,
) -> list[tuple[datetime, Decimal]]:
    """Realise every position that closed at or before ``until``.

    ``until=None`` settles the whole book, which is what happens when the window
    ends: leaving positions unrealised would report a final equity that quietly
    omits them.
    """
    remaining: list[tuple[datetime, Decimal]] = []
    for exit_at, pnl in sorted(book):
        if until is None or exit_at <= until:
            state.cash += pnl
            state.equity += pnl
            state.record_equity(exit_at)
        else:
            remaining.append((exit_at, pnl))
    return remaining


async def _qualified_observations(session: AsyncSession, *, run_id: int) -> list[SignalEvaluation]:
    """The run's qualified observations, oldest first.

    Chronological order is required, not cosmetic: a portfolio's capital at the
    time of a signal depends on every trade before it, so replaying them out of
    order would produce an equity curve that could not have happened.
    """
    stmt = (
        select(SignalEvaluation)
        .where(
            SignalEvaluation.backtest_run_id == run_id,
            SignalEvaluation.qualified.is_(True),
        )
        .order_by(SignalEvaluation.evaluated_at, SignalEvaluation.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _instrument_map(
    session: AsyncSession, observations: list[SignalEvaluation]
) -> dict[int, Instrument]:
    ids = {observation.instrument_id for observation in observations}
    if not ids:
        return {}
    stmt = select(Instrument).where(Instrument.id.in_(ids))
    return {row.id: row for row in (await session.execute(stmt)).scalars().all()}


def _profile_id(profile: SimulationProfileConfig) -> int:
    if profile.id is None:  # pragma: no cover -- persisted profiles always have one
        msg = f"profile {profile.name} has no id; it must be persisted before simulation"
        raise ValueError(msg)
    return profile.id
