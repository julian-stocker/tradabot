"""Phase 5.9: a replay must know, and say, which timeframes it actually had.

The failure this guards against is silent rather than loud. A 2021 replay and a
2026 replay both write to ``signal_evaluations``; both produce scores; nothing
raises. But the 2021 rows were scored with two timeframes and the 2026 rows with
four, and a later ``GROUP BY score_band`` over the union answers a question
nobody asked.

So the mode is measured from stored coverage rather than passed in, and these
tests pin the three verdicts and -- more importantly -- the refusal to guess
when a window straddles the boundary where a timeframe's history begins.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.backtesting.modes import (
    COARSE_TIMEFRAMES,
    MIN_COVERAGE,
    ModeResolution,
    ReplayMode,
    resolve_mode,
)
from app.domain.enums import Timeframe
from app.scanner.timeframes import SCANNER_TIMEFRAMES

# The real provider floors, as measured in phase 5.9.
FLOORS = {
    Timeframe.M5: datetime(2025, 2, 3, tzinfo=UTC),
    Timeframe.M15: datetime(2024, 8, 1, tzinfo=UTC),
    Timeframe.H1: datetime(2020, 7, 27, tzinfo=UTC),
    Timeframe.D1: datetime(2020, 7, 27, tzinfo=UTC),
}
NOW = datetime(2026, 8, 12, tzinfo=UTC)


def coverage(*, latest: datetime = NOW) -> dict[Timeframe, tuple[datetime | None, datetime | None]]:
    return {tf: (FLOORS[tf], latest) for tf in SCANNER_TIMEFRAMES}


def test_the_recent_window_is_production_faithful() -> None:
    """All four timeframes present: this replay *is* the production scanner."""
    resolved = resolve_mode(
        coverage(), start=datetime(2025, 2, 10, tzinfo=UTC), end=datetime(2026, 8, 11, tzinfo=UTC)
    )

    assert resolved.mode is ReplayMode.PRODUCTION_FAITHFUL
    assert set(resolved.available) == set(SCANNER_TIMEFRAMES)
    assert resolved.incomparable_fields == ()


def test_the_long_history_window_is_coarse() -> None:
    """Before 15m history begins, only 1h and 1d exist."""
    resolved = resolve_mode(
        coverage(), start=datetime(2020, 7, 27, tzinfo=UTC), end=datetime(2024, 7, 31, tzinfo=UTC)
    )

    assert resolved.mode is ReplayMode.COARSE_HISTORICAL
    assert set(resolved.available) == set(COARSE_TIMEFRAMES)


def test_a_window_straddling_a_history_boundary_refuses_to_guess() -> None:
    """**The case that matters.**

    2024-01 to 2026-01 has 15m for most of it and 5m for the tail. Calling that
    COARSE_HISTORICAL would claim those bars contributed nothing; calling it
    PRODUCTION_FAITHFUL would claim fidelity for months that never had it. Both
    are wrong in a way no downstream query could detect, so the mode says so.
    """
    resolved = resolve_mode(
        coverage(), start=datetime(2024, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 1, tzinfo=UTC)
    )

    assert resolved.mode is ReplayMode.MIXED
    assert "split" in resolved.detail


def test_partial_coverage_is_not_the_same_as_absent() -> None:
    """A timeframe present for 11% of a window is not a timeframe that never existed.

    Rounding the first case down to the second is exactly how a mixed run gets
    labelled coarse and then compared as though it were uniform.
    """
    barely_present = resolve_mode(
        coverage(), start=datetime(2020, 7, 27, tzinfo=UTC), end=datetime(2025, 2, 2, tzinfo=UTC)
    )
    genuinely_absent = resolve_mode(
        coverage(), start=datetime(2020, 7, 27, tzinfo=UTC), end=datetime(2024, 7, 31, tzinfo=UTC)
    )

    assert barely_present.mode is ReplayMode.MIXED
    assert genuinely_absent.mode is ReplayMode.COARSE_HISTORICAL


def test_a_timeframe_with_no_bars_at_all_is_missing() -> None:
    empty = coverage() | {Timeframe.M5: (None, None), Timeframe.M15: (None, None)}

    resolved = resolve_mode(
        empty, start=datetime(2021, 1, 1, tzinfo=UTC), end=datetime(2022, 1, 1, tzinfo=UTC)
    )

    assert resolved.mode is ReplayMode.COARSE_HISTORICAL
    assert set(resolved.missing) == {Timeframe.M5, Timeframe.M15}


def test_the_coarse_mode_names_what_cannot_be_compared() -> None:
    """**The whole point of the module.**

    A coarse replay's score is computed from the primary timeframe alone, so
    scores compare. `qualified` and `aligned` do not: with 5m and 15m absent,
    context quality is INSUFFICIENT and `aligned` requires 15m, so both are
    structurally false rather than rarely true.
    """
    resolved = resolve_mode(
        coverage(), start=datetime(2020, 7, 27, tzinfo=UTC), end=datetime(2024, 7, 31, tzinfo=UTC)
    )

    assert "score" in resolved.comparable_fields
    for field in ("qualified", "aligned", "agreement", "data_quality"):
        assert field in resolved.incomparable_fields


def test_the_coverage_threshold_is_not_all_or_nothing() -> None:
    """A feed gap of a few days must not demote an otherwise complete run."""
    assert 0.5 < MIN_COVERAGE < 1.0

    nearly_complete = coverage() | {
        # 5m starts a fortnight into a two-year window: ~98% covered.
        Timeframe.M5: (datetime(2025, 1, 15, tzinfo=UTC), NOW)
    }
    resolved = resolve_mode(
        nearly_complete,
        start=datetime(2025, 1, 1, tzinfo=UTC),
        # Ends where the stored data ends. A window running past the last stored
        # bar is under-covered by definition, and would be measuring that rather
        # than the tolerance.
        end=NOW,
    )

    assert resolved.mode is ReplayMode.PRODUCTION_FAITHFUL


def test_no_intraday_history_is_ever_fabricated() -> None:
    """Coarse means *fewer real timeframes*, never synthesised ones.

    The resolution reports 5m/15m as unavailable and the analyser records them
    INSUFFICIENT. Nothing anywhere invents a bar to fill the gap, which is why a
    coarse observation is honest even though it is not comparable.
    """
    resolved = resolve_mode(
        coverage(), start=datetime(2021, 1, 1, tzinfo=UTC), end=datetime(2023, 1, 1, tzinfo=UTC)
    )

    assert Timeframe.M5 not in resolved.available
    assert Timeframe.M15 not in resolved.available
    assert isinstance(resolved, ModeResolution)


# ---------------------------------------------------------------------------
# Production safety: research must not be able to starve the live scheduler
# ---------------------------------------------------------------------------
def test_a_replay_commits_in_bounded_slices() -> None:
    """**Research must never block production.**

    tradabot's replay and its live scheduler share one SQLite file, and SQLite
    allows a single writer. SQLAlchemy autoflushes before each read, so the first
    inserted observation opens the write transaction and holds it until commit.
    Committing once per eight-symbol batch meant a 53-minute write lock over a
    four-year window, and the five-minute market-data sync logged `database is
    locked` and stalled -- observed, not hypothesised, during phase 5.9.

    The bound is what prevents that, so it is asserted rather than trusted to a
    comment.
    """
    from app.backtesting.engine import DEFAULT_SYMBOL_CHUNK, GRID_CHUNK

    assert GRID_CHUNK <= 1000, "a slice this large holds the write lock for minutes"
    assert GRID_CHUNK >= 100, "tiny slices reload the feature warm-up window constantly"
    assert DEFAULT_SYMBOL_CHUNK >= 1


def test_the_grid_is_sliced_without_losing_or_duplicating_instants() -> None:
    """Slicing is a transaction boundary, never a change to what gets evaluated."""
    from app.backtesting.engine import GRID_CHUNK, _chunks_of

    grid = [datetime(2021, 1, 1, tzinfo=UTC) + timedelta(hours=i) for i in range(1801)]

    slices = list(_chunks_of(grid, GRID_CHUNK))
    rejoined = [moment for window in slices for moment in window]

    assert rejoined == grid
    assert len(set(rejoined)) == len(grid)
    assert all(len(window) <= GRID_CHUNK for window in slices)


def test_the_write_timeout_exceeds_a_bulk_commit() -> None:
    """The two numbers that stop research starving production are one decision.

    A replay slice's work must fit inside the scheduler's `busy_timeout`, or the
    five-minute sync fails its lease acquisition with `database is locked` --
    which is what happened in phase 5.9 before both were changed. Asserting the
    relationship keeps a later tweak to either from silently reopening it.
    """
    from app.backtesting.engine import GRID_CHUNK
    from app.db.session import SQLITE_BUSY_TIMEOUT_SECONDS

    observations_per_second = 28.0  # measured over 52 symbols in phase 5.9
    seconds_between_commits = GRID_CHUNK / observations_per_second

    assert seconds_between_commits < SQLITE_BUSY_TIMEOUT_SECONDS
    # ...and the timeout stays well under the shortest scheduling interval, so a
    # genuinely stuck writer still surfaces rather than hanging until next tick.
    assert SQLITE_BUSY_TIMEOUT_SECONDS < 5 * 60
