"""ServiceNow / Jira connections configured in the UI, their change history, and the ticket -> instance link

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26 15:00:00
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('connection',
    sa.Column('system', sa.Text(), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('username', sa.Text(), nullable=False),
    sa.Column('secret', sa.Text(), nullable=True),
    sa.Column('secret_set_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('secret_set_by', sa.Text(), nullable=True),
    sa.Column('options', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_by', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('system', name=op.f('pk_connection')),
    schema='itsm'
    )
    op.create_table('connection_change',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('system', sa.Text(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('by', sa.Text(), nullable=False),
    sa.Column('changes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_connection_change')),
    schema='itsm'
    )
    op.create_table('ticket_link',
    sa.Column('ticket_id', sa.Uuid(), nullable=False),
    sa.Column('sn_instance', sa.Text(), nullable=True),
    sa.Column('sn_sys_id', sa.Text(), nullable=True),
    sa.Column('sn_number', sa.Text(), nullable=True),
    sa.Column('jira_site', sa.Text(), nullable=True),
    sa.Column('jira_key', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('ticket_id', name=op.f('pk_ticket_link')),
    schema='itsm'
    )
    # tickets synced so far: link them to the instance they were created on (host of the incident / issue URL)
    op.execute("""
        INSERT INTO itsm.ticket_link (ticket_id, sn_instance, sn_sys_id, sn_number, jira_site, jira_key)
        SELECT DISTINCT ON (ticket_id) ticket_id,
               lower(substring(result->>'sn_url' from '^https?://([^/]+)')), result->>'sn_sys_id', result->>'sn_number',
               lower(substring(result->>'jira_url' from '^https?://([^/]+)')), result->>'jira_key'
        FROM itsm.outbound_message
        WHERE status = 'SYNCED' AND result->>'sn_sys_id' IS NOT NULL
        ORDER BY ticket_id, seq DESC
    """)


def downgrade() -> None:
    op.drop_table('ticket_link', schema='itsm')
    op.drop_table('connection_change', schema='itsm')
    op.drop_table('connection', schema='itsm')
