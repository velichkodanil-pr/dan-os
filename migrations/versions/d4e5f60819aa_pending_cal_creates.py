"""pending calendar event creations (L3 confirmation)

Revision ID: d4e5f60819aa
Revises: c3d4e5f60718
Create Date: 2026-08-13
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4e5f60819aa'
down_revision = 'c3d4e5f60718'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'pending_cal_creates',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('start_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('end_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('pending_cal_creates')
