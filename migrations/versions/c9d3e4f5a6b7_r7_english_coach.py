"""r7: English coach — profile, memory items, session log

Three empty tables, no backfill. Idempotent: safe on prod, on a fresh DB and
on a replay after a partial apply.

Revision ID: c9d3e4f5a6b7
Revises: b8c2d3e4f5a6
Create Date: 2026-08-21
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'c9d3e4f5a6b7'
down_revision = 'b8c2d3e4f5a6'
branch_labels = None
depends_on = None

PROFILE = "english_profile"
ITEMS = "english_items"
SESSIONS = "english_sessions"
_DOMAIN_CK = "domain IN ('personal','travelon','tech')"


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if PROFILE not in tables:
        op.create_table(
            PROFILE,
            sa.Column("user_id", sa.BigInteger(), primary_key=True),
            sa.Column("domain", sa.String(length=32), nullable=False,
                      server_default=sa.text("'personal'")),
            sa.Column("level", sa.String(length=8), nullable=False,
                      server_default=sa.text("'B1'")),
            sa.Column("minutes_per_day", sa.Integer(), nullable=False,
                      server_default=sa.text("12")),
            sa.Column("goals", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("week", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("day_in_week", sa.Integer(), nullable=False,
                      server_default=sa.text("1")),
            sa.Column("streak", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("best_streak", sa.Integer(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("sessions_done", sa.Integer(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("last_session_on", sa.Date(), nullable=True),
            sa.Column("talk_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("talk_turns", sa.Integer(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("talk_topic", sa.Text(), nullable=False,
                      server_default=sa.text("''")),
            sa.Column("talk_log", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("talk_mistakes", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("drill", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.CheckConstraint(_DOMAIN_CK, name="ck_english_profile_domain"),
        )

    if ITEMS not in tables:
        op.create_table(
            ITEMS,
            sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("domain", sa.String(length=32), nullable=False,
                      server_default=sa.text("'personal'")),
            sa.Column("term", sa.Text(), nullable=False),
            sa.Column("meaning", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("example", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("note", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("scenario", sa.String(length=40), nullable=False,
                      server_default=sa.text("''")),
            sa.Column("source", sa.String(length=16), nullable=False,
                      server_default=sa.text("'plan'")),
            sa.Column("ease", sa.Float(), nullable=False, server_default=sa.text("2.5")),
            sa.Column("interval_days", sa.Integer(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("reps", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("lapses", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("due_on", sa.Date(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.UniqueConstraint("user_id", "term", name="uq_english_item"),
        )
        op.create_index("ix_english_due", ITEMS, ["user_id", "due_on"])

    if SESSIONS not in tables:
        op.create_table(
            SESSIONS,
            sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("domain", sa.String(length=32), nullable=False,
                      server_default=sa.text("'personal'")),
            sa.Column("kind", sa.String(length=16), nullable=False,
                      server_default=sa.text("'drill'")),
            sa.Column("week", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("reviewed", sa.Integer(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("correct", sa.Integer(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("turns", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("mistakes", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
        )
        op.create_index("ix_english_sess", SESSIONS, ["user_id", "created_at"])


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    for t in (SESSIONS, ITEMS, PROFILE):
        if t in tables:
            op.drop_table(t)
