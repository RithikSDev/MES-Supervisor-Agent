"""knowledge store: machine guides, manuals, fault-code lists, the MES user guide (full-text search)

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-26 18:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('knowledge_doc',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('equipment_type', sa.Text(), nullable=True),
    sa.Column('screen', sa.Text(), nullable=True),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('builtin', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('checksum', sa.Text(), nullable=False),
    sa.Column('pages', sa.Integer(), nullable=True),
    sa.Column('chars', sa.Integer(), server_default='0', nullable=False),
    sa.Column('uploaded_by', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge_doc')),
    schema='agent'
    )
    op.create_table('knowledge_chunk',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('doc_id', sa.Uuid(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('page', sa.Integer(), nullable=True),
    sa.Column('heading', sa.Text(), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['doc_id'], ['agent.knowledge_doc.id'], name=op.f('fk_knowledge_chunk_doc_id_knowledge_doc'),
                            ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge_chunk')),
    schema='agent'
    )
    op.create_index(op.f('ix_knowledge_chunk_doc_id'), 'knowledge_chunk', ['doc_id'], unique=False, schema='agent')
    op.execute("CREATE INDEX ix_knowledge_chunk_search ON agent.knowledge_chunk USING gin "
               "(to_tsvector('english', coalesce(heading, '') || ' ' || text))")


def downgrade() -> None:
    op.drop_table('knowledge_chunk', schema='agent')
    op.drop_table('knowledge_doc', schema='agent')
