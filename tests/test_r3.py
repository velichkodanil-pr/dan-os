"""Round 3a tests: chunking, ingest dedupe, retrieval provenance, gaps, digest."""
from sqlalchemy import func, select

from app.core import rag
from app.core.ingest import chunk_text, ingest_document
from app.core.orchestrator import Orchestrator
from app.models import Document, KnowledgeChunk, KnowledgeGap

OWNER = 111

_EGYPT = "Комісія по напрямку Єгипет становить дванадцять відсотків для всіх агентів. "
_TURKEY = "Комісія по напрямку Туреччина становить десять відсотків для партнерів. "
_PAYMENT = "Оплата заявок здійснюється протягом трьох банківських днів після підтвердження. "
# long paragraphs so each becomes its own chunk
DOC_TEXT = "\n\n".join((_EGYPT * 9).strip() for _EGYPT in (_EGYPT, _TURKEY, _PAYMENT))


async def _count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


# 1. Chunking splits and keeps content
def test_chunking():
    chunks = chunk_text("а" * 50 + "\n\n" + "б" * 2000 + "\n\n" + "в" * 100, size=500, overlap=50)
    assert len(chunks) >= 4
    assert all(len(c) <= 500 for c in chunks)
    joined = "".join(chunks)
    assert "а" in joined and "б" in joined and "в" in joined


# 2. Ingest is deduplicated by content hash
async def test_ingest_dedupe(db):
    r1 = await ingest_document(db, user_id=OWNER, title="Умови.txt", text=DOC_TEXT,
                               source_type="telegram_file")
    r2 = await ingest_document(db, user_id=OWNER, title="Умови копія.txt", text=DOC_TEXT,
                               source_type="telegram_file")
    assert r1.status == "indexed" and r1.chunks > 0
    assert r2.status == "duplicate"
    assert await _count(db, Document) == 1
    assert await _count(db, KnowledgeChunk) == r1.chunks


# 3. Retrieval returns the right chunk with provenance (mock embedder: exact text match)
async def test_retrieval_provenance(db):
    await ingest_document(db, user_id=OWNER, title="Умови оператора.txt", text=DOC_TEXT,
                          source_type="telegram_file")
    query = "Комісія по напрямку Єгипет становить дванадцять відсотків для всіх агентів."
    found = await rag.retrieve(db, user_id=OWNER, query=query)
    assert found, "expected a matching chunk"
    assert "Єгипет" in found[0].text
    assert found[0].title == "Умови оператора.txt"
    block = rag.knowledge_block(found)
    assert "Умови оператора.txt" in block and "Джерело" in block


# 4. Unanswered question logs a knowledge gap (coverage map input)
async def test_gap_logged_for_unanswered_question(db):
    o = await Orchestrator().handle_note(
        db, user_id=OWNER, text="Скільки коштує трансфер у Хургаді?", dedupe_key="g1")
    assert o.kind == "chat"
    gaps = (await db.execute(select(KnowledgeGap))).scalars().all()
    assert len(gaps) == 1 and "трансфер" in gaps[0].question


# 5. Question with knowledge available does NOT log a gap
async def test_no_gap_when_knowledge_found(db):
    await ingest_document(db, user_id=OWNER, title="Умови.txt", text=DOC_TEXT,
                          source_type="telegram_file")
    await Orchestrator().handle_note(
        db, user_id=OWNER,
        text="Комісія по напрямку Єгипет становить дванадцять відсотків для всіх агентів?",
        dedupe_key="g2")
    assert await _count(db, KnowledgeGap) == 0


# 6. Question detector
def test_question_detector():
    assert rag.looks_like_question("Скільки коштує тур?")
    assert rag.looks_like_question("як оформити візу")
    assert not rag.looks_like_question("нагадай завтра о 10 подзвонити")
