"""Phase 11.3 — baseline vs risk-layer historical replay.

The question
------------
The risk layer is wired in and tested. What does it actually *change*? Not
"does it make money" — no phase has found directional information and this
replay cannot create any — but the mechanical questions that have answers:

* how many candidates does the gate refuse, and for which arithmetic reasons;
* how often does the noise floor widen a stop the ATR rule proposed;
* what does whole-share execution do to a small account;
* how often does a loss exceed what risk-v1 warned about, and by how much;
* does the 35% cost-share backstop bind, or is it inert as designed.

**Any P&L printed here is not evidence of strategy quality.** It is the
arithmetic consequence of applying a sizing rule to a candidate stream that
eight research phases found no directional edge in. It is reported because the
brief asks for a baseline-vs-risk-layer comparison, and suppressing it would
hide whether the risk layer merely trades less or trades differently.

What this shares with production
--------------------------------
Every calculation comes from the production module that owns it:
:func:`~app.market_data.volatility.estimate`, :func:`~app.market_data.risk.assess`,
:func:`~app.paper.exits.derive_stop_and_target`, :func:`~app.paper.exits.evaluate_exit`,
:func:`~app.paper.risk_gate.evaluate_entry`, :func:`~app.paper.sizing.size_position`
and :func:`~app.paper.execution.price_fill`. Nothing here re-implements sizing,
costs, stops or risk. What it replaces is only the *database* — the portfolio is
an in-memory dataclass — because 48 configurations over the candidate stream is
tens of thousands of position lifecycles and the persistence path is covered by
``tests/integration/test_paper_risk_layer.py`` instead.

The candidate stream
--------------------
Qualified signal evaluations with ``direction == +1`` from backtest run 1
(2025-02-11 to 2026-08-04). Bullish only, because the paper engine is LONG-only
and treating a neutral or bearish evaluation as a long entry would fabricate
candidates the live path would never produce. Run 4 (2020-2024) is excluded: it
produced **zero** qualified evaluations, so it contributes no candidates.

Entries fill at the **next** hourly bar's open. Filling at the signal bar's close
is the most flattering bug a replay can have, and the paper engine forbids it
structurally; this respects the same rule.
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from app.core.config import CostSettings
from app.domain.enums import CandleAmbiguityPolicy, ExitReason, Side
from app.market_data.risk import ShortHorizonRisk, assess
from app.market_data.volatility import estimate
from app.paper.execution import price_fill
from app.paper.exits import BarPrices, derive_stop_and_target, evaluate_exit
from app.paper.risk_gate import evaluate_entry
from app.paper.sizing import ExecutionFractionality, size_position
from app.simulation.models import BrokerCostConfig, RiskConfig, SimulationProfileConfig

CANDIDATE_RUN_ID: Final = 1
"""Backtest run 1. Run 4 covers 2020-2024 and produced no qualified rows."""

VOLATILITY_WINDOW: Final = 400
"""Hourly bars of history handed to volatility-v1 per candidate.

