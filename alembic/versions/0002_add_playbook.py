"""add playbook table

Revision ID: 0002
Revises: 0001
Create Date: 2026-02-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "playbook",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("playbook")
