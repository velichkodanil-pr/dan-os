"""Owner-only domain integrity report (/domain_audit, R6.1B §15).

COUNTS ONLY — never a title, slug, id or any content. It answers «what is
where, and does anything look mis-scoped», so the owner can spot legacy rows a
conscious re-upload should move. No LLM, no embeddings, no provider calls, no
automatic moves.
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.domains import ALLOWED_DOMAINS
from app.models import (
    ChatLog, Document, Goal, GoogleCredential, Habit, HabitLog, KnowledgeChunk,
    KnowledgeGap, MemoryItem, PendingCalAction, PendingCalCreate, PendingDraft,
    Proposal, RawEvent, Reminder, SecurityFinding, Task, WikiPage,
)

# every domain-scoped resource type, with a short human label
_RESOURCES = [
    (RawEvent, "події"), (Proposal, "пропозиції"), (Task, "задачі"),
    (Reminder, "нагадування"), (MemoryItem, "пам'ять"), (Document, "документи"),
    (KnowledgeChunk, "фрагменти"), (KnowledgeGap, "прогалини"),
    (WikiPage, "вікі"), (ChatLog, "чат"), (Goal, "цілі"), (Habit, "звички"),
    (HabitLog, "відмітки"), (PendingDraft, "чернетки"),
    (PendingCalAction, "cal-дії"), (PendingCalCreate, "cal-створення"),
]


async def _counts_by_domain(db, user_id, model) -> dict:
    rows = (await db.execute(
        select(model.domain, func.count()).where(model.user_id == user_id)
        .group_by(model.domain))).all()
    return {(d or "—"): int(n) for d, n in rows}


async def domain_audit_report(db: AsyncSession, user_id: int) -> str:
    lines = ["🧭 <b>Аудит доменів</b> (лише цифри, без вмісту)\n"]

    # 1) resources per domain + anything outside the schema
    invalid_total = 0
    for model, name in _RESOURCES:
        counts = await _counts_by_domain(db, user_id, model)
        inside = " · ".join(f"{d}: {counts.get(d, 0)}" for d in ALLOWED_DOMAINS)
        outside = sum(v for k, v in counts.items() if k not in ALLOWED_DOMAINS)
        invalid_total += outside
        if any(counts.get(d, 0) for d in ALLOWED_DOMAINS) or outside:
            extra = f" · ⚠️ поза схемою: {outside}" if outside else ""
            lines.append(f"• {name}: {inside}{extra}")

    # 2) parent↔child domain mismatches (must always be equal)
    chunk_mismatch = (await db.execute(
        select(func.count()).select_from(KnowledgeChunk).join(
            Document, Document.id == KnowledgeChunk.document_id).where(
            KnowledgeChunk.user_id == user_id,
            KnowledgeChunk.domain != Document.domain))).scalar_one()
    rem_mismatch = (await db.execute(
        select(func.count()).select_from(Reminder).join(
            Task, Task.id == Reminder.task_id).where(
            Reminder.user_id == user_id,
            Reminder.domain != Task.domain))).scalar_one()
    hl_mismatch = (await db.execute(
        select(func.count()).select_from(HabitLog).join(
            Habit, Habit.id == HabitLog.habit_id).where(
            HabitLog.user_id == user_id,
            HabitLog.domain != Habit.domain))).scalar_one()
    lines.append(
        "\n<b>Батько↔дитина (мають збігатися):</b> "
        f"фрагменти≠документ {chunk_mismatch} · нагадування≠задача "
        f"{rem_mismatch} · відмітки≠звичка {hl_mismatch}")

    # 3) Google accounts by domain + unassigned (NULL)
    gcred = {(d or "—"): int(n) for d, n in (await db.execute(
        select(GoogleCredential.domain, func.count()).where(
            GoogleCredential.user_id == user_id)
        .group_by(GoogleCredential.domain))).all()}
    lines.append(
        "\n<b>Google-акаунти:</b> "
        + " · ".join(f"{d}: {gcred.get(d, 0)}" for d in ALLOWED_DOMAINS)
        + f" · ⚪️ не призначено: {gcred.get('—', 0)}")

    # 4) same slug / same content hash in >1 domain (expected & safe — isolated)
    slug_sub = (select(WikiPage.slug).where(WikiPage.user_id == user_id)
                .group_by(WikiPage.slug)
                .having(func.count(func.distinct(WikiPage.domain)) > 1)).subquery()
    dup_slugs = (await db.execute(
        select(func.count()).select_from(slug_sub))).scalar_one()
    hash_sub = (select(Document.content_hash).where(Document.user_id == user_id)
                .group_by(Document.content_hash)
                .having(func.count(func.distinct(Document.domain)) > 1)).subquery()
    dup_hash = (await db.execute(
        select(func.count()).select_from(hash_sub))).scalar_one()
    lines.append(
        "\n<b>Однакові в різних доменах</b> (це нормально — вони ізольовані): "
        f"слаги {dup_slugs} · хеші документів {dup_hash}")

    # 5) open security findings per domain
    sf = (await db.execute(
        select(SecurityFinding.domain, func.count()).where(
            SecurityFinding.user_id == user_id,
            SecurityFinding.status == "open")
        .group_by(SecurityFinding.domain))).all()
    if sf:
        lines.append("\n<b>Відкриті security-записи:</b> "
                     + " · ".join(f"{(d or '—')}: {int(n)}" for d, n in sf))

    if invalid_total:
        lines.append(f"\n⚠️ Значень поза схемою всього: {invalid_total} — "
                     "мали б бути personal/travelon/tech.")
    else:
        lines.append("\n✅ Значень доменів поза схемою немає.")
    lines.append("\nЛегасі-запис у не тому домені НЕ переноситься автоматично: "
                 "свідомо перезавантаж матеріал у потрібному домені (рунбук у "
                 "README).")
    return "\n".join(lines)
