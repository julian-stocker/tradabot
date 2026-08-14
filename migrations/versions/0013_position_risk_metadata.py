"""Risk-v1 metadata on virtual positions.

Purely additive: thirteen nullable columns on ``virtual_positions``. Nothing is
backfilled and nothing is rewritten, so a position opened before the risk layer
existed keeps NULL — which is the truthful record. Writing zeros instead would
claim the layer ran and had nothing to say, and the risk-breach audit reads
these columns to decide which positions it is even allowed to judge.

Storage: the table held 0 rows when this was written, and each row gains roughly
200 bytes once populated. Against a 3.5 GB database that is not a consideration
at any position count this system will reach.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db import types

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# The project's own column type, at the same precision the models declare.
# Raw ``sa.Numeric`` here would type-check and migrate cleanly but leave
# ``alembic check`` reporting a permanent drift between models and schema.
_MONEY = types.Money(precision=18, scale=6)
_PRICE = types.Money(precision=18, scale=6)

_COLUMNS: tuple[sa.Column[object], ...] = (
    sa.Column("stop_excess_loss", _MONEY, nullable=True),
    sa.Column("risk_structural_distance", _PRICE, nullable=True),
    sa.Column("risk_noise_floor", _PRICE, nullable=True),
    sa.Column("risk_distance", _PRICE, nullable=True),
    sa.Column("risk_floor_bound", sa.Boolean(), nullable=True),
    sa.Column("risk_regime", sa.String(16), nullable=True),
    sa.Column("risk_band_1d", _PRICE, nullable=True),
    sa.Column("risk_estimated_cost", _MONEY, nullable=True),
    sa.Column("risk_model_version", sa.String(16), nullable=True),
    sa.Column("execution_fractionality", sa.String(24), nullable=True),
    sa.Column("risk_flag", sa.String(24), nullable=True),
    sa.Column("risk_flag_updated_at", sa.DateTime(timezone=True), nullable=True),
)


def upgrade() -> None:
    with op.batch_alter_table("virtual_positions") as batch:
        for column in _COLUMNS:
            batch.add_column(column)


def downgrade() -> None:
    with op.batch_alter_table("virtual_positions") as batch:
        for column in _COLUMNS:
            batch.drop_column(column.name)
