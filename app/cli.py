"""Command-line entry points for local development.

Kept deliberately small: it wires existing services together and prints results.
No business logic lives here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.advisor import AdvisorService, FactStore, PriceSeries
from app.advisor.cli import render, to_json
from app.core.config import Settings, get_settings
from app.core.errors import InstrumentNotFoundError, ProviderError
from app.core.events import Event, EventCategory, EventType
from app.core.logging import configure_logging, get_logger
from app.core.time import utc_now
from app.corporate_actions.repository import CorporateActionRepository
from app.db.base import Base
from app.db.models import Candle, Instrument
from app.db.session import create_engine, create_session_factory, session_scope
from app.domain.enums import Horizon, Timeframe
from app.features.service import FeatureService
from app.instruments.repository import InstrumentRepository
from app.instruments.service import InstrumentService
from app.market_data.benchmarks import (
    BENCHMARK_TIMEFRAMES,
    BENCHMARKS,
    register_benchmarks,
    watchlisted_benchmarks,
)
from app.market_data.calendars import get_trading_calendar
from app.market_data.import_service import MarketDataImportService
from app.market_data.ingest import IngestionService
from app.market_data.integrity import DiscontinuityKind, scan_price_series
from app.market_data.options_service import OptionSnapshotService, within_capture_window
from app.market_data.provider import MarketDataProvider
from app.market_data.registry import build_provider
from app.market_data.repository import CandleRepository
from app.market_data.risk import EXTREME_UNDER_COVERAGE_NOTE, SUPPORTED_HORIZONS, assess
from app.market_data.volatility import MODEL_VERSION, VolatilityRegime
from app.market_data.volatility_service import DISCLAIMER as VOLATILITY_DISCLAIMER
from app.market_data.volatility_service import VolatilityService
from app.market_data.volatility_service import build_payload as build_volatility_payload
from app.notifications.backends.discord import DiscordWebhookNotifier
from app.notifications.dashboard import LIVENESS_NOTE
from app.notifications.demo import lifecycle_events
from app.notifications.feeds import TRENDS_ROUTING_KEY
from app.notifications.policy import evaluate_overview
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.notifications.status_service import StatusService
from app.notifications.summary import build_daily_summary, daily_summary_already_sent
from app.notifications.trends import DISCLAIMER, TrendEvent, TrendSignal, rank
from app.notifications.trends import build_payload as build_trends_payload
from app.notifications.trends_service import TrendsService
from app.notifications.volatility_events import build_section as build_volatility_section
from app.ops.check import operational_status, run_checks
from app.ops.launchd import (
    LAUNCH_AGENTS_DIR,
    ScheduledJob,
    daemon_jobs,
    install_commands,
    scheduled_jobs,
    uninstall_commands,
    write_plists,
)
from app.ownership.service import ensure_local_ownership
from app.paper.demo import run_demo
from app.paper.performance import PerformanceSummary
from app.paper.replay import REPLAY_DISCLAIMER, ReplayError, replay_symbol
from app.portfolio_fit import Portfolio, PortfolioFitService, Position
from app.portfolio_fit.cli import render as render_fit
from app.portfolio_fit.cli import to_json as fit_to_json
from app.research import cli as research_cli
from app.research.phase12 import load_daily
from app.scanner.demo import DemoResult, phase_boundaries, seed_demo_instrument
from app.scanner.enums import SessionPhase
from app.scanner.repository import (
    ScanRunRepository,
    SignalEvaluationRepository,
    TrackedSignalRepository,
    WatchlistRepository,
)
from app.scanner.seed import seed_watchlist
from app.scanner.service import ScanCycleStats, ScannerService
from app.scanner.sessions import describe_phase, session_phase
from app.signals.service import SignalService
from app.simulation.defaults import build_default_profiles
from app.simulation.portfolios import PORTFOLIO_KEYS, build_personal_profiles
from app.simulation.repository import SimulationProfileRepository

logger = get_logger(__name__)


async def _seed(settings: Settings, symbols: list[str] | None, days: int) -> int:
    """Ingest deterministic synthetic candles."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    provider = build_provider(settings)
    try:
        async with session_scope(factory) as session:
            service = IngestionService(session, provider)
            report = await service.sync_all(
                timeframe=Timeframe.D1,
                symbols=symbols,
                start=utc_now() - timedelta(days=days),
            )
        print(f"instruments synced : {report.instruments_synced}")
        print(f"corporate actions  : {report.corporate_actions_written}")
        print(f"candles written    : {report.candles_written}")
        print(f"symbols ok         : {', '.join(report.symbols_succeeded) or '-'}")
        for symbol, error in report.symbols_failed:
            print(f"FAILED {symbol}: {error}", file=sys.stderr)
        return 0 if report.ok else 1
    finally:
        await engine.dispose()


async def _signal(settings: Settings, symbol: str, horizon: Horizon) -> int:
    """Print an explainable signal for one symbol."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    provider = build_provider(settings)
    try:
        async with session_scope(factory) as session:
            instruments = InstrumentService(InstrumentRepository(session))
            features = FeatureService(
                instruments, CandleRepository(session), CorporateActionRepository(session)
            )
            service = SignalService(features, provider, settings)
            result = await service.evaluate(symbol=symbol, timeframe=Timeframe.D1, horizon=horizon)

        print(f"\n{result.symbol}  {result.classification.value}  (score {result.score:+.1f})")
        print(f"  horizon      : {result.horizon.value} ({result.horizon.bucket.value})")
        print(f"  bar          : {result.timestamp.isoformat()}")
        print(f"  confidence   : {result.confidence:.2f}  [internal agreement, not accuracy]")
        print(
            f"  net edge     : {result.net_edge.net_edge_bps:+.1f} bps "
            f"(move {result.net_edge.expected_move_bps:.1f} - cost {result.net_edge.cost_bps:.1f})"
        )
        print(f"  actionable   : {result.is_actionable}")
        print("\n  Reasons:")
        for reason in result.reasons:
            print(f"    + {reason.message}")
        print("\n  Risks:")
        for risk in result.risks or ():
            print(f"    - {risk.message}")
        if not result.risks:
            print("    (none flagged)")
        print()
        return 0
    finally:
        await engine.dispose()


async def _seed_profiles(settings: Settings) -> int:
    """Install the default simulation-profile catalogue."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            repository = SimulationProfileRepository(session)
            profiles = build_default_profiles()
            await repository.upsert_many(profiles)
            risk_count = await repository.count_risk_profiles()

        print(f"simulation profiles : {len(profiles)}")
        print(f"risk profiles       : {risk_count} (shared, not duplicated per portfolio)")
        for profile in profiles:
            print(
                f"  {profile.name:24} capital={profile.initial_capital:>8.0f} "
                f"{profile.currency}  max position={profile.max_position_notional:>8.2f}"
            )
        return 0
    finally:
        await engine.dispose()


async def _demo(settings: Settings) -> int:
    """Run the deterministic paper-trading demo."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            result = await run_demo(session)

        print("\ntradabot paper-trading demo (deterministic)")
        print("=" * 78)
        print(f"one STRONG_BULLISH signal -> {len(result.rows)} portfolios")
        print(
            f"positions opened: {result.positions_opened}   trades closed: {result.trades_closed}"
        )
        print("-" * 78)
        for name, line in result.rows:
            print(f"  {name:22} {line}")
        print("-" * 78)
        print(
            "Synthetic prices validate execution and accounting mechanics only.\n"
            "They say nothing about profitability, signal quality or predictive power."
        )
        print()
        return 0
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
_SYNTHETIC_WARNING = (
    "NOTE: real data proves ingestion correctness, not predictive edge.\n"
    "      A profitable simulation on any data is not evidence the strategy works."
)


async def _market_data_status(settings: Settings) -> int:
    """Report provider configuration and stored-data freshness."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    provider = build_provider(settings)
    try:
        async with session_scope(factory) as session:
            newest = (
                await session.execute(
                    select(Candle.timestamp).order_by(Candle.timestamp.desc()).limit(1)
                )
            ).scalar_one_or_none()
            instruments = len((await session.execute(select(Instrument.id))).scalars().all())

        configured = settings.alpaca.is_configured if provider.name == "alpaca" else True
        print(f"provider          : {provider.name}")
        print(f"configured        : {configured}")
        print(f"feed              : {settings.alpaca.feed if provider.name == 'alpaca' else 'n/a'}")
        print(f"watchlist         : {', '.join(settings.market_data.watchlist)}")
        print(f"instruments stored: {instruments}")
        print(f"newest candle     : {newest.isoformat() if newest else '(none)'}")
        if provider.name == "alpaca" and not configured:
            print("\nAlpaca is selected but has no credentials.")
            print("Set ALPACA_API_KEY and ALPACA_API_SECRET, or use provider=mock.")
            return 1
        return 0
    finally:
        await engine.dispose()


async def _market_data_benchmarks(settings: Settings, *, register: bool) -> int:
    """Show, or create, the market and sector reference instruments.

    Registration writes to ``instruments`` and, for anything it creates, fetches
    that instrument's corporate actions immediately. Phase 9B found out why that
    matters: QQQ and SMH were registered after the one-shot action sync had
    already run, so SMH sat in the database for a whole phase carrying an
    unadjusted 2-for-1 split -- a -49% bar in the semiconductor sector reference.
    Counting stored actions cannot detect that, because ADBE, AMD, BA and BRK.B
    legitimately have none.

    The check that no benchmark has reached the enabled watchlist runs on both
    paths, because the invariant that matters -- the scanner universe is
    untouched -- is worth re-asserting every time someone looks, not only when
    something is written.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    provider = build_provider(settings)
    try:
        async with session_scope(factory) as session:
            if register:
                report = await register_benchmarks(session, provider=provider.name)
                print(f"\n{report.summary()}")
                if report.registered:
                    written = await _sync_actions_for(
                        session, provider, symbols=list(report.registered)
                    )
                    print(f"corporate actions fetched for new instruments: {written}")

            instruments = InstrumentRepository(session)
            candles = CandleRepository(session)

            print(f"\n{'symbol':<8} {'role':<24} {'stored':<8} coverage")
            print("-" * 74)
            missing = 0
            for benchmark in BENCHMARKS:
                instrument = await instruments.get_by_symbol(benchmark.symbol)
                # An alternate has no sector by definition, so falling back to
                # "(whole market)" would label SOXX as the market reference.
                role = benchmark.sector or (
                    "(whole market)" if benchmark.is_market else f"({benchmark.role.value.lower()})"
                )
                if instrument is None:
                    missing += 1
                    print(f"{benchmark.symbol:<8} {role:<24} {'-':<8} not registered")
                    continue
                stored = 0
                for timeframe in BENCHMARK_TIMEFRAMES:
                    count = await candles.count(instrument_id=instrument.id, timeframe=timeframe)
                    if count == 0:
                        continue
                    stored += count
                    first = await candles.earliest_timestamp(
                        instrument_id=instrument.id, timeframe=timeframe
                    )
                    last = await candles.latest_timestamp(
                        instrument_id=instrument.id, timeframe=timeframe
                    )
                    assert first is not None
                    assert last is not None
                    span = f"{first:%Y-%m-%d} -> {last:%Y-%m-%d}"
                    print(
                        f"{benchmark.symbol:<8} {role:<24} {timeframe.value:<8} {count:>9,}  {span}"
                    )
                if stored == 0:
                    print(f"{benchmark.symbol:<8} {role:<24} {'-':<8} registered, no bars")

            leaked = await watchlisted_benchmarks(session)

        print("-" * 74)
        if leaked:
            print(f"\nWATCHLIST LEAK: {', '.join(leaked)} are enabled for scanning.")
            print("Benchmarks are context, not candidates. Disable them before scanning again.")
            return 1
        print("scanner universe: unaffected (no benchmark is on the enabled watchlist)")
        if missing:
            print(f"\n{missing} not registered. Create them with:")
            print("  tradabot market-data benchmarks --register")
            return 1
        return 0
    finally:
        await engine.dispose()


async def _sync_actions_for(
    session: AsyncSession,
    provider: MarketDataProvider,
    *,
    symbols: list[str],
) -> int:
    """Fetch corporate actions for ``symbols`` over the full stored candle span.

    One implementation, two callers: the bulk command and benchmark
    registration. Duplicating the window arithmetic is exactly how the two
    drifted apart in phase 9A, leaving newly registered instruments unfetched.

    Provider failures are contained per symbol -- one unknown ticker must not
    abandon the rest -- and counted by the caller through the returned total.
    """
    oldest = (
        await session.execute(select(Candle.timestamp).order_by(Candle.timestamp).limit(1))
    ).scalar_one_or_none()
    start = (oldest or utc_now()) - timedelta(days=1)
    end = utc_now() + timedelta(days=1)

    service = IngestionService(session, provider)
    written = 0
    for symbol in symbols:
        try:
            written += await service.sync_corporate_actions(symbol, start=start, end=end)
        except (ProviderError, InstrumentNotFoundError) as exc:
            logger.warning("corporate action sync failed", symbol=symbol, error=str(exc))
    return written


async def _risk(settings: Settings, symbols: list[str] | None) -> int:
    """Short-horizon movement risk, read-only and magnitude-only.

    No provider call: every input is a stored candle, so this answers from what
    the sync job already downloaded. The estimate is a **rolling** one -- it
    describes the next one to three sessions from the current information state,
    not the lifetime of any position.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            wanted = symbols or list(await WatchlistRepository(session).symbols())
            snapshot = await VolatilityService(session).for_symbols(wanted)

        estimates = [assess(m) for m in snapshot.estimates]
        estimates.sort(key=lambda r: r.risk_band_1d, reverse=True)

        print("\nshort-horizon movement risk   model risk-v1 / volatility-v1")
        print(
            f"calibrated horizons: {', '.join(f'{h}d' for h in SUPPORTED_HORIZONS)}"
            f"   band coverage 80%\n"
        )
        print(
            f"{'symbol':<8}{'regime':<14}{'pct':>5}{'ATR%':>7}"
            f"{'1d typ':>8}{'1d band':>9}{'1d stress':>11}"
            f"{'3d typ':>8}{'3d band':>9}{'gap':>7}{'data':>7}"
        )
        for r in estimates:
            print(
                f"{r.symbol:<8}{r.regime.value:<14}{r.percentile * 100:>4.0f}"
                f"{r.atr_pct:>7.2f}{r.expected_move_1d:>8.2f}{r.risk_band_1d:>9.2f}"
                f"{r.stress_move_1d:>11.2f}{r.expected_move_3d:>8.2f}"
                f"{r.risk_band_3d:>9.2f}{r.overnight_gap_pct:>7.2f}"
                f"{r.data_quality:>7}"
            )

        if snapshot.symbols_failed:
            print(f"\n{snapshot.symbols_failed} symbol(s) had too little history to estimate.")
        print("\nMagnitude only — not a direction forecast, target or probability of profit.")
        print("Markets can exceed these ranges, particularly through overnight gaps:")
        print("  the gap column is an 80th-percentile overnight move and is part of,")
        print("  not additional to, the 1d band.")
        print(f"\n{EXTREME_UNDER_COVERAGE_NOTE}")
        return 0
    finally:
        await engine.dispose()


