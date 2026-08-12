"""Async engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

SQLITE_BUSY_TIMEOUT_SECONDS = 30.0
SQLITE_BUSY_TIMEOUT_MS = int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine for the configured database.

    Pooling arguments are only meaningful for the Postgres driver; SQLite (used
    by tests) is given a shared in-memory-friendly configuration instead.
    """
    kwargs: dict[str, Any] = {"echo": settings.db_echo, "future": True}
    if not settings.is_sqlite:
        kwargs |= {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "pool_pre_ping": True,  # survive Postgres restarts / idle timeouts
        }
    else:
        # Scheduled jobs overlap: a sync every five minutes and a scan every
        # fifteen will collide, and default SQLite fails such a collision
        # immediately with "database is locked".
        kwargs["connect_args"] = {"timeout": SQLITE_BUSY_TIMEOUT_SECONDS}

    logger.debug("creating database engine", sqlite=settings.is_sqlite)
    engine = create_async_engine(settings.database_url, **kwargs)

    if settings.is_sqlite and not _is_memory_url(settings.database_url):
        _configure_sqlite(engine)
    return engine


def _is_memory_url(url: str) -> bool:
    """In-memory databases get none of this: there is nothing to journal, and
    WAL on ``:memory:`` is meaningless."""
    return ":memory:" in url


def _configure_sqlite(engine: AsyncEngine) -> None:
    """Make a file-backed SQLite database safe for overlapping scheduled jobs.

    **WAL** lets a reader and a writer proceed at once. Without it the default
    rollback journal takes an exclusive lock for every write, so a scan reading
    candles blocks a sync writing them, and one of the two fails.

    **busy_timeout** makes a contended write *wait* instead of raising
    immediately. It was five seconds, on the stated assumption that this was
    "far longer than any transaction here" -- and phase 5.9 broke that
    assumption. A historical replay commits observations in bulk while the
    scheduler is running, and the five-minute sync began failing its lease
    acquisition with `database is locked`. Thirty seconds comfortably exceeds a
    bulk commit and is still an order of magnitude below the shortest scheduling
    interval, so a genuinely stuck writer still surfaces as an error rather than
    hanging a job until its next tick.

    **synchronous=NORMAL** is the standard pairing with WAL: durable against a
    process crash, which is the failure that actually happens, while not fsyncing
    on every commit. A power cut could lose the last transaction -- acceptable
    for observations that will be re-fetched, and stated rather than assumed.

    None of this makes SQLite a good fit for many concurrent writers; it makes it
    safe for the handful this schedule produces. See docs/operations.md.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory with the settings the rest of the code assumes.

    ``expire_on_commit=False`` keeps ORM objects usable after the request-scoped
    transaction closes, which is what lets route handlers return ORM rows to the
    serialisation layer without triggering lazy loads on a dead session.
    """
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        class_=AsyncSession,
    )


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, roll back on any exception.

    The exception is always re-raised (coding rule 8) -- this helper controls the
    transaction, never the error handling.
    """
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("rolling back database transaction")
            raise
