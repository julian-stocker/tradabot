"""Building the daily report payload.

Reads the paper-trading tables and assembles the numbers a daily summary needs.
Separate from the formatter so *what to report* and *how it reads* stay
independent -- and so this is testable without rendering, and the formatter is
testable without a database.

**Metrics with too little behind them are omitted rather than shown.** A portfolio
that has never traded has no meaningful drawdown, and a win rate over two trades
is noise wearing a percentage sign. Sending them anyway would put numbers on a
channel that get quoted back later as if they meant something.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.core.events import EventType
from app.core.logging import get_logger
from app.core.time import utc_now
from app.db.models import NotificationAttempt, SignalEvaluation
from app.market_data.calendars import get_trading_calendar
from app.paper.performance import summarise
from app.paper.repository import PaperTradingRepository
from app.scanner.repository import SignalEvaluationRepository, TrackedSignalRepository
from app.simulation.repository import SimulationProfileRepository

logger = get_logger(__name__)

MIN_TRADES_FOR_RATE = 5
"""Below this, a win rate is omitted. An arbitrary engineering floor to stop a
1-for-1 record being reported as 100%, not a statistical claim -- the number of
observations that would make a win rate *meaningful* is far higher, and
establishing it is phase 5's job."""


async def build_daily_summary(
    session: AsyncSession, *, session_date: date | None = None, since: datetime | None = None
) -> dict[str, Any]:
    """Assemble the daily report for every enabled profile.

    Returns a payload for :meth:`app.core.events.Event.daily_summary`. Keys the
    formatter cannot render are simply absent rather than null, so "not
    applicable" and "zero" stay distinguishable.

    Args:
        session: caller owns the transaction.
        session_date: the trading session being reported.
        since: scanner activity is counted from this instant. Defaults to the
            last 24 hours, which is what "daily" means for a report sent after
            the close.
    """
    since = since or (utc_now() - timedelta(days=1))
    profiles = SimulationProfileRepository(session)
    repository = PaperTradingRepository(session)

    portfolios: list[dict[str, Any]] = []
    exits = 0

    for profile in await profiles.list_profiles(enabled_only=True):
        if profile.id is None:  # pragma: no cover -- persisted profiles have ids
            continue

        try:
            portfolio = await repository.get_portfolio(profile.id)
        except NotFoundError:
            # A portfolio is created the first time a profile trades. Reporting a
            # configured-but-unstarted profile as its starting capital is honest
            # and useful; creating the portfolio here to make the report tidier
            # would be a read with a side effect.
            portfolios.append(
                {
                    "profile": profile.name,
                    "equity": profile.initial_capital,
                    "open_positions": 0,
                    "trades": 0,
                }
            )
            continue

        trades = await repository.trades(profile.id)
        snapshots = await repository.snapshots(profile.id)
        open_positions = await repository.open_positions(profile.id)
        exits += len(trades)

        summary = summarise(
            profile_name=profile.name,
            portfolio=portfolio,
            trades=trades,
            snapshots=snapshots,
            open_position_count=len(open_positions),
        )

        entry: dict[str, Any] = {
            "profile": summary.profile_name,
            "equity": summary.ending_equity,
            "net_pnl": summary.net_pnl,
            "return_pct": summary.return_pct,
            "open_positions": summary.open_position_count,
            "trades": summary.trade_count,
        }
        # Drawdown needs an equity curve; a portfolio that has never been valued
        # has one point, and its "drawdown" would be a definitional zero.
        if snapshots:
            entry["max_drawdown"] = summary.max_drawdown
        if summary.trade_count >= MIN_TRADES_FOR_RATE and summary.win_rate is not None:
            entry["win_rate"] = summary.win_rate
        portfolios.append(entry)

    payload: dict[str, Any] = {"portfolios": portfolios, "exits": exits}

    # Scanner activity, when the scanner has run. Absent rather than zero if it
    # has not: "no scanner" and "the scanner found nothing" are different facts,
    # and a report showing 0/0/0 for a scanner that never ran is misleading.
    scanner = await _scanner_activity(session, since=since)
    payload.update(scanner)

    if session_date is not None:
        payload["session_date"] = session_date.isoformat()
    return payload


async def _scanner_activity(session: AsyncSession, *, since: datetime) -> dict[str, Any]:
    """Evaluations and qualifications since ``since``.

    Counts what the scanner *stored*, which is every evaluation -- not only the
    ones that qualified. The gap between the two numbers is the useful part: it
    is the base rate, and a report showing only qualifications would hide how
    selective the threshold actually was.
    """
    evaluations = SignalEvaluationRepository(session)
    total = await evaluations.count_since(since)
    if total == 0:
        return {}

    rows = (
        await session.execute(
            select(SignalEvaluation.qualified, SignalEvaluation.instrument_id).where(
                SignalEvaluation.evaluated_at >= since
            )
        )
    ).all()
    qualified = sum(1 for is_qualified, _ in rows if is_qualified)
    strong = len(await TrackedSignalRepository(session).qualified_signals(limit=500))

    return {
        "signals_evaluated": total,
        "signals_qualified": qualified,
        "symbols_scanned": len({instrument_id for _, instrument_id in rows}),
        "currently_qualified": strong,
    }


async def daily_summary_already_sent(session: AsyncSession, settings: Settings) -> bool:
    """Whether today's report has gone out.

    A scheduler firing hourly must not produce hourly reports. "Today" is the
    **trading session date**, not the UTC day: a US session runs past 20:00 UTC,
    so a UTC-midnight boundary would let one session's close produce two reports.

    Reads the notification audit rather than keeping a flag, so the answer
    survives a restart and cannot drift from what was actually delivered.
    """
    calendar = get_trading_calendar(settings.market_data.default_exchange)
    session_date = calendar.session_date_for(utc_now())

    stmt = (
        select(NotificationAttempt.attempted_at)
        .where(
            NotificationAttempt.event_type == EventType.DAILY_SIMULATION_SUMMARY.value,
            NotificationAttempt.status == "delivered",
        )
        .order_by(NotificationAttempt.attempted_at.desc())
        .limit(20)
    )
    for attempted_at in (await session.execute(stmt)).scalars().all():
        if calendar.session_date_for(attempted_at) == session_date:
            return True
    return False
