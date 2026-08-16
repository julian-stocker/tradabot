"""Which symbols the fact store should cover.

The universe comes from the local instruments database -- the same place price
history comes from. That is deliberate: a fundamental record for a company whose
prices Tradabot does not hold cannot be turned into a valuation, and a price
series with no fundamentals produces an Advisor report full of holes.

It notably does **not** come from a research artifact. The previous universe
file lived under ``reports/``, and when that directory was removed the symbol
list went with it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def database_path(database_url: str) -> str:
    """The SQLite file behind a SQLAlchemy URL."""
    return database_url.rsplit("/", maxsplit=1)[-1] if "/" in database_url else database_url


def universe_symbols(
    database: str | Path, *, timeframe: str = "D1", min_candles: int = 1
) -> list[str]:
    """Symbols with daily price history, sorted.

    Opened read-only. Nothing in the fundamentals path may write to the trading
    database, and the connection URI enforces that rather than relying on this
    module never issuing an ``INSERT``.

    Args:
        database: path to the SQLite file.
        timeframe: candle timeframe to require. The table holds several.
        min_candles: minimum bars before a symbol counts as covered.
    """
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT i.symbol, COUNT(*) AS bars FROM candles c "
            "JOIN instruments i ON i.id = c.instrument_id "
            "WHERE c.timeframe = ? GROUP BY i.symbol HAVING bars >= ? ORDER BY i.symbol",
            (timeframe, min_candles),
        ).fetchall()
    finally:
        connection.close()
    return [str(symbol) for symbol, _bars in rows]
