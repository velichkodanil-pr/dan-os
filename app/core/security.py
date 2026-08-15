"""The single security gate (R6.1A) — one place every path goes through.

`secret_policy` decides WHAT is a secret. This module decides what DAN.OS
DOES about it, and it lives in `app/core` on purpose: Telegram, the Mini App,
the Drive indexer, the Cowork admin endpoint and the scheduler all share the
same gate. A check that lives in a handler protects one door and leaves the
rest of the building open.

Three rules hold everywhere below:

1. Containment, not deletion. A blocked resource is marked `quarantined` and
   excluded from retrieval, embeddings, compilation and model context. The
   row stays. Nothing in this round deletes user content — Danylo decides
   what to delete, after he has seen what was found.
2. Metadata only. Findings, audit rows, log lines, exceptions and Telegram
   replies carry categories and counts. Never a value, an excerpt, a
   reversible encoding, or a hash of a secret.
3. Fail closed. If the scanner raises, treat the text as blocked. A scanner
   bug must cost an answer, not a credential.
"""
import logging
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import secret_policy
from app.core.audit import audit
from app.core.secret_policy import (
    SCANNER_VERSION, SecretCategory, SecretScanResult, scan_parts, scan_text,
)
from app.models import AppState, SecurityFinding

logger = logging.getLogger(__name__)

# Configure the scanner's blocking set once, from settings. Passwords are
# allowed by default (owner decision); hard technical secrets always block.
secret_policy.set_blocking_categories(
    secret_policy.ALL_CATEGORIES if settings.quarantine_passwords
    else secret_policy.HARD_SECRET_CATEGORIES)

__all__ = ["SCANNER_VERSION", "SecretCategory", "SecretScanResult",
           "SAFE_REFUSAL", "SAFE_NOT_STORED", "scan", "scan_parts",
           "record_finding", "resolve_findings", "audit_blocked",
           "is_credential_request", "scan_complete", "mark_scan_complete",
           "clear_scan_complete", "SCAN_COMPLETE_KEY"]

# What Danylo sees instead of a hard technical secret. It names the boundary
# and points at the right place — it does not echo back what he sent. Partner
# passwords are NOT covered here: those are allowed and simply get stored.
SAFE_REFUSAL = (
    "🔒 Схоже, тут технічний секрет — API-ключ / токен / приватний ключ. "
    "DAN.OS такого не зберігає і не передає моделі: у базі знань йому не місце.\n\n"
    "Тримай його там, де він виданий (консоль сервісу, BotFather, менеджер "
    "секретів). Логіни й паролі до кабінетів партнерів зберігати можна — їх "
    "я індексую нормально."
)

# A blocked lookup gets a different answer: the question was legitimate, the
# storage of a hard technical secret never was.
SAFE_NOT_STORED = (
    "🔒 Технічних ключів і токенів у мене немає — DAN.OS їх не зберігає за "
    "архітектурою. Візьми його там, де видавали. Доступи до кабінетів "
    "партнерів (логін, пароль, умови) — знайду, якщо є в базі."
)

SCAN_COMPLETE_KEY = "kb_security_scan_complete"

# ---------- credential REQUESTS (no secret in the text, but a lookup intent) ----------
#
# «який пароль до ТОКО?» must answer «не зберігаю» without running a credential
# hunt. «яка політика паролів?» is an ordinary knowledge question and must keep
# working — so the secret word alone is never enough, and any policy/process
# framing wins over the lookup framing.
# HARD-secret words: a request for one of these is always refused, because
# DAN.OS never stores them and starting a lookup would be dishonest.
_HARD_SECRET_WORD_RE = re.compile(
    r"токен|token|ключ\s+доступу|api[ _-]?key|apikey|"
    r"сід[ -]?фраз|seed[ -]?phrase|приватн\w+\s+ключ|private\s+key",
    re.IGNORECASE)
# Password words: only refused when passwords are being blocked. With the
# default (passwords allowed) «який пароль до X» flows to normal retrieval —
# the bot answers from the business tables, which is the point.
_PASSWORD_WORD_RE = re.compile(r"парол|password|passwd", re.IGNORECASE)
_LOOKUP_INTENT_RE = re.compile(
    r"\b(який|яка|яке|які|скажи|дай|давай|надішли|пришли|покажи|знайди|нагадай|"
    r"потрібен|потрібно|треба|what\s+is|what'?s|give\s+me|send\s+me|tell\s+me|"
    r"show\s+me|find)\b", re.IGNORECASE)
_POLICY_FRAMING_RE = re.compile(
    r"політик|policy|правил|вимог|requirement|стандарт|як\s+(?:нам\s+)?зберіга|"
    r"де\s+зберіга|менеджер|1password|bitwarden|keeper|vault|ротаці|rotation|"
    r"безпек|security|скільки\s+символ|міняти|change\s+the\s+password|"
    r"змінити\s+пароль|скинути\s+пароль|reset", re.IGNORECASE)


