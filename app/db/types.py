"""Custom SQLAlchemy column types.

Two invariants are enforced at the type level, because enforcing them by
convention has a 100% historical failure rate in financial codebases:

``Money``
    Monetary values never touch binary floating point. On PostgreSQL this maps to
    ``NUMERIC(p, s)``. SQLite -- used for fast local tests -- has no decimal type
    and would silently round-trip through a C double, so we store a zero-padded
    decimal *string* there and parse it back exactly.

``UTCDateTime``
    Timestamps are stored as ``TIMESTAMPTZ`` and always come back timezone-aware
    in UTC. Naive datetimes are rejected on write.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Dialect, Numeric, String, TypeDecorator

from app.core.time import ensure_utc

# Width of the SQLite text representation. Wide enough for NUMERIC(28, 12)
# plus sign and decimal point, with headroom.
_SQLITE_MONEY_WIDTH = 48


class Money(TypeDecorator[Decimal]):
    """Exact decimal column for prices, volumes and cash amounts.

    Args:
        precision: total number of significant digits.
        scale: digits after the decimal point.

    The default ``NUMERIC(18, 6)`` holds any equity price with sub-cent
    resolution and notionals up to a trillion.
    """

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int = 18, scale: int = 6) -> None:
        self.precision = precision
        self.scale = scale
        super().__init__(precision=precision, scale=scale, asdecimal=True)

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(_SQLITE_MONEY_WIDTH))
        return dialect.type_descriptor(
            Numeric(precision=self.precision, scale=self.scale, asdecimal=True)
        )

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if isinstance(value, float):
            msg = (
                f"refusing to store float {value!r} in a Money column; "
                f"pass a Decimal or a str to avoid binary rounding"
            )
            raise TypeError(msg)
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
        if dialect.name == "sqlite":
            # Zero-padded fixed-point text. Padding the integer part to the full
            # precision makes lexicographic ordering agree with numeric ordering,
            # so CHECK constraints like `high >= low` still hold on SQLite.
            #
            # **This equivalence holds only for non-negative values.** Signed
            # money (P&L, unrealised gains) is stored correctly and reads back
            # exactly, but SQLite will sort negative amounts above positive ones.
            # Nothing orders by a signed money column, and PostgreSQL -- the
            # supported target -- uses real NUMERIC and is unaffected.
            #
            # A sign-aware encoding was tried and reverted: it changes the
            # numeric value SQLite infers from the text, which silently breaks
            # every existing `column <= 1`-style CHECK. Comparing a Money column
            # against a numeric literal is dialect-dependent and fragile, so
            # ratio columns that need such a constraint use Float instead.
            width = self.precision + 1  # digits + the decimal point
            return f"{decimal_value:0{width}.{self.scale}f}"
        return decimal_value

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))


class UTCDateTime(TypeDecorator[datetime]):
    """``TIMESTAMPTZ`` column that is always aware and always UTC in Python."""

    impl = DateTime
    cache_ok = True

    def __init__(self, timezone: bool = True) -> None:
        """Always timezone-aware, whatever is passed.

        The ``timezone`` parameter exists only because Alembic autogenerate
        renders a custom type using its *impl's* keyword arguments, emitting
        ``UTCDateTime(timezone=True)`` into every generated migration. Without
        accepting it, each new migration would fail to import until hand-edited.
        It is ignored: a UTC-only column type has no meaningful naive variant.
        """
        super().__init__(timezone=True)

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            msg = f"expected datetime for a UTCDateTime column, got {type(value).__name__}"
            raise TypeError(msg)
        return ensure_utc(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            msg = f"expected datetime from a UTCDateTime column, got {type(value).__name__}"
            raise TypeError(msg)
        if value.tzinfo is None:
            # SQLite has no tz-aware storage; values were written as UTC.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
