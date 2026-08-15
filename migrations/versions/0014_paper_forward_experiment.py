"""Persistence for the three-account forward paper experiment.

Four additive tables. Nothing existing is altered, so the historical paper
simulation, the research panels and the options snapshots are untouched.

The unique constraints are the idempotency boundaries, enforced in the database
rather than in application logic: one evaluation per (strategy, universe,
session), one decision per (candidate, slot), one order per client_order_id. A
restart that re-evaluates a completed session, or retries a submission, hits a
constraint instead of creating a duplicate trade.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db import types

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_MONEY = types.Money(precision=18, scale=6)
_TS = types.UTCDateTime()


def upgrade() -> None:
    op.create_table(
        "strategy_evaluations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("universe_version", sa.String(32), nullable=False),
        sa.Column("universe_hash", sa.String(32), nullable=False),
        sa.Column("session", _TS, nullable=False),
        sa.Column("data_cutoff", _TS, nullable=False),
        sa.Column("evaluated_at", _TS, nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("universe_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("eligible_symbols", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(512), nullable=True),
        sa.UniqueConstraint(
            "strategy_version",
            "universe_version",
            "session",
            name="uq_strategy_evaluation_session",
        ),
    )
    op.create_index("ix_strategy_evaluations_session", "strategy_evaluations", ["session"])

    op.create_table(
        "strategy_candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("candidate_id", sa.String(32), nullable=False, unique=True),
        sa.Column(
            "evaluation_id",
            sa.Integer(),
            sa.ForeignKey("strategy_evaluations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "instrument_id",
            sa.Integer(),
            sa.ForeignKey("instruments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("session", _TS, nullable=False),
        sa.Column("rank", _MONEY, nullable=False),
        sa.Column("sector", sa.String(32), nullable=False),
        sa.Column("sector_etf_return_20d", _MONEY, nullable=False),
        sa.Column("movement_to_cost", _MONEY, nullable=False),
        sa.Column("atr_pct", _MONEY, nullable=False),
        sa.Column("reference_price", _MONEY, nullable=False),
    )
    op.create_index("ix_strategy_candidates_session", "strategy_candidates", ["session", "symbol"])

    op.create_table(
        "paper_account_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("candidate_id", sa.String(32), nullable=False),
        sa.Column("slot", sa.String(16), nullable=False),
        sa.Column("account_role", sa.String(32), nullable=False),
        sa.Column("decided_at", _TS, nullable=False),
        sa.Column("equity", _MONEY, nullable=False),
        sa.Column("cash", _MONEY, nullable=False),
        sa.Column("effective_capital", _MONEY, nullable=False),
        sa.Column("current_exposure", _MONEY, nullable=False, server_default="0"),
        sa.Column("risk_budget", _MONEY, nullable=False),
        sa.Column("risk_regime", sa.String(16), nullable=True),
        sa.Column("risk_available", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("stop_distance", _MONEY, nullable=True),
        sa.Column("risk_sized_quantity", _MONEY, nullable=True),
        sa.Column("proposed_quantity", _MONEY, nullable=True),
        sa.Column("proposed_notional", _MONEY, nullable=True),
        sa.Column("whole_share_feasible", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("rejection_reason", sa.String(32), nullable=True),
        sa.Column("binding_constraint", sa.String(32), nullable=True),
        sa.Column("detail", sa.String(512), nullable=True),
        sa.UniqueConstraint("candidate_id", "slot", name="uq_paper_decision_candidate_slot"),
    )
    op.create_index(
        "ix_paper_decisions_slot_outcome", "paper_account_decisions", ["slot", "outcome"]
    )

    op.create_table(
        "paper_broker_orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("client_order_id", sa.String(128), nullable=False, unique=True),
        sa.Column("broker_order_id", sa.String(64), nullable=True),
        sa.Column("candidate_id", sa.String(32), nullable=True),
        sa.Column("slot", sa.String(16), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("order_class", sa.String(16), nullable=False, server_default="simple"),
        sa.Column("requested_quantity", _MONEY, nullable=False),
        sa.Column("submitted_at", _TS, nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="NEW"),
        sa.Column("filled_quantity", _MONEY, nullable=False, server_default="0"),
        sa.Column("filled_avg_price", _MONEY, nullable=True),
        sa.Column("filled_at", _TS, nullable=True),
        sa.Column("rejection_reason", sa.String(256), nullable=True),
        sa.Column("stop_order_id", sa.String(64), nullable=True),
        sa.Column("stop_price", _MONEY, nullable=True),
        sa.Column("target_order_id", sa.String(64), nullable=True),
        sa.Column("target_price", _MONEY, nullable=True),
        sa.Column("protected_quantity", _MONEY, nullable=False, server_default="0"),
        sa.Column("entry_session", _TS, nullable=True),
        sa.Column("expiry_session", _TS, nullable=True),
    )
    op.create_index("ix_paper_broker_orders_slot_status", "paper_broker_orders", ["slot", "status"])
    op.create_index("ix_paper_broker_orders_candidate", "paper_broker_orders", ["candidate_id"])


def downgrade() -> None:
    op.drop_table("paper_broker_orders")
    op.drop_table("paper_account_decisions")
    op.drop_table("strategy_candidates")
    op.drop_table("strategy_evaluations")
