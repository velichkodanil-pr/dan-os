"""Calendar RSVP slice (L3): extraction trigger, propose->confirm flow, policy."""
import uuid
from types import SimpleNamespace

import pytest

from app.core import google_client
from app.core.extraction import MockExtractionProvider
from app.core.orchestrator import Orchestrator
from app.core.policy import evaluate
from app.models import PendingCalAction

OWNER = 111


# ---------- policy ----------

def test_policy_calendar_respond():
    d = evaluate("calendar.respond")
    assert d.allowed and d.level == "L3" and d.confirmation_required
    assert not evaluate("calendar.write").allowed  # create/delete still denied
    assert evaluate("calendar.read").allowed


# ---------- extraction (mock, deterministic) ----------

@pytest.mark.asyncio
async def test_mock_extraction_decline():
    ext = await MockExtractionProvider().extract(
        "Скасуй мою участь на зустрічі з маркетингом завтра")
    assert ext.intent == "calendar" and ext.cal_action == "decline"
    assert "маркетинг" in ext.cal_query
    assert ext.cal_date is not None  # "завтра" narrowed the window


@pytest.mark.asyncio
async def test_mock_extraction_not_calendar():
    ext = await MockExtractionProvider().extract("нагадай завтра о 10 подзвонити Юрі")
    assert ext.intent == "task"
    ext2 = await MockExtractionProvider().extract("Що в календарі на завтра?")
    assert ext2.intent == "chat"


# ---------- matching ----------

def test_match_score():
    assert google_client.match_score("Зустріч з Маркетингом", "маркетинг") >= 0.5
    assert google_client.match_score("Зум з Проксімо", "маркетинг") < 0.5
    assert google_client.match_score("", "маркетинг") == 0.0
    assert google_client.match_score("Планерка", "") == 0.0


def test_respond_in_attendees():
    attendees = [{"email": "boss@x.com", "responseStatus": "accepted"},
                 {"email": "me@gmail.com", "self": True,
                  "responseStatus": "needsAction"}]
    out = google_client.respond_in_attendees(attendees, "declined")
    assert out[1]["responseStatus"] == "declined"
    assert out[0]["responseStatus"] == "accepted"
    # not an attendee -> None (organizer-only events can't be declined)
    assert google_client.respond_in_attendees(
        [{"email": "boss@x.com"}], "declined") is None
    # fallback by email when self flag is absent
    out2 = google_client.respond_in_attendees(
        [{"email": "Me@Gmail.com"}], "declined", email="me@gmail.com")
    assert out2[0]["responseStatus"] == "declined"


# ---------- orchestrator flow ----------

async def _patch_google(monkeypatch, matches, db=None):
    from app.models import GoogleCredential
    if db is not None:  # real row so confirm_cal_action can db.get() it
        cred = GoogleCredential(user_id=OWNER, account_email="me@gmail.com",
                                label="me", domain="personal",
                                refresh_token_enc="enc")
        db.add(cred)
        await db.commit()
    else:
        cred = SimpleNamespace(id=uuid.uuid4(), account_email="me@gmail.com",
                               label="me", domain="personal")

    async def fake_accounts(_db, _uid, _domain=None):
        return [cred]

    async def fake_access(_db, _c):
        return "tok"

    async def fake_find(_access, query, day=None):
        return matches
    monkeypatch.setattr(google_client, "get_accounts", fake_accounts)
    monkeypatch.setattr(google_client, "access_for", fake_access)
    monkeypatch.setattr(google_client, "calendar_find_events", fake_find)
    return cred


@pytest.mark.asyncio
async def test_cal_flow_propose_confirm_idempotent(db, monkeypatch):
    await _patch_google(monkeypatch, [{
        "calendar_id": "primary", "event_id": "ev1",
        "summary": "Зустріч з Маркетингом", "start": "2026-08-14T14:00:00+03:00",
        "all_day": False, "score": 1.0}], db=db)
    orch = Orchestrator()
    outcome = await orch.handle_note(
        db, user_id=OWNER, text="Скасуй мою участь на зустрічі з маркетингом",
        dedupe_key="cal-r1")
    assert outcome.kind == "cal_actions" and len(outcome.cal_actions) == 1
    pending = outcome.cal_actions[0]
    assert pending.action == "decline" and pending.status == "proposed"

    calls = {"n": 0}

    async def fake_respond(_access, cal_id, ev_id, action, email=""):
        calls["n"] += 1
        assert (cal_id, ev_id, action) == ("primary", "ev1", "decline")
        return "done"
    monkeypatch.setattr(google_client, "calendar_respond", fake_respond)

    assert await orch.confirm_cal_action(db, user_id=OWNER,
                                         action_id=pending.id) == "done"
    assert calls["n"] == 1
    # second press: no second write to Google
    assert await orch.confirm_cal_action(db, user_id=OWNER,
                                         action_id=pending.id) == "already"
    assert calls["n"] == 1
    row = await db.get(PendingCalAction, pending.id)
    assert row.status == "done"


@pytest.mark.asyncio
async def test_cal_flow_no_match_is_honest(db, monkeypatch):
    await _patch_google(monkeypatch, [])
    orch = Orchestrator()
    outcome = await orch.handle_note(
        db, user_id=OWNER, text="скасуй участь у нараді з бухгалтерією",
        dedupe_key="cal-r2")
    assert outcome.kind == "chat"
    assert "Не знайшов" in outcome.reply


@pytest.mark.asyncio
async def test_cal_flow_reject_keeps_calendar_untouched(db, monkeypatch):
    await _patch_google(monkeypatch, [{
        "calendar_id": "primary", "event_id": "ev2", "summary": "Планерка",
        "start": "2026-08-15T10:00:00+03:00", "all_day": False, "score": 1.0}], db=db)
    orch = Orchestrator()
    outcome = await orch.handle_note(
        db, user_id=OWNER, text="мене не буде на планерці", dedupe_key="cal-r3")
    pending = outcome.cal_actions[0]

    async def boom(*a, **k):
        raise AssertionError("must not call Google on reject")
    monkeypatch.setattr(google_client, "calendar_respond", boom)
    assert await orch.reject_cal_action(db, user_id=OWNER,
                                        action_id=pending.id) == "rejected"
    # confirming a rejected proposal does nothing
    assert await orch.confirm_cal_action(db, user_id=OWNER,
                                         action_id=pending.id) == "rejected"
