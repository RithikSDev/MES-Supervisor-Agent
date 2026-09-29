"""support team and ticket assignee (ServiceNow "Assigned to")

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-28 10:00:00
"""
import sqlalchemy as sa
from alembic import op

from mes_core.modules.compliance.audit_sql import attach_audit_trigger, split_statements

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('support_member',
    sa.Column('plant_id', sa.Uuid(), nullable=False),
    sa.Column('username', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('email', sa.Text(), nullable=True),
    sa.Column('tier', sa.Text(), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('row_version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_by', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_by', sa.Text(), nullable=False),
    sa.CheckConstraint("tier IS NULL OR tier IN ('L1','L2','L3')", name=op.f('ck_support_member_tier')),
    sa.ForeignKeyConstraint(['plant_id'], ['org.plant.id'], name=op.f('fk_support_member_plant_id_plant')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_support_member')),
    sa.UniqueConstraint('plant_id', 'username', name=op.f('uq_support_member_plant_id_username')),
    schema='ticketing'
    )
    op.add_column('ticket', sa.Column('assignee_id', sa.Uuid(), nullable=True), schema='ticketing')
    op.add_column('ticket', sa.Column('assignee_name', sa.Text(), nullable=True), schema='ticketing')
    op.add_column('ticket', sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=True), schema='ticketing')
    op.create_index(op.f('ix_ticket_assignee_id'), 'ticket', ['assignee_id'], unique=False, schema='ticketing')
    op.create_foreign_key(op.f('fk_ticket_assignee_id_support_member'), 'ticket', 'support_member', ['assignee_id'],
                          ['id'], source_schema='ticketing', referent_schema='ticketing')
    bind = op.get_bind()
    for stmt in split_statements(attach_audit_trigger('ticketing.support_member')):
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    op.drop_constraint(op.f('fk_ticket_assignee_id_support_member'), 'ticket', schema='ticketing', type_='foreignkey')
    op.drop_index(op.f('ix_ticket_assignee_id'), table_name='ticket', schema='ticketing')
    for col in ('assigned_at', 'assignee_name', 'assignee_id'):
        op.drop_column('ticket', col, schema='ticketing')
    op.drop_table('support_member', schema='ticketing')
