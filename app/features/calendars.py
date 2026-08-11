"""Bars-per-year constants used to annualise volatility.

These are *assumptions about market structure*, so they live in one named place
rather than as magic numbers inside indicator calls (coding rule 14).

They assume a US-equity-like session: 252 trading days, 6.5 hours each. A crypto
instrument trades 24/7 and needs different numbers; when phase 2 introduces real
venues this should become a per-exchange calendar lookup rather than a constant.
"""

from __future__ import annotations

from app.domain.enums import Timeframe

TRADING_DAYS_PER_YEAR = 252
TRADING_HOURS_PER_DAY = 6.5

_BARS_PER_YEAR: dict[Timeframe, float] = {
    Timeframe.M1: TRADING_DAYS_PER_YEAR * TRADING_HOURS_PER_DAY * 60,
    Timeframe.M5: TRADING_DAYS_PER_YEAR * TRADING_HOURS_PER_DAY * 12,
    Timeframe.M15: TRADING_DAYS_PER_YEAR * TRADING_HOURS_PER_DAY * 4,
    Timeframe.M30: TRADING_DAYS_PER_YEAR * TRADING_HOURS_PER_DAY * 2,
    Timeframe.H1: TRADING_DAYS_PER_YEAR * TRADING_HOURS_PER_DAY,
    Timeframe.H4: TRADING_DAYS_PER_YEAR * (TRADING_HOURS_PER_DAY / 4),
    Timeframe.D1: TRADING_DAYS_PER_YEAR,
    Timeframe.W1: 52,
}


def bars_per_year(timeframe: Timeframe) -> int:
    """Approximate number of ``timeframe`` bars in a trading year.

    Used as the annualisation factor for rolling volatility. Passing the wrong
    one is a common and quiet error: annualising 5-minute returns with 252
    understates volatility by roughly 9x.
    """
    return round(_BARS_PER_YEAR[timeframe])
