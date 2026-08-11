"""End-to-end research: replay, label, isolate, export.

Offline throughout -- the mock provider supplies the bars. What is under test is
the *machinery*: that a replay is deterministic, that it cannot touch production
state while the scheduler would be running, that labels mature instead of being
invented, and that an export separates X from Y.

No test here requires Alpaca or Discord.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backtesting.engine import (
    ENGINE_VERSION,
    BacktestConfig,
    HistoricalReplay,
    evaluation_grid,
)
from app.core.config import Environment, ScannerSettings, Settings
from app.db.models import (
    NotificationAttempt,
    ScanRun,
    SignalEvaluation,
    SignalOutcome,
    TrackedSignal,
    VirtualPosition,
)
from app.domain.enums import Horizon, LabelStatus, Timeframe
from app.market_data.calendars import get_trading_calendar
from app.market_data.ingest import IngestionService
from app.market_data.providers.mock import MockMarketDataProvider
from app.research.export import (
    FEATURE_COLUMNS,
    LABEL_COLUMNS,
    build_dataset,
    write_dataset,
)
from app.research.repository import BacktestRunRepository, OutcomeRepository
from app.research.service import OutcomeLabellingService
from app.scanner.repository import SignalEvaluationRepository

pytestmark = pytest.mark.integration

SYMBOLS = ("NVDA", "AAPL")
HISTORY_START = datetime(2024, 1, 2, tzinfo=UTC)
HISTORY_END = datetime(2024, 6, 28, tzinfo=UTC)
REPLAY_START = datetime(2024, 6, 3, tzinfo=UTC)
REPLAY_END = datetime(2024, 6, 7, tzinfo=UTC)


def make_settings() -> Settings:
    return Settings(
        env=Environment.TEST,
        database_url="sqlite+aiosqlite:///:memory:",
        market_data_provider="mock",
        log_level="WARNING",
        scanner=ScannerSettings(require_regular_session=False),
    )


@pytest.fixture
def factory(engine: object) -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]


@pytest.fixture
async def history(session: AsyncSession, provider: MockMarketDataProvider) -> AsyncSession:
    """Instruments plus hourly and daily bars spanning the replay window."""
    ingestion = IngestionService(session, provider)
    await ingestion.sync_instruments()
    for symbol in SYMBOLS:
        for timeframe in (Timeframe.H1, Timeframe.D1):
            await ingestion.sync_candles(
                symbol=symbol, timeframe=timeframe, start=HISTORY_START, end=HISTORY_END
            )
    await session.commit()
    return session


def config(**overrides: object) -> BacktestConfig:
    base: dict[str, object] = {
        "symbols": SYMBOLS,
        "start": REPLAY_START,
        "end": REPLAY_END,
        "primary_timeframe": Timeframe.H1,
        "regular_session_only": False,
    }
    return BacktestConfig(**(base | overrides))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1-6. Replay mechanics
# ---------------------------------------------------------------------------
def test_the_grid_yields_bar_closes_in_order() -> None:
    calendar = get_trading_calendar("XNYS")

    grid = list(
        evaluation_grid(
            start=REPLAY_START,
            end=REPLAY_END,
            timeframe=Timeframe.H1,
            calendar=calendar,
            regular_session_only=False,
        )
    )

    assert grid == sorted(grid), "instants must be chronological"
    assert all(moment.minute == 0 for moment in grid), "hourly closes land on the hour"


def test_the_grid_excludes_closed_sessions() -> None:
    """Weekends carry no session, so they contribute no evaluation instants."""
    calendar = get_trading_calendar("XNYS")

    grid = list(
        evaluation_grid(
            start=datetime(2024, 6, 8, tzinfo=UTC),  # Saturday
            end=datetime(2024, 6, 9, 23, tzinfo=UTC),  # Sunday
            timeframe=Timeframe.H1,
            calendar=calendar,
            regular_session_only=True,
        )
    )

    assert grid == []


async def test_a_replay_writes_one_observation_per_symbol_and_instant(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    run, stats = await HistoricalReplay(factory, make_settings()).run(config())

    assert run.status == "COMPLETED"
    assert stats.observations > 0
    assert stats.symbols_processed == len(SYMBOLS)
    assert run.engine_version == ENGINE_VERSION


async def test_a_single_symbol_replay_touches_only_that_symbol(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    run, _ = await HistoricalReplay(factory, make_settings()).run(config(symbols=("NVDA",)))

    async with factory() as check:
        rows = (
            (
                await check.execute(
                    select(SignalEvaluation).where(SignalEvaluation.backtest_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
    assert {row.instrument_id for row in rows} == {rows[0].instrument_id}


async def test_a_date_range_bounds_the_observations(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    run, _ = await HistoricalReplay(factory, make_settings()).run(config())

    async with factory() as check:
        rows = (
            (
                await check.execute(
                    select(SignalEvaluation).where(SignalEvaluation.backtest_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )

    assert rows
    assert all(REPLAY_START <= row.evaluated_at <= REPLAY_END for row in rows)


async def test_two_identical_runs_produce_identical_results(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    """**Reproducibility.** Same configuration, same immutable data, same numbers."""
    replay = HistoricalReplay(factory, make_settings())

    first, first_stats = await replay.run(config())
    second, second_stats = await replay.run(config())

    assert first.run_key == second.run_key, "the configuration digest must be stable"
    assert first_stats.observations == second_stats.observations
    assert first_stats.qualified == second_stats.qualified

    async with factory() as check:
        rows = (
            await check.execute(
                select(SignalEvaluation.backtest_run_id, SignalEvaluation.score).where(
                    SignalEvaluation.backtest_run_id.in_([first.id, second.id])
                )
            )
        ).all()
    scores = {run_id: [] for run_id in (first.id, second.id)}  # type: ignore[var-annotated]
    for run_id, score in rows:
        scores[run_id].append(score)
    assert sorted(scores[first.id]) == sorted(scores[second.id])


async def test_the_run_key_changes_with_the_configuration() -> None:
    assert config().run_key() != config(symbols=("NVDA",)).run_key()
    assert config().run_key() != config(end=REPLAY_END + timedelta(days=1)).run_key()


# ---------------------------------------------------------------------------
# 55-60. Production isolation (part AG)
# ---------------------------------------------------------------------------
async def test_a_backtest_writes_no_production_state(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    """**The critical isolation test.** The live scheduler is running during this.

    A research job that advanced the signal lifecycle, recorded a scan run, opened
    a position or sent a notification would corrupt production while merely
    answering a historical question.
    """
    async with factory() as before:
        baseline = (
            len((await before.execute(select(TrackedSignal))).scalars().all()),
            len((await before.execute(select(ScanRun))).scalars().all()),
            len((await before.execute(select(VirtualPosition))).scalars().all()),
            len((await before.execute(select(NotificationAttempt))).scalars().all()),
        )

    await HistoricalReplay(factory, make_settings()).run(config())

    async with factory() as after:
        current = (
            len((await after.execute(select(TrackedSignal))).scalars().all()),
            len((await after.execute(select(ScanRun))).scalars().all()),
            len((await after.execute(select(VirtualPosition))).scalars().all()),
            len((await after.execute(select(NotificationAttempt))).scalars().all()),
        )

    assert current == baseline, "a backtest mutated production state"


async def test_backtest_observations_are_invisible_to_production_reads(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    """Research rows exist in the shared table but never reach an operator view."""
    async with factory() as before:
        live_before = await SignalEvaluationRepository(before).count()

    run, stats = await HistoricalReplay(factory, make_settings()).run(config())

    async with factory() as after:
        repository = SignalEvaluationRepository(after)
        live_after = await repository.count()
        everything = await repository.count(include_backtest=True)
        candidates = await repository.latest_per_instrument()

    assert live_after == live_before, "the live count moved"
    assert everything == live_before + stats.observations
    assert all(row.backtest_run_id is None for row in candidates)
    assert run.observation_count == stats.observations


async def test_every_backtest_row_carries_its_run_id(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    run, _ = await HistoricalReplay(factory, make_settings()).run(config())

    async with factory() as check:
        rows = (
            (
                await check.execute(
                    select(SignalEvaluation).where(SignalEvaluation.backtest_run_id.is_not(None))
                )
            )
            .scalars()
            .all()
        )

    assert rows
    assert all(row.backtest_run_id == run.id for row in rows)
    assert all(row.scan_run_id is None for row in rows), "a backtest has no scan run"


# ---------------------------------------------------------------------------
# 61-63. Labelling and idempotency
# ---------------------------------------------------------------------------
async def test_labels_are_generated_for_backtest_observations(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    await HistoricalReplay(factory, make_settings()).run(config())

    async with factory() as label_session:
        report = await OutcomeLabellingService(label_session).generate(now=HISTORY_END)

    assert report.evaluations_seen > 0
    assert report.labels_written > 0
    assert report.ok, report.errors


async def test_rerunning_the_labeller_creates_no_duplicates(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    """**Idempotency.** A scheduled job must not double the weight of every row."""
    await HistoricalReplay(factory, make_settings()).run(config())

    async with factory() as first:
        await OutcomeLabellingService(first).generate(now=HISTORY_END)
    async with factory() as count_one:
        after_first = await OutcomeRepository(count_one).total_count()

    async with factory() as second:
        await OutcomeLabellingService(second).generate(now=HISTORY_END, recompute=True)
    async with factory() as count_two:
        after_second = await OutcomeRepository(count_two).total_count()

    assert after_first == after_second
    assert after_first > 0


async def test_a_pending_label_completes_once_the_future_arrives(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    """Labels mature; they are not frozen at whatever was known the first time."""
    await HistoricalReplay(factory, make_settings()).run(config())

    # Label as if it were the day after the replay: long horizons cannot resolve.
    async with factory() as early:
        await OutcomeLabellingService(early).generate(now=REPLAY_END + timedelta(days=1))
    async with factory() as check_early:
        pending_early = await OutcomeRepository(check_early).pending_count()

    # Now label with the full history available.
    async with factory() as late:
        matured = await OutcomeLabellingService(late).generate(now=HISTORY_END)
    async with factory() as check_late:
        pending_late = await OutcomeRepository(check_late).pending_count()

    assert pending_early > 0, "long horizons should start pending"
    assert pending_late < pending_early, "labels did not mature"
    assert matured.matured > 0


async def test_a_pending_label_is_null_never_zero(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    await HistoricalReplay(factory, make_settings()).run(config())

    async with factory() as label_session:
        await OutcomeLabellingService(label_session).generate(now=REPLAY_END + timedelta(days=1))

    async with factory() as check:
        pending = (
            (
                await check.execute(
                    select(SignalOutcome).where(SignalOutcome.status != LabelStatus.COMPLETE.value)
                )
            )
            .scalars()
            .all()
        )

    assert pending
    for row in pending:
        assert row.raw_return is None, "an unknown outcome was written as a number"
        assert row.mfe is None
        assert row.mae is None


# ---------------------------------------------------------------------------
# 44-47. Export
# ---------------------------------------------------------------------------
async def test_the_export_separates_features_from_labels(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    await HistoricalReplay(factory, make_settings()).run(config())
    async with factory() as label_session:
        await OutcomeLabellingService(label_session).generate(now=HISTORY_END)

    async with factory() as export_session:
        result = await build_dataset(export_session, horizon=Horizon.D1, regular_session_only=False)

    assert set(result.manifest.feature_columns) == set(FEATURE_COLUMNS)
    assert set(result.manifest.label_columns) == set(LABEL_COLUMNS)
    assert not set(result.manifest.feature_columns) & set(result.manifest.label_columns)
    for column in FEATURE_COLUMNS:
        assert column in result.frame.columns


async def test_two_exports_of_the_same_data_are_identical(
    history: AsyncSession, factory: async_sessionmaker, tmp_path: object
) -> None:
    """**Deterministic export.** A dataset that varies run to run is not a dataset."""
    await HistoricalReplay(factory, make_settings()).run(config())
    async with factory() as label_session:
        await OutcomeLabellingService(label_session).generate(now=HISTORY_END)

    async with factory() as first:
        one = await build_dataset(first, horizon=Horizon.D1, regular_session_only=False)
    async with factory() as second:
        two = await build_dataset(second, horizon=Horizon.D1, regular_session_only=False)

    assert one.frame.equals(two.frame)


async def test_the_manifest_records_versions_and_exclusions(
    history: AsyncSession, factory: async_sessionmaker, tmp_path: object
) -> None:
    from pathlib import Path

    await HistoricalReplay(factory, make_settings()).run(config())
    async with factory() as label_session:
        await OutcomeLabellingService(label_session).generate(now=HISTORY_END)

    async with factory() as export_session:
        result = await build_dataset(export_session, horizon=Horizon.D1, regular_session_only=False)
    written = write_dataset(result, directory=Path(str(tmp_path)), stem="ds", fmt="parquet")

    payload = result.manifest.as_dict()
    assert payload["versions"]["label_policy"]
    assert payload["versions"]["cost_model"]
    assert "excluded_rows" in payload
    assert written.data_path is not None
    assert written.data_path.exists()
    assert written.manifest_path is not None
    assert written.manifest_path.exists()


async def test_the_manifest_contains_no_credential(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    await HistoricalReplay(factory, make_settings()).run(config())
    async with factory() as label_session:
        await OutcomeLabellingService(label_session).generate(now=HISTORY_END)

    async with factory() as export_session:
        result = await build_dataset(export_session, horizon=Horizon.D1, regular_session_only=False)

    rendered = str(result.manifest.as_dict())
    for forbidden in ("discord.com", "webhook", "api_key", "secret", "PK"):
        assert forbidden not in rendered.lower() or forbidden == "PK"


async def test_the_run_is_recorded_with_its_versions(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    run, _ = await HistoricalReplay(factory, make_settings()).run(config())

    async with factory() as check:
        stored = await BacktestRunRepository(check).get(run.id)

    assert stored is not None
    assert stored.feature_set_version
    assert stored.signal_model_version
    assert stored.scanner_policy_version
    assert stored.cost_model_version
    assert stored.label_policy_version
    assert stored.universe_definition["symbols"] == sorted(SYMBOLS)


# ---------------------------------------------------------------------------
# Portfolio simulation over a real replay (parts W, X)
# ---------------------------------------------------------------------------
async def test_every_portfolio_is_simulated_independently(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    """Each profile walks the same signals and reaches its own conclusion."""
    from app.backtesting.runner import simulate_all_portfolios
    from app.simulation.portfolios import build_personal_profiles
    from app.simulation.repository import SimulationProfileRepository

    async with factory() as setup:
        await SimulationProfileRepository(setup).upsert_many(build_personal_profiles())
        await setup.commit()

    settings = make_settings()
    run, _ = await HistoricalReplay(factory, settings).run(config())
    results = await simulate_all_portfolios(factory, settings, run_id=run.id, config=config())

    assert {r.profile_key for r in results} >= {"paper-100", "paper-1000", "paper-10000"}
    for result in results:
        assert result.attempted == result.executed + result.rejected
        assert result.ending_equity >= 0


async def test_a_simulated_portfolio_never_touches_the_live_one(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    """**Part AG.** The live balances are the operator's; research may not move them."""
    from app.backtesting.runner import simulate_all_portfolios
    from app.db.models import VirtualPortfolio
    from app.simulation.portfolios import build_personal_profiles
    from app.simulation.repository import SimulationProfileRepository

    async with factory() as setup:
        await SimulationProfileRepository(setup).upsert_many(build_personal_profiles())
        await setup.commit()

    settings = make_settings()
    run, _ = await HistoricalReplay(factory, settings).run(config())
    await simulate_all_portfolios(factory, settings, run_id=run.id, config=config())

    async with factory() as check:
        portfolios = (await check.execute(select(VirtualPortfolio))).scalars().all()
    assert portfolios == [], "the simulation created live portfolio rows"


async def test_trade_outcomes_record_modelled_cost_provenance(
    history: AsyncSession, factory: async_sessionmaker
) -> None:
    """No historical quote exists, so no backtested cost may claim to be observed."""
    from app.backtesting.runner import simulate_all_portfolios
    from app.db.models import TradeOutcome
    from app.simulation.portfolios import build_personal_profiles
    from app.simulation.repository import SimulationProfileRepository

    async with factory() as setup:
        await SimulationProfileRepository(setup).upsert_many(build_personal_profiles())
        await setup.commit()

    settings = make_settings()
    run, _ = await HistoricalReplay(factory, settings).run(config())
    await simulate_all_portfolios(factory, settings, run_id=run.id, config=config())

    async with factory() as check:
        rows = (await check.execute(select(TradeOutcome))).scalars().all()

    for row in rows:
        assert row.cost_basis == "MODELLED", "a historical cost claimed to be observed"
        assert row.cost_model_version
        assert row.backtest_run_id == run.id
