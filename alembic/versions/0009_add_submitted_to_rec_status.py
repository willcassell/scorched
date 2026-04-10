"""add submitted to rec status constraint

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-10
"""
from typing import Sequence, Union

from alembic import op


revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('ck_rec_status', 'trade_recommendations', type_='check')
    op.create_check_constraint(
        'ck_rec_status',
        'trade_recommendations',
        "status IN ('pending', 'submitted', 'confirmed', 'rejected')",
    )


def downgrade() -> None:
    op.drop_constraint('ck_rec_status', 'trade_recommendations', type_='check')
    op.create_check_constraint(
        'ck_rec_status',
        'trade_recommendations',
        "status IN ('pending', 'confirmed', 'rejected')",
    )
