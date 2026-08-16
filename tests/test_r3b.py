"""Round 3b tests: conflicts, drafts idempotency, weekly ritual, coverage report."""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core import google_client
from app.core.orchestrator import Orchestrator
from app.core.policy import evaluate
from app.core.reports import weekly_coverage_report
from app.core.scheduler import ritual_due
from app.models import KnowledgeGap, MemoryItem, PendingDraft

OWNER = 111


def orch() -> Orchestrator:
    return Orchestrator()


async def _candidate(db, text: str, key: str) -> MemoryItem:
    o = await orch().handle_note(db, user_id=OWNER, text=f"запам'ятай: {text}",
                                 dedupe_key=key)
    assert o.memory_saved
    return (await db.execute(select(MemoryItem).order_by(
        MemoryItem.created_at.desc()))).scalars().first()


# 1. Policy: drafts allowed as L3, sending still denied
def test_policy_draft_vs_send():
    d = evaluate("email.draft")
    assert d.allowed and d.level == "L3" and d.confirmation_required
    assert evaluate("email.send").allowed is False
    assert evaluate("memory.supersede").allowed and evaluate("drive.read").level == "L0"


# 2. Conflict flow: new supersedes old with history
async def test_conflict_supersede(db):
    a = await _candidate(db, "Комісія оператора по Єгипту становить десять відсотків", "c1")
    assert await orch().confirm_memory(db, user_id=OWNER, item_id=a.id) == "confirmed"
    b = await _candidate(db, "Комісія оператора по Єгипту становить дванадцять відсотків", "c2")
    status = await orch().confirm_memory(db, user_id=OWNER, item_id=b.id)
    assert isinstance(status, tuple) and status[0] == "conflict"
    assert status[1].id == a.id
    assert await orch().resolve_conflict(db, user_id=OWNER, new_id=b.id, choice="n") == "resolved"
    await db.refresh(a); await db.refresh(b)
    assert b.status == "confirmed"
    assert a.status == "superseded" and a.superseded_by == b.id
    # idempotent: resolving again does not flip anything
    assert await orch().resolve_conflict(db, user_id=OWNER, new_id=b.id, choice="o") in (
        "confirmed", "not_found")


# 3. Conflict flow: keep old rejects new
async def test_conflict_keep_old(db):
    a = await _candidate(db, "Офіс оператора працює до сімнадцятої години щодня", "k1")
    assert await orch().confirm_memory(db, user_id=OWNER, item_id=a.id) == "confirmed"
    b = await _candidate(db, "Офіс оператора працює до дев'ятнадцятої години щодня", "k2")
    status = await orch().confirm_memory(db, user_id=OWNER, item_id=b.id)
    assert isinstance(status, tuple)
    await orch().resolve_conflict(db, user_id=OWNER, new_id=b.id, choice="o")
    await db.refresh(a); await db.refresh(b)
    assert a.status == "confirmed" and b.status == "rejected"


# 4. Unrelated facts do not conflict
async def test_no_false_conflict(db):
    a = await _candidate(db, "Юра любить каву без цукру зранку", "n1")
    assert await orch().confirm_memory(db, user_id=OWNER, item_id=a.id) == "confirmed"
    b = await _candidate(db, "Пароль від офісного wifi лежить у сейфі рецепції", "n2")
    assert await orch().confirm_memory(db, user_id=OWNER, item_id=b.id) == "confirmed"


# 5. Draft approval is idempotent; gmail called once
async def test_draft_approve_idempotent(db, monkeypatch):
    calls = []

    async def fake_create(access, **kw):
        calls.append(kw)
        return "draft123"

    async def fake_access(_db, _uid, _domain):
        return "tok"

    monkeypatch.setattr(google_client, "gmail_create_draft", fake_create)
    monkeypatch.setattr(google_client, "get_access_token", fake_access)
    draft = PendingDraft(user_id=OWNER, to_addr="x@y.z", subject="Re: тест", body="Привіт")
    db.add(draft)
    await db.commit()
    assert await orch().approve_draft(db, user_id=OWNER, draft_id=draft.id) == "created"
    assert await orch().approve_draft(db, user_id=OWNER, draft_id=draft.id) == "already"
    assert len(calls) == 1


# 6. Weekly ritual only on its weekday and once per day
def test_weekly_ritual_weekday():
    tz = ZoneInfo("Europe/Kyiv")
    sunday_after = datetime(2026, 8, 16, 19, 5, tzinfo=tz)   # Sunday
    assert sunday_after.weekday() == 6
    assert ritual_due(None, sunday_after, "19:00") is True
    assert ritual_due("2026-08-16", sunday_after, "19:00") is False


# 7. Coverage report lists gaps and marks them resolved
async def test_weekly_coverage_report(db):
    db.add(KnowledgeGap(user_id=OWNER, question="Скільки коштує трансфер у Хургаді?"))
    db.add(KnowledgeGap(user_id=OWNER, question="Який розклад рейсів на Мадейру?"))
    await db.commit()
    # R6.1B: weekly_coverage_report now returns ONE domain's labelled section
    # (the global "Тижневий звіт" header moved to send_weekly, which stitches
    # the per-domain sections together). The section still lists the gaps.
    text = await weekly_coverage_report(db, OWNER, "personal")
    assert text is not None and "Особисте" in text and "трансфер" in text
    gaps = (await db.execute(select(KnowledgeGap))).scalars().all()
    assert all(g.resolved for g in gaps)