Above the 252-bar percentile window it needs, and bounded so a replay of
hundreds of candidates does not re-scan years of bars for each one.
"""

REPLAY_CAPITALS: Final = (Decimal("100"), Decimal("1000"), Decimal("10000"))
RISK_BUDGETS: Final = (Decimal("0.0025"), Decimal("0.005"), Decimal("0.01"), Decimal("0.02"))


class BreachClass(StrEnum):
    """How a closed position's loss compared with what risk-v1 warned about.

    The classification is against the **80% one-day band at entry**, which is a
    coverage figure, not a bound: four times in five the day's excursion stayed
    inside it. Exceedances are therefore expected at a measurable rate, and the
    point of this audit is to check that rate rather than to treat any single
    exceedance as a failure.
    """

    WITHIN_EXPECTED_RISK = "WITHIN_EXPECTED_RISK"
    NORMAL_EXCEEDANCE = "NORMAL_EXCEEDANCE"
    """Beyond the band, but the stop itself held — ordinary tail movement."""

    GAP_EXCEEDANCE = "GAP_EXCEEDANCE"
    """The exit gapped through the stop. The model warns this is possible and
    quantifies it; it does not claim it cannot happen."""

    EXTREME_EXCEEDANCE = "EXTREME_EXCEEDANCE"
    """Loss beyond twice the band. The case the model genuinely under-warns."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One qualified evaluation, with the forward path it could have traded."""

    instrument_id: int
    symbol: str
    signal_bar: datetime
    score: float


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    """One cell of the experiment grid."""

    capital: Decimal
    risk_per_trade: Decimal
    fractionality: ExecutionFractionality
    risk_layer_enabled: bool
    enforce_cost_share: bool = True

    @property
    def label(self) -> str:
        layer = "risk" if self.risk_layer_enabled else "base"
        mode = "whole" if self.fractionality is ExecutionFractionality.WHOLE_SHARES_ONLY else "frac"
        return f"paper-{int(self.capital)}/{self.risk_per_trade:.4f}/{mode}/{layer}"


@dataclass(slots=True)
class ClosedTrade:
    symbol: str
    entered_at: datetime
    exited_at: datetime
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    net_pnl: Decimal
    costs: Decimal
    reason: ExitReason
    gapped: bool
    stop_excess: Decimal
    risk_band_1d: Decimal | None
    regime: str | None
    floor_bound: bool


@dataclass(slots=True)
class ReplayResult:
    """What one configuration did. Counts first; P&L last and caveated."""

    config: ReplayConfig
    candidates: int = 0
    entries: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    floor_widened: int = 0
    trades: list[ClosedTrade] = field(default_factory=list)
    final_equity: Decimal = Decimal(0)

    @property
    def rejected(self) -> int:
        return sum(self.rejections.values())

    @property
    def total_costs(self) -> Decimal:
        return sum((t.costs for t in self.trades), Decimal(0))

    @property
    def net_pnl(self) -> Decimal:
        return sum((t.net_pnl for t in self.trades), Decimal(0))

    def breaches(self) -> dict[BreachClass, int]:
        counts = dict.fromkeys(BreachClass, 0)
        for trade in self.trades:
            counts[classify_breach(trade)] += 1
        return counts


def classify_breach(trade: ClosedTrade) -> BreachClass:
    """Compare a realised loss against the band risk-v1 published at entry.

    A winning trade, a trade with no risk estimate, and a trade whose loss stayed
    inside the band all land in ``WITHIN_EXPECTED_RISK``: the audit asks whether
    the model *under-warned*, and there is no such thing as under-warning about a
    profit.
    """
    if trade.net_pnl >= 0 or trade.risk_band_1d is None or trade.risk_band_1d <= 0:
        return BreachClass.WITHIN_EXPECTED_RISK

    loss_pct = (trade.entry_price - trade.exit_price) / trade.entry_price * Decimal(100)
    band = trade.risk_band_1d
    if loss_pct <= band:
        return BreachClass.WITHIN_EXPECTED_RISK
    if loss_pct > band * 2:
        return BreachClass.EXTREME_EXCEEDANCE
    if trade.gapped:
        return BreachClass.GAP_EXCEEDANCE
    return BreachClass.NORMAL_EXCEEDANCE


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _as_utc(raw: str) -> datetime:
    return datetime.fromisoformat(raw).replace(tzinfo=UTC)


def load_candidates(conn: sqlite3.Connection) -> list[Candidate]:
    """Qualified bullish evaluations, oldest first."""
    rows = conn.execute(
        """
        SELECT e.instrument_id, i.symbol, e.market_data_timestamp, e.score
        FROM signal_evaluations e
        JOIN instruments i ON i.id = e.instrument_id
        WHERE e.qualified = 1 AND e.direction = 1 AND e.backtest_run_id = ?
        ORDER BY e.market_data_timestamp
        """,
        (CANDIDATE_RUN_ID,),
    ).fetchall()
    return [
        Candidate(
            instrument_id=int(r[0]), symbol=str(r[1]), signal_bar=_as_utc(r[2]), score=float(r[3])
        )
        for r in rows
    ]


