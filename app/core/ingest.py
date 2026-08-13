"""Knowledge ingestion: extract text -> chunk -> embed -> store with provenance.

Content is DATA: nothing in an ingested file can trigger actions (policy stays
in code). Dedupe by content hash — re-ingesting the same content is a no-op.
"""
import hashlib
import io
import logging
import re
import zipfile
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.embeddings import get_embedder
from app.models import Document, KnowledgeChunk

logger = logging.getLogger(__name__)

MAX_DOC_BYTES = 15 * 1024 * 1024
MAX_CHARS = 400_000
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120

ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md", ".vtt", ".srt"}


class IngestError(Exception):
    pass


_TS_RE = re.compile(r"-->")
_CUE_NUM_RE = re.compile(r"^\d+$")
_SPEAKER_RE = re.compile(r"^([^:\n]{1,60}):\s*(.+)$", re.DOTALL)


def parse_subtitles(data: bytes) -> str:
    """WEBVTT/SRT (Zoom meeting transcripts) -> clean 'Speaker: text' lines.

    Drops headers, cue numbers and timestamps; merges consecutive cues of the
    same speaker so the transcript reads as a dialogue, not subtitle spam."""
    raw = data.decode("utf-8", "ignore").replace("\r\n", "\n")
    lines: list[tuple[str, str]] = []  # (speaker, text)
    for line in raw.split("\n"):
        line = line.strip().lstrip("﻿")
        if (not line or line.upper().startswith(("WEBVTT", "NOTE", "STYLE"))
                or _TS_RE.search(line) or _CUE_NUM_RE.match(line)):
            continue
        m = _SPEAKER_RE.match(line)
        speaker, text = (m.group(1).strip(), m.group(2).strip()) if m else ("", line)
        if lines and lines[-1][0] == speaker:
            prev_speaker, prev_text = lines[-1]
            if text not in prev_text[-len(text) * 2:]:  # skip verbatim repeats
                lines[-1] = (prev_speaker, f"{prev_text} {text}")
        else:
            lines.append((speaker, text))
    out = [f"{s}: {t}" if s else t for s, t in lines]
    return "\n".join(out)


def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if len(data) > MAX_DOC_BYTES:
        raise IngestError("Файл завеликий (ліміт 15 МБ)")
    if name.endswith((".vtt", ".srt")):
        text = parse_subtitles(data)
        text = re.sub(r"[ \t]+", " ", text).strip()
        if len(text) < 20:
            raise IngestError("У транскрипті не знайшлося тексту")
        return text[:MAX_CHARS]
    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:200])
        except Exception as e:
            raise IngestError("Не зміг прочитати PDF") from e
    elif name.endswith(".docx"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            xml = re.sub(r"</w:p>", "\n", xml)
            text = re.sub(r"<[^>]+>", "", xml)
        except Exception as e:
            raise IngestError("Не зміг прочитати DOCX") from e
    elif name.endswith((".txt", ".md")):
        text = data.decode("utf-8", "ignore")
    else:
        raise IngestError("Підтримую pdf, docx, txt, md")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 20:
        raise IngestError("У файлі не знайшлося тексту")
    return text[:MAX_CHARS]


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        while len(p) > size:  # oversized paragraph — hard split
            if current:
                chunks.append(current)
                current = ""
            chunks.append(p[:size])
            p = p[size - overlap:]
        if len(current) + len(p) + 1 <= size:
            current = f"{current}\n{p}".strip()
        else:
            chunks.append(current)
            current = (current[-overlap:] + "\n" + p).strip() if overlap else p
    if current:
        chunks.append(current)
    return [c for c in chunks if len(c) > 30]


@dataclass
class IngestResult:
    status: str  # indexed | duplicate | error
    document: Document | None = None
    chunks: int = 0
    error: str | None = None


async def ingest_document(
    db: AsyncSession, *, user_id: int, title: str, text: str,
    source_type: str, source_ref: str = "", domain: str = "personal",
) -> IngestResult:
    content_hash = hashlib.sha256(text.strip().lower().encode()).hexdigest()
    existing = (await db.execute(
        select(Document).where(Document.content_hash == content_hash))).scalar_one_or_none()
    if existing:
        await audit(db, actor=f"user:{user_id}", action="ingest", resource_type="document",
                    resource_id=existing.id, outcome="dedupe", title=title)
        await db.commit()
        return IngestResult(status="duplicate", document=existing, chunks=existing.chunk_count)

    chunks = chunk_text(text)
    if not chunks:
        return IngestResult(status="error", error="Не вийшло розбити текст")
    embeddings = await get_embedder().embed(chunks)

    doc = Document(user_id=user_id, domain=domain, title=title[:200],
                   source_type=source_type, source_ref=source_ref[:500],
                   content_hash=content_hash, chunk_count=len(chunks))
    db.add(doc)
    try:
        await db.flush()
    except IntegrityError:  # race on hash
        await db.rollback()
        return IngestResult(status="duplicate")
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        db.add(KnowledgeChunk(document_id=doc.id, user_id=user_id, seq=i,
                              text=chunk, embedding=emb))
    await audit(db, actor=f"user:{user_id}", action="ingest", resource_type="document",
                resource_id=doc.id, policy_level="L1", title=title, chunks=len(chunks))
    await db.commit()
    return IngestResult(status="indexed", document=doc, chunks=len(chunks))
