"""Session-aware risk rules.

Replaces two phase 3 approximations now that a real exchange calendar exists:

**Holding periods** were counted in bars processed. That was exact for the data
seen, but could not express "5 trading days" for a caller who thinks in days, and
broke down entirely if bars were skipped. Now a position can carry a real deadline
computed from the venue's sessions.

**Daily loss** was stored and unenforced, because "a day" had no definition. It
does now: a *trading session*, not a UTC calendar day. The distinction is not
pedantic -- a US session runs to 20:00 UTC (21:00 in winter), so a UTC-midnight
reset would split one trading day across two risk budgets and let a portfolio lose
its daily limit twice in one session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.core.logging import get_logger
from app.core.time import ensure_utc
from app.market_data.calendars import TradingCalendar

logger = get_logger(__name__)

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class SessionState:
    """The session a portfolio is currently measuring risk against."""

    session_date: date
    start_equity: Decimal
    is_new_session: bool
    """True when this call rolled the portfolio into a new session, which is when
    the daily-loss budget resets."""


def resolve_session(
    *,
    calendar: TradingCalendar,
    moment: datetime,
    current_session: date | None,
    current_start_equity: Decimal | None,
    equity: Decimal,
) -> SessionState:
    """Determine the active session and its opening equity.

    On the first observation, or whenever the session date changes, the current
    equity becomes the new session's baseline -- which is exactly what resets the
    daily-loss budget.

    Args:
        calendar: the venue's calendar.
        moment: the instant being processed.
        current_session: the portfolio's stored session date, if any.
        current_start_equity: equity recorded at the start of that session.
        equity: equity right now.
    """
    session_date = calendar.session_date_for(ensure_utc(moment))

    if current_session is None or session_date != current_session:
        return SessionState(session_date=session_date, start_equity=equity, is_new_session=True)

    return SessionState(
        session_date=session_date,
        start_equity=current_start_equity if current_start_equity is not None else equity,
        is_new_session=False,
    )


def daily_loss_fraction(*, equity: Decimal, session_start_equity: Decimal) -> float:
    """Loss so far this session, as a non-positive fraction of its opening equity.

    Returns 0.0 for a session that is up. A zero or negative baseline yields 0.0
    rather than a division error -- a portfolio with no equity cannot lose a
    percentage of it.
    """
    if session_start_equity <= 0:
        return 0.0
    change = (equity - session_start_equity) / session_start_equity
    return min(0.0, float(change))


def daily_loss_breached(
    *, equity: Decimal, session_start_equity: Decimal, max_daily_loss: Decimal
) -> bool:
    """Whether this session's loss has passed the configured limit.

    ``max_daily_loss`` is a positive fraction (0.04 == 4%); the comparison is
    against the negative loss fraction.
    """
    return daily_loss_fraction(equity=equity, session_start_equity=session_start_equity) < -float(
        max_daily_loss
    )


def holding_deadline(
    *, calendar: TradingCalendar, entry: datetime, max_holding_days: int | None
) -> date | None:
    """The session on which a position must be closed.

    ``max_holding_days`` counts **trading days**, so a 5-day limit on a Friday
    entry lands on the following Friday, not on the intervening Wednesday. It
    skips weekends and holidays, which is what "5 trading days" has always meant
    to a person and never meant to ``timedelta(days=5)``.

    Returns ``None`` when there is no limit, or when the calendar cannot reach
    that far -- refusing to guess beyond its horizon.
    """
    if max_holding_days is None or max_holding_days < 1:
        return None
    return calendar.add_trading_days(ensure_utc(entry), max_holding_days)


def holding_period_expired_at(
    *, deadline: date | None, moment: datetime, calendar: TradingCalendar
) -> bool:
    """Whether ``moment`` is at or past a position's holding deadline."""
    if deadline is None:
        return False
    return calendar.session_date_for(ensure_utc(moment)) >= deadline
