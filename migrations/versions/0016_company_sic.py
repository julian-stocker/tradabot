"""Company SIC classification.

Two nullable columns on ``companies``. Additive, like 0015: nothing is altered,
nothing is dropped, and a database that has not been backfilled behaves exactly
as it did before -- an unclassified company reads ``unknown``, which the Advisor
already handles.

The reason for persisting it rather than deriving it on demand is that the
source is a network call. Sector classification decides whether the Advisor
*refuses* to read a bank's balance sheet, and a refusal that depends on an
endpoint being up is a refusal that silently stops happening.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("sic", sa.String(length=4), nullable=True))
    op.add_column("companies", sa.Column("sic_description", sa.String(length=128), nullable=True))
    op.create_index("ix_companies_sic", "companies", ["sic"])


def downgrade() -> None:
    op.drop_index("ix_companies_sic", table_name="companies")
    op.drop_column("companies", "sic_description")
    op.drop_column("companies", "sic")
