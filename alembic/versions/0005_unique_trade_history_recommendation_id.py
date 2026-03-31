"""add unique constraint on trade_history recommendation_id

Revision ID: 0005
Revises: 57c944539f48
Create Date: 2026-03-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, None] = '57c944539f48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        'uq_trade_history_recommendation_id',
        'trade_history',
        ['recommendation_id'],
    )


def downgrade() -> None:
    op.drop_constraint(
        'uq_trade_history_recommendation_id',
        'trade_history',
        type_='unique',
    )
