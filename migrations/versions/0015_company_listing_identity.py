"""Company and listing identity, so a ticker can stop being a global name.

Two additive tables. **Nothing existing is altered**, which is deliberate:
`instruments` is the broker-tradable universe and 4.9 million candles, 443
thousand signal evaluations and every paper position hang off its surrogate
`instrument_id`. Relaxing its `UNIQUE(symbol)` would make `get_by_symbol`
ambiguous at ten call sites for no benefit this phase needs.

Instead a listing points *optionally* at an instrument. Deutsche Telekom on
Xetra becomes a real identity with no prices, which is exactly the state
international support begins in.

Rollback drops both tables and restores the previous behaviour exactly, because
no existing column, constraint or row is touched.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db import types

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_TS = types.UTCDateTime()


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("cik", sa.String(10), nullable=True),
        sa.Column("lei", sa.String(20), nullable=True),
        sa.Column("reporting_currency", sa.String(3), nullable=True),
        sa.Column("taxonomy", sa.String(16), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("length(country) = 2", name="company_country_iso3166"),
        sa.CheckConstraint(
            "reporting_currency IS NULL OR length(reporting_currency) = 3",
            name="company_currency_iso4217",
        ),
        sa.UniqueConstraint("lei", name="uq_companies_lei"),
    )
    # Unique *index* rather than a table constraint: the model declares
    # cik as unique=True with index=True, which SQLAlchemy renders this way.
    op.create_index("ix_companies_cik", "companies", ["cik"], unique=True)
    op.create_index("ix_companies_country", "companies", ["country"])

    op.create_table(
        "listings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("mic", sa.String(8), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("quote_currency", sa.String(3), nullable=False),
        sa.Column("isin", sa.String(12), nullable=True),
        sa.Column("asset_type", sa.String(16), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("provider_symbol", sa.String(32), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="SET NULL"),
        # The venue-qualified identity: DTE/XETR and DTE/XNYS are two rows.
        sa.UniqueConstraint("mic", "symbol", name="uq_listings_mic_symbol"),
        sa.CheckConstraint("symbol = upper(symbol)", name="listing_symbol_uppercase"),
        sa.CheckConstraint("length(country) = 2", name="listing_country_iso3166"),
        sa.CheckConstraint("length(quote_currency) = 3", name="listing_currency_iso4217"),
    )
    op.create_index("ix_listings_company_id", "listings", ["company_id"])
    op.create_index("ix_listings_instrument_id", "listings", ["instrument_id"])
    op.create_index("ix_listings_isin", "listings", ["isin"])
    op.create_index("ix_listings_symbol", "listings", ["symbol"])


def downgrade() -> None:
    op.drop_table("listings")
    op.drop_table("companies")
