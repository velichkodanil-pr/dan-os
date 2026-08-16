"""Calendar event creation slice (L3): extraction, propose->confirm, policy."""
from datetime import datetime, timedelta, timezone

import pytest

from app.core import google_client
from app.core.extraction import MockExtractionProvider
from app.core.orchestrator import Orchestrator
from app.core.policy import evaluate
from app.models import GoogleCredential, PendingCalCreate

OWNER = 111


def test_policy_calendar_create():
    d = evaluate("calendar.create")
    assert d.allowed and d.level == "L3" and d.confirmation_required
    assert not evaluate("calendar.write").allowed  # edit/delete still denied


@pytest.mark.asyncio
async def test_mock_extraction_create():
    ext = await MockExtractionProvider().extract("Постав зустріч з Юрою завтра о 15")
    assert ext.intent == "calendar" and ext.cal_action == "create"
    assert "юрою" in ext.cal_title.lower()
    assert ext.cal_start is not None and ext.cal_start.hour == 15
    assert ext.cal_duration_min == 60


async def _setup_account(db, monkeypatch):
    cred = GoogleCredential(user_id=OWNER, account_email="me@gmail.com",
                            label="me", domain="personal", refresh_token_enc="enc")
    db.add(cred)
    await db.commit()

    async def fake_accounts(_db, _uid, _domain=None):
        return [cred]

    async def fake_access(_db, _c):
        return "tok"
    monkeypatch.setattr(google_client, "get_accounts", fake_accounts)
    monkeypatch.setattr(google_client, "access_for", fake_access)
    return cred


@pytest.mark.asyncio
async def test_cal_create_flow(db, monkeypatch):
    await _setup_account(db, monkeypatch)
    orch = Orchestrator()
    outcome = await orch.handle_note(
        db, user_id=OWNER, text="постав зустріч з Юрою завтра о 15",
        dedupe_key="cc-1")
    assert outcome.kind == "cal_create"
    pending = outcome.cal_create
    assert pending.status == "proposed"
    assert (pending.end_at - pending.start_at) == timedelta(minutes=60)
    assert outcome.cal_accounts == [(0, "me@gmail.com")]

    created = {}

    async def fake_create(_access, *, title, start, end, calendar_id="primary"):
        created.update(title=title, start=start, end=end)
        return "https://calendar.google.com/event?eid=x"
    monkeypatch.setattr(google_client, "calendar_create_event", fake_create)

    status, email = await orch.confirm_cal_create(
        db, user_id=OWNER, create_id=pending.id, account_index=0)
    assert status == "done" and email == "me@gmail.com"
    assert created["title"] == pending.title
    # idempotent second press
    status2, _ = await orch.confirm_cal_create(
        db, user_id=OWNER, create_id=pending.id, account_index=0)
    assert status2 == "already"
    row = await db.get(PendingCalCreate, pending.id)
    assert row.status == "done"


@pytest.mark.asyncio
async def test_cal_create_past_time_rejected(db, monkeypatch):
    await _setup_account(db, monkeypatch)
    orch = Orchestrator()
    outcome = await orch.handle_note(
        db, user_id=OWNER, text="постав зустріч з Юрою о 0", dedupe_key="cc-2")
    # 00:00 today is already in the past -> honest ask-back, nothing staged
    assert outcome.kind == "chat" and "минув" in outcome.reply


@pytest.mark.asyncio
async def test_cal_create_reject_never_calls_google(db, monkeypatch):
    await _setup_account(db, monkeypatch)
    orch = Orchestrator()
    outcome = await orch.handle_note(
        db, user_id=OWNER, text="створи подію планерка завтра о 9",
        dedupe_key="cc-3")
    pending = outcome.cal_create

    async def boom(*a, **k):
        raise AssertionError("must not create on reject")
    monkeypatch.setattr(google_client, "calendar_create_event", boom)
    assert await orch.reject_cal_create(db, user_id=OWNER,
                                        create_id=pending.id) == "rejected"
    status, _ = await orch.confirm_cal_create(
        db, user_id=OWNER, create_id=pending.id, account_index=0)
    assert status == "rejected"
