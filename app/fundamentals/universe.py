"""Which symbols the fact store should cover.

The universe comes from the local instruments database -- the same place price
history comes from. That is deliberate: a fundamental record for a company whose
prices Tradabot does not hold cannot be turned into a valuation, and a price
series with no fundamentals produces an Advisor report full of holes.

It notably does **not** come from a research artifact. The previous universe
file lived under ``reports/``, and when that directory was removed the symbol
list went with it.

Prices are not the only reason to hold filings
----------------------------------------------
A company Tradabot can describe but not price is an ordinary state -- it is
what every foreign listing looks like before an international market-data
source exists. Shopify is the case that proved it: its facts were in the store
and **not reproducible from the committed universe**, because Shopify is not in
the broker's US instrument table. A rebuild silently dropped four hundred and
eighty-three rows.

So declared companies are unioned in. The rebuild has to reproduce the store,
and a universe that omits a company the store contains is a universe that
quietly loses it on the next sync.
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
    """Symbols the fact store should cover: priced instruments and declared
    companies, unioned and sorted.

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
        symbols = {str(symbol) for symbol, _bars in rows}
        symbols |= _declared(connection)
    finally:
        connection.close()
    return sorted(symbols)


def _declared(connection: sqlite3.Connection) -> set[str]:
    """SEC tickers of companies the registry knows, priced or not.

    EDGAR is keyed by its own ticker, which is the US one where a company has a
    US line. The registry stores the CIK; the ticker map in the sync resolves it.
    A database predating the registry has no such table and contributes nothing.
    """
    try:
        rows = connection.execute(
            "SELECT DISTINCT l.symbol FROM listings l "
            "JOIN companies c ON c.id = l.company_id "
            "WHERE c.cik IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(symbol) for (symbol,) in rows}
