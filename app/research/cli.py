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
from app.backtesting.modes import ReplayMode
from app.backtesting.runner import PortfolioResult, run_backtest
from app.core.config import Settings
from app.db.session import create_engine, create_session_factory, session_scope
from app.domain.enums import Horizon, Timeframe
from app.market_data.backfill import ChunkResult, HistoricalBackfill, chunk_windows
from app.market_data.calendars import get_trading_calendar
from app.market_data.registry import build_provider
from app.research.analytics import (
    SCORE_BANDS,
    THRESHOLD_BANDS,
    GroupStats,
    by_feature_quantile,
    by_score_band,
    by_sector,
    by_year,
    load_observations,
)
from app.research.export import build_dataset, write_dataset
from app.research.repository import BacktestRunRepository, OutcomeRepository
from app.research.service import OutcomeLabellingService
from app.research.storage import build_plan, human_bytes
from app.research.walkforward import (
    BASELINE,
    SCORE_GE_75,
    SCORE_GE_85,
    THRESHOLD_75,
    THRESHOLD_85,
    assess_stability,
    bootstrap_difference,
    bootstrap_positive_rate,
    build_folds,
    episodes_for,
    evaluate_fold,
)
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

        if feature == "year":
            print(f"calendar year vs {horizon.value} outcome, n={len(rows)}\n")
            _print_groups(by_year(rows))
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


async def storage_plan(
    settings: Settings,
    *,
    start: datetime,
    end: datetime,
    symbols: list[str] | None,
    universe: str | None,
    cadence: float,
) -> int:
    """Project the storage cost of an expansion. Reads only; downloads nothing."""
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        resolved, _ = await _resolve_symbols(factory, symbols, universe)
        calendar = get_trading_calendar(settings.market_data.default_exchange)
        plan = build_plan(
            calendar=calendar,
            symbols=len(resolved),
            start=start,
            end=end,
            evaluations_per_session=cadence,
        )

        print(f"storage plan  {start:%Y-%m-%d} -> {end:%Y-%m-%d}   ({plan.measurement_version})")
        print(f"  symbols                 {plan.symbols}")
        print(f"  trading sessions        {plan.sessions:,}")
        print(f"  timeframes              {', '.join(t.value for t in plan.timeframes)}")
        print()
        print(f"  candle rows             {plan.candle_rows:>14,}")
        print(f"  evaluation rows         {plan.evaluation_rows:>14,}   (at {cadence:g}/session)")
        print(f"  outcome rows            {plan.outcome_rows:>14,}")
        print(f"  trade outcome rows      {plan.trade_outcome_rows:>14,}")
        print()
        print(f"  {'':<22}{'LOW':>12}{'EXPECTED':>12}{'HIGH':>12}")
        for label, rng in (
            ("raw market data", plan.raw_bytes),
            ("research data", plan.research_bytes),
            ("parquet export", plan.export_bytes),
            ("TOTAL db growth", plan.total_bytes),
        ):
            print(
                f"  {label:<22}{human_bytes(rng.low):>12}"
                f"{human_bytes(rng.expected):>12}{human_bytes(rng.high):>12}"
            )
        print()
        if plan.disk is not None:
            print(f"  free disk               {human_bytes(plan.disk.free_bytes)}")
            print(f"  required incl. headroom {human_bytes(plan.disk.required_bytes)}")
            print(f"  verdict                 {plan.disk.verdict} -- {plan.disk.detail}")
        for note in plan.notes:
            print(f"  note: {note}")
        return 0 if plan.disk is None or plan.disk.verdict != "UNSAFE" else 1
    finally:
        await engine.dispose()


