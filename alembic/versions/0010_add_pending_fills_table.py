"""add pending_fills table

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_fills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.String(50), nullable=True, unique=True),
        sa.Column("client_order_id", sa.String(100), nullable=True),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("action", sa.String(4), nullable=False),
        sa.Column("qty", sa.Numeric(15, 6), nullable=False),
        sa.Column("limit_price", sa.Numeric(15, 4), nullable=False),
        sa.Column("recommendation_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("pending_fills")
