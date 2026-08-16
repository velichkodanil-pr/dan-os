"""r6.1b: end-to-end domain isolation

Adds `domain` where it was missing, backfills from a TRUSTED PARENT only
(never from text/title/email/model output), scopes the unique constraints,
and adds CHECK constraints + indexes. Idempotent per step (checks live schema
first) so it is safe on the R6.1A.1 production DB, on a fresh DB, and on a
re-run after a partial apply.

Backfill rules (§15):
  - existing valid domain is kept;
  - child rows inherit from their trusted parent
    (knowledge_chunks<-documents, reminders<-tasks, habit_log<-habits);
  - rows without a trusted parent (chat_log, knowledge_gaps, pending_*) -> personal;
  - google_credentials.domain -> NULL (unassigned; never guessed);
  - user_state.active_domain -> personal.

No LLM, no embeddings, no connectors, no content reads for classification.

Revision ID: a7b1c2d3e4f5
Revises: f6a1b2c3d4e7
Create Date: 2026-08-15
"""
import sqlalchemy as sa
from alembic import op

revision = 'a7b1c2d3e4f5'
down_revision = 'f6a1b2c3d4e7'
branch_labels = None
depends_on = None

_DOMAIN_CK = "domain IN ('personal','travelon','tech')"


def _insp():
    return sa.inspect(op.get_bind())


def _cols(table):
    if table not in _insp().get_table_names():
        return set()
    return {c["name"] for c in _insp().get_columns(table)}


def _constraints(table):
    insp = _insp()
    names = set()
    for uc in insp.get_unique_constraints(table):
        names.add(uc["name"])
    for ck in insp.get_check_constraints(table):
        names.add(ck["name"])
    return names


def _add_domain(table, *, nullable=False, server_default="'personal'"):
    if "domain" not in _cols(table):
        op.add_column(table, sa.Column(
            "domain", sa.String(length=32), nullable=True,
            server_default=sa.text(server_default) if server_default else None))


def _finalize(table, *, nullable=False, drop_default=True):
    if "domain" not in _cols(table):
        return
    if not nullable:
        op.alter_column(table, "domain", nullable=False)
    if drop_default:
        op.alter_column(table, "domain", server_default=None)


def _add_check(table, name):
    if table in _insp().get_table_names() and name not in _constraints(table):
        op.create_check_constraint(name, table, _DOMAIN_CK)


