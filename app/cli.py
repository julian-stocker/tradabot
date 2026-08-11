"""Command-line entry points for local development.

Kept deliberately small: it wires existing services together and prints results.
No business logic lives here.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.events import Event, EventCategory, EventType
from app.core.logging import configure_logging
from app.core.time import utc_now
from app.corporate_actions.repository import CorporateActionRepository
from app.db.base import Base
from app.db.models import Candle, Instrument
from app.db.session import create_engine, create_session_factory, session_scope
from app.domain.enums import Horizon, Timeframe
from app.features.service import FeatureService
from app.instruments.repository import InstrumentRepository
from app.instruments.service import InstrumentService
from app.market_data.calendars import get_trading_calendar
from app.market_data.import_service import MarketDataImportService
from app.market_data.ingest import IngestionService
from app.market_data.registry import build_provider
from app.market_data.repository import CandleRepository
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.notifications.summary import build_daily_summary, daily_summary_already_sent
from app.ops.check import operational_status, run_checks
from app.ops.launchd import (
    LAUNCH_AGENTS_DIR,
    ScheduledJob,
    install_commands,
    scheduled_jobs,
    uninstall_commands,
    write_plists,
)
from app.ownership.service import ensure_local_ownership
from app.paper.demo import run_demo
from app.paper.performance import PerformanceSummary
from app.paper.replay import REPLAY_DISCLAIMER, ReplayError, replay_symbol
from app.scanner.demo import DemoResult, phase_boundaries, seed_demo_instrument
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

        print(f"\nchecked at        : {status.checked_at.isoformat()}")
        print(f"session           : {status.session_phase}")
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


async def _scanner_overview(settings: Settings) -> int:
    """Send the ranked overview to the market channel."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        scanner = _build_scanner(settings, factory)
        candidates = await scanner.top_candidates()
        service = NotificationService(settings, session_factory=factory)
        await service.publish(
            Event(
                type=EventType.MARKET_OVERVIEW,
                occurred_at=utc_now(),
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
        print(f"\noverview sent with {len(candidates)} candidates")
        if not candidates:
            print("(reported honestly as 'no qualified opportunities')")
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
    demo_settings = settings.model_copy(
        update={"notifications": settings.notifications.model_copy(update={"console": True})}
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


def _run_market_data(settings: Settings, args: argparse.Namespace) -> int:
    """Dispatch a ``market-data`` subcommand.

    ``quote`` is the final branch rather than one more ``if`` because the
    subparser is declared ``required=True``: argparse has already rejected every
    other value, so a fallback arm here would be unreachable code pretending to
    be defensive.
    """
    command = args.market_command
    if command == "status":
        return asyncio.run(_market_data_status(settings))
    if command == "import":
        return asyncio.run(
            _market_data_import(
                settings,
                _parse_symbols(args.symbols),
                _parse_utc(args.start),
                _parse_utc(args.end),
                Timeframe(args.timeframe),
            )
        )
    if command == "sync":
        return asyncio.run(_market_data_sync(settings, _parse_symbols(args.symbols) or None))
    return asyncio.run(_market_data_quote(settings, args.symbol.upper()))


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

    simple: dict[str, Callable[[Settings], Any]] = {
        "sync": _scanner_sync,
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
    return asyncio.run(_notifications_test(settings, args.category))


_COMMANDS: dict[str, Callable[[Settings, argparse.Namespace], int]] = {
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
    "market-data": _run_market_data,
    "notifications": _run_notifications,
    "watchlist": _run_watchlist,
    "scanner": _run_scanner,
    "portfolios": _run_portfolios,
    "ops": _run_ops,
}


def _add_ops_parsers(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Portfolio and operations commands."""
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
    runner = scanner_sub.add_parser("run-once", help="Run one scan cycle")
    runner.add_argument("--no-paper", action="store_true", help="Skip paper-trading decisions")
    candidates = scanner_sub.add_parser("candidates", help="Show ranked current candidates")
    candidates.add_argument("--limit", type=int, default=5)
    scanner_sub.add_parser("overview", help="Send the ranked market overview")
    scanner_sub.add_parser("status", help="Scanner configuration and last-run state")
    scanner_sub.add_parser("demo", help="Deterministic end-to-end demonstration")
    scanner_sub.add_parser("daily-summary", help="Send the daily report (alias)")


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

    notify = sub.add_parser("notifications", help="Notification delivery")
    notify_sub = notify.add_subparsers(dest="notification_command", required=True)
    notify_sub.add_parser("status", help="Show configuration and delivery outcomes")
    tester = notify_sub.add_parser("test", help="Send a labelled TEST message to each channel")
    tester.add_argument(
        "--category", choices=[c.value for c in EventCategory], help="Limit to one channel"
    )
    notify_sub.add_parser("daily-summary", help="Build and send the daily portfolio report")

    market = sub.add_parser("market-data", help="Market-data provider operations")
    market_sub = market.add_subparsers(dest="market_command", required=True)
    market_sub.add_parser("status", help="Show provider configuration and data freshness")

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

    # A table rather than a chain of `if`s: argparse has already rejected any
    # command that is not a key here, so a lookup is exhaustive by construction.
    return _COMMANDS[args.command](settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
