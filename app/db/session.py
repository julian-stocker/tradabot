"""Async engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


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
    logger.debug("creating database engine", sqlite=settings.is_sqlite)
    return create_async_engine(settings.database_url, **kwargs)


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
