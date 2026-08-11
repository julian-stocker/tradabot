"""Command implementations for ``backtest``, ``outcomes`` and ``research``.

Kept out of :mod:`app.cli` because that module is already the largest in the
project; the argparse wiring stays there, the work happens here.

Every command is read-only with respect to production state. The backtest writes
observations tagged with a run id, the labeller writes outcome rows, and neither
touches portfolios, tracked signals, scan runs or notifications -- so all of this
is safe to run while the scheduler is live.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.backtesting.engine import BacktestConfig
from app.backtesting.runner import PortfolioResult, run_backtest
from app.core.config import Settings
from app.db.session import create_engine, create_session_factory, session_scope
from app.domain.enums import Horizon, Timeframe
from app.research.analytics import (
    SCORE_BANDS,
    THRESHOLD_BANDS,
    GroupStats,
    by_feature_quantile,
    by_score_band,
    by_sector,
    load_observations,
)
from app.research.export import build_dataset, write_dataset
from app.research.repository import BacktestRunRepository, OutcomeRepository
from app.research.service import OutcomeLabellingService
from app.scanner.repository import WatchlistRepository

DEFAULT_EXPORT_DIR = Path("exports")

_DATE_ONLY_LENGTH = len("YYYY-MM-DD")


async def backtest_run(
    settings: Settings,
    *,
    start: datetime,
    end: datetime,
    symbols: list[str] | None,
    universe: str | None,
    timeframe: Timeframe,
    include_extended: bool,
    skip_portfolios: bool,
) -> int:
    """Replay history and simulate every enabled portfolio over it."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        resolved, scope = await _resolve_symbols(factory, symbols, universe)
        if not resolved:
            print("no symbols to replay; seed the watchlist or pass --symbols")
            return 1

        config = BacktestConfig(
            symbols=tuple(resolved),
            start=start,
            end=end,
            primary_timeframe=timeframe,
            regular_session_only=not include_extended,
            scope=scope,
        )
        print(
            f"replaying {len(resolved)} symbol(s) {start:%Y-%m-%d} to {end:%Y-%m-%d} "
            f"on {timeframe.value} "
            f"({'regular session only' if not include_extended else 'all sessions'})"
        )
        print(f"run key: {config.run_key()}")

        run_id, stats, results = await run_backtest(
            factory, settings, config, simulate_portfolios=not skip_portfolios
        )

        print(
            f"\nrun #{run_id}: {stats.observations} observations across "
            f"{stats.symbols_processed} symbols at {stats.timestamps} instants "
            f"in {stats.duration_seconds:.1f}s"
        )
        print(
            f"  qualified: {stats.qualified}   skipped (insufficient data): "
            f"{stats.skipped_insufficient}"
        )
        if stats.errors:
            print(f"  errors: {len(stats.errors)} (first: {stats.errors[0]})")

        if results:
            _print_portfolios(results)
        return 0
    finally:
        await engine.dispose()


async def backtest_status(settings: Settings, *, limit: int = 10) -> int:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            runs = await BacktestRunRepository(session).list_recent(limit)
            if not runs:
                print("no backtest runs recorded")
                return 0
            print(f"{'id':>4}  {'status':<10} {'observations':>12}  {'window':<24} started")
            for run in runs:
                window = f"{run.from_timestamp:%Y-%m-%d}..{run.to_timestamp:%Y-%m-%d}"
                print(
                    f"{run.id:>4}  {run.status:<10} {run.observation_count:>12}  "
                    f"{window:<24} {run.started_at:%Y-%m-%d %H:%M}"
                )
        return 0
    finally:
        await engine.dispose()


async def backtest_report(settings: Settings, *, run_id: int) -> int:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            run = await BacktestRunRepository(session).get(run_id)
            if run is None:
                print(f"no backtest run #{run_id}")
                return 1
            payload = {
                "id": run.id,
                "run_key": run.run_key,
                "status": run.status,
                "window": [run.from_timestamp.isoformat(), run.to_timestamp.isoformat()],
                "primary_timeframe": run.primary_timeframe,
                "regular_session_only": run.regular_session_only,
                "universe": run.universe_definition,
                "versions": {
                    "engine": run.engine_version,
                    "feature_set": run.feature_set_version,
                    "signal_model": run.signal_model_version,
                    "scanner_policy": run.scanner_policy_version,
                    "cost_model": run.cost_model_version,
                    "label_policy": run.label_policy_version,
                },
                "observations": run.observation_count,
                "duration_seconds": run.duration_seconds,
                "metrics": run.metrics,
            }
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    finally:
        await engine.dispose()


