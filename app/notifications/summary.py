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

from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.paper.performance import summarise
from app.paper.repository import PaperTradingRepository
from app.simulation.repository import SimulationProfileRepository

logger = get_logger(__name__)

MIN_TRADES_FOR_RATE = 5
"""Below this, a win rate is omitted. An arbitrary engineering floor to stop a
1-for-1 record being reported as 100%, not a statistical claim -- the number of
observations that would make a win rate *meaningful* is far higher, and
establishing it is phase 5's job."""


async def build_daily_summary(
    session: AsyncSession, *, session_date: date | None = None
) -> dict[str, Any]:
    """Assemble the daily report for every enabled profile.

    Returns a payload for :meth:`app.core.events.Event.daily_summary`. Keys the
    formatter cannot render are simply absent rather than null, so "not
    applicable" and "zero" stay distinguishable.
    """
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
    if session_date is not None:
        payload["session_date"] = session_date.isoformat()
    return payload
