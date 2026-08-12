"""Storage estimation, the disk gate, gap classification and resampling.

The estimator's constants were measured against a real database, so these tests
do not re-derive them -- they pin the *arithmetic* and the *policy*. The policy
matters more than the arithmetic: an estimator that is 20% low is an
inconvenience, and a disk gate that says SAFE when it should say UNSAFE fills
someone's laptop.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from app.domain.enums import Timeframe
from app.market_data.backfill import CHUNK_DAYS, _covered, chunk_windows
from app.market_data.calendars import get_trading_calendar
from app.market_data.gaps import GapKind, classify_session, resample, summarise
from app.research.storage import (
    BYTES_PER_CANDLE,
    BYTES_PER_EVALUATION,
    HORIZON_COUNT,
    MINIMUM_FREE_BYTES,
    WORKING_HEADROOM_FACTOR,
    build_plan,
    check_disk,
    count_sessions,
    estimate_candles,
    estimate_research,
    measure_parquet_ratio,
)

START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 12, 31, tzinfo=UTC)


@pytest.fixture
def calendar() -> object:
    return get_trading_calendar("XNYS")


# ---------------------------------------------------------------------------
# 1-2. Estimator and session/bar counts
# ---------------------------------------------------------------------------
def test_a_year_holds_about_252_sessions(calendar: object) -> None:
    """The calendar, not weeks x 5: holidays cost ~9 sessions a year."""
    sessions = count_sessions(calendar, start=START, end=END)  # type: ignore[arg-type]

    assert 249 <= sessions <= 253, sessions


def test_bar_counts_scale_with_symbols_and_sessions() -> None:
    one = estimate_candles(symbols=1, sessions=100, timeframes=(Timeframe.M5,))
    ten = estimate_candles(symbols=10, sessions=100, timeframes=(Timeframe.M5,))

    assert ten[Timeframe.M5] == pytest.approx(one[Timeframe.M5] * 10, rel=0.01)


def test_finer_timeframes_produce_more_rows() -> None:
    counts = estimate_candles(
        symbols=52,
        sessions=252,
        timeframes=(Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.D1),
    )

    assert (
        counts[Timeframe.M5] > counts[Timeframe.M15] > counts[Timeframe.H1] > counts[Timeframe.D1]
    )


def test_five_minute_bars_dominate_the_row_count() -> None:
    """Which is why chunking is sized per timeframe rather than uniformly."""
    counts = estimate_candles(
        symbols=52,
        sessions=252,
        timeframes=(Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.D1),
    )
    total = sum(counts.values())

    assert counts[Timeframe.M5] / total > 0.65


# ---------------------------------------------------------------------------
# 14-15. Research row estimation
# ---------------------------------------------------------------------------
def test_outcome_rows_are_seven_per_evaluation() -> None:
    evaluations, outcomes, _ = estimate_research(
        symbols=52, sessions=252, evaluations_per_session=6
    )

    assert outcomes == evaluations * HORIZON_COUNT


def test_trade_outcomes_follow_the_qualification_rate_not_the_row_count() -> None:
    """**The estimate that would otherwise be 200x too big.**

    The phase-5 benchmark qualified 16 of 3,744 observations. Assuming one trade
    per observation would project a trade_outcomes table larger than the
    evaluations it derives from.
    """
    evaluations, _, trades = estimate_research(symbols=52, sessions=252, evaluations_per_session=6)

    assert trades < evaluations / 50


def test_an_evaluation_costs_far_more_than_a_candle() -> None:
    """The asymmetry that drives the whole storage plan."""
    assert BYTES_PER_EVALUATION > BYTES_PER_CANDLE * 10


def test_research_can_exceed_raw_market_data(calendar: object) -> None:
    """At production cadence, scanning the data costs more than storing it."""
    plan = build_plan(
        calendar=calendar,  # type: ignore[arg-type]
        symbols=52,
        start=START,
        end=END,
        evaluations_per_session=26.0,
    )

    assert plan.research_bytes.expected > plan.raw_bytes.expected
    assert any("research data exceeds raw" in note for note in plan.notes)


# ---------------------------------------------------------------------------
# 3-4. The disk gate
# ---------------------------------------------------------------------------
def test_a_tiny_request_is_safe() -> None:
    status = check_disk(1024)

    assert status.verdict == "SAFE"
    assert status.is_safe


def test_an_impossible_request_is_refused() -> None:
    """**The test that protects the user's machine.**"""
    status = check_disk(1024**5)  # a petabyte

    assert status.verdict == "UNSAFE"
    assert not status.is_safe
    assert "free" in status.detail


