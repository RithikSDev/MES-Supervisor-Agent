"""MCP servers the agents may use

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-26 17:00:00
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('mcp_server',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('label', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('token', sa.Text(), nullable=True),
    sa.Column('enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('modes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('tools', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('last_check_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('updated_by', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mcp_server')),
    sa.UniqueConstraint('name', name=op.f('uq_mcp_server_name')),
    schema='agent'
    )


def downgrade() -> None:
    op.drop_table('mcp_server', schema='agent')
