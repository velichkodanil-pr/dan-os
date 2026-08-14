"""Owner-only local security scan of the existing knowledge base (R6.1A §8).

The live gate protects everything ingested from now on. This is the other
half: the base as it stands today was built by a pipeline that indexed and
compiled credential-bearing content, so it has to be examined and contained.

Hard properties, in order of importance:

- ZERO provider calls. No OpenAI, no Anthropic, no web, no connectors. Only
  `secret_policy`, which is local regex code. A security scan that phones a
  provider with the very content it suspects would be self-defeating.
- Nothing is deleted. Affected resources are marked `quarantined` (or, for
  chat turns, `provider_eligible=False`), which removes them from retrieval,
  embeddings, compilation and model context while leaving the row intact.
  Danylo decides what to delete, after he has seen the counts.
- Immutable stays immutable. Raw events get a finding recorded against them;
  their payload is not rewritten and not removed.
- Idempotent. Re-running produces the same containment and no duplicate
  findings (unique key per resource + scanner version).
- Bounded. Everything is read in keyset batches, so memory stays flat
  whatever the size of the base.
- Honest completion. The scan-complete flag is cleared at the start and set
  only after a full pass finishes; an interrupted run leaves the gate closed.
"""
import logging
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.audit import audit
from app.models import (
    ChatLog, Document, KnowledgeChunk, MemoryItem, RawEvent, WikiPage,
)

logger = logging.getLogger(__name__)

BATCH = 300
CONTAINED_MEMORY_STATUSES = ("candidate", "confirmed", "superseded")


@dataclass
class ScanReport:
    """Counts only. No titles, no ids, no excerpts — this text gets sent to
    Telegram, and a «suspicious document» title can itself be a leak."""
    documents_scanned: int = 0
    documents_quarantined: int = 0
    wiki_scanned: int = 0
    wiki_quarantined: int = 0
    memory_scanned: int = 0
    memory_quarantined: int = 0
    chat_scanned: int = 0
    chat_contained: int = 0
    raw_events_scanned: int = 0
    raw_events_flagged: int = 0
    findings: int = 0
    categories: list = field(default_factory=list)
    completed: bool = False

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def affected(self) -> int:
        return (self.documents_quarantined + self.wiki_quarantined
                + self.memory_quarantined + self.chat_contained
                + self.raw_events_flagged)


def _note(report: ScanReport, result) -> None:
    for c in result.categories:
        if str(c) not in report.categories:
            report.categories.append(str(c))


async def _scan_documents(db: AsyncSession, user_id: int, report: ScanReport) -> None:
    """Chunk text grouped by its parent document; the DOCUMENT is contained."""
    last = None
    tripped: dict = {}
    while True:
        q = (select(KnowledgeChunk.id, KnowledgeChunk.document_id,
                    KnowledgeChunk.text)
             .where(KnowledgeChunk.user_id == user_id)
             .order_by(KnowledgeChunk.id).limit(BATCH))
        if last is not None:
            q = q.where(KnowledgeChunk.id > last)
        rows = (await db.execute(q)).all()
        if not rows:
            break
        for row in rows:
            last = row.id
            result = security.scan(row.text)
            if result.blocked:
                prev = tripped.get(row.document_id)
                if prev is None or result.finding_count > prev.finding_count:
                    tripped[row.document_id] = result
                _note(report, result)

    docs = (await db.execute(select(Document).where(
        Document.user_id == user_id))).scalars().all()
    report.documents_scanned = len(docs)
    for doc in docs:
        result = tripped.get(doc.id)
        if result is None:
            continue
        if doc.status != "quarantined":
            doc.status = "quarantined"
            meta = dict(doc.meta or {})
            meta["security"] = result.as_meta()
            doc.meta = meta
            report.documents_quarantined += 1
        if await security.record_finding(
                db, user_id=user_id, domain=doc.domain,
                resource_type="document", resource_id=doc.id, result=result):
            report.findings += 1


async def _scan_wiki(db: AsyncSession, user_id: int, report: ScanReport) -> None:
    last = None
    while True:
        q = (select(WikiPage).where(WikiPage.user_id == user_id)
             .order_by(WikiPage.id).limit(BATCH))
        if last is not None:
            q = q.where(WikiPage.id > last)
        pages = (await db.execute(q)).scalars().all()
        if not pages:
            break
        for page in pages:
            last = page.id
            report.wiki_scanned += 1
            aliases = " ".join(str(a) for a in (page.aliases or []))
            sources = " ".join(
                str(s.get("title", "")) if isinstance(s, dict) else str(s)
                for s in (page.sources or []))
            result = security.scan_parts(page.title, page.summary, page.content,
                                         page.contradictions, aliases, sources)
            if not result.blocked:
                continue
            _note(report, result)
            if page.status != "quarantined":
                page.status = "quarantined"
                report.wiki_quarantined += 1
            if await security.record_finding(
                    db, user_id=user_id, domain=page.domain,
                    resource_type="wiki_page", resource_id=page.id,
                    result=result):
                report.findings += 1