async def _options_capture(settings: Settings, *, dry_run: bool, force: bool) -> int:
    """Capture one point-in-time option surface per watchlist symbol.

    Session-aware and idempotent by trading date, so the job can run on a short
    interval and decide for itself whether today is already done. That is what
    makes a retry, a slept machine and a launchd catch-up all converge on
    exactly one snapshot per symbol per session.

    ``--dry-run`` fetches and derives without writing, which is the honest way
    to verify the pipeline outside a session: it proves the surface can be
    built without fabricating a snapshot dated to a day the market never
    traded.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    provider = build_provider(settings)
    now = utc_now()

    calendar = get_trading_calendar(settings.market_data.default_exchange)
    phase = session_phase(calendar, now)

    try:
        async with session_scope(factory) as session:
            symbols = list(await WatchlistRepository(session).symbols())

            if not dry_run:
                if phase is not SessionPhase.REGULAR:
                    print(f"\nno capture: session is {phase.value}, not REGULAR")
                    return 0
                if not within_capture_window(now) and not force:
                    print(f"\nno capture: {now:%H:%M} UTC is outside the capture window")
                    return 0

            service = OptionSnapshotService(session, provider)
            run = await service.capture(symbols, now=now, persist=not dry_run, force=force)

        print(f"\nmode            : {'DRY RUN (nothing written)' if dry_run else 'CAPTURE'}")
        print(f"session         : {phase.value}")
        print(f"captured_at     : {run.captured_at.isoformat()}")
        feed = getattr(provider, "options_feed", "none")
        print(f"provider / feed : {provider.name} / {feed}")
        print(
            f"symbols         : {run.symbols_captured}/{run.symbols_requested} captured, "
            f"{run.symbols_skipped_existing} already done today"
        )
        print(f"contracts       : {run.contracts_scanned:,} scanned, {run.contracts_stored:,} kept")
        print(f"summaries       : {run.summaries_stored}")
        print(f"no usable IV    : {run.symbols_without_iv}")
        print(f"duration        : {run.duration_seconds:.1f}s")
        print(f"quality         : {run.quality.describe()}")
        for symbol, error in run.failures[:10]:
            print(f"  FAILED {symbol}: {error}", file=sys.stderr)
        return 0 if not run.failures else 1
    finally:
        await engine.dispose()


async def _market_data_verify_adjustments(settings: Settings, symbols: list[str] | None) -> int:
    """Report price discontinuities the stored corporate actions do not explain.

    The check that would have caught SMH. Counting stored actions could not:
    ADBE, AMD, BA and BRK.B legitimately have none, so "zero actions" carries no
    information. Only the prices can say whether a 50% overnight move had a
    reason, and this asks them.

    Exits non-zero on UNEXPLAINED or CONTRADICTED findings so a scheduled job or
    CI step can gate on it. MARKET_GAP findings are printed but never fail the
    run -- a real 20% move is the data working, and treating it as a fault is
    how someone ends up suppressing genuine market behaviour.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            report = await scan_price_series(session, symbols=symbols or None)

        print(f"\nscanned {report.instruments_scanned} instruments, {report.bars_scanned:,} bars")

        for kind in (
            DiscontinuityKind.UNEXPLAINED,
            DiscontinuityKind.CONTRADICTED,
            DiscontinuityKind.EXPLAINED,
            DiscontinuityKind.MARKET_GAP,
        ):
            findings = report.of(kind)
            print(f"\n{kind.value}: {len(findings)}")
            for finding in findings[:40]:
                print(f"  {finding.describe()}")
            if len(findings) > 40:  # noqa: PLR2004
                print(f"  ... and {len(findings) - 40} more")

        if report.healthy:
            print("\nEvery split-shaped discontinuity has a corroborating corporate action.")
            return 0
        print("\nUnexplained or contradicted discontinuities found.")
        print("Fetch actions with: tradabot market-data corporate-actions <symbols>")
        return 1
    finally:
        await engine.dispose()


async def _market_data_corporate_actions(settings: Settings, symbols: list[str] | None) -> int:
    """Fetch and store corporate actions for every stored instrument.

    The gap this closes: ``IngestionService.sync_all`` fetches actions, but the
    two paths that actually built this database -- ``history`` (backfill) and
    ``scanner sync`` -- both go through ``MarketDataImportService``, which does
    not. So the candle table grew to millions of raw bars against two stored
    dividends and no splits at all, and every consumer that reads candles
    without the adjustment layer saw AAPL fall 74% on 2020-08-31.

    The query window is the span of stored candles, widened by a day at each
    end. Alpaca's endpoint defaults to about the current month, so an unbounded
    call returns almost nothing and reports success -- which is how this
    database reached 2.8 million bars holding two dividends and no splits.

    Idempotent, and safe to re-run: actions upsert on their natural key.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    provider = build_provider(settings)
    try:
        async with session_scope(factory) as session:
            instruments = InstrumentRepository(session)
            targets = (
                symbols
                if symbols
                else [i.symbol for i in await instruments.list_all(active_only=False, limit=1000)]
            )

            written = await _sync_actions_for(session, provider, symbols=targets)

        print(f"\nsymbols queried : {len(targets)}")
        print(f"actions written : {written}")
        print("\nSplits are applied on read, never written back into `candles`.")
        print("Re-run after adding instruments, or use `verify-adjustments` to check.")
        return 0
    finally:
        await engine.dispose()


async def _market_data_import(
    settings: Settings,
    symbols: list[str],
    start: datetime,
    end: datetime,
    timeframe: Timeframe,
) -> int:
    """Import an explicit historical window."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    provider = build_provider(settings)
    try:
        async with session_scope(factory) as session:
            service = MarketDataImportService(session, provider)
            await service.ensure_instruments(symbols)
            reports = [
                await service.import_symbol(
                    symbol=symbol, start=start, end=end, timeframe=timeframe
                )
                for symbol in symbols
            ]

        print(f"\nprovider: {provider.name}   timeframe: {timeframe.value}")
        print(f"window  : {start.isoformat()} -> {end.isoformat()}")
        print("-" * 78)
        failed = 0
        for report in reports:
            print(f"  {report.summary()}")
            for gap in report.gaps[:3]:
                print(f"      gap: {gap.describe()}")
            failed += 0 if report.ok else 1
        print("-" * 78)
        print(_SYNTHETIC_WARNING)
        return 0 if failed == 0 else 1
    finally:
        await engine.dispose()


async def _market_data_sync(settings: Settings, symbols: list[str] | None) -> int:
    """Bring symbols up to date from their newest stored bar."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    provider = build_provider(settings)
    watchlist = symbols or list(settings.market_data.watchlist)
    try:
        async with session_scope(factory) as session:
            service = MarketDataImportService(session, provider)
            await service.ensure_instruments(watchlist)
            report = await service.sync_watchlist(watchlist)

        print(f"\nprovider: {report.provider}   symbols: {len(report.reports)}")
        print("-" * 78)
        for item in report.reports:
            print(f"  {item.summary()}")
        print("-" * 78)
        print(f"inserted: {report.total_inserted} bars   failed: {len(report.symbols_failed)}")
        return 0 if report.ok else 1
    finally:
        await engine.dispose()


async def _market_data_quote(settings: Settings, symbol: str) -> int:
    """Fetch and print one latest quote."""
    provider = build_provider(settings)
    quote = await provider.get_latest_quote(symbol)
    age = (utc_now() - quote.timestamp).total_seconds()
    print(f"\n{quote.symbol}  ({provider.name})")
    print(f"  bid      : {quote.bid}  x {quote.bid_size or '-'}")
    print(f"  ask      : {quote.ask}  x {quote.ask_size or '-'}")
    print(f"  mid      : {quote.mid_price}")
    print(f"  spread   : {quote.spread_bps:.2f} bps")
    print(f"  timestamp: {quote.timestamp.isoformat()}  ({age:.0f}s ago)")
    print(
        "\n  This is MARKET DATA, not a broker execution quote. A retail broker's\n"
        "  spread differs -- see docs/market-data.md."
    )
    return 0


async def _simulate(
    settings: Settings,
    symbol: str,
    start: datetime,
    end: datetime,
    timeframe: Timeframe,
    horizon: Horizon,
) -> int:
    """Replay stored real candles through our own PaperBroker.

    Reads what is already imported. It does not fetch, so the same command over
    the same window gives the same answer.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    provider = build_provider(settings)
    try:
        async with session_scope(factory) as session:
            await SimulationProfileRepository(session).upsert_many(build_default_profiles())
            await session.flush()
            try:
                result = await replay_symbol(
                    session,
                    settings=settings,
                    provider=provider,
                    symbol=symbol,
                    start=start,
                    end=end,
                    timeframe=timeframe,
                    horizon=horizon,
                )
            except ReplayError as exc:
                print(f"\ncannot replay: {exc}")
                return 1

        print(f"\n{result.symbol}  {result.timeframe.value}  horizon {horizon.value}")
        print(
            f"  window    : {result.first_bar.isoformat() if result.first_bar else '-'} -> "
            f"{result.last_bar.isoformat() if result.last_bar else '-'}"
        )
        print(f"  bars      : {result.bars_replayed}  (warm-up skipped {result.warmup_skipped})")
        print(
            f"  signals   : {result.signals_evaluated} scored, "
            f"{result.signals_actionable} actionable"
        )
        print(f"  positions : {result.positions_opened} opened, {result.trades_closed} closed")
        print("-" * 78)
        for summary in result.summaries:
            print(f"  {summary.profile_name:<22} {_format_summary(summary)}")
        print("-" * 78)
        print(REPLAY_DISCLAIMER)
        print()
        return 0
    finally:
        await engine.dispose()


def _format_summary(summary: PerformanceSummary) -> str:
    return (
        f"equity={summary.ending_equity:>10.2f}  "
        f"net={summary.net_pnl:>+8.2f} ({summary.return_pct:>+6.2f}%)  "
        f"trades={summary.trade_count} open={summary.open_position_count}  "
        f"costs={summary.total_costs:>6.2f}  "
        f"dd={summary.max_drawdown:>7.2%}"
    )


# ---------------------------------------------------------------------------
# Portfolios and operations
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"


async def _portfolios_seed(settings: Settings) -> int:
    """Install the three personal paper portfolios and the local owner."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            repository = SimulationProfileRepository(session)
            await repository.upsert_many(build_default_profiles())
            personal = build_personal_profiles()
            await repository.upsert_many(personal)
            await session.flush()
            report = await ensure_local_ownership(session, settings)

        print(f"\n{report.summary()}")
        print("-" * 74)
        for profile in personal:
            print(
                f"  {profile.name:<12} {profile.initial_capital:>8.0f} {profile.currency}  "
                f"risk={profile.risk.name:<9} channel={profile.notification_channel}"
            )
        print("-" * 74)
        print("Experimental simulation configurations, NOT financial recommendations.")
        return 0
    finally:
        await engine.dispose()


async def _portfolios_list(settings: Settings) -> int:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            status = await operational_status(session, settings)
        if not status.portfolios:
            print("\nNo personal portfolios. Run: tradabot portfolios seed")
            return 1
        print(f"\n{'PORTFOLIO':<14}{'EQUITY':>12}{'OPEN':>7}{'CLOSED':>8}")
        print("-" * 42)
        for portfolio in status.portfolios:
            print(
                f"{portfolio.key:<14}{portfolio.equity:>12.2f}"
                f"{portfolio.open_positions:>7}{portfolio.closed_trades:>8}"
            )
        return 0
    finally:
        await engine.dispose()


async def _ops_check(settings: Settings) -> int:
    """Validate that this installation can run unattended."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            report = await run_checks(session, settings, project_root=PROJECT_ROOT, log_dir=LOG_DIR)
        print("\ntradabot operations check")
        print("=" * 74)
        print(report.render())
        if not report.ok:
            print("\nFix the FAIL items before installing the scheduler.")
        return 0 if report.ok else 1
    finally:
        await engine.dispose()


async def _ops_status(settings: Settings) -> int:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            status = await operational_status(session, settings)

        def when(moment: object) -> str:
            return moment.isoformat() if isinstance(moment, datetime) else "(never)"

        def secs(value: float | None) -> str:
            return f"{value:.1f}s" if value is not None else "-"

        print(f"\nchecked at        : {status.checked_at.isoformat()}")
        print(f"session           : {status.session_phase}")
        print(f"universe          : {status.universe_size} symbols defined")
        print(f"watchlist         : {status.watchlist_size} enabled")
        print(
            f"last sync         : {secs(status.last_sync_duration)}  "
            f"{status.last_sync_symbols} synced, {status.last_sync_failures} failed"
        )
        print(
            f"last scan detail  : {secs(status.last_scan_duration)}  "
            f"{status.last_scan_evaluated} evaluated, "
            f"{status.last_scan_qualified} qualified, {status.last_scan_strong} strong"
        )
        print(f"evaluations kept  : {status.evaluations_stored}")
        print(f"last market sync  : {when(status.last_sync)}")
        print(f"last scan         : {when(status.last_scan)}  ({status.last_scan_status or '-'})")
        print(f"last scan success : {when(status.last_success)}")
        print(f"last error        : {status.last_error or '-'}")
        print(f"notify last ok    : {when(status.last_notification_success)}")
        print(f"notify last fail  : {when(status.last_notification_failure)}")
        print()
        print(f"{'PORTFOLIO':<14}{'EQUITY':>12}{'OPEN':>7}{'CLOSED':>8}")
        for portfolio in status.portfolios:
            print(
                f"{portfolio.key:<14}{portfolio.equity:>12.2f}"
                f"{portfolio.open_positions:>7}{portfolio.closed_trades:>8}"
            )
        if not status.portfolios:
            print("(no personal portfolios; run `tradabot portfolios seed`)")
        return 0
    finally:
        await engine.dispose()


