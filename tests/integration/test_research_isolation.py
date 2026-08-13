"""Research must not disturb production: locks, portfolios, or Discord.

Phase 5.9 turned each of these from an assumption into a test, because the first
one failed in the field. A four-year replay held one SQLite write transaction for
53 minutes, the five-minute market-data sync logged `database is locked`, and the
live scheduler stalled. Nothing in the test suite would have caught it: every
existing database test uses in-memory SQLite with a `StaticPool`, where there is
one connection and therefore no contention to observe.

So the coexistence test here is deliberately file-backed. It is slower and less
tidy than the rest of the suite, and it is the only shape that can fail the way
production failed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.backtesting.engine import GRID_CHUNK
from app.db.session import SQLITE_BUSY_TIMEOUT_SECONDS, _configure_sqlite

pytestmark = pytest.mark.integration

_metadata = MetaData()
_rows = Table(
    "bulk_rows",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("payload", String(64)),
)


def _engine(path: Path) -> Any:
    """A file-backed engine with the production pragmas applied.

    File-backed on purpose: WAL, `busy_timeout` and single-writer semantics only
    exist for a real file, so an in-memory database cannot exercise any of them.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    _configure_sqlite(engine)
    return engine


@pytest.mark.asyncio
async def test_a_bulk_research_writer_does_not_lock_out_a_production_writer(
    tmp_path: Path,
) -> None:
    """**The phase-5.9 incident, as a test.**

    One task writes in bounded slices the way the replay now does; another writes
    a single row the way the scheduler's lease acquisition does. The second must
    succeed. Before the fix it raised `database is locked`.
    """
    path = tmp_path / "coexist.db"
    engine = _engine(path)
    async with engine.begin() as connection:
        await connection.run_sync(_metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    scheduler_wrote = asyncio.Event()
    failures: list[str] = []

    async def research_writer() -> None:
        """Bulk inserts, committed in bounded slices."""
        for _ in range(6):
            async with factory() as session:
                session.autoflush = False
                for index in range(GRID_CHUNK):
                    await session.execute(insert(_rows).values(payload=f"obs-{index}"))
                await session.commit()
            # Yield, so the scheduler task genuinely interleaves rather than
            # being scheduled only after the writer has finished.
            await asyncio.sleep(0)

    async def scheduler_writer() -> None:
        """One small write, as a lease acquisition would do."""
        await asyncio.sleep(0)
        try:
            async with factory() as session:
                await session.execute(insert(_rows).values(payload="lease"))
                await session.commit()
            scheduler_wrote.set()
        except Exception as exc:  # pragma: no cover -- the failure being guarded
            failures.append(type(exc).__name__)

    await asyncio.gather(research_writer(), scheduler_writer())
    await engine.dispose()

    assert not failures, f"production writer was locked out: {failures}"
    assert scheduler_wrote.is_set()


@pytest.mark.asyncio
async def test_the_pragmas_that_make_coexistence_possible_are_applied(
    tmp_path: Path,
) -> None:
    """WAL and a busy timeout longer than a bulk commit. Both, or neither works."""
    path = tmp_path / "pragmas.db"
    engine = _engine(path)

    async with engine.connect() as connection:
        journal = (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar_one()
        timeout = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one()
    await engine.dispose()

    assert str(journal).lower() == "wal", "without WAL a reader blocks a writer"
    assert int(timeout) == int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)
    assert int(timeout) >= 30_000, "shorter than a bulk commit reopens the stall"


@pytest.mark.asyncio
async def test_bulk_writes_are_visible_after_each_slice(tmp_path: Path) -> None:
    """Slicing is a transaction boundary, not a buffer that could be lost.

    A crash mid-replay must leave the completed slices durably written, which is
    what makes a long run resumable rather than all-or-nothing.
    """
    path = tmp_path / "slices.db"
    engine = _engine(path)
    async with engine.begin() as connection:
        await connection.run_sync(_metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(insert(_rows).values(payload="first-slice"))
        await session.commit()

    # A completely separate connection, so nothing is served from session state.
    async with factory() as reader:
        count = len((await reader.execute(select(_rows.c.id))).scalars().all())
    await engine.dispose()

    assert count == 1


# ---------------------------------------------------------------------------
# Research emits nothing and mutates no production state
# ---------------------------------------------------------------------------
def test_the_replay_engine_cannot_reach_discord() -> None:
    """**Historical research must emit zero Discord messages.**

    Asserted structurally rather than by mocking a backend: the engine does not
    import the notification layer at all, so there is no code path to disable and
    no configuration that could re-enable one by accident.
    """
    source = Path("app/backtesting/engine.py").read_text()
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]

    assert not any("notifications" in line for line in imports), (
        "the replay imported the notification layer"
    )
    assert "discord" not in source.lower()
    assert "NotificationService" not in source
    assert ".publish(" not in source
    # `settings.notifications.signal_threshold` is fine and is why the import
    # check looks at import lines rather than the whole file: the replay reads
    # the frozen 75 threshold from configuration, which sends nothing anywhere.
    assert "signal_threshold" in source


def test_the_replay_engine_cannot_reach_paper_portfolios() -> None:
    """Observations only. Portfolio state belongs to the execution pass."""
    source = Path("app/backtesting/engine.py").read_text()

    for forbidden in ("PaperTradingRepository", "open_position", "SimulationProfile"):
        assert forbidden not in source


def test_research_observations_are_tagged_and_filtered_out_of_production() -> None:
    """The isolation boundary: every live read filters on a NULL run id."""
    source = Path("app/scanner/repository.py").read_text()

    assert "_live_only" in source
    assert "backtest_run_id.is_(None)" in source


def test_the_walkforward_never_groups_on_the_production_flag() -> None:
    """Coarse rows have `qualified` structurally false; grouping on it is a bug.

    Checked against the source because the mistake would be a one-word change
    that still runs, still produces a table, and silently compares an empty set
    against everything.
    """
    source = Path("app/research/walkforward.py").read_text()
    body = source.split('"""', 2)[-1]

    assert ".qualified" not in body.replace("qualified=row.score", "")
    assert "SCORE_GE_85" in source
