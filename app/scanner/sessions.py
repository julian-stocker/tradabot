"""Where an instant falls in the trading day.

Built on the existing :class:`~app.market_data.calendars.TradingCalendar`, which
already knows sessions, holidays and half-days. This module only labels the
phase.

Policy
------
**New signals qualify during the regular session only** (configurable via
``TRADABOT_SCANNER__REQUIRE_REGULAR_SESSION``).

The reason is the data, not caution for its own sake. tradabot's default feed is
IEX, which carries a small fraction of consolidated volume; in extended hours
that fraction is smaller still and the spreads are much wider. A scanner that
qualified setups on pre-market IEX prints would largely be measuring the feed's
thinness. Relative volume, spread and breakout confirmation all mean something
different at 08:00 than at 15:00, and none of the thresholds were chosen for the
former.

**Evaluations are still recorded outside the session.** The observation is real
and belongs in the dataset; what it does not get is a promotion. Existing signals
can still be *downgraded* out of hours -- a setup breaking overnight has still
broken.
"""

from __future__ import annotations

from datetime import datetime

from app.core.time import ensure_utc
from app.market_data.calendars import TradingCalendar
from app.scanner.enums import SessionPhase

SATURDAY = 5

# Pre-market and after-hours windows relative to the session, in hours. US
# equities: 04:00-09:30 ET pre-market, 16:00-20:00 ET after-hours.
PRE_MARKET_HOURS = 5.5
AFTER_HOURS_HOURS = 4.0


def session_phase(calendar: TradingCalendar, moment: datetime) -> SessionPhase:
    """Label ``moment`` relative to the venue's session.

    Weekend and holiday are distinguished from a plain "closed" because they mean
    different things operationally: a weekend gap in the data is expected, a
    weekday closure is a holiday, and neither is an outage. Reporting all three
    as CLOSED would make a genuine feed failure indistinguishable from Sunday.
    """
    moment = ensure_utc(moment)

    if not calendar.is_trading_day(moment):
        return SessionPhase.WEEKEND if moment.weekday() >= SATURDAY else SessionPhase.HOLIDAY

    if calendar.is_open_at(moment):
        return SessionPhase.REGULAR

    open_at = calendar.session_open(moment)
    close_at = calendar.session_close(moment)
    if open_at is None or close_at is None:  # pragma: no cover -- trading day has both
        return SessionPhase.CLOSED

    if moment < open_at:
        hours_before = (open_at - moment).total_seconds() / 3600
        return SessionPhase.PRE_MARKET if hours_before <= PRE_MARKET_HOURS else SessionPhase.CLOSED

    hours_after = (moment - close_at).total_seconds() / 3600
    return SessionPhase.AFTER_HOURS if hours_after <= AFTER_HOURS_HOURS else SessionPhase.CLOSED


def is_after_close(calendar: TradingCalendar, moment: datetime) -> bool:
    """Whether the regular session has finished for this trading day.

    Used by the daily summary, which is meant to report a completed session. It
    returns False on a non-trading day: there was no session to be after.
    """
    moment = ensure_utc(moment)
    if not calendar.is_trading_day(moment):
        return False
    close_at = calendar.session_close(moment)
    return close_at is not None and moment >= close_at


def next_session_open(calendar: TradingCalendar, moment: datetime) -> datetime | None:
    """When the market next opens, for status output."""
    moment = ensure_utc(moment)
    open_at = calendar.session_open(moment)
    if open_at is not None and moment < open_at:
        return open_at
    following = calendar.next_session(moment)
    return calendar.session_open(following) if following is not None else None


def describe_phase(phase: SessionPhase, moment: datetime) -> str:
    """One line for the CLI."""
    clock = moment.strftime("%H:%M UTC")
    return f"{phase.value} at {clock}" + ("" if phase.is_tradable else " (no new qualifications)")


__all__ = [
    "SessionPhase",
    "describe_phase",
    "is_after_close",
    "next_session_open",
    "session_phase",
]