async def _scan_memory(db: AsyncSession, user_id: int, report: ScanReport) -> None:
    last = None
    while True:
        q = (select(MemoryItem).where(
            MemoryItem.user_id == user_id,
            MemoryItem.status.in_(CONTAINED_MEMORY_STATUSES))
            .order_by(MemoryItem.id).limit(BATCH))
        if last is not None:
            q = q.where(MemoryItem.id > last)
        items = (await db.execute(q)).scalars().all()
        if not items:
            break
        for item in items:
            last = item.id
            report.memory_scanned += 1
            result = security.scan(item.content)
            if not result.blocked:
                continue
            _note(report, result)
            item.status = "quarantined"
            report.memory_quarantined += 1
            if await security.record_finding(
                    db, user_id=user_id, domain=item.domain,
                    resource_type="memory_item", resource_id=item.id,
                    result=result):
                report.findings += 1


async def _scan_chat(db: AsyncSession, user_id: int, report: ScanReport) -> None:
    """Conversation turns: contained by flag, so the window still reads
    chronologically but a contained turn is never replayed to a provider."""
    last = 0
    while True:
        rows = (await db.execute(
            select(ChatLog).where(ChatLog.user_id == user_id, ChatLog.id > last)
            .order_by(ChatLog.id).limit(BATCH))).scalars().all()
        if not rows:
            break
        for row in rows:
            last = row.id
            report.chat_scanned += 1
            result = security.scan(row.text)
            if not result.blocked:
                continue
            _note(report, result)
            if row.provider_eligible:
                row.provider_eligible = False
                report.chat_contained += 1
            if await security.record_finding(
                    db, user_id=user_id, resource_type="chat_log",
                    resource_id=row.id, result=result):
                report.findings += 1


async def _scan_raw_events(db: AsyncSession, user_id: int,
                           report: ScanReport) -> None:
    """Immutable by contract: record a finding, change nothing, delete nothing."""
    last = None
    while True:
        q = (select(RawEvent).where(RawEvent.user_id == user_id)
             .order_by(RawEvent.id).limit(BATCH))
        if last is not None:
            q = q.where(RawEvent.id > last)
        events = (await db.execute(q)).scalars().all()
        if not events:
            break
        for event in events:
            last = event.id
            report.raw_events_scanned += 1
            payload = event.payload or {}
            texts = [str(v) for v in payload.values() if isinstance(v, str)]
            result = security.scan_parts(*texts)
            if not result.blocked:
                continue
            _note(report, result)
            report.raw_events_flagged += 1
            if await security.record_finding(
                    db, user_id=user_id, domain=event.domain,
                    resource_type="raw_event", resource_id=event.id,
                    result=result):
                report.findings += 1


async def run_scan(db: AsyncSession, *, user_id: int) -> ScanReport:
    """Full local pass. Safe to re-run; safe to interrupt."""
    report = ScanReport()
    await security.clear_scan_complete(db)   # an interrupted run must not pass
    await db.commit()

    await _scan_documents(db, user_id, report)
    await db.commit()
    await _scan_wiki(db, user_id, report)
    await db.commit()
    await _scan_memory(db, user_id, report)
    await db.commit()
    await _scan_chat(db, user_id, report)
    await db.commit()
    await _scan_raw_events(db, user_id, report)
    await db.commit()

    report.completed = True
    await security.mark_scan_complete(db)
    await audit(db, actor=f"user:{user_id}", action="security.scan_completed",
                resource_type="knowledge_base", policy_level="L1",
                scanner_version=security.SCANNER_VERSION,
                affected=report.affected, findings=report.findings)
    await db.commit()
    logger.info("kb security scan finished: affected=%d findings=%d",
                report.affected, report.findings)
    return report


def report_text(report: ScanReport) -> str:
    """Counts only — never a title, an id or a fragment of what was found."""
    head = ("🔒 <b>Локальний скан бази знань</b> "
            f"(сканер v{security.SCANNER_VERSION}, без жодного виклику до "
            "OpenAI/Anthropic)\n\n")
    body = (
        f"Перевірено: документів {report.documents_scanned}, "
        f"сторінок вікі {report.wiki_scanned}, фактів пам'яті "
        f"{report.memory_scanned}, реплік чату {report.chat_scanned}, "
        f"подій {report.raw_events_scanned}.\n\n"
    )
    if not report.affected:
        return head + body + ("Нічого не знайдено — база чиста ✅\n"
                              "Автокомпіляцію вікі можна вмикати.")
    found = (
        f"<b>Знайдено і поміщено в карантин:</b>\n"
        f"• документів: {report.documents_quarantined}\n"
        f"• сторінок вікі: {report.wiki_quarantined}\n"
        f"• фактів пам'яті: {report.memory_quarantined}\n"
        f"• реплік чату (не підуть у модель): {report.chat_contained}\n"
        f"• подій позначено (не змінювались): {report.raw_events_flagged}\n"
        f"• записів у журналі знахідок: {report.findings}\n"
    )
    cats = (f"Типи: {', '.join(report.categories)}\n" if report.categories else "")
    tail = ("\nКарантин — це ізоляція, не видалення: нічого не стерто, "
            "але ці дані більше не потрапляють у пошук, у вікі та в модель.\n\n"
            "⚠️ <b>Далі — вручну:</b> зміни ті доступи, які могли бути в "
            "проіндексованих джерелах, і перенеси їх у менеджер паролів. "
            "Значення сюди не надсилай.")
    return head + body + found + cats + tail
