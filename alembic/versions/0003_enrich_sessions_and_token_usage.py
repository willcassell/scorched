"""add analysis_text to recommendation_sessions and token_usage table

Revision ID: 0003
Revises: 0002
Create Date: 2026-02-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return column in {c["name"] for c in inspector.get_columns(table)}


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    if not _column_exists("recommendation_sessions", "analysis_text"):
        op.add_column(
            "recommendation_sessions",
            sa.Column("analysis_text", sa.Text(), nullable=True),
        )

    if not _table_exists("token_usage"):
        op.create_table(
            "token_usage",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("session_id", sa.Integer(), sa.ForeignKey("recommendation_sessions.id"), nullable=True),
            sa.Column("call_type", sa.String(20), nullable=False),
            sa.Column("model", sa.String(50), nullable=False),
            sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("thinking_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("estimated_cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    op.drop_table("token_usage")
    op.drop_column("recommendation_sessions", "analysis_text")
