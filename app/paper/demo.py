"""Deterministic paper-trading demo.

Runs one signal through every simulation profile, then walks a fixed sequence of
candles so positions open, are marked, and exit. Produces byte-identical output on
every run.

**What this demonstrates:** that the accounting, execution and risk machinery
work -- cash reconciles, costs are itemised, exits fire, portfolios stay isolated.

**What it does not demonstrate:** that any of this is profitable. The prices are a
hand-written sequence chosen to exercise the code paths, not a market. Reading the
demo's P&L as evidence about the strategy would be reading a unit test as evidence
about the world.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.costs.models import NetEdge
from app.db.models import Instrument
from app.domain.enums import (
    AssetType,
    Classification,
    Horizon,
    PriceSeriesAdjustment,
    Timeframe,
)
from app.domain.quotes import Quote
from app.instruments.repository import InstrumentRepository
from app.market_data.provider import InstrumentInfo
from app.paper.exits import BarPrices
from app.paper.performance import PerformanceSummary, summarise
from app.paper.repository import PaperTradingRepository
from app.paper.service import PaperTradingService
from app.signals.models import SignalResult
from app.signals.repository import SignalRepository
from app.simulation.defaults import build_default_profiles
from app.simulation.repository import SimulationProfileRepository, TradeDecisionRepository

logger = get_logger(__name__)

DEMO_SYMBOL = "DEMO"
DEMO_START = datetime(2024, 3, 1, tzinfo=UTC)
SIGNAL_BAR = DEMO_START
EXECUTION_AT = DEMO_START + timedelta(days=1)

# A hand-written price path: up, a wobble, then through the take-profit. Chosen to
# exercise entry, marking, excursion tracking and a target exit -- not to be
# realistic.
DEMO_BARS: tuple[tuple[str, str, str, str], ...] = (
    ("100.00", "103.00", "99.50", "102.00"),
    ("102.00", "104.00", "100.50", "101.00"),
    ("101.00", "106.00", "100.80", "105.50"),
    ("105.50", "112.00", "105.00", "111.00"),
)


@dataclass(frozen=True, slots=True)
class DemoResult:
    """Per-profile outcome of the demo run."""

    rows: tuple[tuple[str, str], ...]
    """(profile name, one-line summary), in profile order."""
    positions_opened: int
    trades_closed: int


async def run_demo(session: AsyncSession) -> DemoResult:
    """Seed, run and summarise a deterministic paper-trading simulation.

    Everything is created inside ``session``; the caller controls the transaction.
    """
    instrument = await _ensure_demo_instrument(session)
    profiles = SimulationProfileRepository(session)
    await profiles.upsert_many(build_default_profiles())
    await session.flush()

    repository = PaperTradingRepository(session)
    service = PaperTradingService(
        repository=repository,
        profiles=profiles,
        signals=SignalRepository(session),
        decisions=TradeDecisionRepository(session),
    )

    signal = _demo_signal()
    quote = Quote(
        symbol=DEMO_SYMBOL,
        timestamp=EXECUTION_AT,
        bid=Decimal("99.95"),
        ask=Decimal("100.05"),
    )

    run = await service.run_signal(
        signal=signal,
        instrument=instrument,
        adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
        execution_timestamp=EXECUTION_AT,
        execution_price=Decimal("100.00"),
        quote=quote,
        atr=Decimal("2.00"),
        now=EXECUTION_AT,
    )
    await session.flush()

    for index, (open_, high, low, close) in enumerate(DEMO_BARS, start=1):
        timestamp = EXECUTION_AT + timedelta(days=index)
        bar = BarPrices(
            timestamp=timestamp,
            open=Decimal(open_),
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal(close),
        )
        bar_quote = Quote(
            symbol=DEMO_SYMBOL,
            timestamp=timestamp,
            bid=Decimal(close) - Decimal("0.05"),
            ask=Decimal(close) + Decimal("0.05"),
        )
        await service.process_bar(instrument_id=instrument.id, bar=bar, quote=bar_quote)
    await session.flush()

    rows: list[tuple[str, str]] = []
    trades_closed = 0
    for profile in await profiles.list_profiles(enabled_only=True):
        assert profile.id is not None
        portfolio = await repository.get_portfolio(profile.id)
        trades = await repository.trades(profile.id)
        snapshots = await repository.snapshots(profile.id)
        open_positions = await repository.open_positions(profile.id)
        trades_closed += len(trades)

        summary = summarise(
            profile_name=profile.name,
            portfolio=portfolio,
            trades=trades,
            snapshots=snapshots,
            open_position_count=len(open_positions),
        )
        rows.append((profile.name, _format(summary)))

    return DemoResult(
        rows=tuple(rows),
        positions_opened=run.positions_opened,
        trades_closed=trades_closed,
    )


def _format(summary: PerformanceSummary) -> str:
    """One dense line per portfolio."""
    return (
        f"equity={summary.ending_equity:>10.2f}  "
        f"net={summary.net_pnl:>+8.2f} ({summary.return_pct:>+6.2f}%)  "
        f"trades={summary.trade_count} open={summary.open_position_count}  "
        f"costs={summary.total_costs:>6.2f}  "
        f"dd={summary.max_drawdown:>7.2%}"
    )


async def _ensure_demo_instrument(session: AsyncSession) -> Instrument:
    repository = InstrumentRepository(session)
    await repository.upsert_many(
        [
            InstrumentInfo(
                symbol=DEMO_SYMBOL,
                name="Demo Instrument",
                exchange="XNAS",
                currency="EUR",
                asset_type=AssetType.STOCK,
                listed_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        ]
    )
    await session.flush()
    instrument = await repository.get_by_symbol(DEMO_SYMBOL)
    if instrument is None:  # pragma: no cover -- just upserted
        msg = "demo instrument disappeared after upsert"
        raise RuntimeError(msg)
    return instrument


def _demo_signal() -> SignalResult:
    """A fixed, strongly bullish signal.

    Hand-built rather than computed, so the demo does not depend on the feature
    engine, the mock provider's seed, or a warm-up window. Its score clears every
    risk profile's threshold, so any SKIP in the output comes from *portfolio
    economics*, which is the point being demonstrated.
    """
    return SignalResult(
        symbol=DEMO_SYMBOL,
        timestamp=SIGNAL_BAR,
        generated_at=SIGNAL_BAR,
        timeframe=Timeframe.D1,
        horizon=Horizon.D5,
        score=82.0,
        classification=Classification.STRONG_BULLISH,
        confidence=0.75,
        components=(),
        feature_snapshot={"atr_pct_14": 2.0, "rsi_14": 61.0},
        reference_price=Decimal("100.00"),
        spread_bps=Decimal("10"),
        net_edge=NetEdge(
            expected_move_bps=Decimal("300"),
            cost_bps=Decimal("19"),
            net_edge_bps=Decimal("281"),
        ),
        bars_used=200,
        engine_version="demo-v1",
    )
