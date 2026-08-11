"""Convert candles into a TimescaleDB hypertable, if Timescale is available

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-11

This migration is deliberately **conditional**. TimescaleDB gives real benefits
for this workload -- time-based chunking, chunk exclusion on range scans, and
later compression of old chunks -- but nothing in the application depends on it.

If the extension is unavailable (plain PostgreSQL, or SQLite in tests) the
migration logs and does nothing. The result is a perfectly functional regular
table: the composite primary key already covers every query the application
issues. Making Timescale mandatory would trade a real portability cost for a
performance benefit that only matters at a data volume phase 1 does not have.

"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# One week of data per chunk. Sized so that a typical query (a few hundred daily
# bars, or a month of 5-minute bars) touches only a handful of chunks, while the
# chunk count stays manageable over years of history.
CHUNK_INTERVAL = "7 days"


def _timescale_available(connection: sa.Connection) -> bool:
    """True if the timescaledb extension can be created on this database."""
    if connection.dialect.name != "postgresql":
        return False
    result = connection.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
    ).scalar()
    return result is not None


def upgrade() -> None:
    connection = op.get_bind()

    if not _timescale_available(connection):
        logger.info(
            "TimescaleDB is not available on this database; leaving `candles` as a "
            "regular table. This is supported and requires no action."
        )
        return

    connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE"))

    # migrate_data=>TRUE handles the case where rows already exist. It takes an
    # exclusive lock, which is acceptable here: this runs on an empty or small
    # table during initial setup.
    connection.execute(
        sa.text(
            "SELECT create_hypertable("
            "  'candles', 'timestamp',"
            f"  chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}',"
            "  migrate_data => TRUE,"
            "  if_not_exists => TRUE"
            ")"
        )
    )
    logger.info("`candles` is now a TimescaleDB hypertable (chunk interval %s)", CHUNK_INTERVAL)


def downgrade() -> None:
    """No-op.

    There is no supported in-place conversion of a hypertable back to a regular
    table; it would require copying every row into a new table and swapping them.
    Since the hypertable is transparent to the application, downgrading to 0001
    while leaving the hypertable in place is harmless -- and far safer than a
    migration that rewrites the entire candle history on rollback.
    """
    logger.info("hypertable conversion is not reversed; this is intentional and harmless")
