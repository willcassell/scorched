"""Add trailing_stop_price and high_water_mark to positions

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-02
"""
from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("trailing_stop_price", sa.Numeric(15, 4), nullable=True),
    )
    op.add_column(
        "positions",
        sa.Column("high_water_mark", sa.Numeric(15, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("positions", "high_water_mark")
    op.drop_column("positions", "trailing_stop_price")
