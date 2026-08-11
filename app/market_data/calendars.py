"""Exchange trading calendars.

Replaces the phase 1 nominal-duration approximation and the phase 3 bar-counting
approximation with real sessions, holidays and half-days.

Backed by `exchange-calendars <https://github.com/gerrymanoim/exchange_calendars>`_,
deliberately rather than a hand-maintained holiday table. A hardcoded holiday list
fails *silently* once a year: the code keeps running and quietly treats a closed
day as a missing bar, or a half-day as a full session. That class of bug is
invisible until someone reconciles a report by hand.

Provider independence
---------------------
Nothing here knows about Alpaca. A calendar is a property of the *venue*, and the
mapping from an instrument's exchange MIC to a calendar lives in this module. A
second data provider changes nothing.

Caching
-------
Calendar construction is expensive (it materialises decades of sessions), so
instances are cached per MIC. They are immutable and safe to share.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

from app.core.errors import ConfigurationError
from app.core.logging import get_logger
from app.core.time import ensure_utc

logger = get_logger(__name__)

DEFAULT_CALENDAR = "XNYS"
"""Fallback when an exchange has no known calendar. NYSE hours are a reasonable
default for US equities and a *wrong* one for anything else -- which is why the
fallback is logged rather than silent."""

# MIC -> exchange-calendars name. Most MICs are already calendar names; the
# exceptions are listed explicitly rather than guessed at.
_MIC_ALIASES: dict[str, str] = {
    "XNAS": "XNYS",  # NASDAQ keeps NYSE's session hours and holidays.
    "ARCX": "XNYS",
    "BATS": "XNYS",
    "XETR": "XETR",
    "XFRA": "XFRA",
    "XAMS": "XAMS",
    "XLON": "XLON",
    "XPAR": "XPAR",
    "XSWX": "XSWX",
    "XTSE": "XTSE",
    "XJPX": "XJPX",
}


class TradingCalendar:
    """Sessions, opens and closes for one venue.

    Every method takes and returns timezone-aware UTC datetimes, matching the rest
    of the system. Internally the library works in pandas timestamps; that stays
    behind this boundary.
    """

    def __init__(self, mic: str, calendar: xcals.ExchangeCalendar) -> None:
        self._mic = mic
        self._calendar = calendar

    @property
    def mic(self) -> str:
        return self._mic

    @property
    def name(self) -> str:
        return str(self._calendar.name)

    def is_trading_day(self, moment: datetime | date) -> bool:
        """Whether the venue held a session on this calendar date."""
        return bool(self._calendar.is_session(self._to_session(moment)))

    def is_open_at(self, moment: datetime) -> bool:
        """Whether the venue was open at this exact instant.

        Uses the session's real open and close, so a half-day closes early
        exactly as it did in reality.
        """
        moment = ensure_utc(moment)
        return bool(self._calendar.is_open_on_minute(pd.Timestamp(moment)))

    def session_open(self, day: datetime | date) -> datetime | None:
        """Opening instant of the session on ``day``, or None if closed."""
        session = self._to_session(day)
        if not self._calendar.is_session(session):
            return None
        return _to_datetime(self._calendar.session_open(session))

    def session_close(self, day: datetime | date) -> datetime | None:
        """Closing instant of the session on ``day``, or None if closed."""
        session = self._to_session(day)
        if not self._calendar.is_session(session):
            return None
        return _to_datetime(self._calendar.session_close(session))

    def sessions_between(self, start: datetime, end: datetime) -> list[date]:
        """Trading sessions in ``[start, end]``, as calendar dates.

        Inclusive at both ends, matching the library. Used to count trading days
        and to compute how many bars a window *should* contain.
        """
        start, end = ensure_utc(start), ensure_utc(end)
        if start > end:
            return []
        sessions = self._calendar.sessions_in_range(
            pd.Timestamp(start.date()), pd.Timestamp(end.date())
        )
        return [s.date() for s in sessions]

    def count_sessions(self, start: datetime, end: datetime) -> int:
        return len(self.sessions_between(start, end))

    def session_containing(self, moment: datetime) -> date | None:
        """The session an instant belongs to, or None outside any session.

        Used for the daily-loss boundary: "today" for risk purposes means the
        current *trading session*, not the UTC calendar day. A 22:00 UTC fill on a
        US venue belongs to that day's session, not to tomorrow.
        """
        moment = ensure_utc(moment)
        session = self._to_session(moment)
        if self._calendar.is_session(session):
            open_at = self.session_open(session.date())
            close_at = self.session_close(session.date())
            if open_at is not None and close_at is not None and open_at <= moment <= close_at:
                return date(session.year, session.month, session.day)
        return None

    def session_date_for(self, moment: datetime) -> date:
        """The session a moment counts towards, falling back to its UTC date.

        Unlike :meth:`session_containing` this always returns a date. Outside
        market hours it returns the calendar date, which is the pragmatic answer
        for grouping events that happen after the close.
        """
        return self.session_containing(moment) or ensure_utc(moment).date()

    def next_session(self, after: datetime) -> date | None:
        """The first trading session strictly after ``after``."""
        moment = ensure_utc(after)
        probe = moment.date() + timedelta(days=1)
        limit = probe + timedelta(days=30)
        while probe <= limit:
            if self._calendar.is_session(pd.Timestamp(probe)):
                return probe
            probe += timedelta(days=1)
        return None

    def add_trading_days(self, start: datetime, days: int) -> date | None:
        """The session ``days`` trading days after ``start``.

        This is what "5 trading days" actually means: it skips weekends and
        holidays instead of adding 5 x 24 hours.
        """
        if days < 0:
            msg = f"days must be non-negative, got {days}"
            raise ValueError(msg)
        moment = ensure_utc(start)
        # A generous window: 5 trading days never span more than ~2 weeks even
        # across a holiday-heavy stretch.
        horizon = moment + timedelta(days=days * 3 + 21)
        sessions = self.sessions_between(moment, horizon)
        if len(sessions) <= days:
            return None
        return sessions[days]

    def _to_session(self, moment: datetime | date | pd.Timestamp) -> pd.Timestamp:
        """Normalise any accepted input to a tz-naive session date.

        ``pd.Timestamp`` subclasses ``datetime``, so it must be handled *before*
        the datetime branch -- otherwise a session date round-tripping through
        this method is rejected by ``ensure_utc`` as naive.
        """
        if isinstance(moment, pd.Timestamp):
            return pd.Timestamp(moment.date())
        if isinstance(moment, datetime):
            return pd.Timestamp(ensure_utc(moment).date())
        return pd.Timestamp(moment)


@lru_cache(maxsize=32)
def get_trading_calendar(exchange: str) -> TradingCalendar:
    """Calendar for an exchange MIC, cached.

    Falls back to :data:`DEFAULT_CALENDAR` for an unknown venue, with a warning.
    Silently guessing a calendar would mean silently guessing which days a market
    was open, which corrupts gap detection and holding periods at once.
    """
    mic = (exchange or "").upper()
    name = _MIC_ALIASES.get(mic, mic)

    try:
        calendar = xcals.get_calendar(name)
    except Exception:
        logger.warning(
            "unknown exchange calendar; falling back",
            exchange=mic,
            fallback=DEFAULT_CALENDAR,
        )
        try:
            calendar = xcals.get_calendar(DEFAULT_CALENDAR)
        except Exception as exc:  # pragma: no cover -- the default must exist
            msg = f"default calendar {DEFAULT_CALENDAR!r} is unavailable: {exc}"
            raise ConfigurationError(msg) from exc

    return TradingCalendar(mic or DEFAULT_CALENDAR, calendar)


def _to_datetime(value: pd.Timestamp) -> datetime:
    """pandas Timestamp to an aware UTC datetime."""
    stamp = value.tz_convert("UTC") if value.tzinfo is not None else value.tz_localize("UTC")
    converted: datetime = stamp.to_pydatetime()
    return converted.astimezone(UTC)
