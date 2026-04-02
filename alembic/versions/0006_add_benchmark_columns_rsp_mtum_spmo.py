"""Add RSP, MTUM, SPMO benchmark start price columns; drop DJI

Revision ID: 0006
Revises: 57c944539f48
Create Date: 2026-04-02
"""
from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("portfolio", sa.Column("rsp_start_price", sa.Numeric(15, 4), nullable=True))
    op.add_column("portfolio", sa.Column("mtum_start_price", sa.Numeric(15, 4), nullable=True))
    op.add_column("portfolio", sa.Column("spmo_start_price", sa.Numeric(15, 4), nullable=True))

    # Backfill inception prices for Feb 26, 2026
    op.execute("UPDATE portfolio SET rsp_start_price = 202.7122, mtum_start_price = 256.1504, spmo_start_price = 120.1501")


def downgrade():
    op.drop_column("portfolio", "spmo_start_price")
    op.drop_column("portfolio", "mtum_start_price")
    op.drop_column("portfolio", "rsp_start_price")