def upgrade() -> None:
    conn = op.get_bind()

    # ---- user_state.active_domain ----
    if "active_domain" not in _cols("user_state"):
        op.add_column("user_state", sa.Column(
            "active_domain", sa.String(length=16), nullable=True,
            server_default=sa.text("'personal'")))
        conn.execute(sa.text(
            "UPDATE user_state SET active_domain='personal' "
            "WHERE active_domain IS NULL"))
        op.alter_column("user_state", "active_domain", nullable=False)
        op.alter_column("user_state", "active_domain", server_default=None)
    if ("user_state" in _insp().get_table_names()
            and "ck_user_state_domain" not in _constraints("user_state")):
        op.create_check_constraint(
            "ck_user_state_domain", "user_state",
            "active_domain IN ('personal','travelon','tech')")

    # ---- new domain columns with a trusted-parent backfill ----
    _add_domain("chat_log")
    _add_domain("knowledge_gaps")
    _add_domain("knowledge_chunks")
    _add_domain("reminders")
    _add_domain("habits")
    _add_domain("habit_log")
    _add_domain("pending_drafts")
    _add_domain("pending_cal_actions")
    _add_domain("pending_cal_creates")

    # child <- trusted parent
    conn.execute(sa.text(
        "UPDATE knowledge_chunks c SET domain = d.domain "
        "FROM documents d WHERE c.document_id = d.id"))
    conn.execute(sa.text(
        "UPDATE reminders r SET domain = t.domain "
        "FROM tasks t WHERE r.task_id = t.id"))
    conn.execute(sa.text(
        "UPDATE habit_log hl SET domain = h.domain "
        "FROM habits h WHERE hl.habit_id = h.id"))
    # anything still NULL (no trusted parent) -> personal
    for tbl in ("chat_log", "knowledge_gaps", "knowledge_chunks", "reminders",
                "habits", "habit_log", "pending_drafts", "pending_cal_actions",
                "pending_cal_creates"):
        conn.execute(sa.text(
            f"UPDATE {tbl} SET domain='personal' WHERE domain IS NULL"))

    for tbl in ("chat_log", "knowledge_gaps", "knowledge_chunks", "reminders",
                "habits", "habit_log", "pending_drafts", "pending_cal_actions",
                "pending_cal_creates"):
        _finalize(tbl)

    # ---- normalise any pre-existing invalid domain values -> personal ----
    for tbl in ("raw_events", "proposals", "tasks", "memory_items", "documents",
                "wiki_pages", "goals", "security_findings"):
        if "domain" in _cols(tbl):
            conn.execute(sa.text(
                f"UPDATE {tbl} SET domain='personal' "
                f"WHERE domain IS NULL OR domain NOT IN "
                f"('personal','travelon','tech')"))

    # ---- google_credentials.domain (nullable, unassigned) ----
    if "domain" not in _cols("google_credentials"):
        op.add_column("google_credentials",
                      sa.Column("domain", sa.String(length=32), nullable=True))

    # ---- audit_log.domain (nullable, global=NULL) ----
    if "domain" not in _cols("audit_log"):
        op.add_column("audit_log",
                      sa.Column("domain", sa.String(length=32), nullable=True))

    # ---- CHECK constraints ----
    for tbl, name in (
        ("raw_events", "ck_raw_events_domain"),
        ("proposals", "ck_proposals_domain"),
        ("tasks", "ck_tasks_domain"),
        ("memory_items", "ck_memory_items_domain"),
        ("documents", "ck_documents_domain"),
        ("wiki_pages", "ck_wiki_pages_domain"),
        ("goals", "ck_goals_domain"),
        ("habits", "ck_habits_domain"),
        ("chat_log", "ck_chat_log_domain"),
        ("knowledge_chunks", "ck_knowledge_chunks_domain"),
        ("knowledge_gaps", "ck_knowledge_gaps_domain"),
        ("security_findings", "ck_security_findings_domain"),
    ):
        _add_check(tbl, name)
    if ("google_credentials" in _insp().get_table_names()
            and "ck_gcred_domain" not in _constraints("google_credentials")):
        op.create_check_constraint(
            "ck_gcred_domain", "google_credentials",
            "domain IS NULL OR " + _DOMAIN_CK)

    # ---- swap global uniques for domain-scoped ones ----
    # documents.content_hash: global -> (user_id, domain, content_hash)
    doc_uc = _constraints("documents")
    if "uq_document_scope" not in doc_uc:
        for cand in ("documents_content_hash_key",):
            if cand in doc_uc:
                op.drop_constraint(cand, "documents", type_="unique")
        op.create_unique_constraint(
            "uq_document_scope", "documents", ["user_id", "domain", "content_hash"])

    # wiki_pages: (user_id, slug) -> (user_id, domain, slug)
    wiki_uc = _constraints("wiki_pages")
    if "uq_wiki_slug" in wiki_uc:
        op.drop_constraint("uq_wiki_slug", "wiki_pages", type_="unique")
    if "uq_wiki_slug" not in _constraints("wiki_pages") or True:
        # recreate with the domain-scoped columns (name reused intentionally)
        existing = _constraints("wiki_pages")
        if "uq_wiki_slug" not in existing:
            op.create_unique_constraint(
                "uq_wiki_slug", "wiki_pages", ["user_id", "domain", "slug"])

    # raw_events.dedupe_key: global -> (user_id, domain, dedupe_key)
    re_uc = _constraints("raw_events")
    if "uq_rawevent_scope" not in re_uc:
        for cand in ("raw_events_dedupe_key_key",):
            if cand in re_uc:
                op.drop_constraint(cand, "raw_events", type_="unique")
        op.create_unique_constraint(
            "uq_rawevent_scope", "raw_events", ["user_id", "domain", "dedupe_key"])

    # security_findings idempotency now includes domain
    sf_uc = _constraints("security_findings")
    if "uq_secfinding_resource" in sf_uc:
        op.drop_constraint("uq_secfinding_resource", "security_findings", type_="unique")
    if "uq_secfinding_resource" not in _constraints("security_findings"):
        op.create_unique_constraint(
            "uq_secfinding_resource", "security_findings",
            ["user_id", "domain", "resource_type", "resource_id", "scanner_version"])

    # ---- indexes ----
    _idx("ix_documents_domain", "documents", ["user_id", "domain", "status"])
    _idx("ix_chunks_domain", "knowledge_chunks", ["user_id", "domain"])
    _idx("ix_wiki_domain", "wiki_pages", ["user_id", "domain", "status"])
    _idx("ix_chat_log_domain", "chat_log", ["user_id", "domain", "ts"])
    _idx("ix_memory_domain", "memory_items", ["user_id", "domain", "status"])
    _idx("ix_tasks_domain", "tasks", ["user_id", "domain", "status"])
    _idx("ix_goals_domain", "goals", ["user_id", "domain", "status"])
    _idx("ix_habits_domain", "habits", ["user_id", "domain", "active"])
    _idx("ix_gaps_domain", "knowledge_gaps", ["user_id", "domain", "resolved"])


def _idx(name, table, cols):
    insp = _insp()
    if table not in insp.get_table_names():
        return
    if name in {i["name"] for i in insp.get_indexes(table)}:
        return
    op.create_index(name, table, cols)


def downgrade() -> None:
    # Best-effort reverse: drop the added structures. Domain columns that
    # existed before R6.1B (raw_events, proposals, tasks, memory_items,
    # documents, wiki_pages, goals, security_findings) are LEFT in place.
    for name, table in (
        ("ix_gaps_domain", "knowledge_gaps"),
        ("ix_habits_domain", "habits"),
        ("ix_goals_domain", "goals"),
        ("ix_tasks_domain", "tasks"),
        ("ix_memory_domain", "memory_items"),
        ("ix_chat_log_domain", "chat_log"),
        ("ix_wiki_domain", "wiki_pages"),
        ("ix_chunks_domain", "knowledge_chunks"),
        ("ix_documents_domain", "documents"),
    ):
        try:
            op.drop_index(name, table_name=table)
        except Exception:
            pass
    for tbl in ("chat_log", "knowledge_gaps", "knowledge_chunks", "reminders",
                "habits", "habit_log", "pending_drafts", "pending_cal_actions",
                "pending_cal_creates", "audit_log", "google_credentials"):
        if "domain" in _cols(tbl):
            try:
                op.drop_column(tbl, "domain")
            except Exception:
                pass
    if "active_domain" in _cols("user_state"):
        op.drop_column("user_state", "active_domain")