def _ops_jobs(settings: Settings) -> tuple[ScheduledJob, ...]:
    return scheduled_jobs(
        scan_minutes=settings.scanner.scan_interval_minutes,
        sync_minutes=settings.scanner.market_sync_interval_minutes,
        overview_minutes=settings.scanner.overview_interval_minutes,
        trends_minutes=settings.scanner.trends_interval_minutes,
        status_minutes=settings.scanner.status_interval_minutes,
    )


def _ops_install(settings: Settings) -> int:
    """Write launchd plists and print the commands to activate them.

    Writes files; **loads nothing**. Starting a schedule on someone's machine
    should be a deliberate keystroke, not a side effect of a build step.
    """
    jobs = _ops_jobs(settings)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    written = write_plists(
        jobs,
        project_root=PROJECT_ROOT,
        python_path=Path(sys.executable),
        log_dir=LOG_DIR,
        target_dir=LAUNCH_AGENTS_DIR,
    )
    print(f"\nwrote {len(written)} LaunchAgent templates to {LAUNCH_AGENTS_DIR}")
    for job in jobs:
        print(f"  {job.label:<28} every {job.interval_seconds // 60:>3} min  {job.description}")
    print("\nNothing is running yet. To activate:")
    for command in install_commands(jobs, target_dir=LAUNCH_AGENTS_DIR):
        print(f"  {command}")
    print("\nOr: make ops-start")

    # The bot is a daemon, not a scheduled job, so it is written and offered
    # separately. Loading it starts a process that stays running.
    daemons = daemon_jobs()
    write_plists(
        daemons,
        project_root=PROJECT_ROOT,
        python_path=Path(sys.executable),
        log_dir=LOG_DIR,
        target_dir=LAUNCH_AGENTS_DIR,
    )
    print(f"\nwrote {len(daemons)} long-running agent template(s):")
    for job in daemons:
        print(f"  {job.label:<28} restart throttle {job.interval_seconds}s  {job.description}")
    print("\nThe interactive bot is not started either. To activate it:")
    for command in install_commands(daemons, target_dir=LAUNCH_AGENTS_DIR):
        print(f"  {command}")
    return 0


def _ops_uninstall(settings: Settings) -> int:
    jobs = _ops_jobs(settings)
    print("\nTo stop and remove the scheduled jobs:")
    for command in uninstall_commands(jobs, target_dir=LAUNCH_AGENTS_DIR):
        print(f"  {command}")
    for job in jobs:
        print(f"  rm -f {LAUNCH_AGENTS_DIR / job.plist_name}")
    print("\nNothing was removed automatically; uninstalling is your keystroke too.")
    return 0


async def _ops_daily_summary_if_due(settings: Settings) -> int:
    """Send the daily report once, after the session has closed.

    Session-aware rather than pinned to a wall-clock hour: a fixed local time
    bakes in a timezone and slips by an hour twice a year with US daylight
    saving, which shows up as a missing report rather than an error.

    Idempotent for the day -- a scheduler firing hourly must not produce hourly
    reports.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            status = await operational_status(session, settings)
            if not status.session_closed:
                print("\nsession has not closed yet; no report sent")
                return 0
            if await daily_summary_already_sent(session, settings):
                print("\ndaily report already sent for this session")
                return 0
            payload = await build_daily_summary(session)

        service = NotificationService(settings, session_factory=factory)
        await service.publish(Event.daily_summary(payload))
        print(f"\ndaily report sent ({len(payload.get('portfolios', []))} portfolios)")
        return 0
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Watchlist and scanner
# ---------------------------------------------------------------------------
async def _watchlist_list(settings: Settings) -> int:
    """Print the watchlist, enabled and disabled."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            entries = await WatchlistRepository(session).list_entries(enabled_only=False)

        if not entries:
            print("\nWatchlist is empty. Seed it with: tradabot watchlist seed")
            return 0

        print(f"\n{len(entries)} entries")
        print("-" * 62)
        for entry, instrument in entries:
            mark = "on " if entry.enabled else "off"
            tags = ",".join(entry.tags) if entry.tags else "-"
            print(f"  [{mark}] {instrument.symbol:<8} prio={entry.priority:<3} {tags}")
        enabled = sum(1 for e, _ in entries if e.enabled)
        print("-" * 62)
        print(f"enabled: {enabled}   disabled: {len(entries) - enabled}")
        return 0
    finally:
        await engine.dispose()


async def _watchlist_seed(settings: Settings) -> int:
    """Seed the initial development universe."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    provider = build_provider(settings)
    try:
        async with session_scope(factory) as session:
            report = await seed_watchlist(session, provider)

        print(f"\nprovider: {provider.name}")
        print(report.summary())
        if report.missing:
            print("\nThese are not in the provider's universe.")
            if provider.name == "alpaca":
                print("Add them to TRADABOT_MARKET_DATA__WATCHLIST, then re-run.")
            else:
                print(f"The {provider.name} provider serves a fixed set of symbols.")
        print("\nInstrument inclusion is NOT an investment recommendation.")
        return 0
    finally:
        await engine.dispose()


async def _watchlist_add(settings: Settings, symbol: str) -> int:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    provider = build_provider(settings)
    try:
        async with session_scope(factory) as session:
            instruments = InstrumentRepository(session)
            instrument = await instruments.get_by_symbol(symbol)
            if instrument is None:
                infos = [i for i in await provider.get_instruments() if i.symbol.upper() == symbol]
                if not infos:
                    print(f"\n{symbol} is not available from provider '{provider.name}'.")
                    return 1
                await instruments.upsert_many(infos, provider=provider.name)
                await session.flush()
                instrument = await instruments.get_by_symbol(symbol)
            assert instrument is not None
            await WatchlistRepository(session).add(instrument.id)
        print(f"\n{symbol} added to the watchlist.")
        return 0
    finally:
        await engine.dispose()


async def _watchlist_set_enabled(settings: Settings, symbol: str, *, enabled: bool) -> int:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            found = await WatchlistRepository(session).set_enabled(symbol, enabled=enabled)
        if not found:
            print(f"\n{symbol} is not on the watchlist.")
            return 1
        print(f"\n{symbol} {'enabled' if enabled else 'disabled'}.")
        return 0
    finally:
        await engine.dispose()


def _build_scanner(settings: Settings, factory: object) -> ScannerService:
    return ScannerService(
        factory,  # type: ignore[arg-type]
        settings=settings,
        provider=build_provider(settings),
        notifications=NotificationService(settings, session_factory=factory),  # type: ignore[arg-type]
    )


def _print_stats(stats: ScanCycleStats) -> None:
    print(f"\nsession    : {stats.session_phase.value}")
    if stats.skipped_reason:
        print(f"skipped    : {stats.skipped_reason}")
    print(
        f"symbols    : {stats.symbols_total} total, {stats.symbols_evaluated} evaluated, "
        f"{stats.symbols_failed} failed"
    )
    print(f"qualified  : {stats.signals_qualified}   strong: {stats.signals_strong}")
    print(f"hit rate   : {stats.hit_rate:.1%}   <- the base rate; high means unselective")
    print(f"paper      : {stats.paper_decisions} decisions, {stats.positions_opened} opened")
    print(f"duration   : {stats.duration_seconds:.1f}s")
    for symbol, error in stats.failures[:5]:
        print(f"  FAILED {symbol}: {error}")


async def _scanner_sync(settings: Settings) -> int:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        stats = await _build_scanner(settings, factory).sync_market_data()
        print(
            f"\nsynced {stats.symbols_synced}/{stats.symbols_total} symbols "
            f"in {stats.duration_seconds:.1f}s"
        )
        for symbol, error in stats.failures[:5]:
            print(f"  FAILED {symbol}: {error}")
        return 0 if stats.symbols_failed == 0 else 1
    finally:
        await engine.dispose()


async def _scanner_run_once(settings: Settings, *, paper: bool) -> int:
    """One scan cycle against the configured provider."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        stats = await _build_scanner(settings, factory).run_scan_cycle(with_paper_trading=paper)
        _print_stats(stats)
        print(
            "\nZero qualified signals is a valid result. Thresholds control "
            "notification volume,\nnot data collection -- every evaluation was stored."
        )
        return 0
    finally:
        await engine.dispose()


async def _scanner_candidates(settings: Settings, limit: int) -> int:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        candidates = await _build_scanner(settings, factory).top_candidates(limit)
        if not candidates:
            print("\nNo candidates currently meet the configured threshold.")
            return 0
        print(f"\n{len(candidates)} candidates")
        print("-" * 78)
        for index, candidate in enumerate(candidates, start=1):
            print(f"  {index}. {candidate.explain()}")
        return 0
    finally:
        await engine.dispose()


async def _notifications_demo_lifecycle(settings: Settings) -> int:
    """Send one clearly-marked synthetic message to every destination.

    **Only ever runs when a human types it.** It is not scheduled, not invoked by
    any test, and writes nothing: no evaluation, no tracked signal, no position,
    no trade. Every message is prefixed TEST and uses a fake ticker, so nothing
    it produces can be mistaken later for a real opportunity.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        service = NotificationService(settings, session_factory=factory)
        pairs = lifecycle_events(settings)
        print(f"sending {len(pairs)} clearly-marked TEST messages")
        delivered = 0
        for destination, event in pairs:
            ok = await service.publish(event)
            delivered += 1 if ok else 0
            print(f"  {'ok ' if ok else 'FAIL'} {destination:<12} {event.type.value}")
        print(f"{delivered}/{len(pairs)} delivered")
        print("no evaluation, position, trade or research row was written")
        return 0 if delivered == len(pairs) else 1
    finally:
        await engine.dispose()


async def _refresh_identity(settings: Settings) -> int:
    """Replace placeholder instrument metadata with the provider's own.

    Read-only: it calls Alpaca's asset endpoint and nothing else. No order is
    placed, and none can be -- see `AlpacaMarketDataProvider.get_asset_metadata`.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        provider = build_provider(settings)
        async with session_scope(factory) as session:
            symbols = await WatchlistRepository(session).symbols()
            service = MarketDataImportService(session, provider)
            report = await service.refresh_identity(symbols)

        if not report.ok:
            print(f"identity refresh failed: {report.error}")
            return 1
        print(report.summary())
        for change, affected in sorted(report.exchange_changes.items()):
            print(f"  {change}: {len(affected)} -- {', '.join(sorted(affected)[:8])}")
        if report.unresolved:
            print(f"  unresolved: {', '.join(report.unresolved[:10])}")
        return 0
    finally:
        await engine.dispose()


async def _scanner_overview(settings: Settings) -> int:
    """Send the ranked overview to the market channel -- when there is one.

    This job runs hourly from launchd. It used to publish unconditionally, which
    meant "No qualified opportunities." every hour of every weekend. Zero
    candidates is the normal state (385 qualified out of 116,844 observations in
    the phase-5.5 benchmark), so the decision of whether to speak belongs in the
    notification policy alongside every other suppression rule.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        scanner = _build_scanner(settings, factory)
        candidates = await scanner.top_candidates()

        calendar = get_trading_calendar(settings.market_data.default_exchange)
        now = utc_now()
        decision = evaluate_overview(
            candidate_count=len(candidates),
            session=session_phase(calendar, now),
            require_regular_session=settings.scanner.require_regular_session,
        )
        if not decision.should_publish:
            print(f"overview suppressed: {decision.reason}")
            print("(silence here is healthy -- see docs/discord.md)")
            return 0

        service = NotificationService(settings, session_factory=factory)
        await service.publish(
            Event(
                type=EventType.MARKET_OVERVIEW,
                occurred_at=now,
                payload={
                    "candidates": [
                        {
                            "symbol": c.symbol,
                            "score": c.score,
                            "direction": "bullish" if c.direction > 0 else "bearish",
                            "horizon": c.horizon,
                            "confidence": c.confidence,
                        }
                        for c in candidates
                    ]
                },
            )
        )
        print(f"overview sent with {len(candidates)} candidates")
        return 0
    finally:
        await engine.dispose()


async def _scanner_trends(settings: Settings, *, preview: bool, test: bool) -> int:
    """Evaluate #market-trends from stored scan data and publish what is new.

    Reads the evaluations the scanner already persisted -- **no provider call**.
    Zero notable events is the normal outcome and sends nothing; see
    :mod:`app.notifications.trends` for why silence is the design.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        if test:
            return await _trends_test(settings, factory)

        notifications = None if preview else NotificationService(settings, session_factory=factory)
        service = TrendsService(factory, settings=settings, notifications=notifications)

        if preview:
            run = await service.evaluate()
            print("\n--- PREVIEW: nothing was sent to Discord ---")
            print(f"session    : {run.session}")
            if run.skipped_reason:
                print(f"suppressed : {run.skipped_reason}")
                return 0
            print(f"symbols    : {run.symbols_considered} considered")
            print(f"notable    : {run.events_detected} event(s)")
            print(
                f"volatility : {run.volatility_evaluated} evaluated, "
                f"{run.volatility_elevated} elevated, "
                f"{len(run.volatility_events)} regime transition(s)"
            )
            if not run.signals and not run.volatility_events:
                print("\nNothing notable. No message would be sent -- that is the healthy state.")
                return 0
            if not run.signals:
                _print_volatility_preview(run)
                return 0
            print("\nWould send:")
            for index, signal in enumerate(rank(run.signals), start=1):
                detail = f"   ({signal.detail})" if signal.detail else ""
                print(f"  {index}. {signal.symbol:<6} {signal.headline}{detail}")
            print(f"\n{DISCLAIMER}")
            _print_volatility_preview(run)
            print("\n(Cooldown is not applied in a preview; a live run may send fewer.)")
            return 0

        run = await service.publish()
        print(f"\ntrends: {run.summary()}")
        return 0
    finally:
        await engine.dispose()


def _print_volatility_preview(run: object) -> None:
    """The volatility section of the preview, if this cycle has one."""
    events = getattr(run, "volatility_events", [])
    if not events:
        return
    section = build_volatility_section(
        events, elevated_total=getattr(run, "volatility_elevated", 0)
    )
    print(f"\n{section['title']}")
    for line in section["lines"]:
        print("  " + str(line).replace("\n", "\n  "))
    print(f"\n{section['disclaimer']}")


