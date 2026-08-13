"""DAN.OS core tables (round 1).

Invariants (see CLAUDE.md):
- raw events are immutable, deduplicated by a unique key;
- a proposal becomes at most ONE task (unique tasks.proposal_id);
- audit_log is append-only: application code has no update/delete path for it;
- memory items carry provenance (source event, domain, status).
"""
import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBED_DIM = 1536


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSONB}


class RawEvent(Base):
    __tablename__ = "raw_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(32), default="telegram")
    event_type: Mapped[str] = mapped_column(String(64))
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    domain: Mapped[str] = mapped_column(String(32), default="personal")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_events.id"))
    kind: Mapped[str] = mapped_column(String(16), default="task")
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed|approved|rejected|superseded
    version: Mapped[int] = mapped_column(Integer, default=1)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    domain: Mapped[str] = mapped_column(String(32), default="personal")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)  # title, due_at, remind_at, memory_text
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("proposal_id", name="uq_tasks_proposal"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("proposals.id"), nullable=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    domain: Mapped[str] = mapped_column(String(32), default="personal")
    title: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|completed|cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="scheduled")  # scheduled|fired|cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_reminders_due", Reminder.status, Reminder.fire_at)


class MemoryItem(Base):
    __tablename__ = "memory_items"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(BigInteger)
    domain: Mapped[str] = mapped_column(String(32), default="personal")
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="candidate")  # candidate|confirmed|superseded|deleted
    confidence: Mapped[float] = mapped_column(Float, default=0.7)
    sensitivity: Mapped[str] = mapped_column(String(16), default="private")
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_events.id"), nullable=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditRecord(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(64))  # "user:<id>" | "system:scheduler"
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str] = mapped_column(String(64), default="")
    outcome: Mapped[str] = mapped_column(String(16), default="ok")  # ok|denied|dedupe|error
    policy_level: Mapped[str | None] = mapped_column(String(8), nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)  # short metadata only — never full bodies/secrets


class UserState(Base):
    __tablename__ = "user_state"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pending_edit_proposal: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class GoogleCredential(Base):
    """OAuth tokens for one Google account (multi-account; Fernet-encrypted)."""
    __tablename__ = "google_credentials"
    __table_args__ = (UniqueConstraint("user_id", "account_email", name="uq_gcred_user_email"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(BigInteger)
    account_email: Mapped[str] = mapped_column(String(255))
    label: Mapped[str] = mapped_column(String(64), default="")  # short tag, e.g. mail local part
    refresh_token_enc: Mapped[str] = mapped_column(Text)
    access_token: Mapped[str] = mapped_column(Text, default="")
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AppState(Base):
    """Small key/value store for ritual bookkeeping (last brief date etc.)."""
    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Document(Base):
    """Ingested knowledge source (raw -> indexed) with provenance."""
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(BigInteger)
    domain: Mapped[str] = mapped_column(String(32), default="personal")
    title: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(32))  # telegram_file|telegram_forward|drive|email
    source_ref: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)  # dedupe
    status: Mapped[str] = mapped_column(String(16), default="indexed")  # raw|indexed|failed
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(Vector(EMBED_DIM))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeGap(Base):
    """Questions the knowledge base could not answer — feeds the coverage map (R3b)."""
    __tablename__ = "knowledge_gaps"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    question: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PendingDraft(Base):
    """Email draft proposal awaiting L3 confirmation (draft-only, never send)."""
    __tablename__ = "pending_drafts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(BigInteger)
    to_addr: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    thread_id: Mapped[str] = mapped_column(Text, default="")
    in_reply_to: Mapped[str] = mapped_column(Text, default="")
    references: Mapped[str] = mapped_column(Text, default="")
    credential_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed|created|rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatLog(Base):
    """Short conversation window for multi-turn chat context (trimmed reads)."""
    __tablename__ = "chat_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(8))  # user|bot
    text: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PendingCalAction(Base):
    """Calendar participation change awaiting L3 confirmation (RSVP only)."""
    __tablename__ = "pending_cal_actions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(BigInteger)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    calendar_id: Mapped[str] = mapped_column(Text)
    event_id: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    start_str: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(String(16), default="decline")  # decline|accept|tentative
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed|done|rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PendingCalCreate(Base):
    """New calendar event awaiting L3 confirmation (own calendars only)."""
    __tablename__ = "pending_cal_creates"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(Text)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed|done|rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Goal(Base):
    """Coach (R4): a mid-term goal Danylo tracks with DAN.OS."""
    __tablename__ = "goals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(BigInteger)
    domain: Mapped[str] = mapped_column(String(32), default="personal")
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|done|dropped
    target_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Habit(Base):
    """Coach (R4): a daily habit; done-marks live in habit_log."""
    __tablename__ = "habits"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HabitLog(Base):
    """One row = habit done on that local date (unique per habit+date)."""
    __tablename__ = "habit_log"
    __table_args__ = (UniqueConstraint("habit_id", "log_date", name="uq_habitlog_day"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    habit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("habits.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    log_date: Mapped[str] = mapped_column(String(10))  # ISO YYYY-MM-DD in Europe/Kyiv
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
