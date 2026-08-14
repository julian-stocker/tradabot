"""Record which timeframes a historical replay actually had.

Phase 5.9 extends research back to 2020-07-27, where only 1h and 1d exist. Those
observations are valid but **not interchangeable** with ones scored while 5m and
15m were available, and the way that goes wrong is silent: both land in
``signal_evaluations`` and a later ``GROUP BY score_band`` mixes them without
complaint.

Existing rows are stamped PRODUCTION_FAITHFUL because the only run so far
(2025-02-10 onward) had all four timeframes -- that is a statement about the
window that was replayed, not a default guess.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backtest_runs",
        sa.Column(
            "replay_mode",
            sa.String(length=24),
            nullable=False,
            server_default="PRODUCTION_FAITHFUL",
        ),
    )
    op.add_column(
        "backtest_runs",
        sa.Column("available_timeframes", sa.String(length=64), nullable=False, server_default=""),
    )
    op.create_index("ix_backtest_runs_replay_mode", "backtest_runs", ["replay_mode"])


def downgrade() -> None:
    op.drop_index("ix_backtest_runs_replay_mode", table_name="backtest_runs")
    op.drop_column("backtest_runs", "available_timeframes")
    op.drop_column("backtest_runs", "replay_mode")
