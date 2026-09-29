"""engineer chat

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26 13:10:00
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('engineer_chat',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('plant_id', sa.Uuid(), nullable=False),
    sa.Column('user_sub', sa.Text(), nullable=False),
    sa.Column('user_name', sa.Text(), nullable=True),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('messages', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('transcript', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), server_default='0', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_engineer_chat')),
    schema='agent'
    )
    op.create_index('ix_agent_engineer_chat_user', 'engineer_chat', ['user_sub', 'last_at'], unique=False, schema='agent')


def downgrade() -> None:
    op.drop_index('ix_agent_engineer_chat_user', table_name='engineer_chat', schema='agent')
    op.drop_table('engineer_chat', schema='agent')