async def outcomes_generate(
    settings: Settings,
    *,
    since: datetime | None,
    until: datetime | None,
    recompute: bool,
) -> int:
    """Label every stored evaluation, maturing anything previously pending."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            service = OutcomeLabellingService(session)
            report = await service.generate(since=since, until=until, recompute=recompute)
        print(report.summary())
        if report.errors:
            print(f"  {len(report.errors)} error(s); first: {report.errors[0]}")
        return 0 if report.ok else 1
    finally:
        await engine.dispose()


async def outcomes_status(settings: Settings) -> int:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            repo = OutcomeRepository(session)
            total = await repo.total_count()
            statuses = await repo.status_counts()
            horizons = await repo.horizon_counts()

        print(f"outcome labels: {total}")
        for status, count in sorted(statuses.items()):
            print(f"  {status:<26} {count:>7}")
        print("\ncomplete labels by horizon:")
        if not horizons:
            print("  (none yet)")
        for horizon, count in sorted(horizons.items()):
            print(f"  {horizon:<26} {count:>7}")
        return 0
    finally:
        await engine.dispose()


async def research_calibration(
    settings: Settings, *, horizon: Horizon, run_id: int | None, threshold_view: bool
) -> int:
    """Score bands against outcomes. **Measurement, not tuning.**"""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            rows = await load_observations(session, horizon=horizon, backtest_run_id=run_id)
        if not rows:
            print(f"no complete {horizon.value} labels yet; run `outcomes generate` first")
            return 0

        bands = THRESHOLD_BANDS if threshold_view else SCORE_BANDS
        print(f"score calibration, horizon {horizon.value}, n={len(rows)}")
        print("(descriptive only -- this does not justify moving the 75/85 thresholds)\n")
        _print_groups(by_score_band(rows, bands=bands))
        return 0
    finally:
        await engine.dispose()


async def research_features(
    settings: Settings, *, horizon: Horizon, feature: str | None, run_id: int | None
) -> int:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            rows = await load_observations(session, horizon=horizon, backtest_run_id=run_id)
        if not rows:
            print(f"no complete {horizon.value} labels yet; run `outcomes generate` first")
            return 0

        if feature == "sector":
            print(f"sector vs {horizon.value} outcome, n={len(rows)}\n")
            _print_groups(by_sector(rows))
            return 0

        features = [feature] if feature else list(_DEFAULT_FEATURES)
        for name in features:
            groups = by_feature_quantile(rows, feature=name)
            if not groups:
                print(f"{name}: too few observations carry this feature\n")
                continue
            print(f"{name} vs {horizon.value} outcome, n={len(rows)}\n")
            _print_groups(groups)
            print()
        return 0
    finally:
        await engine.dispose()


async def research_export(
    settings: Settings,
    *,
    horizon: Horizon,
    run_id: int | None,
    directory: Path,
    fmt: str,
    include_extended: bool,
) -> int:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            result = await build_dataset(
                session,
                horizon=horizon,
                backtest_run_id=run_id,
                regular_session_only=not include_extended,
            )
        stem = f"tradabot-{horizon.value}-{result.manifest.created_at:%Y%m%d}"
        written = write_dataset(result, directory=directory, stem=stem, fmt=fmt)

        print(f"rows       : {result.manifest.row_count}")
        print(f"symbols    : {len(result.manifest.symbols)}")
        print(f"features   : {len(result.manifest.feature_columns)}")
        print(f"labels     : {len(result.manifest.label_columns)}")
        if result.manifest.excluded:
            print("excluded   :")
            for reason, count in result.manifest.excluded.items():
                print(f"  {reason:<32} {count:>7}")
        print(f"data       : {written.data_path}")
        print(f"manifest   : {written.manifest_path}")
        return 0
    finally:
        await engine.dispose()


_DEFAULT_FEATURES = ("score", "relative_volume", "rsi", "volatility", "agreement")


def _print_groups(groups: list[GroupStats]) -> None:
    header = f"{'group':<34} {'n':>6} {'mean':>9} {'median':>9} {'pos%':>7} {'MFE':>8} {'MAE':>8}"
    print(header)
    print("-" * len(header))
    for group in groups:
        if group.n == 0:
            print(f"{group.label:<34} {0:>6}        --        --      --       --       --")
            continue
        flag = "" if group.is_reportable else "  (small n)"
        print(
            f"{group.label:<34} {group.n:>6} "
            f"{_pct(group.mean_return):>9} {_pct(group.median_return):>9} "
            f"{_rate(group.positive_rate):>7} "
            f"{_pct(group.mean_mfe):>8} {_pct(group.mean_mae):>8}{flag}"
        )


def _print_portfolios(results: list[PortfolioResult]) -> None:
    print("\nper-portfolio execution (costs MODELLED, never observed):")
    header = (
        f"{'portfolio':<14} {'attempt':>8} {'exec':>6} {'rej':>5} "
        f"{'net P/L':>10} {'return':>8} {'win%':>7} {'maxDD':>8} {'end equity':>11}"
    )
    print(header)
    print("-" * len(header))
    for result in results:
        print(
            f"{result.profile_key:<14} {result.attempted:>8} {result.executed:>6} "
            f"{result.rejected:>5} {float(result.net_pnl):>10.2f} "
            f"{result.net_return * 100:>7.2f}% {_rate(result.win_rate):>7} "
            f"{float(result.max_drawdown) * 100:>7.2f}% {float(result.ending_equity):>11.2f}"
        )
    for result in results:
        if result.rejection_reasons:
            reasons = ", ".join(
                f"{reason}={count}" for reason, count in result.rejection_reasons.items()
            )
            print(f"  {result.profile_key} rejections: {reasons}")


def _pct(value: float | None) -> str:
    return "--" if value is None else f"{value * 100:.3f}%"


def _rate(value: float | None) -> str:
    return "--" if value is None else f"{value * 100:.1f}%"


async def _resolve_symbols(
    factory: Any, symbols: list[str] | None, universe: str | None
) -> tuple[list[str], str]:
    """Resolve the requested scope to an explicit symbol list.

    Resolved eagerly and stored on the run, because "the active universe" is a
    moving target: re-reading it next month would silently make two runs
    incomparable while both claimed the same scope.
    """
    if symbols:
        return [symbol.upper() for symbol in symbols], "symbols"
    async with session_scope(factory) as session:
        watchlist = await WatchlistRepository(session).symbols()
    return list(watchlist), universe or "active"


def parse_day(value: str) -> datetime:
    """Parse ``YYYY-MM-DD`` (or a full ISO instant) as UTC."""
    text = value.strip()
    if len(text) == _DATE_ONLY_LENGTH:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
