"""Command-line entry points for local development.

Kept deliberately small: it wires existing services together and prints results.
No business logic lives here.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.time import utc_now
from app.corporate_actions.repository import CorporateActionRepository
from app.db.base import Base
from app.db.session import create_engine, create_session_factory, session_scope
from app.domain.enums import Horizon, Timeframe
from app.features.service import FeatureService
from app.instruments.repository import InstrumentRepository
from app.instruments.service import InstrumentService
from app.market_data.ingest import IngestionService
from app.market_data.registry import build_provider
from app.market_data.repository import CandleRepository
from app.paper.demo import run_demo
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
    sub.add_parser("create-tables", help="Create tables from metadata (SQLite dev only)")

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    if args.command == "seed":
        symbols = (
            [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            if args.symbols
            else None
        )
        return asyncio.run(_seed(settings, symbols, args.days))
    if args.command == "signal":
        return asyncio.run(_signal(settings, args.symbol.upper(), Horizon(args.horizon)))
    if args.command == "seed-profiles":
        return asyncio.run(_seed_profiles(settings))
    if args.command == "demo-simulation":
        return asyncio.run(_demo(settings))

    # The subparser is declared with `required=True`, so argparse has already
    # rejected anything that is not one of the three registered commands.
    return asyncio.run(_create_tables(settings))


if __name__ == "__main__":
    raise SystemExit(main())