def test_the_gate_reserves_headroom_beyond_the_data_itself() -> None:
    """WAL growth, vacuum copies and exports all need room the data does not."""
    status = check_disk(1024**3)

    assert status.required_bytes == pytest.approx(1024**3 * WORKING_HEADROOM_FACTOR)


def test_a_request_that_would_leave_less_than_the_floor_is_unsafe() -> None:
    """Sized from the machine's real free space, so it holds on any disk."""
    import shutil

    free = shutil.disk_usage(".").free
    # Ask for everything free, minus a little: after headroom this cannot leave
    # MINIMUM_FREE_BYTES.
    status = check_disk((free - MINIMUM_FREE_BYTES / 2) / WORKING_HEADROOM_FACTOR)

    assert status.verdict == "UNSAFE"


# ---------------------------------------------------------------------------
# 16. Determinism
# ---------------------------------------------------------------------------
def test_the_same_plan_twice_is_identical(calendar: object) -> None:
    """A projection that moved between runs could not justify a decision."""
    first = build_plan(calendar=calendar, symbols=52, start=START, end=END)  # type: ignore[arg-type]
    second = build_plan(calendar=calendar, symbols=52, start=START, end=END)  # type: ignore[arg-type]

    assert first.as_dict()["rows"] == second.as_dict()["rows"]
    assert first.as_dict()["bytes"] == second.as_dict()["bytes"]


def test_low_expected_and_high_are_ordered(calendar: object) -> None:
    plan = build_plan(calendar=calendar, symbols=52, start=START, end=END)  # type: ignore[arg-type]

    assert plan.raw_bytes.low < plan.raw_bytes.expected < plan.raw_bytes.high


def test_the_upper_bound_is_further_away_than_the_lower(calendar: object) -> None:
    """Overshooting a budget is recoverable; running out of disk mid-write is not."""
    plan = build_plan(calendar=calendar, symbols=52, start=START, end=END)  # type: ignore[arg-type]
    expected = plan.raw_bytes.expected

    assert (plan.raw_bytes.high - expected) > (expected - plan.raw_bytes.low)


def test_excluding_research_reports_no_research_rows(calendar: object) -> None:
    plan = build_plan(
        calendar=calendar,  # type: ignore[arg-type]
        symbols=52,
        start=START,
        end=END,
        include_research=False,
    )

    assert plan.evaluation_rows == 0
    assert plan.research_bytes.expected == 0


# ---------------------------------------------------------------------------
# 5-8. Backfill chunking and resume
# ---------------------------------------------------------------------------
def test_windows_tile_the_range_without_gaps_or_overlap() -> None:
    windows = list(chunk_windows(start=START, end=END, timeframe=Timeframe.M5))

    assert windows[0][0] == START
    assert windows[-1][1] == END
    for (_, first_end), (second_start, _) in pairwise(windows):
        assert first_end == second_start, "a gap or overlap between chunks"


def test_finer_timeframes_use_shorter_windows() -> None:
    """Chunks are sized by expected rows, not by days."""
    assert CHUNK_DAYS[Timeframe.M5] < CHUNK_DAYS[Timeframe.M15]
    assert CHUNK_DAYS[Timeframe.M15] < CHUNK_DAYS[Timeframe.H1]
    assert CHUNK_DAYS[Timeframe.H1] < CHUNK_DAYS[Timeframe.D1]


def test_windows_are_produced_oldest_first() -> None:
    """A partial run must leave contiguous history with a known frontier."""
    windows = list(chunk_windows(start=START, end=END, timeframe=Timeframe.H1))

    assert windows == sorted(windows)