async def historical_backfill(
    settings: Settings,
    *,
    start: datetime,
    end: datetime,
    symbols: list[str] | None,
    universe: str | None,
    timeframes: list[str],
    resume: bool,
    dry_run: bool,
) -> int:
    """Expand stored history in resumable chunks."""

    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        resolved, _ = await _resolve_symbols(factory, symbols, universe)
        if not resolved:
            print("no symbols; seed the watchlist or pass --symbols")
            return 1
        frames = [Timeframe(value) for value in timeframes]

        calendar = get_trading_calendar(settings.market_data.default_exchange)
        plan = build_plan(
            calendar=calendar,
            symbols=len(resolved),
            start=start,
            end=end,
            timeframes=tuple(frames),
            include_research=False,
        )
        # Requests, not (symbol x window) pairs: one batched call covers the
        # whole universe for a window, which is what makes this finish at all.
        requests = sum(
            len(list(chunk_windows(start=start, end=end, timeframe=tf))) for tf in frames
        )

        print(f"historical backfill  {start:%Y-%m-%d} -> {end:%Y-%m-%d}")
        print(
            f"  symbols {len(resolved)}  timeframes {','.join(timeframes)}  "
            f"sessions {plan.sessions:,}"
        )
        print(f"  projected {plan.candle_rows:,} rows, {human_bytes(plan.raw_bytes.expected)}")
        print(
            f"  {requests:,} batched requests (52 symbols each); "
            f"disk verdict {plan.disk.verdict if plan.disk else 'n/a'}"
        )

        if plan.disk is not None and plan.disk.verdict == "UNSAFE":
            print(f"  REFUSED: {plan.disk.detail}")
            return 1
        if dry_run:
            print("  dry run; nothing downloaded")
            return 0

        # A provider check that cannot be satisfied by mock data: historical
        # expansion must never pollute the archive with synthetic bars.
        if settings.market_data_provider != "alpaca":
            print(f"  REFUSED: provider is {settings.market_data_provider!r}, not alpaca")
            return 1

        done = {"n": 0, "rows": 0}

        def on_chunk(result: ChunkResult) -> None:
            done["n"] += 1
            done["rows"] += result.inserted
            if done["n"] % 5 == 0 or not result.ok:
                flag = "" if result.ok else f"  FAILED: {result.error}"
                print(
                    f"  [{done['n']:>4}/{requests}] {result.timeframe.value:<4} "
                    f"{result.start:%Y-%m-%d} +{result.inserted:>7,} rows "
                    f"(total {done['rows']:,}){flag}",
                    flush=True,
                )

        backfill = HistoricalBackfill(
            factory, build_provider(settings), exchange=settings.market_data.default_exchange
        )
        report = await backfill.run(
            symbols=resolved,
            timeframes=frames,
            start=start,
            end=end,
            resume=resume,
            progress=on_chunk,
        )
        print(f"\n{report.summary()}")
        for failure in report.failed[:10]:
            print(
                f"  failed: {failure.symbol} {failure.timeframe.value} "
                f"{failure.start:%Y-%m-%d} -- {failure.error}"
            )
        return 0 if report.ok else 1
    finally:
        await engine.dispose()