def is_credential_request(text: str) -> bool:
    """Deterministic: is this asking DAN.OS to hand over a BLOCKED credential?

    A password lookup is a blocked credential request only when passwords are
    being blocked; otherwise it is an ordinary knowledge question.
    """
    if not text:
        return False
    if _POLICY_FRAMING_RE.search(text):
        return False
    if not _LOOKUP_INTENT_RE.search(text):
        return False
    if _HARD_SECRET_WORD_RE.search(text):
        return True
    if settings.quarantine_passwords and _PASSWORD_WORD_RE.search(text):
        return True
    return False


async def resolve_findings(db: AsyncSession, *, user_id: int, resource_type: str,
                           resource_id) -> int:
    """Mark a resource's open findings resolved (used when it is released)."""
    rows = (await db.execute(select(SecurityFinding).where(
        SecurityFinding.user_id == user_id,
        SecurityFinding.resource_type == resource_type,
        SecurityFinding.resource_id == str(resource_id)[:64],
        SecurityFinding.status == "open"))).scalars().all()
    for r in rows:
        r.status = "resolved"
    return len(rows)


def scan(text: str | None) -> SecretScanResult:
    """Scan text, failing CLOSED if the scanner itself misbehaves."""
    try:
        return scan_text(text or "")
    except Exception:  # pragma: no cover — defensive: a bug must not open the gate
        logger.exception("secret scanner failed — treating input as blocked")
        return SecretScanResult(blocked=True, categories=(), finding_count=0)


async def record_finding(db: AsyncSession, *, user_id: int, resource_type: str,
                         resource_id, result: SecretScanResult,
                         domain: str = "personal") -> SecurityFinding | None:
    """Append one finding. Idempotent per (resource, scanner version).

    Uses a SAVEPOINT so a concurrent duplicate cannot poison the caller's
    transaction (ingest is mid-flight when this runs).
    """
    if not result.blocked:
        return None
    rid = str(resource_id)[:64]
    existing = (await db.execute(select(SecurityFinding).where(
        SecurityFinding.user_id == user_id,
        SecurityFinding.resource_type == resource_type,
        SecurityFinding.resource_id == rid,
        SecurityFinding.scanner_version == SCANNER_VERSION,
    ))).scalar_one_or_none()
    if existing is not None:
        if existing.status == "resolved":  # tripped again after a release
            existing.status = "open"
        return existing
    finding = SecurityFinding(
        user_id=user_id, domain=domain, resource_type=resource_type,
        resource_id=rid, categories=[str(c) for c in result.categories],
        finding_count=result.finding_count, scanner_version=SCANNER_VERSION,
        status="open")
    try:
        async with db.begin_nested():
            db.add(finding)
            await db.flush()
    except IntegrityError:  # race on the unique key — the row exists, that is fine
        return (await db.execute(select(SecurityFinding).where(
            SecurityFinding.user_id == user_id,
            SecurityFinding.resource_type == resource_type,
            SecurityFinding.resource_id == rid,
            SecurityFinding.scanner_version == SCANNER_VERSION,
        ))).scalar_one_or_none()
    return finding


async def audit_blocked(db: AsyncSession, *, user_id: int, action: str,
                        resource_type: str, resource_id="",
                        result: SecretScanResult) -> None:
    """Audit a containment decision — categories and counts, never content."""
    await audit(db, actor=f"user:{user_id}", action=action,
                resource_type=resource_type, resource_id=str(resource_id),
                outcome="denied", policy_level="L1",
                categories=",".join(str(c) for c in result.categories) or "unknown",
                finding_count=result.finding_count,
                scanner_version=SCANNER_VERSION)


# ---------- scan gate (auto-compilation stays off until the base is checked) ----------

async def scan_complete(db: AsyncSession) -> bool:
    """True only after a FULL successful local scan of the existing base."""
    row = await db.get(AppState, SCAN_COMPLETE_KEY)
    return bool(row and row.value == str(SCANNER_VERSION))


async def mark_scan_complete(db: AsyncSession) -> None:
    row = await db.get(AppState, SCAN_COMPLETE_KEY)
    if row is None:
        db.add(AppState(key=SCAN_COMPLETE_KEY, value=str(SCANNER_VERSION)))
    else:
        row.value = str(SCANNER_VERSION)


async def clear_scan_complete(db: AsyncSession) -> None:
    """An interrupted scan must never leave the gate open."""
    row = await db.get(AppState, SCAN_COMPLETE_KEY)
    if row is not None:
        await db.delete(row)