def _sessions(calendar: object, start: datetime, end: datetime) -> frozenset[date]:
    return frozenset(calendar.sessions_between(start, end))  # type: ignore[attr-defined]


def test_a_fully_stored_window_is_skipped(calendar: object) -> None:
    """Resume: every session present means nothing to fetch."""
    stored = _sessions(calendar, START, END)

    assert _covered(stored, START, END, calendar)  # type: ignore[arg-type]


def test_an_empty_window_is_not_skipped(calendar: object) -> None:
    assert not _covered(frozenset(), START, END, calendar)  # type: ignore[arg-type]


def test_a_partially_stored_window_is_not_skipped(calendar: object) -> None:
    """**The bug this replaced.**

    A frontier check asked only "is the oldest stored bar older than this
    window?". One exploratory 2020 chunk plus recent live data satisfied that for
    every window in between, so a five-year hole in the hourly series was
    reported as complete. Coverage is now measured session by session, which
    cannot be fooled by data at the edges.
    """
    everything = _sessions(calendar, START, END)
    first_month = _sessions(calendar, START, datetime(2024, 2, 1, tzinfo=UTC))
    last_month = _sessions(calendar, datetime(2024, 12, 1, tzinfo=UTC), END)
    edges_only = first_month | last_month

    assert len(edges_only) < len(everything)
    assert not _covered(edges_only, START, END, calendar), (  # type: ignore[arg-type]
        "a window with only its edges filled was treated as complete"
    )


def test_a_window_with_no_sessions_is_trivially_covered(calendar: object) -> None:
    # 8 June 2024 is a Saturday; the window ends before Monday's session.
    saturday = datetime(2024, 6, 8, tzinfo=UTC)
    sunday_end = datetime(2024, 6, 9, 23, 59, tzinfo=UTC)

    assert _covered(frozenset(), saturday, sunday_end, calendar)  # type: ignore[arg-type]


def test_a_single_missing_session_is_tolerated(calendar: object) -> None:
    """A thin symbol occasionally has no prints; demanding 100% would loop forever."""
    stored = set(_sessions(calendar, START, END))
    stored.discard(next(iter(sorted(stored))))

    assert _covered(frozenset(stored), START, END, calendar)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 11. Gap classification
# ---------------------------------------------------------------------------
def test_a_weekend_is_not_a_data_problem(calendar: object) -> None:
    """~70% of naive 'gaps' on US equities are the market being shut."""
    gap = classify_session(
        symbol="NVDA",
        timeframe=Timeframe.D1,
        session=date(2024, 6, 8),  # Saturday
        received=0,
        expected=1,
        calendar=calendar,  # type: ignore[arg-type]
        first_known=datetime(2020, 1, 1, tzinfo=UTC),
    )

    assert gap.kind is GapKind.EXPECTED_MARKET_CLOSURE
    assert not gap.kind.is_actionable


def test_a_holiday_is_not_a_data_problem(calendar: object) -> None:
    gap = classify_session(
        symbol="NVDA",
        timeframe=Timeframe.D1,
        session=date(2024, 7, 4),
        received=0,
        expected=1,
        calendar=calendar,  # type: ignore[arg-type]
        first_known=datetime(2020, 1, 1, tzinfo=UTC),
    )

    assert gap.kind is GapKind.EXPECTED_MARKET_CLOSURE


def test_an_open_session_with_no_bars_is_the_providers(calendar: object) -> None:
    gap = classify_session(
        symbol="NVDA",
        timeframe=Timeframe.D1,
        session=date(2024, 6, 5),
        received=0,
        expected=1,
        calendar=calendar,  # type: ignore[arg-type]
        first_known=datetime(2020, 1, 1, tzinfo=UTC),
    )

    assert gap.kind is GapKind.PROVIDER_MISSING
    assert gap.kind.is_actionable
    assert gap.missing == 1


