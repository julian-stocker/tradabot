"""Alembic environment.

Reads the database URL from application settings rather than alembic.ini, so
there is exactly one place a connection string is configured.

Runs migrations through a *sync* engine even though the application is async:
Alembic's migration context is synchronous, and driving it through async adds
moving parts for no benefit in a one-shot CLI process. The async driver in the
URL is swapped for its sync counterpart here.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import get_settings
from app.db.base import Base
from app.db import models  # noqa: F401 -- registers models on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    """Application URL with the async driver replaced by its sync equivalent."""
    url = get_settings().database_url
    return url.replace("+asyncpg", "+psycopg2").replace("+aiosqlite", "")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database."""
    connectable = create_engine(_sync_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # compare_type catches column type changes, which autogenerate
            # otherwise misses -- important when a Numeric precision changes.
            compare_type=True,
            compare_server_default=True,
            # batch mode lets ALTER TABLE work on SQLite, which cannot alter
            # columns in place.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
