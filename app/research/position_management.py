"""Part J-M: can exit management turn a modest hit rate into expectancy?

Phase 7's directional work produced at best a 5.7pp separation in hit rate with
**identical MFE and MAE across buckets**. That combination is the whole question
this module exists to answer: if entries are near-random in payoff terms, does
how you *manage* them still produce positive net expectancy?

The honest answer is knowable in advance for one case and must be measured for
the rest. A symmetric random walk cannot be made profitable by any stop rule --
stops change the *shape* of the return distribution, not its mean, and every
transaction costs money. So the null hypothesis here is strong, and any positive
result must clear costs by a margin rather than by a rounding error.

The grid is declared before it was run
--------------------------------------
Three initial stops x five exit families = fifteen combinations, fixed below. No
parameter was added, removed or re-centred after seeing a result. That is the
entire anti-overfitting mechanism, and it is worth more than any amount of
cross-validation applied afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

INITIAL_STOPS: Final[tuple[tuple[str, float], ...]] = (
    ("atr_1.5", 1.5),
    ("atr_2.5", 2.5),
    ("atr_4.0", 4.0),
)
"""Initial stop distance in ATR multiples.

Three, spanning tight to loose. A finer grid would be a search: with a symmetric
payoff the optimum is wherever the noise happens to fall, and reporting that
number as a finding is how backtests lie.
"""

EXIT_FAMILIES: Final[tuple[str, ...]] = (
    "fixed_2R",
    "trail_atr_3",
    "partial_1R_trail",
    "time_only",
    "structure_trail",
)
"""The five families from the brief, one representative parameterisation each."""

TRAIL_ATR_MULTIPLE: Final = 3.0
PARTIAL_TAKE_R: Final = 1.0
PARTIAL_FRACTION: Final = 0.5
FIXED_TARGET_R: Final = 2.0
MAX_HOLDING_BARS: Final = 140
"""Twenty sessions of hourly bars -- the brief's stated upper holding period."""


@dataclass(frozen=True, slots=True)
class Bar:
    """One forward bar. Deliberately minimal: high, low and close are all a stop
    rule can legitimately consult."""

    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class TradeResult:
    """One simulated trade, gross of costs."""

    exit_reason: str
    gross_return_pct: float
    bars_held: int
    mfe_pct: float
    mae_pct: float


def simulate_trade(
    *,
    entry: float,
    atr: float,
    bars: list[Bar],
    stop_multiple: float,
    family: str,
    ema_trail: list[float] | None = None,
) -> TradeResult:
    """Walk one long position forward under one exit rule.

    **Pessimistic on ambiguity.** When a bar's range contains both the stop and
    the target, the stop is taken. Intrabar order is unknowable from OHLC, and
    assuming the favourable one is the single easiest way to manufacture a
    profitable backtest.

    The trailing stop only ever ratchets upward; a stop that could loosen would
    be re-underwriting the trade after it went wrong.
    """
    if atr <= 0 or entry <= 0 or not bars:
        return TradeResult("NO_DATA", 0.0, 0, 0.0, 0.0)

    risk = atr * stop_multiple
    stop = entry - risk
    target = entry + risk * FIXED_TARGET_R if family == "fixed_2R" else None
    realised = 0.0
    remaining = 1.0
    partial_done = False

    best = entry
    worst = entry

    for index, bar in enumerate(bars[:MAX_HOLDING_BARS]):
        best = max(best, bar.high)
        worst = min(worst, bar.low)

        # Stop first: the pessimistic ordering.
        if bar.low <= stop:
            gross = realised + remaining * (stop / entry - 1.0) * 100
            return TradeResult(
                "STOP", gross, index + 1, (best / entry - 1) * 100, (worst / entry - 1) * 100
            )

        if target is not None and bar.high >= target:
            gross = realised + remaining * (target / entry - 1.0) * 100
            return TradeResult(
                "TARGET", gross, index + 1, (best / entry - 1) * 100, (worst / entry - 1) * 100
            )

        if family == "partial_1R_trail" and not partial_done and bar.high >= entry + risk:
            realised += PARTIAL_FRACTION * (risk / entry) * 100
            remaining -= PARTIAL_FRACTION
            partial_done = True
            stop = max(stop, entry)  # the remainder is now risk-free

        if family in {"trail_atr_3", "partial_1R_trail"}:
            stop = max(stop, bar.close - atr * TRAIL_ATR_MULTIPLE)
        elif family == "structure_trail" and ema_trail is not None and index < len(ema_trail):
            stop = max(stop, ema_trail[index])

    last = bars[min(len(bars), MAX_HOLDING_BARS) - 1].close
    gross = realised + remaining * (last / entry - 1.0) * 100
    return TradeResult(
        "TIME",
        gross,
        min(len(bars), MAX_HOLDING_BARS),
        (best / entry - 1) * 100,
        (worst / entry - 1) * 100,
    )


@dataclass(frozen=True, slots=True)
class StrategyStats:
    """Expectancy for one (stop, exit family) combination."""

    label: str
    trades: int
    win_rate: float
    average_winner: float
    average_loser: float
    median_trade: float
    gross_expectancy: float
    net_expectancy: float
    profit_factor: float
    average_bars: float
    max_drawdown: float

    def render(self) -> str:
        return (
            f"    {self.label:<26}n={self.trades:>5}  win={self.win_rate * 100:>5.1f}%  "
            f"avgW={self.average_winner:>6.2f}%  avgL={self.average_loser:>6.2f}%  "
            f"gross={self.gross_expectancy:>+6.2f}%  net={self.net_expectancy:>+6.2f}%  "
            f"PF={self.profit_factor:>5.2f}  bars={self.average_bars:>5.0f}  "
            f"maxDD={self.max_drawdown:>6.1f}%"
        )


def summarise(results: list[TradeResult], *, label: str, cost_pct: float) -> StrategyStats:
    """Expectancy after a round-trip cost charged to every trade.

    ``cost_pct`` is subtracted from each trade rather than netted at the end, so
    a high-turnover rule is penalised exactly as often as it trades. That is the
    difference between a strategy comparison and a ranking of gross returns.
    """
    if not results:
        return StrategyStats(label, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    gross = [r.gross_return_pct for r in results]
    net = [value - cost_pct for value in gross]
    winners = [v for v in net if v > 0]
    losers = [v for v in net if v <= 0]

    won = sum(winners)
    lost = abs(sum(losers))

    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in net:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)

    return StrategyStats(
        label=label,
        trades=len(results),
        win_rate=len(winners) / len(net),
        average_winner=won / len(winners) if winners else 0.0,
        average_loser=-lost / len(losers) if losers else 0.0,
        median_trade=sorted(net)[len(net) // 2],
        gross_expectancy=sum(gross) / len(gross),
        net_expectancy=sum(net) / len(net),
        profit_factor=(won / lost) if lost > 0 else float("inf"),
        average_bars=sum(r.bars_held for r in results) / len(results),
        max_drawdown=drawdown,
    )
