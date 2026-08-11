"""Dialect-aware bulk upsert.

Both PostgreSQL and SQLite support ``INSERT ... ON CONFLICT DO UPDATE``, but
through different SQLAlchemy constructs. This module hides that difference so
repositories stay dialect-agnostic.

Upsert rather than insert-or-catch because market-data ingestion is expected to
overlap: re-fetching a window that is already partly stored must be a safe,
idempotent no-op, not an integrity error.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql.dml import Insert


def build_upsert(
    session: AsyncSession,
    table: Table,
    rows: Sequence[dict[str, Any]],
    *,
    index_elements: Sequence[str],
    update_columns: Sequence[str],
) -> Insert:
    """Build an ``INSERT ... ON CONFLICT DO UPDATE`` statement.

    Args:
        session: bound session, used only to detect the dialect.
        table: target table, obtained via :func:`table_of`.
        rows: values to insert. Must be non-empty.
        index_elements: columns forming the conflict target (the natural key).
        update_columns: columns overwritten when a conflicting row exists.

    Raises:
        ValueError: on empty ``rows``, or an unsupported dialect. Falling back to
            a plain INSERT would turn a re-ingest into a crash.
    """
    if not rows:
        msg = "cannot build an upsert with no rows"
        raise ValueError(msg)

    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        stmt: Any = pg_insert(table).values(list(rows))
    elif dialect == "sqlite":
        stmt = sqlite_insert(table).values(list(rows))
    else:
        msg = f"upsert is not implemented for dialect {dialect!r}; supported: postgresql, sqlite"
        raise ValueError(msg)

    return stmt.on_conflict_do_update(  # type: ignore[no-any-return]
        index_elements=list(index_elements),
        set_={column: getattr(stmt.excluded, column) for column in update_columns},
    )


def table_of(model: type[DeclarativeBase]) -> Table:
    """The :class:`~sqlalchemy.Table` behind an ORM model.

    SQLAlchemy types ``Model.__table__`` as the broader ``FromClause`` (a mapper
    may in principle be bound to a join or a subquery). Every model here maps to a
    real table, so the narrowing is done once, here, instead of at each call site.
    """
    table = model.__table__
    if not isinstance(table, Table):
        msg = f"{model.__name__} is not mapped to a plain Table (got {type(table).__name__})"
        raise TypeError(msg)
    return table
