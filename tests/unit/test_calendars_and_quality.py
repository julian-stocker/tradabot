"""Exchange calendars, data-quality checks and session-aware risk rules.

Offline: ``exchange_calendars`` ships its schedules as data, so none of this
touches the network.

Dates are hard-coded against the real NYSE calendar. That is deliberate --
computing the expected answer with the same library under test would assert only
that the code is self-consistent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.enums import Timeframe
from app.domain.quotes import Quote
from app.market_data.calendars import TradingCalendar, get_trading_calendar
from app.market_data.quality import (
    check_series,
    detect_gaps,
    expected_bar_count,
    quote_age_seconds,
    quote_is_stale,
)
from app.paper.sessions import (
    daily_loss_breached,
    daily_loss_fraction,
    holding_deadline,
    holding_period_expired_at,
    resolve_session,
)

SYMBOL = "NVDA"

# Real NYSE facts, chosen because each one breaks a naive calendar:
INDEPENDENCE_DAY_2024 = date(2024, 7, 4)  # Thursday holiday
HALF_DAY_2024 = date(2024, 7, 3)  # 13:00 ET close
NEW_YEARS_DAY_2024 = date(2024, 1, 1)
GOOD_FRIDAY_2024 = date(2024, 3, 29)  # closed, though not a federal holiday
SATURDAY = date(2024, 6, 8)
NORMAL_SESSION = date(2024, 6, 3)  # a Monday


@pytest.fixture
def calendar() -> TradingCalendar:
    return get_trading_calendar("XNYS")


class _StubCandle:
    """Only the attributes the quality checks read."""

    def __init__(self, timestamp: datetime) -> None:
        self.timestamp = timestamp
        self.open = self.high = self.low = self.close = Decimal(100)
        self.volume = Decimal(1_000)


def daily_candles(days: list[date]) -> list[_StubCandle]:
    return [_StubCandle(datetime(d.year, d.month, d.day, 20, 0, tzinfo=UTC)) for d in days]


def daily_timestamps(days: list[date]) -> list[datetime]:
    return [candle.timestamp for candle in daily_candles(days)]


# ---------------------------------------------------------------------------
# Sessions and holidays
# ---------------------------------------------------------------------------
def test_a_normal_weekday_is_a_trading_day(calendar: TradingCalendar) -> None:
    assert calendar.is_trading_day(NORMAL_SESSION)


def test_weekends_are_not_trading_days(calendar: TradingCalendar) -> None:
    assert not calendar.is_trading_day(SATURDAY)
    assert not calendar.is_trading_day(SATURDAY + timedelta(days=1))


@pytest.mark.parametrize("holiday", [INDEPENDENCE_DAY_2024, NEW_YEARS_DAY_2024, GOOD_FRIDAY_2024])
def test_market_holidays_are_not_trading_days(calendar: TradingCalendar, holiday: date) -> None:
    """Including Good Friday, which no weekday-plus-federal-holiday rule catches."""
    assert not calendar.is_trading_day(holiday)


def test_a_half_day_is_a_trading_day_that_closes_early(calendar: TradingCalendar) -> None:
    """3 July 2024 closed at 13:00 ET. Assuming 16:00 invents three hours of bars."""
    assert calendar.is_trading_day(HALF_DAY_2024)

    close = calendar.session_close(HALF_DAY_2024)
    normal_close = calendar.session_close(NORMAL_SESSION)
    assert close.hour < normal_close.hour


def test_the_market_is_open_during_a_session_and_shut_outside_it(
    calendar: TradingCalendar,
) -> None:
    assert calendar.is_open_at(datetime(2024, 6, 3, 15, 0, tzinfo=UTC))  # 11:00 ET
    assert not calendar.is_open_at(datetime(2024, 6, 3, 8, 0, tzinfo=UTC))  # pre-market
    assert not calendar.is_open_at(datetime(2024, 6, 3, 23, 0, tzinfo=UTC))  # after close


def test_sessions_between_excludes_weekends_and_holidays(calendar: TradingCalendar) -> None:
    sessions = calendar.sessions_between(
        datetime(2024, 7, 1, tzinfo=UTC), datetime(2024, 7, 8, tzinfo=UTC)
    )

    assert INDEPENDENCE_DAY_2024 not in sessions
    assert date(2024, 7, 6) not in sessions, "Saturday"
    assert date(2024, 7, 5) in sessions, "the Friday after the holiday still trades"


def test_adding_trading_days_skips_a_weekend(calendar: TradingCalendar) -> None:
    """Friday + 1 trading day is Monday, not Saturday."""
    friday = datetime(2024, 6, 7, 20, 0, tzinfo=UTC)

    assert calendar.add_trading_days(friday, 1) == date(2024, 6, 10)


def test_adding_trading_days_skips_a_holiday(calendar: TradingCalendar) -> None:
    """Wed 3 July + 2 trading days is Mon 8 July: the 4th is shut, the 6th is Saturday."""
    wednesday = datetime(2024, 7, 3, 17, 0, tzinfo=UTC)

    assert calendar.add_trading_days(wednesday, 2) == date(2024, 7, 8)


def test_session_containing_returns_a_plain_date(calendar: TradingCalendar) -> None:
    """pandas must not leak past the calendar boundary."""
    session = calendar.session_containing(datetime(2024, 6, 3, 15, 0, tzinfo=UTC))

    assert session == NORMAL_SESSION
    assert type(session) is date


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------
def test_a_weekend_is_not_a_gap(calendar: TradingCalendar) -> None:
    """The core distinction: a gap in the data is not a gap in the market."""
    timestamps = daily_timestamps([date(2024, 6, 6), date(2024, 6, 7), date(2024, 6, 10)])

    gaps = detect_gaps(timestamps=timestamps, timeframe=Timeframe.D1, calendar=calendar)

    assert gaps == []


def test_a_holiday_is_not_a_gap(calendar: TradingCalendar) -> None:
    timestamps = daily_timestamps([date(2024, 7, 3), date(2024, 7, 5)])

    assert detect_gaps(timestamps=timestamps, timeframe=Timeframe.D1, calendar=calendar) == []


def test_a_missing_trading_day_is_a_gap(calendar: TradingCalendar) -> None:
    """Tuesday 4 June is missing, and the market was open."""
    timestamps = daily_timestamps([date(2024, 6, 3), date(2024, 6, 5)])

    gaps = detect_gaps(timestamps=timestamps, timeframe=Timeframe.D1, calendar=calendar)

    assert len(gaps) == 1
    assert gaps[0].missing_bars == 1


def test_a_multi_day_outage_is_reported_as_one_gap(calendar: TradingCalendar) -> None:
    timestamps = daily_timestamps([date(2024, 6, 3), date(2024, 6, 7)])

    gaps = detect_gaps(timestamps=timestamps, timeframe=Timeframe.D1, calendar=calendar)

    assert len(gaps) == 1
    assert gaps[0].missing_bars == 3, "the 4th, 5th and 6th"


def test_expected_bar_count_counts_sessions_not_days(calendar: TradingCalendar) -> None:
    """1 to 9 July inclusive is nine calendar days but six sessions.

    The 4th is a holiday, the 6th and 7th are a weekend.
    """
    expected = expected_bar_count(
        start=datetime(2024, 7, 1, tzinfo=UTC),
        end=datetime(2024, 7, 9, tzinfo=UTC),
        timeframe=Timeframe.D1,
        calendar=calendar,
    )

    assert expected == 6


def test_expected_bar_count_declines_to_guess_intraday(calendar: TradingCalendar) -> None:
    """Half-days and pre/post-market policy make an intraday count a guess."""
    expected = expected_bar_count(
        start=datetime(2024, 7, 1, tzinfo=UTC),
        end=datetime(2024, 7, 9, tzinfo=UTC),
        timeframe=Timeframe.M5,
        calendar=calendar,
    )

    assert expected is None


def test_check_series_reports_gaps_without_discarding_data(
    calendar: TradingCalendar,
) -> None:
    candles = daily_candles([date(2024, 6, 3), date(2024, 6, 5)])

    report = check_series(candles, symbol=SYMBOL, timeframe=Timeframe.D1, calendar=calendar)

    assert report.symbol == SYMBOL
    assert len(report.gaps) == 1


# ---------------------------------------------------------------------------
# Quote freshness
# ---------------------------------------------------------------------------
def make_quote(age_seconds: float, *, now: datetime) -> Quote:
    return Quote(
        symbol=SYMBOL,
        timestamp=now - timedelta(seconds=age_seconds),
        bid=Decimal("99.95"),
        ask=Decimal("100.05"),
    )


def test_a_recent_quote_is_not_stale() -> None:
    now = datetime(2024, 6, 3, 15, 0, tzinfo=UTC)

    assert not quote_is_stale(make_quote(30, now=now), max_age_seconds=900, now=now)


def test_an_old_quote_is_stale() -> None:
    """Executing against a quote from an hour ago is executing against fiction."""
    now = datetime(2024, 6, 3, 15, 0, tzinfo=UTC)

    assert quote_is_stale(make_quote(3_600, now=now), max_age_seconds=900, now=now)


def test_quote_age_is_measured_in_seconds() -> None:
    now = datetime(2024, 6, 3, 15, 0, tzinfo=UTC)

    assert quote_age_seconds(make_quote(120, now=now), now=now) == pytest.approx(120)


# ---------------------------------------------------------------------------
# Session-aware risk
# ---------------------------------------------------------------------------
def test_a_holding_deadline_counts_trading_days(calendar: TradingCalendar) -> None:
    """5 trading days from Friday 28 June is Friday 5 July, not Wednesday 3 July.

    The window contains both a weekend and Independence Day, so a naive
    ``timedelta(days=5)`` lands two sessions early.
    """
    deadline = holding_deadline(
        calendar=calendar,
        entry=datetime(2024, 6, 28, 20, 0, tzinfo=UTC),
        max_holding_days=5,
    )

    assert deadline == date(2024, 7, 8)
    assert deadline != date(2024, 7, 3), "a calendar-day count would stop here"


def test_no_limit_means_no_deadline(calendar: TradingCalendar) -> None:
    assert (
        holding_deadline(
            calendar=calendar, entry=datetime(2024, 6, 3, tzinfo=UTC), max_holding_days=None
        )
        is None
    )


def test_a_position_is_not_expired_before_its_deadline(calendar: TradingCalendar) -> None:
    assert not holding_period_expired_at(
        deadline=date(2024, 7, 8),
        moment=datetime(2024, 7, 5, 20, 0, tzinfo=UTC),
        calendar=calendar,
    )


def test_a_position_expires_on_its_deadline(calendar: TradingCalendar) -> None:
    assert holding_period_expired_at(
        deadline=date(2024, 7, 8),
        moment=datetime(2024, 7, 8, 20, 0, tzinfo=UTC),
        calendar=calendar,
    )


def test_a_weekend_does_not_advance_the_holding_period(calendar: TradingCalendar) -> None:
    """Saturday belongs to Friday's session, so nothing expires over a weekend."""
    assert not holding_period_expired_at(
        deadline=date(2024, 6, 10),
        moment=datetime(2024, 6, 8, 12, 0, tzinfo=UTC),
        calendar=calendar,
    )


