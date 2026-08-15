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
import re
from dataclasses import asdict, dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.audit import audit
from app.models import (
    ChatLog, Document, Goal, Habit, KnowledgeChunk, KnowledgeGap, MemoryItem,
    PendingCalCreate, PendingDraft, Proposal, RawEvent, SecurityFinding, Task,
    WikiPage,
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
    documents_released: int = 0
    wiki_scanned: int = 0
    wiki_quarantined: int = 0
    wiki_released: int = 0
    memory_scanned: int = 0
    memory_quarantined: int = 0
    chat_scanned: int = 0
    chat_contained: int = 0
    chat_released: int = 0
    raw_events_scanned: int = 0
    raw_events_flagged: int = 0
    other_scanned: int = 0
    other_flagged: int = 0
    findings: int = 0
    # current totals after the pass (not just this run's transitions), so a
    # re-run reports what is STILL held, not «clean» when tokens remain
    documents_in_quarantine: int = 0
    wiki_in_quarantine: int = 0
    memory_in_quarantine: int = 0
    chat_in_quarantine: int = 0
    open_findings: int = 0
    categories: list = field(default_factory=list)
    completed: bool = False

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def affected(self) -> int:
        """How much is CONTAINED right now (drives the report + autocompile hint)."""
        return (self.documents_in_quarantine + self.wiki_in_quarantine
                + self.memory_in_quarantine + self.chat_in_quarantine
                + self.other_flagged)

    @property
    def released(self) -> int:
        return (self.documents_released + self.wiki_released
                + self.chat_released)


def _note(report: ScanReport, result) -> None:
    for c in result.categories:
        if str(c) not in report.categories:
            report.categories.append(str(c))


async def _scan_documents(db: AsyncSession, user_id: int, report: ScanReport) -> None:
    """Chunk text grouped by its parent document; the DOCUMENT is contained.

    Reconciles both ways: a document that trips is quarantined, and a
    quarantined document whose stored chunks no longer trip (e.g. because
    passwords are now allowed) is RELEASED back to indexed and its finding
    resolved. A document quarantined at ingest has no chunks stored, so it is
    left as-is — there is nothing to re-verify or restore without re-ingesting.
    """
    last = None
    tripped: dict = {}
    has_chunks: set = set()
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
            has_chunks.add(row.document_id)
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
        if result is not None:
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
        elif security.scan_envelope(doc.title, doc.source_ref,
                                    doc.meta or {}).blocked:
            # v2: the ENVELOPE. A clean body with a secret filename was left
            # fully indexed by v1 — and the filename is what /kb shows.
            envelope = security.scan_envelope(doc.title, doc.source_ref,
                                              doc.meta or {})
            _note(report, envelope)
            if doc.status != "quarantined":
                doc.status = "quarantined"
                doc.title = security.safe_title(doc.title)
                doc.source_ref = security.safe_title(doc.source_ref, "")
                doc.meta = {**security.safe_meta(doc.meta),
                            "security": envelope.as_meta()}
                report.documents_quarantined += 1
            if await security.record_finding(
                    db, user_id=user_id, domain=doc.domain,
                    resource_type="document", resource_id=doc.id,
                    result=envelope):
                report.findings += 1
        elif doc.status == "quarantined" and doc.id in has_chunks:
            doc.status = "indexed"           # content verified clean now
            meta = dict(doc.meta or {})
            meta.pop("security", None)
            doc.meta = meta
            await security.resolve_findings(
                db, user_id=user_id, resource_type="document", resource_id=doc.id)
            report.documents_released += 1


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
                if page.status == "quarantined":  # no longer trips -> release
                    page.status = "active"
                    await security.resolve_findings(
                        db, user_id=user_id, resource_type="wiki_page",
                        resource_id=page.id)
                    report.wiki_released += 1
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
                if not row.provider_eligible:  # contained before, clean now
                    row.provider_eligible = True
                    await security.resolve_findings(
                        db, user_id=user_id, resource_type="chat_log",
                        resource_id=row.id)
                    report.chat_released += 1
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
            # v2: RECURSIVE. v1 only looked at top-level string values, so a
            # secret in payload["meeting"]["notes"] was invisible to the scan.
            result = security.scan_envelope(event.payload or {})
            if not result.blocked:
                await security.resolve_findings(
                    db, user_id=user_id, resource_type="raw_event",
                    resource_id=event.id)   # e.g. was a password, now allowed
                continue
            _note(report, result)
            report.raw_events_flagged += 1
            if await security.record_finding(
                    db, user_id=user_id, domain=event.domain,
                    resource_type="raw_event", resource_id=event.id,
                    result=result):
                report.findings += 1


# Everything else that stores model- or user-authored text and can reach the
# model context or the UI. v1 stopped at documents/wiki/memory/chat/events, so
# a Proposal title, a staged Gmail draft or a goal could hold a secret that no
# scan ever looked at. These have no status column to quarantine, so the scan
# records a finding (visibility) and the read paths apply the scan filter.
_OTHER_ENTITIES = (
    ("proposal", Proposal, lambda r: (r.payload,)),
    ("task", Task, lambda r: (r.title,)),
    ("goal", Goal, lambda r: (r.title,)),
    ("habit", Habit, lambda r: (r.title,)),
    ("knowledge_gap", KnowledgeGap, lambda r: (r.question,)),
    ("pending_draft", PendingDraft, lambda r: (r.to_addr, r.subject, r.body)),
    ("pending_cal_create", PendingCalCreate, lambda r: (r.title,)),
)


async def _scan_other_entities(db: AsyncSession, user_id: int,
                               report: ScanReport) -> None:
    for kind, model, fields in _OTHER_ENTITIES:
        last = None
        while True:
            q = (select(model).where(model.user_id == user_id)
                 .order_by(model.id).limit(BATCH))
            if last is not None:
                q = q.where(model.id > last)
            rows = (await db.execute(q)).scalars().all()
            if not rows:
                break
            for row in rows:
                last = row.id
                report.other_scanned += 1
                result = security.scan_envelope(*fields(row))
                if not result.blocked:
                    await security.resolve_findings(
                        db, user_id=user_id, resource_type=kind,
                        resource_id=row.id)
                    continue
                _note(report, result)
                report.other_flagged += 1
                if await security.record_finding(
                        db, user_id=user_id, resource_type=kind,
                        resource_id=row.id, result=result):
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
    await _scan_other_entities(db, user_id, report)
    await db.commit()

    # current totals — what is CONTAINED right now, not just this run's changes
    report.documents_in_quarantine = (await db.execute(
        select(func.count()).select_from(Document).where(
            Document.user_id == user_id,
            Document.status == "quarantined"))).scalar_one()
    report.wiki_in_quarantine = (await db.execute(
        select(func.count()).select_from(WikiPage).where(
            WikiPage.user_id == user_id,
            WikiPage.status == "quarantined"))).scalar_one()
    report.memory_in_quarantine = (await db.execute(
        select(func.count()).select_from(MemoryItem).where(
            MemoryItem.user_id == user_id,
            MemoryItem.status == "quarantined"))).scalar_one()
    report.chat_in_quarantine = (await db.execute(
        select(func.count()).select_from(ChatLog).where(
            ChatLog.user_id == user_id,
            ChatLog.provider_eligible.is_(False)))).scalar_one()
    report.open_findings = (await db.execute(
        select(func.count()).select_from(SecurityFinding).where(
            SecurityFinding.user_id == user_id,
            SecurityFinding.status == "open"))).scalar_one()

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


# ---------- owner-facing quarantine listing (/kb_quarantine) ----------

_PART_RE = re.compile(r"\s*\(ч\.\d+\)\s*$")


def _base_title(title: str) -> str:
    """«Доступи (ч.3)» -> «Доступи», so a file split into parts is one line."""
    return _PART_RE.sub("", title or "").strip()


def _safe_title(title: str) -> str:
    """A file NAME can itself carry a secret; mask it rather than echo it."""
    if security.scan(title).blocked:
        return "🔒 (назву приховано — вона сама схожа на секрет)"
    return title


async def quarantine_listing(db: AsyncSession, user_id: int) -> dict:
    """What is quarantined right now — titles, dates and categories ONLY.

    Owner-only diagnostics for the rotation walk-through. Deliberately never
    includes content, excerpts, chunk text or chat lines: the listing names
    WHICH sources to deal with, not what was inside them.
    """
    docs = (await db.execute(
        select(Document).where(Document.user_id == user_id,
                               Document.status == "quarantined")
        .order_by(Document.title))).scalars().all()
    grouped: dict[str, dict] = {}
    for d in docs:
        key = _base_title(d.title)
        entry = grouped.setdefault(key, {
            "title": _safe_title(key), "parts": 0,
            "date": d.created_at, "categories": set(), "source": d.source_type})
        entry["parts"] += 1
        entry["date"] = min(entry["date"], d.created_at)
        sec = (d.meta or {}).get("security") or {}
        entry["categories"] |= {str(c) for c in (sec.get("categories") or [])}

    pages = (await db.execute(
        select(WikiPage).where(WikiPage.user_id == user_id,
                               WikiPage.status == "quarantined")
        .order_by(WikiPage.title))).scalars().all()

    chat_contained = (await db.execute(
        select(func.count()).select_from(ChatLog)
        .where(ChatLog.user_id == user_id,
               ChatLog.provider_eligible.is_(False)))).scalar_one()

    return {
        "documents": [
            {"title": e["title"], "parts": e["parts"],
             "date": e["date"].strftime("%d.%m.%Y"),
             "categories": sorted(e["categories"])}
            for e in grouped.values()],
        "doc_rows": len(docs),
        "wiki": [{"title": _safe_title(p.title), "slug": p.slug} for p in pages],
        "chat_contained": int(chat_contained),
    }


def quarantine_text(listing: dict) -> list[str]:
    """Telegram-ready messages (≤3500 chars each), titles and metadata only."""
    import html as _html
    docs = listing["documents"]
    pages = listing["wiki"]
    if not docs and not pages and not listing["chat_contained"]:
        return ["🔒 Карантин порожній — у базі немає ізольованих записів ✅"]
    lines = [f"🔒 <b>У карантині зараз</b> · файлів {len(docs)} "
             f"(рядків у базі {listing['doc_rows']}), сторінок вікі {len(pages)}\n"]
    if docs:
        lines.append("<b>Файли/аркуші (за ними йди міняти доступи):</b>")
        for e in docs:
            parts = f" · {e['parts']} ч." if e["parts"] > 1 else ""
            cats = f" — {', '.join(e['categories'])}" if e["categories"] else ""
            lines.append(f" • {_html.escape(e['title'])}{parts} ({e['date']}){cats}")
    if pages:
        lines.append("\n<b>Сторінки вікі (перезбереш через /wiki_build "
                     "після очищення файлів):</b>")
        lines += [f" • {_html.escape(p['title'])}" for p in pages]
    if listing["chat_contained"]:
        lines.append(f"\nРеплік чату ізольовано: {listing['chat_contained']} "
                     "(текст не показую навмисно).")
    lines.append("\nПлан: зміни доступи з цих файлів → прибери колонки паролів "
                 "у самих таблицях → /drive_all → /wiki_build. Нічого з цього "
                 "списку не видалено — лише ізольовано.")
    out: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 3500:
            out.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        out.append(current)
    return out


def report_text(report: ScanReport) -> str:
    """Counts only — never a title, an id or a fragment of what was found."""
    head = ("🔒 <b>Локальний скан бази знань</b> "
            f"(сканер v{security.SCANNER_VERSION}, без жодного виклику до "
            "OpenAI/Anthropic)\n\n")
    body = (
        f"Перевірено: документів {report.documents_scanned}, "
        f"сторінок вікі {report.wiki_scanned}, фактів пам'яті "
        f"{report.memory_scanned}, реплік чату {report.chat_scanned}, "
        f"подій {report.raw_events_scanned}, інших записів "
        f"{report.other_scanned}.\n\n"
    )
    released = ""
    if report.released:
        released = (
            f"\n<b>Повернуто з карантину</b> (паролі тепер дозволені): "
            f"документів {report.documents_released}, сторінок вікі "
            f"{report.wiki_released}, реплік чату {report.chat_released}.\n")
    if not report.affected:
        return head + body + released + (
            "\nТехнічних секретів у карантині немає — база чиста ✅\n"
            "Автокомпіляцію вікі можна вмикати.")
    found = (
        f"<b>Технічні секрети в карантині</b> (ключі/токени, у модель не йдуть):\n"
        f"• документів: {report.documents_in_quarantine}\n"
        f"• сторінок вікі: {report.wiki_in_quarantine}\n"
        f"• фактів пам'яті: {report.memory_in_quarantine}\n"
        f"• реплік чату: {report.chat_in_quarantine}\n"
        f"• подій позначено (не змінювались): {report.raw_events_flagged}\n"
        f"• інших записів (задачі/цілі/чернетки): {report.other_flagged}\n"
        f"• відкритих записів у журналі: {report.open_findings}\n"
    )
    cats = (f"Типи: {', '.join(report.categories)}\n" if report.categories else "")
    tail = ("\nКарантин — це ізоляція, не видалення. У карантині лишились "
            "тільки технічні секрети (API-ключі, токени, приватні ключі) — "
            "їм не місце в базі знань. Ключі з тих файлів варто перевипустити "
            "там, де їх видавали. Значення сюди не надсилай.")
    return head + body + released + found + cats + tail
