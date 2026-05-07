"""add gate_decisions table

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gate_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("recommendation_sessions.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "recommendation_id",
            sa.Integer(),
            sa.ForeignKey("trade_recommendations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("action", sa.String(4), nullable=False),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("gate", sa.String(40), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_gate_decisions_created_at", "gate_decisions", ["created_at"])
    op.create_index("ix_gate_decisions_phase_gate", "gate_decisions", ["phase", "gate"])
    op.create_index("ix_gate_decisions_recommendation_id", "gate_decisions", ["recommendation_id"])


def downgrade() -> None:
    op.drop_index("ix_gate_decisions_recommendation_id", table_name="gate_decisions")
    op.drop_index("ix_gate_decisions_phase_gate", table_name="gate_decisions")
    op.drop_index("ix_gate_decisions_created_at", table_name="gate_decisions")
    op.drop_table("gate_decisions")