async def _trends_test(settings: Settings, factory: object) -> int:
    """Send one clearly-marked TEST message to the trends webhook. **Manual only.**

    Constructed observations with fictional symbols, so nothing here can be read
    as a claim about a real instrument. Writes no state: the cooldown is
    untouched, so a test cannot silence a real observation.
    """
    if not settings.discord.webhook_for_portfolio(TRENDS_ROUTING_KEY):
        print("\nno trends webhook configured; set TRADABOT_DISCORD__TRENDS_WEBHOOK")
        return 1

    service = NotificationService(settings, session_factory=factory)  # type: ignore[arg-type]
    payload = build_trends_payload(
        [
            TrendSignal(
                symbol="TEST-A",
                event=TrendEvent.STRONG_MOVE_UP,
                value=4.2,
                headline="+4.2% today",
                detail="5d +8.1%",
            ),
            TrendSignal(
                symbol="TEST-B",
                event=TrendEvent.VOLUME_SPIKE,
                value=2.4,
                headline="volume 2.4x average",
            ),
        ],
        context={"test": True},
    )
    payload["title"] = "🧪 TEST — MARKET ACTIVITY"
    # Exercises the volatility rendering path with constructed symbols. Writes no
    # regime state, so a test cannot silence a real transition.
    payload["volatility"] = {
        "title": "📊 TEST — EXPECTED MOVEMENT",
        "lines": [
            "**TEST-C** — EXTREME expected movement\n   "
            "94th pct · typical session ~2.7% · stress ~5.8%",
            "**TEST-D** — HIGH expected movement\n   "
            "78th pct · typical session ~2.3% · stress ~4.8%",
            "+ 2 more symbol(s) changed volatility state",
        ],
        "disclaimer": VOLATILITY_DISCLAIMER,
        "model": MODEL_VERSION,
    }
    delivered = await service.publish(
        Event(
            type=EventType.MARKET_TRENDS,
            occurred_at=utc_now(),
            payload=payload,
            key="trends:test",
            routing_key=TRENDS_ROUTING_KEY,
        )
    )
    print(
        f"\ntrends TEST message {'delivered' if delivered else 'NOT delivered'} to #market-trends"
    )
    print("Constructed symbols, no state written, no order placed.")
    return 0 if delivered else 1


