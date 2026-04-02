"""Add peak_portfolio_value column to portfolio table

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-02
"""
from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "portfolio",
        sa.Column("peak_portfolio_value", sa.Numeric(15, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("portfolio", "peak_portfolio_value")
