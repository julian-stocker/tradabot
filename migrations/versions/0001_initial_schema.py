"""Initial schema: instruments and candles

Revision ID: 0001
Revises:
Create Date: 2026-08-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import Money, UTCDateTime

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE = Money(18, 6)
VOLUME = Money(24, 4)

ASSET_TYPES = ("STOCK", "ETF", "INDEX", "FUND", "CRYPTO", "FX", "OTHER")
TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "asset_type",
            sa.Enum(*ASSET_TYPES, native_enum=False, length=16, name="asset_type"),
            nullable=False,
        ),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", UTCDateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), server_default=sa.func.now(), nullable=False),
        # Bare names: the metadata naming convention adds the `ck_<table>_` prefix.
        sa.CheckConstraint("length(currency) = 3", name="currency_iso4217"),
        sa.CheckConstraint("symbol = upper(symbol)", name="symbol_uppercase"),
        sa.PrimaryKeyConstraint("id", name="pk_instruments"),
        sa.UniqueConstraint("symbol", name="uq_instruments_symbol"),
        sa.UniqueConstraint("isin", name="uq_instruments_isin"),
    )
    op.create_index("ix_instruments_exchange_symbol", "instruments", ["exchange", "symbol"])

    op.create_table(
        "candles",
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column(
            "timeframe",
            sa.Enum(*TIMEFRAMES, native_enum=False, length=8, name="timeframe"),
            nullable=False,
        ),
        sa.Column("timestamp", UTCDateTime(), nullable=False),
        sa.Column("open", PRICE, nullable=False),
        sa.Column("high", PRICE, nullable=False),
        sa.Column("low", PRICE, nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("volume", VOLUME, nullable=False),
        sa.Column("trade_count", sa.BigInteger(), nullable=True),
        sa.Column("vwap", PRICE, nullable=True),
        sa.CheckConstraint("high >= low", name="high_ge_low"),
        sa.CheckConstraint("high >= open AND high >= close", name="high_is_max"),
        sa.CheckConstraint("low <= open AND low <= close", name="low_is_min"),
        sa.CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0", name="prices_positive"
        ),
        sa.CheckConstraint("volume >= 0", name="volume_non_negative"),
        sa.CheckConstraint(
            "trade_count IS NULL OR trade_count >= 0", name="trade_count_non_negative"
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name="fk_candles_instrument_id_instruments",
            ondelete="CASCADE",
        ),
        # The composite PK is also the primary read index: it answers
        # "all 5m candles for instrument X between t0 and t1" as one range scan,
        # and it keeps `timestamp` inside every unique index, which TimescaleDB
        # requires of a hypertable partition column (see migration 0002).
        sa.PrimaryKeyConstraint("instrument_id", "timeframe", "timestamp", name="pk_candles"),
    )
    # Supports "the newest bars across all instruments for a timeframe", the
    # access pattern the future scanner needs.
    op.create_index("ix_candles_timeframe_timestamp", "candles", ["timeframe", "timestamp"])


def downgrade() -> None:
    op.drop_index("ix_candles_timeframe_timestamp", table_name="candles")
    op.drop_table("candles")
    op.drop_index("ix_instruments_exchange_symbol", table_name="instruments")
    op.drop_table("instruments")
