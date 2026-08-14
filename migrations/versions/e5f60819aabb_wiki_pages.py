"""wiki pages (compiled knowledge layer)

Revision ID: e5f60819aabb
Revises: d4e5f60819aa
Create Date: 2026-08-14
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = 'e5f60819aabb'
down_revision = 'd4e5f60819aa'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'wiki_pages',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('slug', sa.String(length=120), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('contradictions', sa.Text(), nullable=False),
        sa.Column('aliases', sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column('tags', sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column('sources', sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column('domain', sa.String(length=32), nullable=False),
        sa.Column('embedding', Vector(1536), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'slug', name='uq_wiki_slug'),
    )
    op.create_index('ix_wiki_kind', 'wiki_pages', ['user_id', 'kind'])


def downgrade() -> None:
    op.drop_index('ix_wiki_kind', table_name='wiki_pages')
    op.drop_table('wiki_pages')
