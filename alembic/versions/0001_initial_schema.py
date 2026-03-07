"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-02-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portfolio",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cash_balance", sa.Numeric(15, 4), nullable=False),
        sa.Column("starting_capital", sa.Numeric(15, 4), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(10), nullable=False, unique=True),
        sa.Column("shares", sa.Numeric(15, 6), nullable=False),
        sa.Column("avg_cost_basis", sa.Numeric(15, 4), nullable=False),
        sa.Column("first_purchase_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "recommendation_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_date", sa.Date(), nullable=False, unique=True),
        sa.Column("raw_research", sa.Text()),
        sa.Column("claude_response", sa.Text()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "trade_recommendations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("recommendation_sessions.id"), nullable=False),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("action", sa.String(4), nullable=False),
        sa.Column("suggested_price", sa.Numeric(15, 4), nullable=False),
        sa.Column("quantity", sa.Numeric(15, 6), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("confidence", sa.String(10), nullable=False, server_default="medium"),
        sa.Column("key_risks", sa.Text()),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("action IN ('buy', 'sell')", name="ck_rec_action"),
        sa.CheckConstraint("status IN ('pending', 'confirmed', 'rejected')", name="ck_rec_status"),
    )

    op.create_table(
        "trade_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("recommendation_id", sa.Integer(), sa.ForeignKey("trade_recommendations.id"), nullable=True),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("action", sa.String(4), nullable=False),
        sa.Column("shares", sa.Numeric(15, 6), nullable=False),
        sa.Column("execution_price", sa.Numeric(15, 4), nullable=False),
        sa.Column("total_value", sa.Numeric(15, 4), nullable=False),
        sa.Column("executed_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("realized_gain", sa.Numeric(15, 4)),
        sa.Column("tax_category", sa.String(10)),
        sa.CheckConstraint("action IN ('buy', 'sell')", name="ck_hist_action"),
        sa.CheckConstraint(
            "tax_category IN ('short_term', 'long_term') OR tax_category IS NULL",
            name="ck_hist_tax_category",
        ),
    )


def downgrade() -> None:
    op.drop_table("trade_history")
    op.drop_table("trade_recommendations")
    op.drop_table("recommendation_sessions")
    op.drop_table("positions")
    op.drop_table("portfolio")