async def research_walkforward(
    settings: Settings,
    *,
    run_id: int | None = None,
    folds: int = 8,
    horizons: tuple[str, ...] = ("1d", "5d"),
) -> int:
    """Chronological out-of-sample validation of the frozen scoring rule.

    Reports each fold separately and never averages a thin one into a headline.
    The grouping variable is the **score band**, not the production ``qualified``
    flag -- see :mod:`app.research.walkforward` for why that distinction is
    load-bearing rather than pedantic.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            run = await BacktestRunRepository(session).get(run_id) if run_id else None
            if run_id is not None and run is None:
                print(f"no backtest run {run_id}")
                return 1

            mode = run.replay_mode if run else "LIVE+ALL"
            available = run.available_timeframes if run else "-"
            print(f"\nWALK-FORWARD VALIDATION   run={run_id or 'all'}  mode={mode}")
            print(f"timeframes available: {available}")
            print(
                "grouping: SCORE BANDS (score >= 75 / >= 85). NOT the production `qualified` flag."
            )
            if run is not None and run.replay_mode == ReplayMode.COARSE_HISTORICAL.value:
                print(
                    "NOTE: in this window `qualified` and `aligned` are structurally "
                    "false (5m/15m absent), so neither is used or reported."
                )

            for horizon_name in horizons:
                await _walkforward_horizon(
                    session, run=run, horizon_name=horizon_name, fold_count=folds
                )
        return 0
    finally:
        await engine.dispose()


async def _walkforward_horizon(
    session: Any, *, run: Any, horizon_name: str, fold_count: int
) -> None:
    rows = await load_observations(
        session,
        horizon=Horizon(horizon_name),
        backtest_run_id=run.id if run else None,
        complete_only=True,
    )
    dated = [row for row in rows if row.timestamp is not None]
    print(
        f"\n{'=' * 78}\nHORIZON {horizon_name}   observations with complete labels: {len(dated):,}"
    )
    if not dated:
        print("  (none)")
        return

    start = min(row.timestamp for row in dated if row.timestamp)
    end = max(row.timestamp for row in dated if row.timestamp)

    count = fold_count
    while count > 1:
        try:
            folds = build_folds(start=start, end=end, count=count, horizon=horizon_name)
            break
        # Fewer folds rather than folds shorter than their own outcome window.
        except ValueError:
            count -= 1
    else:
        print("  window too short for any fold")
        return
    if count != fold_count:
        print(f"  using {count} folds; {fold_count} would be shorter than the outcome window")

    results = [evaluate_fold(dated, fold, horizon=horizon_name) for fold in folds]

    header = (
        f"  {'FOLD':<22}{'BAND':<16}{'obs':>8}{'eps':>6}{'pos%':>8}{'mean%':>9}{'MFE':>8}{'MAE':>8}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for result in results:
        for band in (BASELINE, SCORE_GE_75, SCORE_GE_85):
            entry = result.bands.get(band)
            if entry is None:
                continue
            stats = entry.episodes
            flag = "  <- thin" if entry.is_thin else ""
            print(
                f"  {result.fold.label:<18}{band:<16}"
                f"{entry.observations.n:>8}{entry.episode_count:>6}"
                f"{_rate(stats.positive_rate):>8}{_pct(stats.mean_return):>9}"
                f"{_pct(stats.mean_mfe):>8}{_pct(stats.mean_mae):>8}{flag}"
            )
        print()

    for band in (SCORE_GE_75, SCORE_GE_85):
        verdict = assess_stability(results, band=band)
        print(
            f"  STABILITY {band}: better in {verdict.folds_better}/{verdict.folds_measured} folds, "
            f"worse in {verdict.folds_worse}, median delta "
            f"{_pct(verdict.median_delta)}"
            f"{'  DOMINATED BY ONE FOLD' if verdict.dominated_by_one_fold else ''}"
            f"{'  [consistent]' if verdict.consistent else '  [not consistent]'}"
        )

    _print_pooled_uncertainty(dated, horizon_name=horizon_name)
    _print_subgroup(
        dated,
        title=f"YEAR STABILITY ({horizon_name}) -- episode level",
        key=lambda row: row.year,
        note="calendar years: crude on purpose, so they cannot be tuned to flatter a result",
    )
    _print_subgroup(
        dated,
        title=f"SECTOR STABILITY ({horizon_name}) -- episode level",
        key=lambda row: row.sector,
        note="watchlist sectors, unchanged; thin groups are marked and should not be read",
    )
    _print_extension(dated, horizon_name=horizon_name)


def _print_pooled_uncertainty(rows: list[Any], *, horizon_name: str) -> None:
    """Episode-level intervals over the pooled window.

    Pooled *after* the per-fold table, never instead of it: pooling is what makes
    an unstable effect look steady, so it is reported as the weaker evidence it
    is.
    """
    baseline = [
        row.raw_return
        for _, row in _collapse(rows, threshold=float("-inf"), ceiling=THRESHOLD_75)
        if row.raw_return is not None
    ]
    print(f"\n  POOLED EPISODE-LEVEL UNCERTAINTY ({horizon_name}, 95% bootstrap)")
    print(f"    baseline <75      episodes={len(baseline):>5}  rate={_rate(_positive(baseline))}")

    for label, threshold in ((SCORE_GE_75, THRESHOLD_75), (SCORE_GE_85, THRESHOLD_85)):
        values = [
            row.raw_return
            for _, row in _collapse(rows, threshold=threshold, ceiling=float("inf"))
            if row.raw_return is not None
        ]
        interval = bootstrap_positive_rate(values)
        delta = bootstrap_difference(values, baseline)
        print(
            f"    {label:<17} episodes={len(values):>5}  rate={_rate(_positive(values))}  "
            f"CI={_interval(interval)}  delta-vs-baseline CI={_interval(delta)}"
        )


def _collapse(rows: list[Any], *, threshold: float, ceiling: float) -> list[Any]:
    selected = [row for row in rows if threshold <= row.score < ceiling]
    return episodes_for(selected, threshold=threshold if threshold > float("-inf") else -1e9)


def _positive(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value > 0) / len(values)


def _interval(interval: tuple[float, float] | None) -> str:
    if interval is None:
        return "n/a (too thin)"
    return f"[{interval[0] * 100:.1f}%, {interval[1] * 100:.1f}%]"


def _episode_returns(rows: list[Any], *, threshold: float, ceiling: float) -> list[float]:
    """Episode-level returns for one score band. Independence, not row count."""
    return [
        row.raw_return
        for _, row in _collapse(rows, threshold=threshold, ceiling=ceiling)
        if row.raw_return is not None
    ]


def _print_subgroup(
    rows: list[Any], *, title: str, key: Any, note: str = "", min_episodes: int = 5
) -> None:
    """SCORE_GE_85 against baseline within each subgroup, always with n.

    Subgroups are the existing deterministic splits (calendar year, watchlist
    sector). No new grouping is invented here: searching for a subgroup where the
    signal works is how a null result gets converted into a false positive, and
    the more history there is the easier that search becomes.
    """
    groups = sorted({key(row) for row in rows if key(row) is not None}, key=str)
    if not groups:
        return

    print(f"\n  {title}")
    if note:
        print(f"    {note}")
    print(
        f"    {'GROUP':<16}{'base eps':>10}{'base pos%':>11}"
        f"{'GE85 eps':>10}{'GE85 pos%':>11}{'delta':>9}"
    )
    for group in groups:
        subset = [row for row in rows if key(row) == group]
        base = _episode_returns(subset, threshold=float("-inf"), ceiling=THRESHOLD_75)
        high = _episode_returns(subset, threshold=THRESHOLD_85, ceiling=float("inf"))
        base_rate = _positive(base)
        high_rate = _positive(high)
        delta = (
            f"{(high_rate - base_rate) * 100:+.1f}pp"
            if base_rate is not None and high_rate is not None
            else "-"
        )
        thin = "  <- thin" if len(high) < min_episodes else ""
        print(
            f"    {group!s:<16}{len(base):>10}{_rate(base_rate):>11}"
            f"{len(high):>10}{_rate(high_rate):>11}{delta:>9}{thin}"
        )


EXTENSION_FEATURES: tuple[str, ...] = (
    "atr_pct",
    "ema_spread_pct",
    "rsi",
    "relative_volume",
    "volatility",
)
"""The phase-5.8 extension features, unchanged.

