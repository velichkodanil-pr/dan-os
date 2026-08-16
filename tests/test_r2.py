"""Round 2 tests: memory review, OAuth state signing, rituals, brief builder."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core import briefs, google_client
from app.core.domains import Domain
from app.core.orchestrator import NotOwner, Orchestrator
from app.core.scheduler import ritual_due
from app.models import ChatLog, MemoryItem

OWNER = 111
STRANGER = 999


def orch() -> Orchestrator:
    return Orchestrator()


async def _make_candidate(db) -> MemoryItem:
    o = await orch().handle_note(db, user_id=OWNER,
                                 text="запам'ятай: офіс працює до 19:00",
                                 dedupe_key="r2m1")
    assert o.memory_saved
    return (await db.execute(select(MemoryItem))).scalar_one()


# 1. Confirm is idempotent; reject after confirm does not flip the status
async def test_memory_confirm_reject_idempotent(db):
    item = await _make_candidate(db)
    assert await orch().confirm_memory(db, user_id=OWNER, item_id=item.id) == "confirmed"
    assert await orch().confirm_memory(db, user_id=OWNER, item_id=item.id) == "confirmed"
    assert await orch().reject_memory(db, user_id=OWNER, item_id=item.id) == "confirmed"
    await db.refresh(item)
    assert item.status == "confirmed"


# 2. Stranger cannot touch memory review
async def test_memory_review_owner_only(db):
    item = await _make_candidate(db)
    with pytest.raises(NotOwner):
        await orch().confirm_memory(db, user_id=STRANGER, item_id=item.id)


# 3. OAuth state: valid roundtrip carries user_id AND domain; tamper/expiry rejected
def test_oauth_state_sign_verify():
    state = google_client.sign_state(OWNER, "travelon")
    assert google_client.verify_state(state) == (OWNER, Domain.TRAVELON)
    assert google_client.verify_state(state[:-4] + "beef") is None
    expired = google_client.sign_state(OWNER, "travelon", ttl=-10)
    assert google_client.verify_state(expired) is None
    assert google_client.verify_state("garbage") is None


# 4. Daily ritual fires once per day, only after its time
def test_ritual_due_once_per_day():
    tz = ZoneInfo("Europe/Kyiv")
    morning = datetime(2026, 8, 13, 7, 29, tzinfo=tz)
    after = datetime(2026, 8, 13, 7, 31, tzinfo=tz)
    assert ritual_due(None, morning, "07:30") is False
    assert ritual_due(None, after, "07:30") is True
    assert ritual_due("2026-08-13", after, "07:30") is False       # already ran today
    assert ritual_due("2026-08-12", after, "07:30") is True        # ran yesterday
    assert ritual_due(None, after, "broken") is False


# 5. Brief without Google still builds and shows this domain's tasks
async def test_brief_without_google(db):
    from datetime import datetime, timezone

    from app.core.domains import label
    from app.models import Task

    # a task due today lands in the personal-domain morning-brief section
    db.add(Task(user_id=OWNER, domain="personal", title="Подзвонити в банк",
                due_at=datetime.now(timezone.utc)))
    await db.commit()
    today = await orch().today(db, user_id=OWNER, domain="personal")
    text = await briefs.morning_brief(db, OWNER, "personal", today)
    assert text is not None                    # brief builds without Google connected
    assert label("personal") in text           # per-domain section header
    assert "Подзвонити в банк" in text          # and it shows the task


# 6. Chat replies are logged into the short conversation window
async def test_chat_history_logged(db):
    o = await orch().handle_note(db, user_id=OWNER, text="як справи?",
                                 dedupe_key="r2c1")
    assert o.kind == "chat"
    rows = (await db.execute(select(ChatLog).order_by(ChatLog.id))).scalars().all()
    assert [r.role for r in rows] == ["user", "bot"]
