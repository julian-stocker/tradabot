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

from sqlalchemy import select

from app.core.config import Settings, get_settings
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
from app.market_data.import_service import MarketDataImportService
from app.market_data.ingest import IngestionService
from app.market_data.registry import build_provider
from app.market_data.repository import CandleRepository
from app.paper.demo import run_demo
from app.paper.performance import PerformanceSummary
from app.paper.replay import REPLAY_DISCLAIMER, ReplayError, replay_symbol
from app.signals.service import SignalService
from app.simulation.defaults import build_default_profiles
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
}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
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

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    # A table rather than a chain of `if`s: argparse has already rejected any
    # command that is not a key here, so a lookup is exhaustive by construction.
    return _COMMANDS[args.command](settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
