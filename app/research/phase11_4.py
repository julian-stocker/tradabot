"""Phase 11.4 — true chronological portfolio replay.

What phase 11.3 could not answer
--------------------------------
The 11.3 replay walked each candidate's position forward independently. It
tracked cash, but it passed ``current_exposure=0`` into every sizing call and
never marked open positions against a shared clock. So it could not answer the
questions that only exist once a portfolio is a portfolio: how much was already
invested when this candidate arrived, how many positions were already open, and
whether the cash freed by an exit was available to the next entry.

This module drives the **real engine** instead. ``PaperTradingEngine`` opens and
closes every position, ``PaperBroker`` gates every order, and
``app.paper.portfolio`` is the only ledger. Nothing here re-implements cash,
equity or exposure -- and a test asserts this module defines no such function.

The event loop
--------------
Every distinct bar timestamp, in order::

    process_bar for each instrument holding an open position
        -> marks, exits, cash released, equity/exposure recomputed
    then, candidates at this timestamp, in deterministic order
        -> risk gate -> canonical sizing -> broker gates -> fill

Exits run before entries at the same instant on purpose: an exit releases cash
and a position slot, and a replay that ordered these the other way would refuse
entries the live engine would have allowed. Only instruments *holding a
position* are stepped, because ``process_bar`` marks and exits positions and an
instrument without one has nothing to mark.

No look-ahead: entries fill at the bar **after** the signal bar, enforced by
``PaperTradingEngine`` itself, which raises rather than accepting an earlier
execution timestamp.

The database
------------
A **temporary** SQLite file, created per run and deleted after. The production
database is opened read-only for candidates and bars and is never written to.

Nothing here is a strategy claim
--------------------------------
signal-v1 is directionally unvalidated. Ending equity, drawdown and P&L in this
module are **descriptive arithmetic**, not evidence that any configuration is
worth running.
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from app.db.models import Instrument, VirtualPortfolio
from app.domain.enums import PositionStatus
from app.market_data.risk import ShortHorizonRisk
from app.paper.engine import PaperTradingEngine
from app.paper.exits import BarPrices
from app.paper.portfolio import value_portfolio
from app.paper.repository import PaperTradingRepository
from app.paper.sizing import ExecutionFractionality
from app.research.phase11_3 import Candidate, _risk_at, load_bars
from app.simulation.models import BrokerCostConfig, RiskConfig, SimulationProfileConfig

# ---------------------------------------------------------------------------
# Pre-registered classification. Fixed BEFORE any 11.4 run was executed.
# ---------------------------------------------------------------------------
COST_BURDEN_LOW: Final = Decimal("0.05")
COST_BURDEN_ACCEPTABLE: Final = Decimal("0.15")
COST_BURDEN_HIGH: Final = Decimal("0.40")
"""Execution cost as a fraction of **starting capital**, over the replay window.

Thresholds set in advance, and the window is fixed at roughly 17.5 months, so
these are annualised-ish burdens rather than per-trade ones:

* under 5% -- friction is a rounding error against any plausible edge
* 5-15% -- material, survivable with a real edge
* 15-40% -- the edge must be large before costs to survive
* over 40% -- nearly half the account paid to the broker in under two years

The 40% line is where the account is consumed by frictions regardless of
whether the signal is any good, which is the definition this phase needs.
"""

MIN_ENTRIES_FOR_PRACTICAL: Final = 30
"""Below this the account is not doing the thing being evaluated.

