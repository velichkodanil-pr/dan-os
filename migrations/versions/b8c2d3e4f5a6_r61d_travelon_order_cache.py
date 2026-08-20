"""r6.1d: local TravelON order cache for aggregate questions

One table, no backfill, no data migration. Idempotent: safe on prod, on a
fresh DB and on a replay after a partial apply.

Revision ID: b8c2d3e4f5a6
Revises: a7b1c2d3e4f5
Create Date: 2026-08-20
"""
import sqlalchemy as sa
from alembic import op

revision = 'b8c2d3e4f5a6'
down_revision = 'a7b1c2d3e4f5'
branch_labels = None
depends_on = None

TABLE = "travelon_orders"
SYNC_TABLE = "travelon_sync_days"


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if TABLE in insp.get_table_names():
        _create_sync_days(insp)
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("domain", sa.String(length=32), nullable=False,
                  server_default=sa.text("'travelon'")),
        sa.Column("order_no", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False,
                  server_default=sa.text("''")),
        sa.Column("provider", sa.String(length=120), nullable=False,
                  server_default=sa.text("''")),
        sa.Column("hotel", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("country", sa.String(length=80), nullable=False,
                  server_default=sa.text("''")),
        sa.Column("check_in", sa.Date(), nullable=True),
        sa.Column("created", sa.Date(), nullable=True),
        sa.Column("nights", sa.Integer(), nullable=True),
        sa.Column("tourists", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("gross_cost", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False,
                  server_default=sa.text("''")),
        sa.Column("debt", sa.Float(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "order_no", name="uq_tvorder_user_no"),
        sa.CheckConstraint("domain IN ('personal','travelon','tech')",
                           name="ck_travelon_orders_domain"),
    )
    op.create_index("ix_tvorder_scope", TABLE, ["user_id", "domain", "check_in"])
    op.create_index("ix_tvorder_provider", TABLE, ["user_id", "domain", "provider"])
    _create_sync_days(insp)


def _create_sync_days(insp) -> None:
    """Coverage map: which report days were actually fetched, per basis.
    Without it, "no orders" and "never looked" are the same thing."""
    if SYNC_TABLE in insp.get_table_names():
        return
    op.create_table(
        SYNC_TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("basis", sa.String(length=16), nullable=False,
                  server_default=sa.text("'check_in'")),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("orders", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "basis", "day", name="uq_tvsync_day"),
    )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    for t in (SYNC_TABLE, TABLE):
        if t in insp.get_table_names():
            op.drop_table(t)
