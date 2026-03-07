"""Store benchmark starting prices on portfolio row

Revision ID: 0004
Revises: 0003
Create Date: 2026-02-26
"""
from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("portfolio", sa.Column("spy_start_price", sa.Numeric(15, 4), nullable=True))
    op.add_column("portfolio", sa.Column("qqq_start_price", sa.Numeric(15, 4), nullable=True))
    op.add_column("portfolio", sa.Column("dji_start_price", sa.Numeric(15, 4), nullable=True))


def downgrade():
    op.drop_column("portfolio", "dji_start_price")
    op.drop_column("portfolio", "qqq_start_price")
    op.drop_column("portfolio", "spy_start_price")
