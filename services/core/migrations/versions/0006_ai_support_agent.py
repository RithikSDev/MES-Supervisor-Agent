"""AI support agent: AI origin on ticket activity, full-text ticket search

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-25 12:00:00
"""
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

SEARCH_DOCUMENT = ("to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, '') || ' ' || "
                   "coalesce(resolution_note, ''))")


def upgrade() -> None:
    op.drop_constraint('origin', 'ticket_activity', schema='ticketing', type_='check')
    op.create_check_constraint('origin', 'ticket_activity',
                               "origin IN ('MES','SERVICENOW','JIRA','SYSTEM','AI')", schema='ticketing')
    op.execute(f"CREATE INDEX ix_ticket_search ON ticketing.ticket USING gin ({SEARCH_DOCUMENT})")


def downgrade() -> None:
    op.execute("DROP INDEX ticketing.ix_ticket_search")
    op.execute("UPDATE ticketing.ticket_activity SET origin = 'MES' WHERE origin = 'AI'")
    op.drop_constraint('origin', 'ticket_activity', schema='ticketing', type_='check')
    op.create_check_constraint('origin', 'ticket_activity',
                               "origin IN ('MES','SERVICENOW','JIRA','SYSTEM')", schema='ticketing')
