"""Paper trading engine

Revision ID: 0004
Revises: 0003
Phase 3 schema: the paper-trading lifecycle.

Additive apart from `risk_profiles`, which gains execution-policy columns.
Existing risk profiles get NULL stop/target multiples and therefore cannot open
positions until configured -- deliberate: a NULL stop with `require_stop_loss`
on is a refusal, never an invented stop distance.

Ratio columns (drawdown, returns) are Float rather than Money: they are
dimensionless, and a Float compares correctly against a numeric literal on every
dialect, which a text-encoded Money column on SQLite does not.

Create Date: 2026-08-11 13:16:23.126133

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types



revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('portfolio_snapshots',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('simulation_profile_id', sa.Integer(), nullable=False),
    sa.Column('timestamp', app.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.Column('cash', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('positions_value', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('equity', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('realized_pnl', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('unrealized_pnl', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('open_position_count', sa.Integer(), nullable=False),
    sa.Column('gross_exposure', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('drawdown', sa.Float(), nullable=False),
    sa.CheckConstraint('drawdown <= 0', name=op.f('ck_portfolio_snapshots_drawdown_non_positive')),
    sa.ForeignKeyConstraint(['simulation_profile_id'], ['simulation_profiles.id'], name=op.f('fk_portfolio_snapshots_simulation_profile_id_simulation_profiles'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_portfolio_snapshots')),
    sa.UniqueConstraint('simulation_profile_id', 'timestamp', name='simulation_profile_id_timestamp')
    )
    with op.batch_alter_table('portfolio_snapshots', schema=None) as batch_op:
        batch_op.create_index('ix_portfolio_snapshots_profile_timestamp', ['simulation_profile_id', 'timestamp'], unique=False)

    op.create_table('virtual_portfolios',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('simulation_profile_id', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('initial_capital', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('cash', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('realized_pnl', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('total_fees', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('total_spread_cost', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('total_slippage_cost', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('peak_equity', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('max_drawdown', sa.Float(), server_default='0', nullable=False),
    sa.Column('trade_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('winning_trades', sa.Integer(), server_default='0', nullable=False),
    sa.Column('losing_trades', sa.Integer(), server_default='0', nullable=False),
    sa.Column('bars_processed', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('last_valued_at', app.db.types.UTCDateTime(timezone=True), nullable=True),
    sa.Column('halted_reason', sa.String(length=64), nullable=True),
    sa.Column('created_at', app.db.types.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', app.db.types.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint('cash >= 0', name=op.f('ck_virtual_portfolios_cash_non_negative')),
    sa.CheckConstraint('initial_capital > 0', name=op.f('ck_virtual_portfolios_initial_capital_positive')),
    sa.CheckConstraint('max_drawdown <= 0', name=op.f('ck_virtual_portfolios_max_drawdown_non_positive')),
    sa.CheckConstraint('peak_equity >= 0', name=op.f('ck_virtual_portfolios_peak_equity_non_negative')),
    sa.CheckConstraint('total_fees >= 0', name=op.f('ck_virtual_portfolios_total_fees_non_negative')),
    sa.CheckConstraint('trade_count >= 0', name=op.f('ck_virtual_portfolios_trade_count_non_negative')),
    sa.ForeignKeyConstraint(['simulation_profile_id'], ['simulation_profiles.id'], name=op.f('fk_virtual_portfolios_simulation_profile_id_simulation_profiles'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_virtual_portfolios')),
    sa.UniqueConstraint('simulation_profile_id', name=op.f('uq_virtual_portfolios_simulation_profile_id'))
    )
    op.create_table('decision_outcomes',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('trade_decision_id', sa.Integer(), nullable=False),
    sa.Column('instrument_id', sa.Integer(), nullable=False),
    sa.Column('evaluated_at', app.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.Column('horizon_end', app.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.Column('bars_evaluated', sa.Integer(), nullable=False),
    sa.Column('reference_price', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('horizon_close', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('forward_return', sa.Float(), nullable=False),
    sa.Column('max_favorable_excursion', sa.Float(), nullable=False),
    sa.Column('max_adverse_excursion', sa.Float(), nullable=False),
    sa.Column('created_at', app.db.types.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', app.db.types.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint('bars_evaluated >= 1', name=op.f('ck_decision_outcomes_bars_evaluated_positive')),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_decision_outcomes_instrument_id_instruments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['trade_decision_id'], ['trade_decisions.id'], name=op.f('fk_decision_outcomes_trade_decision_id_trade_decisions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_decision_outcomes')),
    sa.UniqueConstraint('trade_decision_id', name=op.f('uq_decision_outcomes_trade_decision_id'))
    )
    with op.batch_alter_table('decision_outcomes', schema=None) as batch_op:
        batch_op.create_index('ix_decision_outcomes_instrument_id', ['instrument_id'], unique=False)

    op.create_table('virtual_positions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('simulation_profile_id', sa.Integer(), nullable=False),
    sa.Column('instrument_id', sa.Integer(), nullable=False),
    sa.Column('originating_signal_id', sa.Integer(), nullable=True),
    sa.Column('originating_trade_decision_id', sa.Integer(), nullable=True),
    sa.Column('side', sa.Enum('LONG', 'SHORT', name='side', native_enum=False, length=8), nullable=False),
    sa.Column('status', sa.Enum('OPEN', 'CLOSED', name='positionstatus', native_enum=False, length=8), nullable=False),
    sa.Column('quantity', app.db.types.Money(precision=24, scale=8), nullable=False),
    sa.Column('average_entry_price', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('entry_timestamp', app.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.Column('entry_bar_index', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('current_mark_price', app.db.types.Money(precision=18, scale=6), nullable=True),
    sa.Column('unrealized_pnl', app.db.types.Money(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('stop_loss', app.db.types.Money(precision=18, scale=6), nullable=True),
    sa.Column('take_profit', app.db.types.Money(precision=18, scale=6), nullable=True),
    sa.Column('maximum_holding_until_bar', sa.BigInteger(), nullable=True),
    sa.Column('highest_price_seen', app.db.types.Money(precision=18, scale=6), nullable=True),
    sa.Column('lowest_price_seen', app.db.types.Money(precision=18, scale=6), nullable=True),
    sa.Column('entry_costs', app.db.types.Money(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('entry_fee', app.db.types.Money(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('exit_costs', app.db.types.Money(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('realized_pnl', app.db.types.Money(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('exit_price', app.db.types.Money(precision=18, scale=6), nullable=True),
    sa.Column('exit_timestamp', app.db.types.UTCDateTime(timezone=True), nullable=True),
    sa.Column('exit_reason', sa.Enum('STOP_LOSS', 'TAKE_PROFIT', 'MAX_HOLDING_PERIOD', 'SIGNAL_REVERSAL', 'SIMULATION_END', 'MANUAL', name='exitreason', native_enum=False, length=24), nullable=True),
    sa.Column('exit_was_gap', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('exit_was_ambiguous', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', app.db.types.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', app.db.types.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("status <> 'CLOSED' OR (exit_price IS NOT NULL AND exit_timestamp IS NOT NULL AND exit_reason IS NOT NULL)", name=op.f('ck_virtual_positions_closed_requires_exit')),
    sa.CheckConstraint('average_entry_price > 0', name=op.f('ck_virtual_positions_entry_price_positive')),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_virtual_positions_quantity_positive')),
    sa.CheckConstraint('stop_loss IS NULL OR stop_loss > 0', name=op.f('ck_virtual_positions_stop_loss_positive')),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_virtual_positions_instrument_id_instruments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['originating_signal_id'], ['signals.id'], name=op.f('fk_virtual_positions_originating_signal_id_signals'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['originating_trade_decision_id'], ['trade_decisions.id'], name=op.f('fk_virtual_positions_originating_trade_decision_id_trade_decisions'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['simulation_profile_id'], ['simulation_profiles.id'], name=op.f('fk_virtual_positions_simulation_profile_id_simulation_profiles'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_virtual_positions'))
    )
    with op.batch_alter_table('virtual_positions', schema=None) as batch_op:
        batch_op.create_index('ix_virtual_positions_instrument_status', ['instrument_id', 'status'], unique=False)
        batch_op.create_index('ix_virtual_positions_profile_status', ['simulation_profile_id', 'status'], unique=False)
        batch_op.create_index('uq_virtual_positions_open_per_instrument', ['simulation_profile_id', 'instrument_id'], unique=True, sqlite_where=sa.text("status = 'OPEN'"), postgresql_where=sa.text("status = 'OPEN'"))

    op.create_table('virtual_orders',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('simulation_profile_id', sa.Integer(), nullable=False),
    sa.Column('instrument_id', sa.Integer(), nullable=False),
    sa.Column('trade_decision_id', sa.Integer(), nullable=True),
    sa.Column('position_id', sa.Integer(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('side', sa.Enum('LONG', 'SHORT', name='side', native_enum=False, length=8), nullable=False),
    sa.Column('order_type', sa.Enum('MARKET', 'LIMIT', name='ordertype', native_enum=False, length=8), nullable=False),
    sa.Column('status', sa.Enum('PENDING', 'FILLED', 'PARTIALLY_FILLED', 'CANCELLED', 'REJECTED', name='orderstatus', native_enum=False, length=20), nullable=False),
    sa.Column('quantity', app.db.types.Money(precision=24, scale=8), nullable=False),
    sa.Column('requested_at', app.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.Column('filled_at', app.db.types.UTCDateTime(timezone=True), nullable=True),
    sa.Column('requested_price', app.db.types.Money(precision=18, scale=6), nullable=True),
    sa.Column('executed_price', app.db.types.Money(precision=18, scale=6), nullable=True),
    sa.Column('touch_price', app.db.types.Money(precision=18, scale=6), nullable=True),
    sa.Column('fees', app.db.types.Money(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('spread_cost', app.db.types.Money(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('slippage_cost', app.db.types.Money(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('rejection_reason', sa.Enum('INSUFFICIENT_CASH', 'MAX_OPEN_POSITIONS', 'MAX_EXPOSURE', 'MAX_DRAWDOWN', 'MAX_DAILY_LOSS', 'STALE_QUOTE', 'INVALID_STOP', 'EDGE_TOO_SMALL', 'POSITION_ALREADY_OPEN', 'BELOW_MIN_NOTIONAL', 'QUANTITY_TOO_SMALL', 'INSTRUMENT_NOT_TRADABLE', 'PROFILE_DISABLED', 'SHORT_NOT_SUPPORTED', 'UNSUPPORTED_ORDER_TYPE', name='orderrejectionreason', native_enum=False, length=32), nullable=True),
    sa.Column('rejection_detail', sa.String(length=500), nullable=False),
    sa.Column('used_live_quote', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', app.db.types.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', app.db.types.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("status <> 'FILLED' OR (executed_price IS NOT NULL AND filled_at IS NOT NULL)", name=op.f('ck_virtual_orders_filled_requires_price')),
    sa.CheckConstraint("status <> 'REJECTED' OR rejection_reason IS NOT NULL", name=op.f('ck_virtual_orders_rejected_requires_reason')),
    sa.CheckConstraint('fees >= 0', name=op.f('ck_virtual_orders_fees_non_negative')),
    sa.CheckConstraint('quantity >= 0', name=op.f('ck_virtual_orders_quantity_non_negative')),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_virtual_orders_instrument_id_instruments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['position_id'], ['virtual_positions.id'], name=op.f('fk_virtual_orders_position_id_virtual_positions'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['simulation_profile_id'], ['simulation_profiles.id'], name=op.f('fk_virtual_orders_simulation_profile_id_simulation_profiles'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['trade_decision_id'], ['trade_decisions.id'], name=op.f('fk_virtual_orders_trade_decision_id_trade_decisions'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_virtual_orders')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_virtual_orders_idempotency_key'))
    )
    with op.batch_alter_table('virtual_orders', schema=None) as batch_op:
        batch_op.create_index('ix_virtual_orders_position_id', ['position_id'], unique=False)
        batch_op.create_index('ix_virtual_orders_profile_requested_at', ['simulation_profile_id', 'requested_at'], unique=False)

    op.create_table('virtual_trades',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('simulation_profile_id', sa.Integer(), nullable=False),
    sa.Column('position_id', sa.Integer(), nullable=False),
    sa.Column('instrument_id', sa.Integer(), nullable=False),
    sa.Column('originating_signal_id', sa.Integer(), nullable=True),
    sa.Column('side', sa.Enum('LONG', 'SHORT', name='side', native_enum=False, length=8), nullable=False),
    sa.Column('quantity', app.db.types.Money(precision=24, scale=8), nullable=False),
    sa.Column('entry_timestamp', app.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.Column('entry_price', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('exit_timestamp', app.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.Column('exit_price', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('exit_reason', sa.Enum('STOP_LOSS', 'TAKE_PROFIT', 'MAX_HOLDING_PERIOD', 'SIGNAL_REVERSAL', 'SIMULATION_END', 'MANUAL', name='exitreason', native_enum=False, length=24), nullable=False),
    sa.Column('holding_bars', sa.Integer(), server_default='0', nullable=False),
    sa.Column('gross_pnl', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('total_fees', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('total_spread_cost', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('total_slippage_cost', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('net_pnl', app.db.types.Money(precision=18, scale=6), nullable=False),
    sa.Column('net_return', sa.Float(), nullable=False),
    sa.Column('max_favorable_excursion', app.db.types.Money(precision=18, scale=6), nullable=True),
    sa.Column('max_adverse_excursion', app.db.types.Money(precision=18, scale=6), nullable=True),
    sa.Column('outcome', sa.Enum('WIN', 'LOSS', 'BREAKEVEN', name='tradeoutcome', native_enum=False, length=12), nullable=False),
    sa.Column('created_at', app.db.types.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', app.db.types.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_virtual_trades_instrument_id_instruments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['originating_signal_id'], ['signals.id'], name=op.f('fk_virtual_trades_originating_signal_id_signals'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['position_id'], ['virtual_positions.id'], name=op.f('fk_virtual_trades_position_id_virtual_positions'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['simulation_profile_id'], ['simulation_profiles.id'], name=op.f('fk_virtual_trades_simulation_profile_id_simulation_profiles'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_virtual_trades')),
    sa.UniqueConstraint('position_id', name=op.f('uq_virtual_trades_position_id'))
    )
    with op.batch_alter_table('virtual_trades', schema=None) as batch_op:
        batch_op.create_index('ix_virtual_trades_outcome', ['outcome'], unique=False)
        batch_op.create_index('ix_virtual_trades_profile_exit', ['simulation_profile_id', 'exit_timestamp'], unique=False)

    with op.batch_alter_table('risk_profiles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('stop_loss_atr_multiple', app.db.types.Money(precision=9, scale=6), nullable=True))
        batch_op.add_column(sa.Column('take_profit_r_multiple', app.db.types.Money(precision=9, scale=6), nullable=True))
        batch_op.add_column(sa.Column('max_holding_bars', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('require_stop_loss', sa.Boolean(), server_default='true', nullable=False))
        batch_op.add_column(sa.Column('allow_pyramiding', sa.Boolean(), server_default='false', nullable=False))
        batch_op.add_column(sa.Column('max_quote_age_seconds', sa.Integer(), server_default='900', nullable=False))



def downgrade() -> None:
    with op.batch_alter_table('risk_profiles', schema=None) as batch_op:
        batch_op.drop_column('max_quote_age_seconds')
        batch_op.drop_column('allow_pyramiding')
        batch_op.drop_column('require_stop_loss')
        batch_op.drop_column('max_holding_bars')
        batch_op.drop_column('take_profit_r_multiple')
        batch_op.drop_column('stop_loss_atr_multiple')

    with op.batch_alter_table('virtual_trades', schema=None) as batch_op:
        batch_op.drop_index('ix_virtual_trades_profile_exit')
        batch_op.drop_index('ix_virtual_trades_outcome')

    op.drop_table('virtual_trades')
    with op.batch_alter_table('virtual_orders', schema=None) as batch_op:
        batch_op.drop_index('ix_virtual_orders_profile_requested_at')
        batch_op.drop_index('ix_virtual_orders_position_id')

    op.drop_table('virtual_orders')
    with op.batch_alter_table('virtual_positions', schema=None) as batch_op:
        batch_op.drop_index('uq_virtual_positions_open_per_instrument', sqlite_where=sa.text("status = 'OPEN'"), postgresql_where=sa.text("status = 'OPEN'"))
        batch_op.drop_index('ix_virtual_positions_profile_status')
        batch_op.drop_index('ix_virtual_positions_instrument_status')

    op.drop_table('virtual_positions')
    with op.batch_alter_table('decision_outcomes', schema=None) as batch_op:
        batch_op.drop_index('ix_decision_outcomes_instrument_id')

    op.drop_table('decision_outcomes')
    op.drop_table('virtual_portfolios')
    with op.batch_alter_table('portfolio_snapshots', schema=None) as batch_op:
        batch_op.drop_index('ix_portfolio_snapshots_profile_timestamp')

    op.drop_table('portfolio_snapshots')