def test_the_first_observation_opens_a_session(calendar: TradingCalendar) -> None:
    state = resolve_session(
        calendar=calendar,
        moment=datetime(2024, 6, 3, 15, 0, tzinfo=UTC),
        current_session=None,
        current_start_equity=None,
        equity=Decimal(10_000),
    )

    assert state.is_new_session
    assert state.session_date == NORMAL_SESSION
    assert state.start_equity == Decimal(10_000)


def test_the_baseline_holds_within_one_session(calendar: TradingCalendar) -> None:
    """Equity moving during the day must not move the day's starting point."""
    state = resolve_session(
        calendar=calendar,
        moment=datetime(2024, 6, 3, 19, 0, tzinfo=UTC),
        current_session=NORMAL_SESSION,
        current_start_equity=Decimal(10_000),
        equity=Decimal(9_500),
    )

    assert not state.is_new_session
    assert state.start_equity == Decimal(10_000)


def test_a_new_session_resets_the_baseline(calendar: TradingCalendar) -> None:
    state = resolve_session(
        calendar=calendar,
        moment=datetime(2024, 6, 4, 15, 0, tzinfo=UTC),
        current_session=NORMAL_SESSION,
        current_start_equity=Decimal(10_000),
        equity=Decimal(9_500),
    )

    assert state.is_new_session
    assert state.start_equity == Decimal(9_500)


