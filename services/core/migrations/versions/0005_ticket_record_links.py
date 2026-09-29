"""ticket record links (real ServiceNow / Jira URLs from the ITSM adapter)

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-24 20:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('ticket', sa.Column('sn_url', sa.Text(), nullable=True), schema='ticketing')
    op.add_column('ticket', sa.Column('jira_url', sa.Text(), nullable=True), schema='ticketing')


def downgrade() -> None:
    op.drop_column('ticket', 'jira_url', schema='ticketing')
    op.drop_column('ticket', 'sn_url', schema='ticketing')