Frozen deliberately: adding a feature here after seeing a null result would be
searching for one that works, which is exactly the exhaustion analysis's failure
mode rather than its purpose.
"""


def _print_extension(rows: list[Any], *, horizon_name: str) -> None:
    """Does high extension worsen downside while leaving upside intact?

    Buckets are **quantiles of the observed distribution**, not fixed cut points:
    a hard threshold would be a parameter chosen on this data. The question is
    directional -- does MAE deteriorate across buckets while MFE holds -- and it
    is asked of the SCORE_GE_85 subset, because that is the population a future
    entry rule would actually face.
    """
    high = [row for row in rows if row.score >= THRESHOLD_85]
    print(f"\n  EXTENSION / EXHAUSTION ({horizon_name}, SCORE_GE_85 subset, n={len(high):,})")
    if len(high) < 20:  # noqa: PLR2004
        print("    too few observations to bucket")
        return

    for feature in EXTENSION_FEATURES:
        buckets = by_feature_quantile(high, feature=feature, buckets=4)
        if not buckets:
            continue
        print(f"    {feature}")
        for bucket in buckets:
            print(
                f"      {bucket.label:<44}n={bucket.n:>5}  "
                f"pos={_rate(bucket.positive_rate):>7}  "
                f"MFE={_pct(bucket.mean_mfe):>8}  MAE={_pct(bucket.mean_mae):>8}"
            )