def load_bars(
    conn: sqlite3.Connection, instrument_ids: Iterable[int]
) -> dict[int, list[BarPrices]]:
    """Hourly bars per instrument, oldest first.

    Read RAW and unadjusted on purpose: the replay window is 2025-02 to 2026-08
    and a split inside it would corrupt the forward path. This is checked rather
    than assumed -- see :func:`splits_in_window`.
    """
    bars: dict[int, list[BarPrices]] = {}
    for instrument_id in instrument_ids:
        rows = conn.execute(
            """
            SELECT timestamp, open, high, low, close FROM candles
            WHERE instrument_id = ? AND timeframe = 'H1'
            ORDER BY timestamp
            """,
            (instrument_id,),
        ).fetchall()
        bars[instrument_id] = [
            BarPrices(
                timestamp=_as_utc(r[0]),
                open=Decimal(str(r[1])),
                high=Decimal(str(r[2])),
                low=Decimal(str(r[3])),
                close=Decimal(str(r[4])),
            )
            for r in rows
        ]
    return bars


def splits_in_window(
    conn: sqlite3.Connection, instrument_ids: Iterable[int], start: datetime, end: datetime
) -> list[tuple[str, str, float]]:
    """Corporate actions that would invalidate an unadjusted forward path.

    Returns rows rather than raising: a non-empty result is a finding to report,
    not a crash. Phase 9A found exactly this defect the hard way.
    """
    ids = tuple(instrument_ids)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    return [
        (str(r[0]), str(r[1]), float(r[2]))
        for r in conn.execute(
            f"""
            SELECT i.symbol, a.effective_at, a.to_shares / a.from_shares
            FROM corporate_actions a JOIN instruments i ON i.id = a.instrument_id
            WHERE a.instrument_id IN ({placeholders})
              AND a.effective_at >= ? AND a.effective_at <= ?
              AND a.action_type = 'SPLIT'
            """,
            (*ids, start.isoformat(sep=" "), end.isoformat(sep=" ")),
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------
def build_profile(config: ReplayConfig) -> SimulationProfileConfig:
    """A profile at an arbitrary capital.

    The nine stored profiles are 50/500/5000 EUR; the brief asks about
    100/1000/10000. These are constructed rather than inserted, so the replay
    cannot disturb the profiles the live paper book uses.
    """
    return SimulationProfileConfig(
        id=0,
        name=config.label,
        initial_capital=config.capital,
        currency="EUR",
        risk=RiskConfig(
            name="replay",
            risk_per_trade=config.risk_per_trade,
            max_daily_loss=Decimal("0.05"),
            max_drawdown=Decimal("0.25"),
            min_signal_score=75.0,
            min_confidence=0.5,
            max_position_percent=Decimal("0.30"),
            max_total_exposure=Decimal("1.0"),
            max_open_positions=5,
            stop_loss_atr_multiple=Decimal("2.0"),
            take_profit_r_multiple=Decimal("2.0"),
            max_holding_bars=40,
            require_stop_loss=True,
        ),
        costs=BrokerCostConfig(
            name="replay",
            order_fee=Decimal("1.00"),
            variable_fee_rate=Decimal("0"),
            slippage_spread_multiple=Decimal("0.5"),
            default_spread_bps=Decimal("10"),
            min_order_notional=Decimal("0"),
        ),
    )


def _risk_at(symbol: str, bars: Sequence[BarPrices], index: int) -> ShortHorizonRisk | None:
    """volatility-v1 then risk-v1, from bars at or before ``index``.

    Causal by construction: the slice ends at ``index`` inclusive, so nothing
    after the signal bar can reach the estimate.
    """
    window = bars[max(0, index - VOLATILITY_WINDOW + 1) : index + 1]
    movement = estimate(
        symbol=symbol,
        highs=[float(b.high) for b in window],
        lows=[float(b.low) for b in window],
        closes=[float(b.close) for b in window],
        bar_timestamp=window[-1].timestamp,
        now=window[-1].timestamp,
    )
    if movement is None:
        return None
    return assess(movement, now=window[-1].timestamp)


def replay(  # noqa: PLR0915 -- one straight-line pass per candidate; splitting it
    # would hide the ordering, which is the part that has to be right
    config: ReplayConfig,
    candidates: Sequence[Candidate],
    bars: dict[int, list[BarPrices]],
) -> ReplayResult:
    """Run one configuration over the candidate stream, chronologically.

    Two simplifications, both stated because they make this **more** permissive
    than the live engine rather than less:

    * ``current_exposure`` is passed as zero, so the ``max_total_exposure`` cap
      never binds. Positions are walked forward independently rather than marked
      against a shared clock, so there is no point-in-time exposure to pass.
    * The concurrency limit is applied by counting positions whose exit is still
      in the future at each entry, not by simulating a portfolio ledger bar by
      bar.

    Cash and equity are tracked and do bind, through the real sizer.
    """
    profile = build_profile(config)
    costs: CostSettings = profile.costs.to_cost_settings()
    result = ReplayResult(config=config, candidates=len(candidates))

    cash = config.capital
    equity = config.capital
    open_until: list[datetime] = []

    for candidate in candidates:
        series = bars.get(candidate.instrument_id) or []
        if not series:
            continue
        timestamps = [b.timestamp for b in series]
        signal_index = bisect_right(timestamps, candidate.signal_bar) - 1
        entry_index = signal_index + 1
        if signal_index < 0 or entry_index >= len(series):
            continue

        entry_bar = series[entry_index]
        open_until = [t for t in open_until if t > entry_bar.timestamp]
        if len(open_until) >= profile.risk.max_open_positions:
            result.rejections["MAX_OPEN_POSITIONS"] = (
                result.rejections.get("MAX_OPEN_POSITIONS", 0) + 1
            )
            continue

        expected_fill = entry_bar.open
        if expected_fill <= 0:
            continue

        risk = _risk_at(candidate.symbol, series, signal_index)
        atr = (
            Decimal(str(risk.atr_pct)) / Decimal(100) * expected_fill if risk is not None else None
        )
        stop_loss, take_profit = derive_stop_and_target(
            entry_price=expected_fill,
            atr=atr,
            stop_loss_atr_multiple=profile.risk.stop_loss_atr_multiple,
            take_profit_r_multiple=profile.risk.take_profit_r_multiple,
        )

        floor_bound = False
        band_1d = Decimal(str(risk.risk_band_1d)) if risk is not None else None
        regime = risk.regime.value if risk is not None else None

        if config.risk_layer_enabled:
            gate = evaluate_entry(
                risk=risk,
                entry_price=expected_fill,
                structural_stop=stop_loss,
                risk_budget=equity * config.risk_per_trade,
                costs=costs,
                allow_stale=True,  # replayed bars are always "old"
                enforce_cost_share=config.enforce_cost_share,
            )
            if not gate.permits_entry:
                reason = gate.reason.value if gate.reason else gate.decision.value
                result.rejections[reason] = result.rejections.get(reason, 0) + 1
                continue
            if gate.risk_distance is not None:
                stop_loss = expected_fill - gate.risk_distance
                take_profit = expected_fill + gate.risk_distance * (
                    profile.risk.take_profit_r_multiple or Decimal(2)
                )
            floor_bound = gate.tighter_than_noise
            if floor_bound:
                result.floor_widened += 1

        sizing = size_position(
            profile=profile,
            equity=equity,
            available_cash=cash,
            current_exposure=Decimal(0),
            entry_price=expected_fill,
            stop_loss=stop_loss,
            fractionality=config.fractionality,
        )
        if not sizing.is_tradable:
            reason = sizing.rejection.value if sizing.rejection else "UNSIZED"
            result.rejections[reason] = result.rejections.get(reason, 0) + 1
            continue

        entry = price_fill(
            side=Side.LONG,
            quantity=sizing.quantity,
            settings=costs,
            reference_price=expected_fill,
        )

        trade = _walk_forward(
            symbol=candidate.symbol,
            series=series,
            entry_index=entry_index,
            quantity=sizing.quantity,
            entry_pricing_price=entry.fill_price,
            entry_cost=entry.total_cost,
            entry_fee=entry.fee,
            stop_loss=stop_loss,
            take_profit=take_profit,
            max_bars=profile.risk.max_holding_bars or 40,
            costs=costs,
            band_1d=band_1d,
            regime=regime,
            floor_bound=floor_bound,
        )
        if trade is None:
            continue

        result.entries += 1
        result.trades.append(trade)
        cash += trade.net_pnl
        equity += trade.net_pnl
        open_until.append(trade.exited_at)

    result.final_equity = equity
    return result


def _walk_forward(
    *,
    symbol: str,
    series: Sequence[BarPrices],
    entry_index: int,
    quantity: Decimal,
    entry_pricing_price: Decimal,
    entry_cost: Decimal,
    entry_fee: Decimal,
    stop_loss: Decimal | None,
    take_profit: Decimal | None,
    max_bars: int,
    costs: CostSettings,
    band_1d: Decimal | None,
    regime: str | None,
    floor_bound: bool,
) -> ClosedTrade | None:
    """Walk bars after entry until an exit fires or the holding period expires.

    Uses :func:`~app.paper.exits.evaluate_exit`, so gap-through behaves exactly
    as it does in the engine: a bar that opens beyond the stop fills at the open,
    not at the stop, and the shortfall is recorded rather than absorbed.
    """
    entry_bar = series[entry_index]
    last = min(entry_index + max_bars, len(series) - 1)
    if last <= entry_index:
        return None

    exit_price = series[last].close
    exit_at = series[last].timestamp
    reason = ExitReason.MAX_HOLDING_PERIOD
    gapped = False

    for bar in series[entry_index + 1 : last + 1]:
        evaluation = evaluate_exit(
            side=Side.LONG,
            bar=bar,
            stop_loss=stop_loss,
            take_profit=take_profit,
            policy=CandleAmbiguityPolicy.CONSERVATIVE,
        )
        if evaluation.triggered and evaluation.exit_price is not None:
            exit_price = evaluation.exit_price
            exit_at = bar.timestamp
            reason = evaluation.reason or ExitReason.MAX_HOLDING_PERIOD
            gapped = evaluation.gapped
            break

    exit_pricing = price_fill(
        side=Side.SHORT, quantity=quantity, settings=costs, reference_price=exit_price
    )
    outflow = entry_pricing_price * quantity + entry_fee
    inflow = exit_pricing.fill_price * quantity - exit_pricing.fee
    excess = (
        max(Decimal(0), stop_loss - exit_price) * quantity
        if reason is ExitReason.STOP_LOSS and stop_loss is not None
        else Decimal(0)
    )
    return ClosedTrade(
        symbol=symbol,
        entered_at=entry_bar.timestamp,
        exited_at=exit_at,
        quantity=quantity,
        entry_price=entry_pricing_price,
        exit_price=exit_pricing.fill_price,
        net_pnl=inflow - outflow,
        costs=entry_cost + exit_pricing.total_cost,
        reason=reason,
        gapped=gapped,
        stop_excess=excess,
        risk_band_1d=band_1d,
        regime=regime,
        floor_bound=floor_bound,
    )
