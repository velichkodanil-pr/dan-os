"""r6.1a: security findings + containment flags

Structure only. This migration deliberately does NOT:
  - delete or rewrite any content,
  - call an LLM or an embedder,
  - scan anything (the content scan is `/kb_security_scan`, run by the owner).

It is written to be safe on three paths: upgrading the live R6 database,
building a fresh database from zero, and being re-run after a partial apply
(every step checks the live schema first).

Revision ID: f6a1b2c3d4e7
Revises: e5f60819aabb
Create Date: 2026-08-14
"""
import sqlalchemy as sa
from alembic import op

revision = 'f6a1b2c3d4e7'
down_revision = 'e5f60819aabb'
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return column in {c["name"] for c in _inspector().get_columns(table)}


def upgrade() -> None:
    if not _has_table('security_findings'):
        op.create_table(
            'security_findings',
            sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column('user_id', sa.BigInteger(), nullable=False),
            sa.Column('domain', sa.String(length=32), nullable=False,
                      server_default='personal'),
            sa.Column('resource_type', sa.String(length=32), nullable=False),
            sa.Column('resource_id', sa.String(length=64), nullable=False,
                      server_default=''),
            sa.Column('categories', sa.dialects.postgresql.JSONB(), nullable=False,
                      server_default='[]'),
            sa.Column('finding_count', sa.Integer(), nullable=False,
                      server_default='0'),
            sa.Column('scanner_version', sa.Integer(), nullable=False,
                      server_default='1'),
            sa.Column('status', sa.String(length=16), nullable=False,
                      server_default='open'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text('now()')),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('user_id', 'resource_type', 'resource_id',
                                'scanner_version', name='uq_secfinding_resource'),
        )
        op.create_index('ix_secfinding_open', 'security_findings',
                        ['user_id', 'status'])

    # Containment flags. Defaults keep every existing row exactly as it is:
    # pages stay active, past conversation turns stay eligible. Only the
    # owner-run scan and the live gate move rows out of those defaults.
    if not _has_column('wiki_pages', 'status'):
        op.add_column('wiki_pages', sa.Column(
            'status', sa.String(length=16), nullable=False,
            server_default='active'))
    if not _has_column('chat_log', 'provider_eligible'):
        op.add_column('chat_log', sa.Column(
            'provider_eligible', sa.Boolean(), nullable=False,
            server_default=sa.true()))


def downgrade() -> None:
    if _has_column('chat_log', 'provider_eligible'):
        op.drop_column('chat_log', 'provider_eligible')
    if _has_column('wiki_pages', 'status'):
        op.drop_column('wiki_pages', 'status')
    if _has_table('security_findings'):
        op.drop_index('ix_secfinding_open', table_name='security_findings')
        op.drop_table('security_findings')