async def _ops_status_publish(settings: Settings, *, preview: bool, test: bool) -> int:
    """Refresh the persistent #status dashboard.

    Edits one message rather than posting a new one; publishes on a real change
    or on the heartbeat, and stays quiet otherwise.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        notifier = None
        if not preview and settings.discord.enabled and settings.discord.is_configured:
            notifier = DiscordWebhookNotifier(
                settings.discord, max_characters=settings.notifications.max_message_characters
            )
        service = StatusService(factory, settings=settings, notifier=notifier)

        if preview:
            fields = await service.render()
            print("\n--- PREVIEW: nothing was sent to Discord ---")
            for name, value in fields.items():
                print(f"  {name:<16} {value}")
            print(f"\n{LIVENESS_NOTE}")
            return 0

        run = await service.publish(force=test)
        if test:
            print("\n(TEST: forced a refresh of the existing dashboard message.)")
        if run.error:
            print(f"\nstatus dashboard not published: {run.error}")
            return 1
        verb = "created" if run.created else "updated" if run.published else "unchanged"
        print(f"\nstatus dashboard {verb} ({run.reason})")
        return 0
    finally:
        await engine.dispose()


async def _volatility(settings: Settings, *, symbol: str | None, preview: bool) -> int:
    """Expected movement per symbol. **Read-only, magnitude only.**

    Makes no provider call: every input is a stored candle, exactly as the
    scheduled trends job does. Prints no direction, target or price, because
    phases 6-8 found no evidence supporting any of them.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            symbols = [symbol.upper()] if symbol else await WatchlistRepository(session).symbols()
            snapshot = await VolatilityService(session).for_symbols(symbols)

        if not snapshot.estimates:
            print(f"\nno estimate available for {', '.join(symbols)}")
            print("(a symbol needs enough stored hourly history to rank against)")
            return 1

        if preview:
            payload = build_volatility_payload(snapshot)
            print("\n--- PREVIEW: nothing was sent to Discord ---")
            print(f"  {payload['title']}")
            if not payload["lines"]:
                print("\n  No symbol is in an elevated volatility regime right now.")
                print("  Silence is the healthy state -- see docs/volatility.md.")
            for line in payload["lines"]:
                print("  " + line.replace("\n", "\n  "))
            print(f"\n  {payload['disclaimer']}")
            print(f"  model={payload['model']}  evaluated={payload['evaluated']}")
            return 0

        counts = snapshot.by_regime
        print(f"\nmodel      : {MODEL_VERSION}")
        print(f"evaluated  : {len(snapshot.estimates)}/{snapshot.symbols_requested}")
        print(
            "regimes    : "
            + "  ".join(f"{regime.value}={counts[regime]}" for regime in VolatilityRegime)
        )
        stale = snapshot.stale()
        if stale:
            print(f"stale      : {len(stale)} symbol(s) beyond the freshness window")
        print()
        header = f"  {'SYMBOL':<8}{'REGIME':<14}{'PCT':>5}{'TYPICAL':>9}{'STRESS':>8}"
        print(header + f"{'RECENT':>8}{'ATR%':>7}{'AGE':>7}")
        print("  " + "-" * (len(header) + 22))
        for item in sorted(snapshot.estimates, key=lambda e: e.percentile, reverse=True):
            age = int(item.data_age().total_seconds() // 60)
            flag = " stale" if item.is_stale() else ""
            print(
                f"  {item.symbol:<8}{item.regime.value:<14}"
                f"{item.percentile * 100:>4.0f}%{item.typical_range_pct:>8.1f}%"
                f"{item.stress_range_pct:>7.1f}%{item.recent_range_pct:>7.1f}%"
                f"{item.atr_pct:>6.2f}%{age:>6}m{flag}"
            )
        print(f"\n{VOLATILITY_DISCLAIMER}")
        return 0
    finally:
        await engine.dispose()


async def _scanner_demo(settings: Settings) -> int:
    """Deterministic end-to-end scanner demonstration.

    Mock data, console notifier, constructed prices. No Discord, no Alpaca, no
    network. Scans at each phase boundary so the lifecycle progression is
    reproducible rather than dependent on where a scan happens to land.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    # Console notifier, and the session gate lifted. The demo exercises execution
    # *mechanics* with constructed prices; requiring a live market session would
    # make it pass during the day and silently do nothing in the evening, which
    # is the opposite of deterministic. Real scans keep the gate.
    demo_settings = settings.model_copy(
        update={
            "notifications": settings.notifications.model_copy(update={"console": True}),
            "scanner": settings.scanner.model_copy(update={"require_regular_session": False}),
        }
    )
    try:
        now = utc_now()
        async with session_scope(factory) as session:
            await SimulationProfileRepository(session).upsert_many(build_default_profiles())

        scanner = ScannerService(
            factory,
            settings=demo_settings,
            provider=build_provider(demo_settings),
            notifications=NotificationService(demo_settings, session_factory=factory),
        )

        print("\ntradabot scanner demo (deterministic, constructed prices)")
        print("=" * 78)
        result = DemoResult()

        for name, index, note in phase_boundaries():
            # Reveal more of the price path, then scan at the same instant. See
            # seed_demo_instrument for why the data advances and the clock does not.
            async with session_scope(factory) as session:
                await seed_demo_instrument(session, now=now, bars=index)
            stats = await scanner.run_scan_cycle(as_of=now, with_paper_trading=True)
            result.cycles += 1
            result.evaluations += stats.symbols_evaluated
            result.paper_decisions += stats.paper_decisions
            result.positions_opened += stats.positions_opened

            async with session_scope(factory) as session:
                signals = await TrackedSignalRepository(session).active_signals()
                states = [(s.lifecycle, s.current_score) for s in signals]
            state = states[0] if states else ("(none)", 0.0)
            result.transitions.append((name, state[0], state[1]))
            print(f"  {name:<14} score={state[1]:>6.1f}  lifecycle={state[0]:<12} {note}")

        print("-" * 78)
        print(result.describe())
        print("-" * 78)
        print(
            "Constructed prices exercise the scan, lifecycle and paper machinery.\n"
            "They are not a market and say nothing about profitability or edge."
        )
        return 0
    finally:
        await engine.dispose()


async def _scanner_status(settings: Settings) -> int:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            watchlist = await WatchlistRepository(session).count()
            runs = ScanRunRepository(session)
            latest = await runs.latest()
            success = await runs.latest_successful()
            signals = TrackedSignalRepository(session)
            qualified = await signals.qualified_signals()
            evaluations = await SignalEvaluationRepository(session).count()

        calendar = get_trading_calendar(settings.market_data.default_exchange)
        now = utc_now()
        print(f"\nenabled          : {settings.scanner.enabled}")
        print(f"watchlist        : {watchlist} enabled")
        print(f"session          : {describe_phase(session_phase(calendar, now), now)}")
        print(
            f"scan interval    : {settings.scanner.scan_interval_minutes} min (external scheduler)"
        )
        print(f"last scan        : {latest.started_at.isoformat() if latest else '(never)'}")
        print(f"last status      : {latest.status if latest else '-'}")
        duration = (
            f"{latest.duration_seconds:.1f}s"
            if latest and latest.duration_seconds is not None
            else "-"
        )
        print(f"last duration    : {duration}")
        print(f"last success     : {success.started_at.isoformat() if success else '(never)'}")
        print(f"last error       : {latest.error if latest and latest.error else '-'}")
        print(f"qualified signals: {len(qualified)}")
        print(f"evaluations kept : {evaluations}")
        return 0
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
async def _notifications_test(settings: Settings, category: str | None) -> int:
    """Send a clearly-labelled test message to each configured channel.

    Sends real messages to real channels, so it says which ones before doing it.
    The payload contains a channel name and an environment name -- nothing that
    is a credential.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        service = NotificationService(settings, session_factory=factory)
        if not service.enabled:
            print("\nNotifications are disabled or no backend is configured.")
            print("Set TRADABOT_DISCORD__ENABLED=true and at least one webhook,")
            print("or TRADABOT_NOTIFICATIONS__CONSOLE=true to print them locally.")
            return 1

        target = EventCategory(category) if category else None
        # Portfolio channels are tested alongside the category channels, so a
        # single command proves all six destinations route correctly.
        keys = () if category else PORTFOLIO_KEYS
        print(f"\nbackends: {', '.join(service.backend_names)}")
        print(f"channels: {', '.join(sorted(settings.discord.configured_categories)) or '(none)'}")
        sent = await service.send_test(target, routing_keys=keys)
        print(f"sent test messages to: {', '.join(sent)}")
        if service.last_error:
            print(f"last error: {service.last_error}")
            return 1
        return 0
    finally:
        await engine.dispose()


async def _notifications_daily_summary(settings: Settings) -> int:
    """Build and send the daily portfolio report.

    A plain callable with no scheduler attached (Part L). Cron, a systemd timer
    or a future in-process scheduler all invoke the same command, and none of
    the business logic knows which.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            payload = await build_daily_summary(session)

        service = NotificationService(settings, session_factory=factory)
        await service.publish(Event.daily_summary(payload))

        print(f"\nportfolios summarised: {len(payload.get('portfolios', []))}")
        print(f"backends: {', '.join(service.backend_names) or '(none)'}")
        if not service.enabled:
            print("notifications are disabled; nothing was delivered")
        return 0
    finally:
        await engine.dispose()


async def _notifications_status(settings: Settings) -> int:
    """Print delivery configuration and recent outcomes. Never prints a webhook."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        service = NotificationService(settings, session_factory=factory)
        async with session_scope(factory) as session:
            repository = NotificationRepository(session)
            counts = await repository.counts_by_status()
            last_success, last_failure = await repository.last_outcome()

        print(f"\nenabled       : {service.enabled}")
        print(f"backends      : {', '.join(service.backend_names) or '(none)'}")
        print(
            f"channels      : "
            f"{', '.join(sorted(settings.discord.configured_categories)) or '(none configured)'}"
        )
        print(f"signal thresh : {settings.notifications.signal_threshold}")
        print(f"strong thresh : {settings.notifications.strong_signal_threshold}")
        print(f"cooldown      : {settings.notifications.signal_cooldown_minutes} min")
        print(f"delivered     : {counts.get('delivered', 0)}")
        print(f"failed        : {counts.get('failed', 0)}")
        print(f"skipped       : {counts.get('skipped', 0)}")
        print(f"last success  : {last_success.isoformat() if last_success else '(never)'}")
        print(f"last failure  : {last_failure.isoformat() if last_failure else '(never)'}")
        return 0
    finally:
        await engine.dispose()


async def _create_tables(settings: Settings) -> int:
    """Create tables directly from metadata.

    An escape hatch for throwaway SQLite databases. **Use Alembic for anything
    else** -- create_all does not record a revision, so a database built this way
    cannot be migrated afterwards.
    """
    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        print("tables created (schema not under Alembic control)")
        return 0
    finally:
        await engine.dispose()


def _parse_symbols(raw: str | None) -> list[str]:
    return [s.strip().upper() for s in (raw or "").split(",") if s.strip()]


def _parse_utc(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp, requiring or assuming UTC.

    A bare date is accepted and read as UTC midnight -- convenient on the command
    line, and unambiguous because tradabot has no other timezone.
    """
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _run_risk(settings: Settings, args: argparse.Namespace) -> int:
    """Dispatch the ``risk`` command."""
    return asyncio.run(_risk(settings, _parse_symbols(args.symbols) or None))


def _run_options(settings: Settings, args: argparse.Namespace) -> int:
    """Dispatch an ``options`` subcommand."""
    return asyncio.run(_options_capture(settings, dry_run=args.dry_run, force=args.force))


def _run_market_data(settings: Settings, args: argparse.Namespace) -> int:
    """Dispatch a ``market-data`` subcommand.

    A dispatch table rather than a chain of ``if``s: the subparser is declared
    ``required=True``, so argparse has already rejected every value not listed
    here and a fallback arm would be unreachable code pretending to be
    defensive. Each entry is a thunk so only the selected coroutine is built.
    """
    routes: dict[str, Callable[[], Any]] = {
        "status": lambda: _market_data_status(settings),
        "benchmarks": lambda: _market_data_benchmarks(settings, register=args.register),
        "verify-adjustments": lambda: _market_data_verify_adjustments(
            settings, _parse_symbols(args.symbols) or None
        ),
        "corporate-actions": lambda: _market_data_corporate_actions(
            settings, _parse_symbols(args.symbols) or None
        ),
        "import": lambda: _market_data_import(
            settings,
            _parse_symbols(args.symbols),
            _parse_utc(args.start),
            _parse_utc(args.end),
            Timeframe(args.timeframe),
        ),
        "sync": lambda: _market_data_sync(settings, _parse_symbols(args.symbols) or None),
        "quote": lambda: _market_data_quote(settings, args.symbol.upper()),
    }
    return int(asyncio.run(routes[args.market_command]()))


def _run_portfolios(settings: Settings, args: argparse.Namespace) -> int:
    """Dispatch a ``portfolios`` subcommand."""
    if args.portfolios_command == "seed":
        return asyncio.run(_portfolios_seed(settings))
    return asyncio.run(_portfolios_list(settings))


def _run_ops(settings: Settings, args: argparse.Namespace) -> int:
    """Dispatch an ``ops`` subcommand.

    ``install`` and ``uninstall`` are synchronous and touch only the filesystem;
    neither runs ``launchctl``.
    """
    command = args.ops_command
    if command == "install":
        return _ops_install(settings)
    if command == "uninstall":
        return _ops_uninstall(settings)
    if command == "status-publish":
        return asyncio.run(_ops_status_publish(settings, preview=args.preview, test=args.test))

    coroutines: dict[str, Callable[[Settings], Any]] = {
        "check": _ops_check,
        "status": _ops_status,
        "daily-summary-if-due": _ops_daily_summary_if_due,
    }
    return int(asyncio.run(coroutines[command](settings)))


def _run_watchlist(settings: Settings, args: argparse.Namespace) -> int:
    """Dispatch a ``watchlist`` subcommand."""
    command = args.watchlist_command
    if command == "list":
        return asyncio.run(_watchlist_list(settings))
    if command == "seed":
        return asyncio.run(_watchlist_seed(settings))
    symbol = args.symbol.upper()
    if command == "add":
        return asyncio.run(_watchlist_add(settings, symbol))
    return asyncio.run(_watchlist_set_enabled(settings, symbol, enabled=command == "enable"))


def _run_scanner(settings: Settings, args: argparse.Namespace) -> int:
    """Dispatch a ``scanner`` subcommand.

    A table rather than a chain: the two commands taking arguments are handled
    first, and the rest are a lookup.
    """
    command = args.scanner_command
    if command == "run-once":
        return asyncio.run(_scanner_run_once(settings, paper=not args.no_paper))
    if command == "candidates":
        return asyncio.run(_scanner_candidates(settings, args.limit))
    if command == "trends":
        return asyncio.run(_scanner_trends(settings, preview=args.preview, test=args.test))

    simple: dict[str, Callable[[Settings], Any]] = {
        "sync": _scanner_sync,
        "refresh-identity": _refresh_identity,
        "overview": _scanner_overview,
        "daily-summary": _notifications_daily_summary,
        "demo": _scanner_demo,
        "status": _scanner_status,
    }
    return int(asyncio.run(simple[command](settings)))


def _run_notifications(settings: Settings, args: argparse.Namespace) -> int:
    """Dispatch a ``notifications`` subcommand."""
    command = args.notification_command
    if command == "status":
        return asyncio.run(_notifications_status(settings))
    if command == "daily-summary":
        return asyncio.run(_notifications_daily_summary(settings))
    if command == "demo-lifecycle":
        return asyncio.run(_notifications_demo_lifecycle(settings))
    return asyncio.run(_notifications_test(settings, args.category))


def _run_backtest(settings: Settings, args: argparse.Namespace) -> int:
    """Dispatch a ``backtest`` subcommand."""

    command = args.backtest_command
    if command == "run":
        return asyncio.run(
            research_cli.backtest_run(
                settings,
                start=research_cli.parse_day(args.start),
                end=research_cli.parse_day(args.end),
                symbols=_parse_symbols(args.symbols) or None,
                universe=args.universe,
                timeframe=Timeframe(args.timeframe),
                include_extended=args.include_extended,
                skip_portfolios=args.no_portfolios,
            )
        )
    if command == "status":
        return asyncio.run(research_cli.backtest_status(settings, limit=args.limit))
    return asyncio.run(research_cli.backtest_report(settings, run_id=args.run_id))


def _parse_timeframes(value: str | None) -> list[str]:
    """Split a comma-separated timeframe list, preserving case.

    Not `_parse_symbols`: that uppercases, and `5m` is not `5M`.
    """
    if not value:
        return ["5m", "15m", "1h", "1d"]
    return [item.strip() for item in value.split(",") if item.strip()]


def _run_history(settings: Settings, args: argparse.Namespace) -> int:
    """Dispatch a ``history`` subcommand."""
    return asyncio.run(
        research_cli.historical_backfill(
            settings,
            start=research_cli.parse_day(args.start),
            end=research_cli.parse_day(args.end),
            symbols=_parse_symbols(args.symbols) or None,
            universe=args.universe,
            timeframes=_parse_timeframes(args.timeframes),
            resume=not args.no_resume,
            dry_run=args.dry_run,
        )
    )


def _run_outcomes(settings: Settings, args: argparse.Namespace) -> int:
    """Dispatch an ``outcomes`` subcommand."""

    if args.outcomes_command == "generate":
        return asyncio.run(
            research_cli.outcomes_generate(
                settings,
                since=research_cli.parse_day(args.since) if args.since else None,
                until=research_cli.parse_day(args.until) if args.until else None,
                recompute=args.recompute,
            )
        )
    return asyncio.run(research_cli.outcomes_status(settings))


def _run_research(settings: Settings, args: argparse.Namespace) -> int:
    """Dispatch a ``research`` subcommand."""

    command = args.research_command
    # `storage-plan` is the one research subcommand with no horizon: it projects
    # disk cost, which is a property of the range, not of a forecast window.
    if command == "storage-plan":
        return asyncio.run(
            research_cli.storage_plan(
                settings,
                start=research_cli.parse_day(args.start),
                end=research_cli.parse_day(args.end),
                symbols=_parse_symbols(args.symbols) or None,
                universe=args.universe,
                cadence=args.cadence,
            )
        )

    # `walkforward` reports several horizons in one pass, so it takes its own
    # list rather than the single `--horizon` the descriptive reports use.
    if command == "walkforward":
        return asyncio.run(
            research_cli.research_walkforward(
                settings,
                run_id=args.run_id,
                folds=args.folds,
                horizons=tuple(h.strip() for h in args.horizons.split(",") if h.strip()),
            )
        )

    horizon = Horizon(args.horizon)
    if command == "score-calibration":
        return asyncio.run(
            research_cli.research_calibration(
                settings,
                horizon=horizon,
                run_id=args.run_id,
                threshold_view=args.around_threshold,
            )
        )
    if command == "features":
        return asyncio.run(
            research_cli.research_features(
                settings, horizon=horizon, feature=args.feature, run_id=args.run_id
            )
        )
    return asyncio.run(
        research_cli.research_export(
            settings,
            horizon=horizon,
            run_id=args.run_id,
            directory=Path(args.out),
            fmt=args.format,
            include_extended=args.include_extended,
        )
    )


def _portfolio_fit(settings: Settings, args: argparse.Namespace) -> int:
    """Describe a portfolio, and optionally a hypothetical addition. Read-only.

    The portfolio comes either from a paper account slot -- read from the broker,
    never modified -- or from holdings given on the command line. Both paths
    reach the same analysis; only where the positions come from differs.
    """
    name = str(args.portfolio).upper()
    # Any PAPER_* name is routed to the account reader, including one that does
    # not exist. Falling through to the inline path would silently describe an
    # empty portfolio named PAPER_50K, which reads exactly like a real account
    # that happens to be flat.
    if name.startswith("PAPER_"):
        snapshot = _paper_snapshot(name)
        if not snapshot.available:
            print(f"ACCOUNT UNAVAILABLE: {name}: {snapshot.error}", file=sys.stderr)
            return 2
        portfolio = snapshot.to_portfolio()
        holdings = [(p.symbol, p.quantity) for p in portfolio.positions]
        cash = portfolio.cash
    else:
        portfolio = None
        cash = float(args.cash)
        holdings = []
        for item in args.holding or []:
            try:
                symbol, quantity = item.split(":", maxsplit=1)
            except ValueError:
                print(f"holding must look like SYMBOL:QTY, got {item!r}", file=sys.stderr)
                return 2
            holdings.append((symbol.upper(), float(quantity)))

    prices = _advisor_prices(
        settings,
        [s for s, _q in holdings] + ([args.candidate.upper()] if args.candidate else []) + ["SPY"],
    )
    as_of = args.as_of or max((max(v.closes) for v in prices.values() if v.closes), default=None)
    if as_of is None:
        print("DATA NOT SYNCED: no local price history", file=sys.stderr)
        return 2
    if portfolio is None:
        positions: list[Position] = []
        for symbol, quantity in holdings:
            series = prices.get(symbol)
            if series is None or not series.closes:
                print(f"DATA NOT SYNCED: no price history for {symbol}", file=sys.stderr)
                return 2
            days = [d for d in series.closes if d <= as_of]
            positions.append(Position(symbol, quantity, series.closes[max(days)]))
        portfolio = Portfolio(args.portfolio, cash, tuple(positions), as_of)

    service = PortfolioFitService(
        {k: v.closes for k, v in prices.items()},
        _advisor_sectors(),
        _advisor_context(Path(args.facts), prices),
    )
    report = service.analyse(portfolio, as_of=as_of, candidate=args.candidate, amount=args.amount)
    print(fit_to_json(report) if args.json else render_fit(report))
    return 0


def _paper_snapshot(slot: str) -> Any:
    """One read-only paper account snapshot.

    The reader is constructed here rather than inside the analysis package, so
    the vendor client stays unreachable from anything that describes portfolios.
    """
    from app.broker.paper_snapshots import PaperAccountSnapshotReader  # noqa: PLC0415

    return PaperAccountSnapshotReader().snapshot(slot)


def _advisor_context(facts_path: Path, prices: dict[str, Any]) -> Any:
    """Company context from the production Advisor, or nothing at all.

    An unsynced fact store costs the report its narrative blocks and leaves the
    portfolio arithmetic untouched, which is why this returns ``None`` instead
    of refusing.
    """
    if not facts_path.exists():
        return None
    from app.advisor.context import AdvisorCompanyContext  # noqa: PLC0415

    try:
        facts = FactStore.from_parquet(facts_path)
    except Exception:
        return None
    return AdvisorCompanyContext(
        AdvisorService(
            facts,
            prices,
            sectors=_advisor_sectors(),
            company_sectors=_company_sectors(),
        )
    )


def _publish(settings: Settings, args: argparse.Namespace) -> int:
    """Deliver what the monitor decided is worth sending. Output-only."""
    return asyncio.run(_publish_async(settings, args))


def _publisher(settings: Settings, args: argparse.Namespace) -> Any:
    """A publisher wired to the canonical destinations.

    Built here rather than inside the publishing package so the vendor client
    and the credentials stay outside the layer that formats messages.
    """
    from app.core.webhooks import WebhookRegistry  # noqa: PLC0415
    from app.publishing import DeliveryLedger, Publisher  # noqa: PLC0415

    registry = WebhookRegistry.load(dotenv=Path(".env"))
    notifier = None
    if not args.dry_run and registry.enabled:
        notifier = DiscordWebhookNotifier(
            settings.discord,
            max_characters=settings.notifications.max_message_characters,
            registry=registry,
        )
    return Publisher(
        notifier=notifier,
        registry=registry,
        ledger=DeliveryLedger(Path(args.ledger)),
        dry_run=args.dry_run,
    )


async def _publish_async(settings: Settings, args: argparse.Namespace) -> int:
    from app.monitoring import EventJournal, build_digest  # noqa: PLC0415
    from app.publishing import MARKET_TRENDS  # noqa: PLC0415
    from app.publishing import newsletter as letter  # noqa: PLC0415

    publisher = _publisher(settings, args)
    command = args.publish_command

    if command == "smoke-test":
        outcome = await _publish_smoke_test(publisher, args)
    elif command == "events":
        run = _monitor_run(settings, args)
        outcome = await publisher.publish_events(run)
        await publisher.reconcile(current=run.events)
    elif command == "portfolio":
        outcome = await _publish_portfolios(settings, args, publisher)
    else:
        since = (datetime.now(UTC).date() - timedelta(days=args.days)).isoformat()
        journal = EventJournal(Path(args.journal))
        events = journal.read(since=date.fromisoformat(since))
        state = _monitor_state(Path(args.state))
        week_ending = datetime.now(UTC).date().isoformat()
        if args.if_due and _weekly_already_sent(Path(args.ledger), week_ending):
            print("weekly newsletter already published for this week")
            return 0
        digest = build_digest(events, state, since=since, until=week_ending)
        message = letter.message(
            digest,
            week_ending=week_ending,
            regime=(state.get("market") or {}).get("SPY"),
            coverage=_publish_coverage(args),
            events=events,
            occurred_at=datetime.now(UTC),
        )
        outcome = await publisher.publish_message(
            MARKET_TRENDS, message, identity=f"weekly:{week_ending}"
        )

    for item in publisher.rendered:
        if args.render:
            print(
                f"--- {item['channel']}  {item['colour']}  "
                f"{item['characters']} chars  content={item['content']!r} ---"
            )
            print(item["title"])
            if item["body"]:
                print(item["body"])
            for name, value in item["fields"].items():
                print(f"  [{name}] {value}")
            print()
    print(json.dumps(outcome.as_dict(), indent=1) if args.json else _publish_line(outcome))
    return 0


SMOKE_TARGETS: tuple[tuple[str, str], ...] = (
    ("TRENDS", "market-trends: weekly market intelligence"),
    ("MARKET", "market-signals: material market and company events"),
    ("PAPER_1K", "paper-1k: PAPER_1K portfolio analyst"),
    ("PAPER_3K", "paper-3k: PAPER_3K portfolio analyst"),
    ("PAPER_10K", "paper-10k: PAPER_10K portfolio analyst"),
    ("STATUS", "status: operational status"),
)


async def _publish_smoke_test(publisher: Any, args: argparse.Namespace) -> Any:
    """One clearly-labelled test message per configured destination.

    Every message travels the ordinary routing and transport path, so a success
    here is evidence about the real delivery route rather than about a special
    test harness. No market content is manufactured.
    """
    from app.core.webhooks import WebhookChannel  # noqa: PLC0415
    from app.publishing import PublishOutcome  # noqa: PLC0415
    from app.publishing import format as render  # noqa: PLC0415

    stamp = datetime.now(UTC)
    combined = PublishOutcome(dry_run=args.dry_run)
    for name, purpose in SMOKE_TARGETS:
        channel = WebhookChannel(name)
        detail: list[str] = []
        if name.startswith("PAPER_"):
            # Real, read-only account state rather than invented numbers.
            snapshot = _paper_snapshot(name)
            detail = (
                [
                    f"**Account:** {name}",
                    f"**Equity:** ${snapshot.equity:,.2f}",
                    f"**Positions:** {len(snapshot.positions)}",
                    f"**Coverage:** {_coverage_label(name)}",
                ]
                if snapshot.available
                else [f"**Account:** {name} unavailable ({snapshot.error})"]
            )
        message = render.smoke_test_message(
            destination=purpose,
            purpose="verifying routing and delivery",
            detail=detail,
            occurred_at=stamp,
        )
        outcome = await publisher.publish_message(
            channel, message, identity=f"smoke:{name}:{stamp.isoformat(timespec='seconds')}"
        )
        combined.messages += outcome.messages
        combined.delivered += outcome.delivered
        combined.failed += outcome.failed
        combined.not_configured += outcome.not_configured
        combined.destinations.extend(outcome.destinations)
        combined.errors.extend(outcome.errors)
    return combined


def _publish_line(outcome: Any) -> str:
    if outcome.quiet:
        return "Nothing to publish."
    return (
        f"{outcome.messages} message(s): {outcome.delivered} delivered, "
        f"{outcome.failed} failed, {outcome.not_configured} unconfigured, "
        f"{outcome.suppressed_already_delivered} already delivered"
        + (" [dry run]" if outcome.dry_run else "")
    )


def _weekly_already_sent(ledger_dir: Path, week_ending: str) -> bool:
    from app.publishing import DeliveryLedger  # noqa: PLC0415
    from app.publishing.channels import MARKET_TRENDS  # noqa: PLC0415

    return DeliveryLedger(ledger_dir).already_delivered(
        f"weekly:{week_ending}", MARKET_TRENDS.value
    )


def _publish_coverage(args: argparse.Namespace) -> dict[str, str]:
    """Factual data-coverage lines for the newsletter footer."""
    from app.fundamentals import health  # noqa: PLC0415

    state = health(Path(args.facts))
    return {
        "SEC coverage": f"{state.symbols:,} symbols, {state.rows:,} facts",
        "Fact store": str(state.status),
        "Latest SEC filing": state.newest_filed or "unknown",
    }


def _monitor_run(settings: Settings, args: argparse.Namespace) -> Any:
    """One monitoring pass, using the persisted baseline."""
    from app.monitoring import (  # noqa: PLC0415
        EventJournal,
        JsonStateStore,
        MonitoringEngine,
        MonitoringInputs,
    )

    bars, sectors, watched = _monitor_market(settings)
    as_of = args.as_of or max(
        (max(b.upto("9999-12-31")) for b in bars.values() if b.upto("9999-12-31")),
        default=None,
    )
    contexts, filings = _monitor_companies(settings, watched, as_of or "", args)
    run = MonitoringEngine(JsonStateStore(Path(args.state))).run(
        MonitoringInputs(
            as_of=as_of or "",
            bars=bars,
            benchmark="SPY",
            sectors=sectors,
            watched=watched,
            company_contexts=contexts,
            latest_filings=filings,
            fact_store_health=_monitor_health(Path(args.facts)),
        )
    )
    EventJournal(Path(args.journal)).append(run.events)
    return run


async def _publish_portfolios(settings: Settings, args: argparse.Namespace, publisher: Any) -> Any:
    """One message per readable account, each to its own channel."""
    from app.monitoring import (  # noqa: PLC0415
        EventJournal,
        JsonStateStore,
        MonitoringEngine,
        MonitoringInputs,
    )
    from app.publishing import PublishOutcome, paper_channel  # noqa: PLC0415
    from app.publishing import format as render  # noqa: PLC0415

    bars, sectors, _watched = _monitor_market(settings)
    as_of = args.as_of or max(
        (max(b.upto("9999-12-31")) for b in bars.values() if b.upto("9999-12-31")),
        default="",
    )
    prices = {s: b.as_closes() for s, b in bars.items()}
    service = PortfolioFitService(prices, sectors)

    reports: dict[str, Any] = {}
    snapshots: dict[str, Any] = {}
    for slot in ("PAPER_1K", "PAPER_3K", "PAPER_10K"):
        snapshot = _paper_snapshot(slot)
        if not snapshot.available:
            continue
        snapshots[slot] = snapshot
        reports[slot] = service.analyse(snapshot.to_portfolio(), as_of=as_of)

    run = MonitoringEngine(JsonStateStore(Path(args.state))).run(
        MonitoringInputs(as_of=as_of, bars=bars, sectors=sectors, portfolios=reports)
    )
    EventJournal(Path(args.journal)).append(run.events)

    combined = PublishOutcome(dry_run=args.dry_run)
    for slot, report in reports.items():
        channel = paper_channel(slot)
        if channel is None:
            continue
        events = [e for e in run.events if e.scope.account == slot]
        if not events and not args.always:
            continue
        clusters = service.clusters(snapshots[slot].to_portfolio(), as_of)
        state, coverage_text = _coverage_state(slot)
        message = render.portfolio_message(
            slot,
            report.exposure,
            report.risk,
            events=events,
            holdings=report.holdings_detail,
            cluster=clusters[0] if clusters else None,
            coverage=coverage_text,
            coverage_state=state,
            confidence=str(report.confidence),
            occurred_at=datetime.now(UTC),
        )
        outcome = await publisher.publish_message(
            channel, message, identity=f"portfolio:{slot}:{as_of}"
        )
        combined.messages += outcome.messages
        combined.delivered += outcome.delivered
        combined.failed += outcome.failed
        combined.not_configured += outcome.not_configured
        combined.suppressed_already_delivered += outcome.suppressed_already_delivered
        combined.destinations.extend(outcome.destinations)
    return combined


def _coverage_state(slot: str) -> tuple[str, str]:
    """The coverage state name and its display text for one account."""
    from app.publishing.coverage import resolve  # noqa: PLC0415

    state, text = resolve(slot, _dotenv_env())
    return str(state), text


def _coverage_label(slot: str) -> str:
    """Configured coverage note for one account. Never empty.

    Read from configuration rather than inferred from holdings: whether an
    account represents someone's whole portfolio is a fact about their
    circumstances, and no amount of position data reveals it.
    """
    from app.publishing.coverage import label  # noqa: PLC0415

    return label(slot, _dotenv_env())


def _dotenv_env() -> dict[str, str]:
    """Process environment layered over the dotenv file, for read-only lookups."""
    from app.core.webhooks import _dotenv_values  # noqa: PLC0415

    merged = dict(_dotenv_values(Path(".env")))
    merged.update(os.environ)
    return merged


def _discord_bot(settings: Settings, args: argparse.Namespace) -> int:
    """Run the interactive Discord bot until stopped.

    A long-running process, deliberately separate from the scheduled publisher
    jobs: a bot crash must not stop market monitoring, and a publisher failure
    must not disconnect the bot.
    """
    import asyncio as _asyncio  # noqa: PLC0415

    from app.discord_bot import BotConfigurationError, load  # noqa: PLC0415
    from app.discord_bot.bot import run  # noqa: PLC0415

    try:
        bot_settings = load()
    except BotConfigurationError as exc:
        print(f"DISCORD BOT NOT CONFIGURED: {exc}", file=sys.stderr)
        return 2

    if args.check_config:
        print(json.dumps(bot_settings.describe(), indent=1))
        return 0

    def factory() -> Any:
        return _build_analyst(settings, Path(args.facts))

    try:
        _asyncio.run(run(bot_settings, analyst_factory=factory))
    except KeyboardInterrupt:
        print("discord bot stopped")
    return 0


def _build_analyst(settings: Settings, facts_path: Path) -> Any:
    """Assemble the analyst from the production layers. Computes nothing itself."""
    from app.advisor import AdvisorService, FactStore  # noqa: PLC0415
    from app.discord_bot.analysis import StockAnalyst  # noqa: PLC0415
    from app.fundamentals import health  # noqa: PLC0415
    from app.instruments.registry import load as load_registry  # noqa: PLC0415

    state = health(facts_path)
    prices = _advisor_prices(settings, None)
    sectors = _advisor_sectors()
    facts = FactStore.from_parquet(facts_path) if state.ok else FactStore([])
    advisor = AdvisorService(facts, prices, sectors=sectors, company_sectors=_company_sectors())
    as_of = max((max(v.closes) for v in prices.values() if v.closes), default="")

    from app.peers import PeerComparisonService, PeerUniverse  # noqa: PLC0415

    registry = load_registry(_database_file(settings))
    # Peer groups are computed lazily and cached per (industry, as_of), so this
    # costs nothing at startup -- building all 858 comparable issuers up front
    # would add ~45s to serve groups nobody asked for.
    peers = PeerComparisonService(
        universe=PeerUniverse(registry.all_candidates()), advisor=advisor, facts=facts
    )

    # No broker handle: /check answers a company question, so an Alpaca outage
    # cannot slow it down or degrade it.
    return StockAnalyst(
        registry=registry,
        peers=peers,
        advisor=advisor,
        universe=sorted(prices),
        fundamentals=frozenset(facts.symbols) if state.ok else frozenset(),
        fact_store_ready=state.ok,
        as_of=as_of,
    )


def _heartbeat(_settings: Settings, args: argparse.Namespace) -> int:
    """Ping the external watchdog. Never fails the caller.

    Exits 0 even when the ping fails: this runs from the scheduler, and a
    non-zero exit would make launchd treat a network blip as a broken job.
    """
    from app.ops.heartbeat import HEARTBEAT_ENV, declared_policy, emit  # noqa: PLC0415

    url = _dotenv_env().get(HEARTBEAT_ENV, "")
    result = emit(url or None, now=datetime.now(UTC))
    payload = {
        "sent": result.sent,
        "configured": result.configured,
        "attempts": result.attempts,
        "error": result.error,
        **declared_policy(),
    }
    print(json.dumps(payload, indent=1) if args.json else _heartbeat_line(result))
    return 0


def _heartbeat_line(result: Any) -> str:
    if not result.configured:
        return (
            "heartbeat not configured: set TRADABOT_HEARTBEAT_URL to an external "
            "dead-man's-switch endpoint (see docs/operations-discord.md)"
        )
    return "heartbeat sent" if result.sent else f"heartbeat NOT sent ({result.error})"


def _monitor(settings: Settings, args: argparse.Namespace) -> int:
    """Report what changed since the last run, or that nothing did."""
    from app.monitoring import (  # noqa: PLC0415
        EventJournal,
        JsonStateStore,
        MonitoringEngine,
        MonitoringInputs,
        build_digest,
    )
    from app.monitoring.cli import (  # noqa: PLC0415
        digest_to_json,
        render_digest,
        render_run,
        run_to_json,
    )

    store = JsonStateStore(Path(args.state))
    journal = EventJournal(Path(args.journal))

    if args.monitor_command == "digest":
        since = (datetime.now(UTC).date() - timedelta(days=args.days)).isoformat()
        events = journal.read(since=date.fromisoformat(since))
        digest = build_digest(
            events,
            _monitor_state(Path(args.state)),
            since=since,
            until=datetime.now(UTC).date().isoformat(),
            limit=args.limit,
        )
        print(digest_to_json(digest) if args.json else render_digest(digest))
        return 0

    bars, sectors, watched = _monitor_market(settings)
    as_of = args.as_of or max(
        (max(b.upto("9999-12-31")) for b in bars.values() if b.upto("9999-12-31")),
        default=None,
    )
    if as_of is None:
        print("DATA NOT SYNCED: no local price history", file=sys.stderr)
        return 2

    contexts, filings = _monitor_companies(settings, watched, as_of, args)
    engine = MonitoringEngine(store, cooldowns=not args.ignore_cooldowns)
    run = engine.run(
        MonitoringInputs(
            as_of=as_of,
            bars=bars,
            benchmark="SPY",
            sectors=sectors,
            watched=watched,
            company_contexts=contexts,
            latest_filings=filings,
            portfolios=_monitor_portfolios(bars, sectors, as_of, args),
            fact_store_health=_monitor_health(Path(args.facts)),
        )
    )
    if not args.no_journal:
        journal.append(run.events)
    print(
        run_to_json(run) if args.json else render_run(run, evidence=args.evidence, limit=args.limit)
    )
    return 0


def _monitor_state(directory: Path) -> dict[str, dict[str, Any]]:
    """The current baseline as plain dictionaries, for unresolved-risk reporting."""
    out: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out[path.stem] = {k: (v or {}).get("state", {}) for k, v in raw.items()}
    return out


def _monitor_market(settings: Settings) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Price history, sector labels and the watched universe."""
    from app.monitoring import Bars  # noqa: PLC0415

    path = _database_file(settings)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        candles, _sectors = load_daily(connection)
        rows = connection.execute(
            "SELECT i.symbol, w.tags FROM watchlist w "
            "JOIN instruments i ON i.id = w.instrument_id WHERE w.enabled = 1"
        ).fetchall()
    finally:
        connection.close()
    bars: dict[str, Any] = {}
    for (symbol,), group in candles.group_by("symbol", maintain_order=True):
        records = list(group.iter_rows(named=True))
        bars[str(symbol)] = Bars(
            {str(r["timestamp"])[:10]: float(r["close"]) for r in records},
            {str(r["timestamp"])[:10]: float(r["volume"] or 0) for r in records},
        )
    sectors = {str(s): str(json.loads(t)[0]) for s, t in rows if t}
    sectors.update(_advisor_sectors())
    watched = sorted({str(s) for s, _t in rows} & set(bars))
    return bars, sectors, watched


def _monitor_companies(
    settings: Settings, watched: list[str], as_of: str, args: argparse.Namespace
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Advisor context and latest filings, when company monitoring is requested.

    Off by default: a company pass runs the Advisor once per watched symbol,
    which is the expensive part of a run and only changes on filing days.
    """
    if not args.companies:
        return None, None
    from app.fundamentals import latest_filings  # noqa: PLC0415

    facts_path = Path(args.facts)
    prices = _advisor_prices(settings, [*watched, "SPY"])
    provider = _advisor_context(facts_path, prices)
    if provider is None:
        return None, None
    contexts = {s: provider.context(s, as_of) for s in watched}
    return contexts, latest_filings(facts_path, symbols=watched, as_of=as_of)


def _monitor_portfolios(
    bars: dict[str, Any],
    sectors: dict[str, str],
    as_of: str,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    """Portfolio Fit reports for each readable paper account."""
    if not args.accounts:
        return None
    prices = {s: b.as_closes() for s, b in bars.items()}
    service = PortfolioFitService(prices, sectors)
    out: dict[str, Any] = {}
    for slot in ("PAPER_1K", "PAPER_3K", "PAPER_10K"):
        snapshot = _paper_snapshot(slot)
        if not snapshot.available:
            continue
        out[slot] = service.analyse(snapshot.to_portfolio(), as_of=as_of)
    return out or None


def _monitor_health(facts_path: Path) -> Any:
    from app.fundamentals import health  # noqa: PLC0415

    return health(facts_path)


def _database_file(settings: Settings) -> str:
    url = settings.database_url
    return url.rsplit("/", maxsplit=1)[-1] if "/" in url else url


def _fundamentals(settings: Settings, args: argparse.Namespace) -> int:
    """Inspect or rebuild the persisted SEC fact store."""
    from app.fundamentals import (  # noqa: PLC0415
        database_path,
        health,
        sync_facts,
        universe_symbols,
    )

    store = Path(args.store)
    if args.fundamentals_command == "status":
        state = health(store)
        if args.json:
            print(json.dumps(state.as_dict(), indent=1))
        else:
            print(f"FACT STORE {state.status}")
            print(f"  path              {state.path}")
            print(f"  rows              {state.rows:,}")
            print(f"  symbols           {state.symbols:,}")
            print(f"  metrics           {state.metrics}")
            print(f"  filings           {state.oldest_filed} to {state.newest_filed}")
            print(f"  newest acceptance {state.newest_accepted}")
            if state.acceptance_coverage is not None:
                print(f"  acceptance cover  {state.acceptance_coverage * 100:.1f}%")
            print(f"  schema            {state.schema_version} ({state.schema_hash})")
            if state.detail:
                print(f"  detail            {state.detail}")
            for note in state.notes:
                print(f"  note              {note}")
        return 0 if state.ok else 1

    symbols = (
        _parse_symbols(args.symbols)
        if args.symbols
        else universe_symbols(database_path(settings.database_url))
    )
    print(f"syncing {len(symbols)} symbols from SEC EDGAR into {store}")

    def _progress(index: int, total: int, outcome: Any) -> None:
        if index % 50 == 0 or outcome.status == "UNAVAILABLE":
            print(f"  {index}/{total} {outcome.symbol} {outcome.status}", flush=True)

    result = sync_facts(
        symbols,
        output=store,
        cache_dir=Path(args.cache),
        force=args.force,
        progress=_progress,
    )
    print(
        f"wrote {result.written:,} facts for {result.symbols:,} symbols "
        f"({result.fetched} fetched, {result.from_cache} cached, "
        f"{result.failed} unavailable, {len(result.unmapped)} unmapped) "
        f"in {result.seconds:.0f}s"
    )
    return 0 if result.written else 1


def _company_sectors() -> dict[str, str]:
    """Sector by company key, from the SEC's SIC classification of each filer.

    Covers every company in the fact store, foreign issuers included, and is
    consulted only where :func:`_advisor_sectors` is silent.
    """
    from app.advisor.facts import company_key  # noqa: PLC0415
    from app.fundamentals.sectors import sector_for  # noqa: PLC0415

    settings = get_settings()
    url = settings.database_url
    path = url.rsplit("/", maxsplit=1)[-1] if "/" in url else url
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT cik, sic FROM companies WHERE cik IS NOT NULL AND sic IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        # A database that predates the SIC migration simply has no fallback.
        return {}
    finally:
        connection.close()
    return {company_key(int(cik)): sector_for(sic) for cik, sic in rows}


def _advisor_sectors() -> dict[str, str]:
    """Sector labels from the watchlist. Proxy-derived, surfaced as such."""
    settings = get_settings()
    url = settings.database_url
    path = url.rsplit("/", maxsplit=1)[-1] if "/" in url else url
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT i.symbol, w.tags FROM watchlist w "
            "JOIN instruments i ON i.id = w.instrument_id WHERE w.enabled = 1"
        ).fetchall()
    finally:
        connection.close()
    out: dict[str, str] = {}
    for symbol, tags in rows:
        if tags:
            out[str(symbol)] = str(json.loads(tags)[0])
    return out


def _advisor(settings: Settings, args: argparse.Namespace) -> int:
    """Print a factual, read-only company report. Never a recommendation."""
    store_path = Path(args.facts)
    if not store_path.exists():
        print(f"DATA NOT SYNCED: no fact store at {store_path}", file=sys.stderr)
        print(
            "The Advisor reads persisted SEC facts; it does not fetch on demand.", file=sys.stderr
        )
        return 2
    facts = FactStore.from_parquet(store_path)
    prices = _advisor_prices(settings, [args.symbol.upper(), "SPY"])
    # Same sector knowledge as /check. Without it this command described banks
    # as manufacturers, which is the defect it exists to help diagnose.
    service = AdvisorService(
        facts,
        prices,
        sectors=_advisor_sectors(),
        company_sectors=_company_sectors(),
    )
    report = service.analyse(args.symbol, as_of=args.as_of)
    print(to_json(report) if args.json else render(report, provenance=args.provenance))
    return 0


def _advisor_prices(settings: Settings, symbols: list[str] | None) -> dict[str, Any]:
    """Split-adjusted closes for the Advisor, read from the local database.

    ``None`` means every instrument, which is what the interactive bot needs:
    it cannot know in advance which symbol someone will ask about, and an empty
    universe makes every request look like an unknown ticker.
    """
    url = settings.database_url
    path = url.rsplit("/", maxsplit=1)[-1] if "/" in url else url
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        candles, _sectors = load_daily(connection)
    finally:
        connection.close()
    wanted = None if symbols is None else set(symbols)
    out: dict[str, Any] = {}
    for (symbol,), group in candles.group_by("symbol", maintain_order=True):
        name = str(symbol)
        if wanted is not None and name not in wanted:
            continue
        out[name] = PriceSeries(
            {str(r["timestamp"])[:10]: float(r["close"]) for r in group.iter_rows(named=True)}
        )
    return out


_COMMANDS: dict[str, Callable[[Settings, argparse.Namespace], int]] = {
    "advisor": _advisor,
    "portfolio-fit": _portfolio_fit,
    "fundamentals": _fundamentals,
    "monitor": _monitor,
    "publish": _publish,
    "heartbeat": _heartbeat,
    "discord-bot": _discord_bot,
    "seed": lambda settings, args: asyncio.run(
        _seed(settings, _parse_symbols(args.symbols) or None, args.days)
    ),
    "signal": lambda settings, args: asyncio.run(
        _signal(settings, args.symbol.upper(), Horizon(args.horizon))
    ),
    "seed-profiles": lambda settings, _args: asyncio.run(_seed_profiles(settings)),
    "demo-simulation": lambda settings, _args: asyncio.run(_demo(settings)),
    "create-tables": lambda settings, _args: asyncio.run(_create_tables(settings)),
    "simulate": lambda settings, args: asyncio.run(
        _simulate(
            settings,
            args.symbol.upper(),
            _parse_utc(args.start),
            _parse_utc(args.end),
            Timeframe(args.timeframe),
            Horizon(args.horizon),
        )
    ),
    "volatility": lambda settings, args: asyncio.run(
        _volatility(settings, symbol=args.symbol, preview=args.preview)
    ),
    "market-data": _run_market_data,
    "options": _run_options,
    "risk": _run_risk,
    "notifications": _run_notifications,
    "watchlist": _run_watchlist,
    "scanner": _run_scanner,
    "portfolios": _run_portfolios,
    "ops": _run_ops,
    "backtest": _run_backtest,
    "outcomes": _run_outcomes,
    "research": _run_research,
    "history": _run_history,
}


def _add_monitor_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """The monitoring commands: one pass, or a period summary."""
    monitor = sub.add_parser("monitor", help="What changed since the last run, or that nothing did")
    monitor_sub = monitor.add_subparsers(dest="monitor_command", required=True)
    for name, description in (
        ("run", "Detect material changes since the previous run"),
        ("digest", "Summarise a period from the event journal"),
    ):
        parser = monitor_sub.add_parser(name, help=description)
        parser.add_argument(
            "--state", default="data/monitor_state", help="Where the previous baseline is kept"
        )
        parser.add_argument(
            "--journal", default="data/monitor_events", help="Append-only record of reported events"
        )
        parser.add_argument("--json", action="store_true", help="Emit structured JSON")
        if name == "run":
            parser.add_argument(
                "--as-of",
                dest="as_of",
                default=None,
                help="Observe as of a past session (YYYY-MM-DD)",
            )
            parser.add_argument(
                "--facts",
                default="data/sec_facts.parquet",
                help="Fact store used for company context and health",
            )
            parser.add_argument(
                "--companies",
                action="store_true",
                help="Include company fundamentals, valuation and "
                "filings (runs the Advisor per watched symbol)",
            )
            parser.add_argument(
                "--accounts", action="store_true", help="Include read-only paper account portfolios"
            )
            parser.add_argument(
                "--evidence",
                action="store_true",
                help="Show the measurements and thresholds behind each event",
            )
            parser.add_argument(
                "--ignore-cooldowns",
                action="store_true",
                help="Report repeats that a cooldown would suppress",
            )
            parser.add_argument(
                "--no-journal",
                action="store_true",
                help="Do not append reported events to the journal",
            )
            parser.add_argument(
                "--limit",
                type=int,
                default=10,
                help="How many ranked events to display; the run "
                "still records and journals all of them",
            )
        else:
            parser.add_argument("--days", type=int, default=7, help="How far back to summarise")
            parser.add_argument("--limit", type=int, default=5, help="Rows per section")


def _add_publish_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Discord delivery. Output-only, and dry by default until told otherwise."""
    publish = sub.add_parser("publish", help="Deliver monitoring output to Discord (output-only)")
    publish_sub = publish.add_subparsers(dest="publish_command", required=True)
    for name, description in (
        ("events", "Publish material market and company changes to market-signals"),
        ("portfolio", "Publish per-account analysis to its own paper channel"),
        ("weekly", "Publish the weekly market intelligence letter to market-trends"),
        ("smoke-test", "Send one labelled TRADABOT TEST message per destination"),
    ):
        parser = publish_sub.add_parser(name, help=description)
        parser.add_argument(
            "--dry-run", action="store_true", help="Render and route without sending anything"
        )
        parser.add_argument(
            "--render", action="store_true", help="Print every message that would be sent"
        )
        parser.add_argument("--json", action="store_true", help="Emit structured JSON")
        parser.add_argument(
            "--state", default="data/monitor_state", help="Monitoring baseline directory"
        )
        parser.add_argument(
            "--journal", default="data/monitor_events", help="Monitoring event journal directory"
        )
        parser.add_argument(
            "--ledger",
            default="data/monitor_delivery",
            help="Delivery ledger directory (idempotency)",
        )
        parser.add_argument(
            "--facts",
            default="data/sec_facts.parquet",
            help="Fact store for company context and coverage",
        )
        parser.add_argument(
            "--as-of", dest="as_of", default=None, help="Observe as of a past session (YYYY-MM-DD)"
        )
        if name == "events":
            parser.add_argument(
                "--companies",
                action="store_true",
                help="Include fundamentals, filings and valuation",
            )
            parser.add_argument("--accounts", action="store_true", help=argparse.SUPPRESS)
        if name == "portfolio":
            parser.add_argument(
                "--always", action="store_true", help="Send a report even when nothing changed"
            )
            parser.add_argument("--companies", action="store_true", help=argparse.SUPPRESS)
            parser.add_argument("--accounts", action="store_true", help=argparse.SUPPRESS)
        if name == "weekly":
            parser.add_argument(
                "--days", type=int, default=7, help="How far back the letter summarises"
            )
            parser.add_argument(
                "--if-due",
                dest="if_due",
                action="store_true",
                help="Publish only if this week has not been sent",
            )


def _add_ops_parsers(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Portfolio and operations commands."""
    advisor = sub.add_parser(
        "advisor", help="Factual read-only company report (never a recommendation)"
    )
    advisor.add_argument("symbol", help="Ticker to analyse")
    advisor.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="Analyse as of a past date (YYYY-MM-DD), point-in-time",
    )
    advisor.add_argument("--json", action="store_true", help="Emit structured JSON")
    advisor.add_argument(
        "--provenance",
        action="store_true",
        help="Show the SEC concept, filing and accession behind each figure",
    )
    advisor.add_argument(
        "--facts",
        default="data/sec_facts.parquet",
        help="Path to the persisted point-in-time SEC fact store",
    )

    fundamentals = sub.add_parser(
        "fundamentals", help="The persisted SEC fact store the Advisor reads"
    )
    fundamentals_sub = fundamentals.add_subparsers(dest="fundamentals_command", required=True)
    for name, description in (
        ("status", "Report whether the fact store is synced, stale or corrupt"),
        ("sync", "Rebuild the fact store from SEC EDGAR (resumable, idempotent)"),
    ):
        parser = fundamentals_sub.add_parser(name, help=description)
        parser.add_argument(
            "--store", default="data/sec_facts.parquet", help="Path to the persisted fact store"
        )
        parser.add_argument("--json", action="store_true", help="Emit structured JSON")
        if name == "sync":
            parser.add_argument(
                "--symbols",
                default=None,
                help="Comma-separated tickers; defaults to every instrument with daily history",
            )
            parser.add_argument(
                "--cache", default="data/sec_cache", help="Durable per-symbol payload cache"
            )
            parser.add_argument(
                "--force", action="store_true", help="Re-fetch every symbol, ignoring the cache"
            )

    _add_monitor_parser(sub)
    _add_publish_parser(sub)

    heartbeat = sub.add_parser(
        "heartbeat", help="Ping the external watchdog that detects this host going down"
    )
    heartbeat.add_argument("--json", action="store_true", help="Emit structured JSON")

    bot = sub.add_parser(
        "discord-bot", help="Run the interactive Discord bot (/check). Long-running."
    )
    bot.add_argument(
        "--facts", default="data/sec_facts.parquet", help="Fact store the Advisor reads"
    )
    bot.add_argument(
        "--check-config",
        action="store_true",
        help="Report configuration presence and exit; sends nothing",
    )

    fit = sub.add_parser(
        "portfolio-fit", help="Describe how a candidate fits a portfolio (read-only)"
    )
    fit.add_argument(
        "portfolio",
        help="PAPER_1K, PAPER_3K or PAPER_10K to read a real paper "
        "account, or any other name for a portfolio given inline",
    )
    fit.add_argument(
        "--facts",
        default="data/sec_facts.parquet",
        help="Fact store used for company context; omitted context "
        "leaves the portfolio analysis intact",
    )
    fit.add_argument("--cash", type=float, default=0.0, help="Cash held")
    fit.add_argument("--holding", action="append", help="Position as SYMBOL:QTY, repeatable")
    fit.add_argument("--candidate", default=None, help="Hypothetical candidate symbol")
    fit.add_argument(
        "--amount",
        type=float,
        default=None,
        help="Hypothetical cash amount to allocate to the candidate",
    )
    fit.add_argument("--as-of", dest="as_of", default=None, help="Analyse as of a date")
    fit.add_argument("--json", action="store_true", help="Emit structured JSON")

    portfolios = sub.add_parser("portfolios", help="Personal paper portfolios")
    portfolios_sub = portfolios.add_subparsers(dest="portfolios_command", required=True)
    portfolios_sub.add_parser("seed", help="Install the three personal portfolios")
    portfolios_sub.add_parser("list", help="Show portfolio equity and positions")

    ops = sub.add_parser("ops", help="Local operations and scheduling")
    ops_sub = ops.add_subparsers(dest="ops_command", required=True)
    ops_sub.add_parser("check", help="Validate this installation can run unattended")
    ops_sub.add_parser("status", help="What has run, and where the portfolios stand")
    ops_sub.add_parser("install", help="Write launchd templates (starts nothing)")
    ops_sub.add_parser("uninstall", help="Print the commands to remove them")
    ops_sub.add_parser(
        "daily-summary-if-due", help="Send the daily report once, after the session closes"
    )
    publish = ops_sub.add_parser(
        "status-publish", help="Refresh the persistent #status dashboard (edits one message)"
    )
    publish.add_argument(
        "--preview", action="store_true", help="Render the dashboard and send nothing"
    )
    publish.add_argument(
        "--test",
        action="store_true",
        help="Force a refresh even if nothing changed. Sends a REAL message.",
    )


def _add_scanner_parsers(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Watchlist and scanner commands.

    Grouped into their own builder because they are the largest command
    family and their declaration would otherwise dominate the parser.
    """
    watchlist = sub.add_parser("watchlist", help="Manage the scanner watchlist")
    watchlist_sub = watchlist.add_subparsers(dest="watchlist_command", required=True)
    watchlist_sub.add_parser("list", help="Show the watchlist")
    watchlist_sub.add_parser("seed", help="Seed the initial development universe")
    for name, helptext in (
        ("add", "Add a symbol"),
        ("enable", "Enable a symbol"),
        ("disable", "Disable a symbol"),
    ):
        parser_ = watchlist_sub.add_parser(name, help=helptext)
        parser_.add_argument("symbol")

    scanner = sub.add_parser("scanner", help="Continuous market scanner")
    scanner_sub = scanner.add_subparsers(dest="scanner_command", required=True)
    scanner_sub.add_parser("sync", help="Incrementally sync watchlist market data")
    scanner_sub.add_parser(
        "refresh-identity",
        help="Replace placeholder instrument metadata with the provider's (read-only)",
    )
    runner = scanner_sub.add_parser("run-once", help="Run one scan cycle")
    runner.add_argument("--no-paper", action="store_true", help="Skip paper-trading decisions")
    candidates = scanner_sub.add_parser("candidates", help="Show ranked current candidates")
    candidates.add_argument("--limit", type=int, default=5)
    scanner_sub.add_parser("overview", help="Send the ranked market overview")
    trends = scanner_sub.add_parser(
        "trends", help="Publish descriptive market activity from stored scan data"
    )
    trends.add_argument(
        "--preview", action="store_true", help="Print what would be sent and send nothing"
    )
    trends.add_argument(
        "--test",
        action="store_true",
        help="Send one clearly-marked TEST message with constructed symbols. Sends a REAL message.",
    )
    scanner_sub.add_parser("status", help="Scanner configuration and last-run state")
    scanner_sub.add_parser("demo", help="Deterministic end-to-end demonstration")
    scanner_sub.add_parser("daily-summary", help="Send the daily report (alias)")


def _add_walkforward_parser(research_sub: Any) -> None:
    """The validation command, split out to keep its parent within budget."""
    walk = research_sub.add_parser(
        "walkforward",
        help="Chronological out-of-sample validation by SCORE BAND (not `qualified`)",
    )
    walk.add_argument("--run-id", type=int, help="Restrict to one backtest run")
    walk.add_argument("--folds", type=int, default=8, help="Chronological test blocks")
    walk.add_argument("--horizons", default="1d,5d", help="Comma-separated outcome horizons")


def _add_research_parsers(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Backtesting, outcome labelling and research commands.

    None of these mutate production state: the backtest tags its observations
    with a run id that every live read filters out, so they are safe to run while
    the scheduler is going.
    """
    backtest = sub.add_parser("backtest", help="Historical replay of the production scanner")
    backtest_sub = backtest.add_subparsers(dest="backtest_command", required=True)

    runner = backtest_sub.add_parser("run", help="Replay a date range and simulate portfolios")
    runner.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD (UTC)")
    runner.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD (UTC)")
    runner.add_argument("--symbols", help="Comma-separated tickers; omit to use the watchlist")
    runner.add_argument("--universe", choices=["active"], help="Replay the active watchlist")
    runner.add_argument(
        "--timeframe", default=Timeframe.H1.value, choices=[t.value for t in Timeframe]
    )
    runner.add_argument(
        "--include-extended",
        action="store_true",
        help="Include pre/post-market observations (excluded from the benchmark by default)",
    )
    runner.add_argument(
        "--no-portfolios", action="store_true", help="Generate observations only, no execution"
    )

    status = backtest_sub.add_parser("status", help="Recent backtest runs")
    status.add_argument("--limit", type=int, default=10)

    report = backtest_sub.add_parser("report", help="Full metadata for one run")
    report.add_argument("run_id", type=int)

    outcomes = sub.add_parser("outcomes", help="Future outcome labels for stored evaluations")
    outcomes_sub = outcomes.add_subparsers(dest="outcomes_command", required=True)
    generate = outcomes_sub.add_parser(
        "generate", help="Compute labels; matures anything previously pending"
    )
    generate.add_argument("--since", help="Only evaluations from this date (YYYY-MM-DD)")
    generate.add_argument("--until", help="Only evaluations up to this date (YYYY-MM-DD)")
    generate.add_argument(
        "--recompute", action="store_true", help="Also revisit labels already complete"
    )
    outcomes_sub.add_parser("status", help="Label counts by status and horizon")

    research = sub.add_parser("research", help="Descriptive research reports and dataset export")
    research_sub = research.add_subparsers(dest="research_command", required=True)

    _add_walkforward_parser(research_sub)

    for name, helptext in (
        ("score-calibration", "Outcome quality by score band (measurement only)"),
        ("features", "Feature values against outcomes"),
        ("export", "Write a versioned research dataset and manifest"),
    ):
        parser_ = research_sub.add_parser(name, help=helptext)
        parser_.add_argument(
            "--horizon", default=Horizon.D1.value, choices=[h.value for h in Horizon]
        )
        parser_.add_argument("--run-id", type=int, help="Limit to one backtest run")
        if name == "score-calibration":
            parser_.add_argument(
                "--around-threshold",
                action="store_true",
                help="Use the finer 60-65..>=85 bands around the 75 cutoff",
            )
        if name == "features":
            parser_.add_argument("--feature", help="A feature name, or 'sector' / 'year'")
        if name == "export":
            parser_.add_argument("--out", default="exports", help="Output directory")
            parser_.add_argument("--format", default="parquet", choices=["parquet", "csv"])
            parser_.add_argument("--include-extended", action="store_true")

    planner = research_sub.add_parser(
        "storage-plan", help="Project the disk cost of a historical expansion"
    )
    planner.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    planner.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD")
    planner.add_argument("--symbols", help="Comma-separated tickers; omit for the watchlist")
    planner.add_argument("--universe", choices=["active"])
    planner.add_argument(
        "--cadence",
        type=float,
        default=6.0,
        help="Evaluations per symbol per session (6 = hourly, 26 = every 15 minutes)",
    )

    history = sub.add_parser("history", help="Historical market-data expansion")
    history.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    history.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD")
    history.add_argument("--symbols", help="Comma-separated tickers; omit for the watchlist")
    history.add_argument("--universe", choices=["active"])
    history.add_argument("--timeframes", default="5m,15m,1h,1d", help="Comma-separated timeframes")
    history.add_argument(
        "--no-resume", action="store_true", help="Re-request windows already stored"
    )
    history.add_argument(
        "--dry-run", action="store_true", help="Report the plan and download nothing"
    )


def _add_risk_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Read-only short-horizon risk."""
    risk = sub.add_parser(
        "risk", help="Short-horizon expected movement and risk bands (magnitude only)"
    )
    risk.add_argument(
        "symbols", nargs="?", help="Comma-separated tickers; omit for the whole watchlist"
    )


def _add_options_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """The option collector and the read-only risk view.

    Both live here rather than in ``_build_parser`` to keep that function within
    its statement budget; they are grouped because both are magnitude-only
    market-data surfaces with no order path.
    """
    _add_risk_parser(sub)

    options = sub.add_parser("options", help="Point-in-time option surface snapshots")
    options_sub = options.add_subparsers(dest="options_command", required=True)
    capture = options_sub.add_parser("capture", help="Capture today's option surface")
    capture.add_argument(
        "--dry-run", action="store_true", help="Fetch and derive without writing anything"
    )
    capture.add_argument(
        "--force", action="store_true", help="Capture even outside the window, replacing today's"
    )


def _build_parser() -> argparse.ArgumentParser:
    """Every command and its arguments.

    Separate from `main` so dispatch stays readable: the parser is
    declaration, `main` is behaviour, and mixing them made the entry point
    mostly setup.
    """
    parser = argparse.ArgumentParser(prog="tradabot", description="tradabot developer CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="Ingest deterministic synthetic market data")
    seed.add_argument("--symbols", help="Comma-separated tickers; omit for the full universe")
    seed.add_argument("--days", type=int, default=400, help="Days of history (default: 400)")

    signal = sub.add_parser("signal", help="Print an explainable signal for one symbol")
    signal.add_argument("symbol")
    signal.add_argument("--horizon", default=Horizon.D5.value, choices=[h.value for h in Horizon])

    sub.add_parser("seed-profiles", help="Install the default simulation-profile catalogue")
    sub.add_parser("demo-simulation", help="Run the deterministic paper-trading demo")

    simulate = sub.add_parser(
        "simulate", help="Replay imported real candles through the paper broker"
    )
    simulate.add_argument("--symbol", required=True)
    simulate.add_argument("--from", dest="start", required=True, help="ISO-8601 UTC start")
    simulate.add_argument("--to", dest="end", required=True, help="ISO-8601 UTC end")
    simulate.add_argument(
        "--timeframe", default=Timeframe.D1.value, choices=[t.value for t in Timeframe]
    )
    simulate.add_argument("--horizon", default=Horizon.D5.value, choices=[h.value for h in Horizon])

    _add_scanner_parsers(sub)
    _add_ops_parsers(sub)
    _add_research_parsers(sub)

    notify = sub.add_parser("notifications", help="Notification delivery")
    notify_sub = notify.add_subparsers(dest="notification_command", required=True)
    notify_sub.add_parser("status", help="Show configuration and delivery outcomes")
    notify_sub.add_parser(
        "demo-lifecycle",
        help="Send clearly-marked TEST messages to every destination (manual only)",
    )
    tester = notify_sub.add_parser("test", help="Send a labelled TEST message to each channel")
    tester.add_argument(
        "--category", choices=[c.value for c in EventCategory], help="Limit to one channel"
    )
    notify_sub.add_parser("daily-summary", help="Build and send the daily portfolio report")

    volatility = sub.add_parser(
        "volatility", help="Expected movement per symbol (magnitude only, read-only)"
    )
    volatility.add_argument("symbol", nargs="?", help="One ticker; omit for the whole watchlist")
    volatility.add_argument(
        "--preview", action="store_true", help="Render the Discord view and send nothing"
    )

    market = sub.add_parser("market-data", help="Market-data provider operations")
    market_sub = market.add_subparsers(dest="market_command", required=True)
    market_sub.add_parser("status", help="Show provider configuration and data freshness")

    benchmarks = market_sub.add_parser(
        "benchmarks", help="Market and sector reference instruments (never watchlisted)"
    )
    benchmarks.add_argument(
        "--register",
        action="store_true",
        help="Create any missing benchmark instruments (writes to `instruments` only)",
    )

    _add_options_parser(sub)

    verifier = market_sub.add_parser(
        "verify-adjustments",
        help="Report price jumps the stored corporate actions do not explain",
    )
    verifier.add_argument(
        "symbols", nargs="?", help="Comma-separated tickers; omit to scan every instrument"
    )

    actions = market_sub.add_parser(
        "corporate-actions", help="Fetch splits and dividends used to adjust prices on read"
    )
    actions.add_argument(
        "symbols", nargs="?", help="Comma-separated tickers; omit for every stored instrument"
    )

    importer = market_sub.add_parser("import", help="Import an explicit historical window")
    importer.add_argument("symbols", help="Comma-separated tickers")
    importer.add_argument("--start", required=True, help="ISO-8601 UTC start")
    importer.add_argument("--end", required=True, help="ISO-8601 UTC end")
    importer.add_argument(
        "--timeframe", default=Timeframe.D1.value, choices=[t.value for t in Timeframe]
    )

    syncer = market_sub.add_parser("sync", help="Update symbols from their newest stored bar")
    syncer.add_argument(
        "symbols", nargs="?", help="Comma-separated tickers; omit for the watchlist"
    )

    quoter = market_sub.add_parser("quote", help="Fetch one latest quote")
    quoter.add_argument("symbol")
    sub.add_parser("create-tables", help="Create tables from metadata (SQLite dev only)")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    try:
        return _COMMANDS[args.command](settings, args)
    except OperationalError as exc:
        # A schema behind the code is an ordinary situation with a one-line fix,
        # and a two-hundred-line SQLAlchemy traceback buries it. Only missing
        # tables and columns are translated; anything else re-raises, because a
        # real database fault should not be dressed up as a migration prompt.
        missing = _missing_schema_object(exc)
        if missing is None:
            raise
        print(f"\nThe database is missing `{missing}`.", file=sys.stderr)
        print("The schema is older than the code. Apply the migrations:", file=sys.stderr)
        print("\n    make migrate\n", file=sys.stderr)
        print("Then re-run this command.", file=sys.stderr)
        return 2


def _missing_schema_object(exc: OperationalError) -> str | None:
    """The table or column a database error says is absent, if that is the cause."""
    message = str(getattr(exc, "orig", exc))
    for marker in ("no such table: ", "no such column: ", "does not exist"):
        if marker in message:
            fragment = (
                message.rsplit(marker, maxsplit=1)[-1].strip()
                if marker != "does not exist"
                else message
            )
            return fragment.split()[0].strip("\"'")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