def test_a_session_spans_utc_midnight_without_resetting(calendar: TradingCalendar) -> None:
    """The reason sessions exist rather than UTC days.

    A US session runs past 20:00 UTC. If the budget reset at UTC midnight, a late
    US session would be split across two budgets and could lose its daily limit
    twice in one trading day.
    """
    late = datetime(2024, 6, 3, 23, 30, tzinfo=UTC)

    state = resolve_session(
        calendar=calendar,
        moment=late,
        current_session=NORMAL_SESSION,
        current_start_equity=Decimal(10_000),
        equity=Decimal(9_600),
    )

    assert state.session_date == NORMAL_SESSION
    assert not state.is_new_session


def test_daily_loss_is_a_negative_fraction_of_the_session_baseline() -> None:
    assert daily_loss_fraction(
        equity=Decimal(9_600), session_start_equity=Decimal(10_000)
    ) == pytest.approx(-0.04)


def test_a_session_that_is_up_reports_no_loss() -> None:
    assert daily_loss_fraction(equity=Decimal(10_500), session_start_equity=Decimal(10_000)) == 0.0


def test_a_zero_baseline_yields_no_loss_rather_than_dividing_by_zero() -> None:
    assert daily_loss_fraction(equity=Decimal(0), session_start_equity=Decimal(0)) == 0.0


def test_the_daily_loss_limit_triggers_only_once_breached() -> None:
    limit = Decimal("0.04")

    assert not daily_loss_breached(
        equity=Decimal(9_700), session_start_equity=Decimal(10_000), max_daily_loss=limit
    )
    assert daily_loss_breached(
        equity=Decimal(9_500), session_start_equity=Decimal(10_000), max_daily_loss=limit
    )