def test_a_session_before_the_first_known_bar_is_not_missing(calendar: object) -> None:
    """Before the rolling window's floor there is nothing to be missing."""
    gap = classify_session(
        symbol="NVDA",
        timeframe=Timeframe.D1,
        session=date(2019, 6, 5),
        received=0,
        expected=1,
        calendar=calendar,  # type: ignore[arg-type]
        first_known=datetime(2020, 7, 27, tzinfo=UTC),
    )

    assert gap.kind is GapKind.SYMBOL_NOT_TRADING
    assert not gap.kind.is_actionable


def test_a_partial_session_is_unknown_rather_than_assumed(calendar: object) -> None:
    gap = classify_session(
        symbol="NVDA",
        timeframe=Timeframe.M5,
        session=date(2024, 6, 5),
        received=40,
        expected=78,
        calendar=calendar,  # type: ignore[arg-type]
        first_known=datetime(2020, 1, 1, tzinfo=UTC),
    )

    assert gap.kind is GapKind.UNKNOWN
    assert gap.missing == 38


def test_gaps_are_summarised_by_kind(calendar: object) -> None:
    gaps = [
        classify_session(
            symbol="NVDA",
            timeframe=Timeframe.D1,
            session=day,
            received=0,
            expected=1,
            calendar=calendar,  # type: ignore[arg-type]
            first_known=datetime(2020, 1, 1, tzinfo=UTC),
        )
        for day in (date(2024, 6, 8), date(2024, 6, 9), date(2024, 6, 5))
    ]

    counts = summarise(gaps)
    assert counts[GapKind.EXPECTED_MARKET_CLOSURE.value] == 2
    assert counts[GapKind.PROVIDER_MISSING.value] == 1


# ---------------------------------------------------------------------------
# 13. Resampling
# ---------------------------------------------------------------------------
class Bar:
    def __init__(self, minute: int, o: str, h: str, low: str, c: str, v: str) -> None:
        self.timestamp = datetime(2024, 6, 5, 14, 0, tzinfo=UTC) + timedelta(minutes=minute)
        self.open = Decimal(o)
        self.high = Decimal(h)
        self.low = Decimal(low)
        self.close = Decimal(c)
        self.volume = Decimal(v)


def test_three_five_minute_bars_become_one_fifteen_minute_bar() -> None:
    """Open from the first, close from the last, extremes and volume aggregated."""
    bars = [
        Bar(0, "100", "101", "99", "100.5", "1000"),
        Bar(5, "100.5", "103", "100", "102", "2000"),
        Bar(10, "102", "102.5", "98", "99", "1500"),
    ]

    out = resample(bars, target=Timeframe.M15)

    assert len(out) == 1
    assert out[0].open == Decimal("100")
    assert out[0].close == Decimal("99")
    assert out[0].high == Decimal("103")
    assert out[0].low == Decimal("98")
    assert out[0].volume == Decimal("4500")


def test_resampling_splits_on_bucket_boundaries() -> None:
    bars = [Bar(minute, "100", "101", "99", "100", "10") for minute in (0, 5, 10, 15, 20)]

    out = resample(bars, target=Timeframe.M15)

    assert len(out) == 2
    assert out[0].volume == Decimal(30)
    assert out[1].volume == Decimal(20)


def test_resampling_preserves_total_volume() -> None:
    """The invariant that catches a bucketing error."""
    bars = [Bar(minute, "100", "101", "99", "100", "7") for minute in range(0, 60, 5)]

    out = resample(bars, target=Timeframe.H1)

    assert sum(bar.volume for bar in out) == sum(bar.volume for bar in bars)


# ---------------------------------------------------------------------------
# 19. Parquet ratio
# ---------------------------------------------------------------------------
def test_the_parquet_ratio_is_measured_not_assumed() -> None:
    ratio = measure_parquet_ratio(parquet_bytes=120, sqlite_bytes=1000)

    assert ratio == pytest.approx(0.12)


def test_an_empty_sample_reports_zero_rather_than_dividing_by_zero() -> None:
    assert measure_parquet_ratio(parquet_bytes=0, sqlite_bytes=0) == 0.0
