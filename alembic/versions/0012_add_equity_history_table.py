"""add equity_history table

Daily portfolio equity snapshots. The `portfolio` table is a single row updated
in place, so it carries no history — this table makes invested-% and equity
questions answerable without replaying trade_history.

Revision ID: 0012
Revises: 22b454287b33
Create Date: 2026-08-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0012"
down_revision: Union[str, None] = "22b454287b33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "equity_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("total_value", sa.Numeric(15, 4), nullable=False),
        sa.Column("cash_balance", sa.Numeric(15, 4), nullable=False),
        sa.Column("positions_value", sa.Numeric(15, 4), nullable=False),
        sa.Column("invested_pct", sa.Numeric(6, 2), nullable=False),
        sa.Column("unrealized_gain", sa.Numeric(15, 4), nullable=False),
        sa.Column("realized_pnl_to_date", sa.Numeric(15, 4), nullable=False),
        sa.Column("position_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("starting_capital", sa.Numeric(15, 4), nullable=False),
        sa.Column("broker_equity", sa.Numeric(15, 4), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    # Unique so a Phase 3 re-run updates the day's row instead of duplicating it.
    op.create_index(
        "ix_equity_history_snapshot_date", "equity_history", ["snapshot_date"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_equity_history_snapshot_date", table_name="equity_history")
    op.drop_table("equity_history")