An account that manages four entries in seventeen months is not "active trading
with a low cost burden", it is an account that could not participate. Set in
advance so a low cost burden achieved by never trading cannot read as success.
"""


class CostBurden(StrEnum):
    LOW = "LOW_COST_BURDEN"
    ACCEPTABLE = "ACCEPTABLE_COST_BURDEN"
    HIGH = "HIGH_COST_BURDEN"
    UNVIABLE = "ECONOMICALLY_UNVIABLE"


class Viability(StrEnum):
    PRACTICAL = "PRACTICAL"
    LIMITED = "LIMITED"
    IMPRACTICAL = "IMPRACTICAL"
    UNVIABLE = "ECONOMICALLY_UNVIABLE"


def classify_cost_burden(
    *, execution_cost: Decimal, starting_capital: Decimal, gross_result: Decimal
) -> CostBurden:
    """Apply the pre-registered thresholds. **No result was seen before these.**

    The second clause -- cost exceeding the absolute gross trading result -- is a
    separate route to ``ECONOMICALLY_UNVIABLE``: when frictions are larger than
    everything the trading did, the account cannot be profitable at any realistic
    edge, whatever the ratio to capital says.
    """
    if starting_capital <= 0:
        return CostBurden.UNVIABLE
    share = execution_cost / starting_capital
    if execution_cost > abs(gross_result) and execution_cost > 0:
        return CostBurden.UNVIABLE
    if share < COST_BURDEN_LOW:
        return CostBurden.LOW
    if share < COST_BURDEN_ACCEPTABLE:
        return CostBurden.ACCEPTABLE
    if share < COST_BURDEN_HIGH:
        return CostBurden.HIGH
    return CostBurden.UNVIABLE


def classify_viability(*, burden: CostBurden, entries: int, entries_any_budget: int) -> Viability:
    """Pre-registered. Trading volume and cost burden together, never separately.

    ``entries_any_budget`` is the best entry count the account achieved across
    every predefined risk budget, so an account that simply cannot participate at
    any setting is separated from one that merely participates badly at this one.
    """
    if entries_any_budget == 0:
        return Viability.UNVIABLE
    if burden is CostBurden.UNVIABLE:
        return Viability.UNVIABLE
    if entries == 0:
        return Viability.IMPRACTICAL
    if burden is CostBurden.HIGH or entries < MIN_ENTRIES_FOR_PRACTICAL:
        return Viability.LIMITED
    return Viability.PRACTICAL


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PortfolioRun:
    """One replay: a profile, an execution mode, and the layer on or off."""

    profile: SimulationProfileConfig
    fractionality: ExecutionFractionality
    risk_layer_enabled: bool

    @property
    def label(self) -> str:
        mode = "whole" if self.fractionality is ExecutionFractionality.WHOLE_SHARES_ONLY else "frac"
        layer = "risk" if self.risk_layer_enabled else "base"
        return f"{self.profile.name}/{mode}/{layer}"


def controlled_profile(capital: Decimal, risk_per_trade: Decimal) -> SimulationProfileConfig:
    """Experiment 1's frozen configuration: identical at every capital size.

    Only ``initial_capital`` varies, which is the entire point -- any difference
    between the three runs is attributable to capital and nothing else.
    """
    return SimulationProfileConfig(
        id=0,
        name=f"controlled-{int(capital)}-{risk_per_trade}",
        initial_capital=capital,
        currency="EUR",
        risk=RiskConfig(
            name="controlled",
            risk_per_trade=risk_per_trade,
            max_position_percent=Decimal("0.30"),
            max_total_exposure=Decimal("1.0"),
            max_open_positions=5,
            max_daily_loss=Decimal("1.0"),
            max_drawdown=Decimal("1.0"),
            min_signal_score=75.0,
            min_confidence=0.5,
            stop_loss_atr_multiple=Decimal("2.0"),
            take_profit_r_multiple=Decimal("2.5"),
            max_holding_bars=15,
            require_stop_loss=True,
        ),
        costs=BrokerCostConfig(
            name="controlled",
            order_fee=Decimal("1.00"),
            variable_fee_rate=Decimal("0"),
            slippage_spread_multiple=Decimal("0.5"),
            default_spread_bps=Decimal("10"),
            min_order_notional=Decimal("0"),
        ),
    )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class EquityPoint:
    timestamp: datetime
    equity: Decimal
    cash: Decimal
    exposure: Decimal
    open_positions: int


@dataclass(slots=True)
class RunResult:
    """Everything one replay produced. Counts and costs first; P&L is last."""

    label: str
    starting_capital: Decimal
    candidates: int = 0
    entries: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    floor_bound_entries: int = 0
    curve: list[EquityPoint] = field(default_factory=list)

    ending_equity: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    spread_cost: Decimal = Decimal(0)
    slippage_cost: Decimal = Decimal(0)
    trade_count: int = 0
    closed_trades: list[dict[str, object]] = field(default_factory=list)

    @property
    def execution_cost(self) -> Decimal:
        return self.fees + self.spread_cost + self.slippage_cost

    @property
    def gross_result(self) -> Decimal:
        """Realised P&L before execution costs.

        Realised P&L is already net of costs -- ``price_fill`` puts spread and
        slippage inside the fill price and subtracts the fee -- so gross is the
        net figure with costs added back, not a separately computed number.
        """
        return self.realized_pnl + self.execution_cost

    @property
    def peak_equity(self) -> Decimal:
        return max((p.equity for p in self.curve), default=self.starting_capital)

    @property
    def max_drawdown_pct(self) -> float:
        """True chronological drawdown: mark-to-market, not a sum of trade returns."""
        peak = self.starting_capital
        worst = 0.0
        for point in self.curve:
            peak = max(peak, point.equity)
            if peak > 0:
                worst = min(worst, float((point.equity - peak) / peak))
        return worst

    @property
    def max_drawdown_eur(self) -> Decimal:
        peak = self.starting_capital
        worst = Decimal(0)
        for point in self.curve:
            peak = max(peak, point.equity)
            worst = min(worst, point.equity - peak)
        return worst

    @property
    def longest_drawdown_bars(self) -> int:
        """Longest run of consecutive marks strictly below the running peak."""
        peak = self.starting_capital
        longest = current = 0
        for point in self.curve:
            if point.equity >= peak:
                peak, current = point.equity, 0
            else:
                current += 1
                longest = max(longest, current)
        return longest

    @property
    def max_concurrent_positions(self) -> int:
        return max((p.open_positions for p in self.curve), default=0)

    @property
    def avg_concurrent_positions(self) -> float:
        return sum(p.open_positions for p in self.curve) / len(self.curve) if self.curve else 0.0

    @property
    def max_exposure(self) -> Decimal:
        return max((p.exposure for p in self.curve), default=Decimal(0))

    @property
    def min_cash(self) -> Decimal:
        return min((p.cash for p in self.curve), default=self.starting_capital)

    @property
    def avg_cash_utilisation(self) -> float:
        """Mean fraction of equity that was invested rather than idle."""
        usable = [p for p in self.curve if p.equity > 0]
        if not usable:
            return 0.0
        return sum(float(p.exposure / p.equity) for p in usable) / len(usable)

    @property
    def fully_allocated_fraction(self) -> float:
        """Share of marks at the maximum position count. Time spent unable to act."""
        if not self.curve:
            return 0.0
        cap = self.max_concurrent_positions
        if cap == 0:
            return 0.0
        return sum(1 for p in self.curve if p.open_positions >= cap) / len(self.curve)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
def order_simultaneous(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Deterministic order for candidates sharing a timestamp.

    Highest score first, ties broken by symbol. The engine defines no ordering of
    its own -- it processes whatever it is handed -- so one is defined here
    rather than inherited from SQLite's row order, which is not a specification
    and would make the replay unreproducible.

    Score-first matters once a portfolio is full: whichever candidate is offered
    first takes the last slot, so an arbitrary order silently randomises which
    trades the experiment contains.
    """
    return sorted(candidates, key=lambda c: (-c.score, c.symbol))


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------
class PortfolioReplay:
    """Drives one :class:`PaperTradingEngine` chronologically.

    Owns no accounting of its own. Cash, equity, exposure, position count and
    drawdown all come from ``app.paper.portfolio`` via the engine; this class
    only decides *when* to call it and records what it said.
    """

    def __init__(
        self,
        *,
        engine: PaperTradingEngine,
        repository: PaperTradingRepository,
        portfolio: VirtualPortfolio,
        instruments: dict[int, Instrument],
        bars: dict[int, list[BarPrices]],
        run: PortfolioRun,
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._portfolio = portfolio
        self._instruments = instruments
        self._bars = bars
        self._run = run
        self._timestamps: dict[int, list[datetime]] = {
            key: [b.timestamp for b in series] for key, series in bars.items()
        }

    async def run(self, candidates: Sequence[Candidate]) -> RunResult:
        result = RunResult(
            label=self._run.label,
            starting_capital=self._run.profile.initial_capital,
            candidates=len(candidates),
        )

        by_time: dict[datetime, list[Candidate]] = {}
        for candidate in candidates:
            by_time.setdefault(candidate.signal_bar, []).append(candidate)

        # The clock starts at the first candidate, not at the first stored bar.
        # Bars back to 2020 are kept in ``self._bars`` because volatility-v1
        # needs the trailing window to rank against -- but stepping the portfolio
        # through five years in which nothing can trade would dilute every
        # time-weighted average (concurrency, utilisation, drawdown duration) by
        # the ratio of empty history to replay window.
        start = min((c.signal_bar for c in candidates), default=None)
        clock = sorted(
            {
                t
                for series in self._timestamps.values()
                for t in series
                if start is None or t >= start
            }
        )
        pending: dict[datetime, list[tuple[Candidate, int]]] = {}
        for signal_bar, group in by_time.items():
            for candidate in order_simultaneous(group):
                series = self._timestamps.get(candidate.instrument_id) or []
                index = bisect_right(series, signal_bar) - 1
                if index < 0 or index + 1 >= len(series):
                    continue
                pending.setdefault(series[index + 1], []).append((candidate, index))

        decision_id = 0
        for now in clock:
            # 1-4. Exits, marks, and the cash/exposure they release, first.
            open_ids = {
                p.instrument_id
                for p in await self._repository.open_positions(self._run.profile.id or 0)
            }
            # The portfolio bar counter must advance exactly **once** per
            # timestamp, not once per instrument. ``max_holding_bars`` is counted
            # in portfolio bars, so advancing it per open position would make a
            # 15-bar limit expire after four timestamps whenever four positions
            # were open -- shortening holding periods in proportion to how busy
            # the portfolio was, which is not a rule anyone chose.
            first = True
            for instrument_id in sorted(open_ids):
                bar = self._bar_at(instrument_id, now)
                if bar is None:
                    continue
                await self._engine.process_bar(
                    instrument_id=instrument_id, bar=bar, advance_clock=first
                )
                first = False

            # 5-9. Then candidates, in the order fixed above.
            for candidate, signal_index in pending.get(now, []):
                decision_id += 1
                await self._attempt(candidate, signal_index, now, decision_id, result)

            await self._mark(now, result)

        result.ending_equity = result.curve[-1].equity if result.curve else result.starting_capital
        await self._collect(result)
        return result

    def _bar_at(self, instrument_id: int, when: datetime) -> BarPrices | None:
        series = self._timestamps.get(instrument_id) or []
        index = bisect_right(series, when) - 1
        if index < 0 or series[index] != when:
            return None
        return self._bars[instrument_id][index]

    async def _attempt(
        self,
        candidate: Candidate,
        signal_index: int,
        now: datetime,
        decision_id: int,
        result: RunResult,
    ) -> None:
        instrument = self._instruments.get(candidate.instrument_id)
        series = self._bars.get(candidate.instrument_id) or []
        if instrument is None or signal_index + 1 >= len(series):
            return
        entry_bar = series[signal_index + 1]

        risk: ShortHorizonRisk | None = None
        atr: Decimal | None = None
        if self._run.risk_layer_enabled:
            risk = _risk_at(candidate.symbol, series, signal_index)
            if risk is not None:
                atr = Decimal(str(risk.atr_pct)) / Decimal(100) * entry_bar.open
        else:
            # Baseline still needs an ATR for its stop, and it must be the same
            # ATR the risk arm sees -- otherwise the A/B compares two stops for
            # two reasons and neither difference is attributable.
            baseline = _risk_at(candidate.symbol, series, signal_index)
            if baseline is not None:
                atr = Decimal(str(baseline.atr_pct)) / Decimal(100) * entry_bar.open

        outcome = await self._engine.open_from_decision(
            instrument=instrument,
            trade_decision_id=decision_id,
            signal_id=None,
            signal_bar_timestamp=series[signal_index].timestamp,
            execution_timestamp=now,
            execution_price=entry_bar.open,
            quote=None,
            atr=atr,
            risk=risk,
        )
        if outcome.accepted:
            result.entries += 1
            if outcome.position_id is not None:
                position = await self._repository.get_position(outcome.position_id)
                if position is not None and position.risk_floor_bound:
                    result.floor_bound_entries += 1
        else:
            key = outcome.rejection.value if outcome.rejection else "UNKNOWN"
            result.rejections[key] = result.rejections.get(key, 0) + 1

    async def _mark(self, now: datetime, result: RunResult) -> None:
        positions = await self._repository.open_positions(self._run.profile.id or 0)
        valuation = value_portfolio(
            portfolio=self._portfolio,
            positions=positions,
            quotes={},
            marks={},
            timestamp=now,
        )
        result.curve.append(
            EquityPoint(
                timestamp=now,
                equity=valuation.equity,
                cash=valuation.cash,
                exposure=valuation.gross_exposure,
                open_positions=valuation.open_position_count,
            )
        )

    async def _collect(self, result: RunResult) -> None:
        portfolio = self._portfolio
        result.realized_pnl = portfolio.realized_pnl
        result.fees = portfolio.total_fees
        result.spread_cost = portfolio.total_spread_cost
        result.slippage_cost = portfolio.total_slippage_cost
        result.trade_count = portfolio.trade_count

        closed = await self._repository.positions(
            self._run.profile.id or 0, status=PositionStatus.CLOSED, limit=100_000
        )
        for position in closed:
            result.closed_trades.append(
                {
                    "symbol": position.instrument_id,
                    "entered_at": position.entry_timestamp,
                    "exited_at": position.exit_timestamp,
                    "reason": position.exit_reason,
                    "gapped": position.exit_was_gap,
                    "excess": position.stop_excess_loss,
                    "pnl": position.realized_pnl,
                    "entry": position.average_entry_price,
                    "exit": position.exit_price,
                    "band": position.risk_band_1d,
                    "regime": position.risk_regime,
                    "floor_bound": position.risk_floor_bound,
                    "structural": position.risk_structural_distance,
                    "floor": position.risk_noise_floor,
                    "distance": position.risk_distance,
                    "quantity": position.quantity,
                }
            )


def load_production_candles(
    conn: sqlite3.Connection, instrument_ids: Sequence[int]
) -> dict[int, list[BarPrices]]:
    """Hourly bars from the production database, read-only."""
    return load_bars(conn, instrument_ids)
