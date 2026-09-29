"""SLA at-risk flags on tickets (alerts at 75% of a target)

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-25 18:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col in ('response_warned', 'resolution_warned'):
        op.add_column('ticket', sa.Column(col, sa.Boolean(), server_default='false', nullable=False), schema='ticketing')


def downgrade() -> None:
    for col in ('response_warned', 'resolution_warned'):
        op.drop_column('ticket', col, schema='ticketing')
