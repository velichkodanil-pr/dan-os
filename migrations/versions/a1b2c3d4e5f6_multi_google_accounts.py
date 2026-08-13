"""multi google accounts

Revision ID: a1b2c3d4e5f6
Revises: f75ba5fb32c6
Create Date: 2026-08-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a1b2c3d4e5f6'
down_revision = 'f75ba5fb32c6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Multi-account rework: old single-account rows carried old scopes and
    # require re-consent anyway — recreate the table cleanly.
    op.drop_table('google_credentials')
    op.create_table(
        'google_credentials',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('account_email', sa.String(length=255), nullable=False),
        sa.Column('label', sa.String(length=64), nullable=False),
        sa.Column('refresh_token_enc', sa.Text(), nullable=False),
        sa.Column('access_token', sa.Text(), nullable=False),
        sa.Column('access_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('scopes', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'account_email', name='uq_gcred_user_email'),
    )
    op.add_column('pending_drafts', sa.Column('credential_id', sa.UUID(), nullable=True))


def downgrade() -> None:
    op.drop_column('pending_drafts', 'credential_id')
    op.drop_table('google_credentials')
    op.create_table(
        'google_credentials',
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('refresh_token_enc', sa.Text(), nullable=False),
        sa.Column('access_token', sa.Text(), nullable=False),
        sa.Column('access_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('scopes', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('user_id'),
    )
