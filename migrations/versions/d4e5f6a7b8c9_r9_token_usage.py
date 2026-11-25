"""r9: token usage meter — one numbers-only row per provider call

Idempotent: safe on prod, on a fresh DB and on a replay after a partial apply.

Revision ID: d4e5f6a7b8c9
Revises: c9d3e4f5a6b7
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from alembic import op

revision = 'd4e5f6a7b8c9'
down_revision = 'c9d3e4f5a6b7'
branch_labels = None
depends_on = None

TABLE = "token_usage"
INDEX = "ix_token_usage_at"


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if TABLE not in set(insp.get_table_names()):
        op.create_table(
            TABLE,
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.Column("provider", sa.String(length=16), nullable=False),
            sa.Column("model", sa.String(length=64), nullable=False,
                      server_default=sa.text("''")),
            sa.Column("feature", sa.String(length=48), nullable=False,
                      server_default=sa.text("''")),
            sa.Column("input_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("output_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("cache_read", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("cache_write", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("web_searches", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("cost_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
            sa.Column("approx", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
        insp = sa.inspect(op.get_bind())
    if INDEX not in {i["name"] for i in insp.get_indexes(TABLE)}:
        op.create_index(INDEX, TABLE, ["at"])


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if TABLE in set(insp.get_table_names()):
        op.drop_table(TABLE)
