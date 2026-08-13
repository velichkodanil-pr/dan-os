"""pending calendar actions (RSVP with L3 confirmation)

Revision ID: c3d4e5f60718
Revises: b7c8d9e0f1a2
Create Date: 2026-08-13
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f60718'
down_revision = 'b7c8d9e0f1a2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'pending_cal_actions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('credential_id', sa.UUID(), nullable=True),
        sa.Column('calendar_id', sa.Text(), nullable=False),
        sa.Column('event_id', sa.Text(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('start_str', sa.Text(), nullable=False),
        sa.Column('action', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('pending_cal_actions')
