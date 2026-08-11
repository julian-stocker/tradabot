"""Walk-forward paper simulation over stored historical candles.

Runs **our** PaperBroker against **real** imported bars. Not Alpaca's paper-trading
engine: their simulator would apply their fill model, their costs and their
assumptions, which is precisely the machinery phase 3 exists to make explicit and
auditable. The provider supplies data; execution stays here.

Ordering, and why it is the whole design
----------------------------------------
For each bar ``i``:

1. **Mark and exit** open positions against bar ``i``. Existing positions are
   resolved before anything new is considered, so a stop that would have been hit
   this bar is not silently kept alive by a fresh entry.
2. **Score** bar ``i`` with ``as_of=bar[i].timestamp``. The feature service loads
   only bars at or before that instant, so the signal cannot see its own future.
3. **Execute** at ``bar[i+1].open``. A signal computed from a bar's close could not
   have been acted on inside that bar. Filling at bar ``i``'s close would book a
   trade at a price that was only knowable at the moment the opportunity ended --
   the single most common way a backtest invents returns that do not exist.

The final bar therefore never produces an entry: there is no next open to fill at.
That is a real constraint, not an off-by-one.

Costs of the honest ordering
----------------------------
Entering at the next open means overnight gaps are paid for, in both directions.
A simulation that fills at the signal bar's close will look better than this one
and be wrong.

What this proves
----------------
That the ingestion, feature, signal, execution and accounting path runs end to end
on real market data and reconciles. **It is not evidence of predictive edge.** A
positive result here is one path through one instrument over one window, with no
significance testing, no multiple-comparison correction and no out-of-sample
discipline. Phase 4 (backtesting and validation) is where that question gets
asked; until then, a profitable replay says the plumbing works.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import InsufficientDataError
from app.core.logging import get_logger
from app.corporate_actions.repository import CorporateActionRepository
from app.db.models import Candle
from app.domain.enums import CorporateActionType, Horizon, PriceSeriesAdjustment, Timeframe
from app.domain.quotes import Quote
from app.features.service import FeatureService
from app.instruments.repository import InstrumentRepository
from app.instruments.service import InstrumentService
from app.market_data.provider import MarketDataProvider
from app.market_data.repository import CandleRepository
from app.paper.corporate_actions import PositionCorporateActionService
from app.paper.exits import BarPrices
from app.paper.performance import PerformanceSummary, summarise
from app.paper.repository import PaperTradingRepository
from app.paper.service import PaperTradingService
from app.signals.repository import SignalRepository
from app.signals.service import SignalService
from app.simulation.repository import SimulationProfileRepository, TradeDecisionRepository

logger = get_logger(__name__)

BASIS_POINTS = Decimal(10_000)
MIN_BARS_FOR_REPLAY = 2

REPLAY_DISCLAIMER = (
    "This replay proves REAL DATA INGESTION CORRECTNESS end to end.\n"
    "It does NOT prove PREDICTIVE EDGE. One symbol, one window, no significance\n"
    "testing and no out-of-sample split -- see docs/roadmap.md, phase 4."
)


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """What a replay did, and over what."""

    symbol: str
    timeframe: Timeframe
    provider: str
    bars_replayed: int
    first_bar: datetime | None
    last_bar: datetime | None
    signals_evaluated: int
    signals_actionable: int
    positions_opened: int
    trades_closed: int
    warmup_skipped: int
    """Bars that could not be scored because the feature window was not yet full.
    Expected, not an error: indicators need history before they mean anything."""
    positions_adjusted: int = 0
    """Open positions rescaled for a split that took effect mid-replay."""
    summaries: tuple[PerformanceSummary, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.bars_replayed > 0


class ReplayError(RuntimeError):
    """A replay could not be run at all."""


async def replay_symbol(
    session: AsyncSession,
    *,
    settings: Settings,
    provider: MarketDataProvider,
    symbol: str,
    start: datetime,
    end: datetime,
    timeframe: Timeframe = Timeframe.D1,
    horizon: Horizon = Horizon.D5,
    adjustment: PriceSeriesAdjustment = PriceSeriesAdjustment.SPLIT_ADJUSTED,
) -> ReplayResult:
    """Replay stored candles for one symbol through every enabled profile.

    Reads candles that are **already in the database**; it never fetches. Import
    first, then replay -- so a replay is reproducible and does not silently depend
    on what a provider happened to return today.

    Args:
        session: caller owns the transaction.
        settings: cost and risk configuration.
        provider: passed to the signal service; only consulted for live quotes,
            which a historical ``as_of`` never requests.
        symbol: ticker to replay.
        start: first bar, inclusive.
        end: last bar, inclusive.
        timeframe: bar size, which must match what was imported.
        horizon: forecast horizon scored at each bar.
        adjustment: price series the features are computed from. Split-adjusted by
            default -- scoring momentum on raw prices reads a split as a crash.

    Raises:
        ReplayError: unknown symbol, or too few stored bars to replay.
    """
    instruments = InstrumentRepository(session)
    instrument = await instruments.get_by_symbol(symbol)
    if instrument is None:
        msg = f"{symbol} is not in the instrument table; import it first"
        raise ReplayError(msg)

    candles = CandleRepository(session)
    bars = await candles.get_range(
        instrument_id=instrument.id, timeframe=timeframe, start=start, end=end
    )
    if len(bars) < MIN_BARS_FOR_REPLAY:
        msg = (
            f"{symbol} has {len(bars)} stored {timeframe.value} bars in the requested "
            f"window; at least {MIN_BARS_FOR_REPLAY} are needed (a signal bar and a "
            "bar to execute on). Run `market-data import` first."
        )
        raise ReplayError(msg)

    profiles = SimulationProfileRepository(session)
    repository = PaperTradingRepository(session)
    service = PaperTradingService(
        repository=repository,
        profiles=profiles,
        signals=SignalRepository(session),
        decisions=TradeDecisionRepository(session),
    )
    signals = SignalService(
        FeatureService(InstrumentService(instruments), candles, CorporateActionRepository(session)),
        provider,
        settings,
    )

    # Splits that fall inside the window, so positions the replay itself opens are
    # rescaled as the split passes. The import-time adjustment cannot cover these:
    # it runs before any of these positions exist.
    actions = await CorporateActionRepository(session).list_for_instrument(
        instrument_id=instrument.id, symbol=symbol
    )
    pending_splits = sorted(
        (
            action
            for action in actions
            if action.action_type is CorporateActionType.SPLIT
            and bars[0].timestamp <= action.effective_at <= bars[-1].timestamp
        ),
        key=lambda a: a.effective_at,
    )
    corporate_action_service = PositionCorporateActionService(session)

    half_spread = _half_spread_fraction(settings)
    evaluated = actionable = opened = warmup_skipped = adjusted = 0

    for index, bar in enumerate(bars):
        # 0. Rescale open positions for any split now in effect, *before* marking
        #    them. Marking a pre-split quantity against a post-split price books a
        #    loss that never happened -- a 2-for-1 reads as -50%.
        while pending_splits and pending_splits[0].effective_at <= bar.timestamp:
            split = pending_splits.pop(0)
            adjusted += len(
                await corporate_action_service.apply_actions(
                    instrument_id=instrument.id,
                    actions=[split],
                    as_of=bar.timestamp,
                )
            )

        # 1. Resolve what is already open, before considering anything new.
        await service.process_bar(
            instrument_id=instrument.id,
            bar=_bar_prices(bar),
            quote=_synthetic_quote(symbol, bar.timestamp, bar.close, half_spread),
        )

        # The last bar can be marked but not entered on: there is no next open.
        if index + 1 >= len(bars):
            continue

        try:
            signal = await signals.evaluate(
                symbol=symbol,
                timeframe=timeframe,
                horizon=horizon,
                as_of=bar.timestamp,
                adjustment=adjustment,
            )
        except InsufficientDataError:
            warmup_skipped += 1
            continue

        evaluated += 1
        if not signal.is_actionable:
            continue
        actionable += 1

        execution_bar = bars[index + 1]
        run = await service.run_signal(
            signal=signal,
            instrument=instrument,
            adjustment=adjustment,
            execution_timestamp=execution_bar.timestamp,
            execution_price=execution_bar.open,
            quote=_synthetic_quote(
                symbol, execution_bar.timestamp, execution_bar.open, half_spread
            ),
            atr=_atr_from_snapshot(signal.feature_snapshot, execution_bar.open),
            now=execution_bar.timestamp,
        )
        opened += run.positions_opened

    await session.flush()

    summaries: list[PerformanceSummary] = []
    trades_closed = 0
    for profile in await profiles.list_profiles(enabled_only=True):
        if profile.id is None:  # pragma: no cover -- persisted profiles have ids
            continue
        trades = await repository.trades(profile.id)
        trades_closed += len(trades)
        summaries.append(
            summarise(
                profile_name=profile.name,
                portfolio=await repository.get_portfolio(profile.id),
                trades=trades,
                snapshots=await repository.snapshots(profile.id),
                open_position_count=len(await repository.open_positions(profile.id)),
            )
        )

    logger.info(
        "replay complete",
        symbol=symbol,
        bars=len(bars),
        splits_applied=adjusted,
        signals=evaluated,
        actionable=actionable,
        opened=opened,
        closed=trades_closed,
    )

    return ReplayResult(
        symbol=symbol,
        timeframe=timeframe,
        provider=provider.name,
        bars_replayed=len(bars),
        first_bar=bars[0].timestamp,
        last_bar=bars[-1].timestamp,
        signals_evaluated=evaluated,
        signals_actionable=actionable,
        positions_opened=opened,
        trades_closed=trades_closed,
        warmup_skipped=warmup_skipped,
        positions_adjusted=adjusted,
        summaries=tuple(summaries),
    )


def _bar_prices(candle: Candle) -> BarPrices:
    """Stored *raw* prices, which is what execution must use.

    Features are computed on the split-adjusted series, but a fill happens at the
    price actually quoted that day. A split inside the replay window is handled by
    rescaling the *position* as the split passes (see
    :mod:`app.paper.corporate_actions`), not by pretending the historical fill
    occurred at a back-adjusted price.
    """
    return BarPrices(
        timestamp=candle.timestamp,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
    )


def _half_spread_fraction(settings: Settings) -> Decimal:
    """Half the configured default spread, as a fraction of price."""
    return Decimal(str(settings.costs.default_spread_bps)) / BASIS_POINTS / 2


def _synthetic_quote(
    symbol: str, timestamp: datetime, price: Decimal, half_spread: Decimal
) -> Quote:
    """A bid/ask reconstructed around a historical price.

    **Historical bars carry no quote.** Alpaca's bar endpoint returns trades, not
    the book, and tradabot does not yet store historical quotes (phase 5). So the
    spread here is the *configured assumption*, applied symmetrically -- the same
    number the signal's cost model used, which keeps costs consistent between
    scoring and execution instead of optimistic in one and pessimistic in the other.

    It is an assumption, not a measurement. A real spread widens exactly when it
    matters most, and this one does not.
    """
    offset = (price * half_spread).quantize(Decimal("0.000001"))
    return Quote(
        symbol=symbol,
        timestamp=timestamp,
        bid=price - offset,
        ask=price + offset,
    )


def _atr_from_snapshot(snapshot: dict[str, float | None], price: Decimal) -> Decimal | None:
    """Recover an absolute ATR from the signal's percentage ATR.

    Stops are sized in ATR, and the feature snapshot carries ``atr_pct_14``
    (percent of price) rather than an absolute figure. Returning ``None`` when it
    is missing lets the risk model fall back to its configured percentage stop
    instead of sizing against a zero.
    """
    atr_pct = snapshot.get("atr_pct_14")
    if atr_pct is None or atr_pct <= 0:
        return None
    return (price * Decimal(str(atr_pct)) / Decimal(100)).quantize(Decimal("0.000001"))
