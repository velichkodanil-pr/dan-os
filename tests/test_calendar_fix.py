"""Calendar honesty fixes: access errors are surfaced, never shown as 'empty';
short follow-ups inherit the calendar trigger from recent turns."""
from types import SimpleNamespace

import pytest

from app.config import settings
from app.core import briefs, google_client
from app.core.orchestrator import Orchestrator

OWNER = 111


def _fake_cred(email="acc@gmail.com"):
    return SimpleNamespace(account_email=email, label=email.split("@")[0])


@pytest.fixture
def google_on(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "test-client-id")


async def _patch_accounts(monkeypatch, creds, access="tok"):
    async def fake_accounts(_db, _uid):
        return creds

    async def fake_access(_db, _cred):
        return access
    monkeypatch.setattr(google_client, "get_accounts", fake_accounts)
    monkeypatch.setattr(google_client, "access_for", fake_access)


@pytest.mark.asyncio
async def test_agenda_block_reports_access_problem(db, monkeypatch, google_on):
    await _patch_accounts(monkeypatch, [_fake_cred()])

    async def denied(_access, _start, _end):
        raise google_client.CalendarAccessError("403")
    monkeypatch.setattr(google_client, "calendar_range", denied)

    block = await briefs.agenda_block(db, OWNER)
    assert "НЕДОСТУПНИЙ" in block and "acc@gmail.com" in block
    assert "connect_google" in block
    # the block must forbid the 'calendar is empty' claim
    assert "порожній" in block and "НЕ стверджуй" in block


@pytest.mark.asyncio
async def test_agenda_block_lists_events(db, monkeypatch, google_on):
    await _patch_accounts(monkeypatch, [_fake_cred()])

    async def events(_access, _start, _end):
        return [{"summary": "Зустріч з Юрою", "start": "2026-08-14T11:00:00+03:00",
                 "all_day": False}]
    monkeypatch.setattr(google_client, "calendar_range", events)

    block = await briefs.agenda_block(db, OWNER)
    assert "Зустріч з Юрою" in block and "11:00" in block
    assert "НЕДОСТУПНИЙ" not in block


@pytest.mark.asyncio
async def test_agenda_block_mixed_accounts(db, monkeypatch, google_on):
    """One account works, the other has no calendar grant — show both truths."""
    ok, broken = _fake_cred("ok@gmail.com"), _fake_cred("broken@gmail.com")
    await _patch_accounts(monkeypatch, [ok, broken])

    async def per_account(access, _start, _end, _seen={"n": 0}):
        _seen["n"] += 1
        if _seen["n"] == 1:
            return [{"summary": "Планерка", "start": "2026-08-14T09:00:00+03:00",
                     "all_day": False}]
        raise google_client.CalendarAccessError("403")
    monkeypatch.setattr(google_client, "calendar_range", per_account)

    block = await briefs.agenda_block(db, OWNER)
    assert "Планерка" in block
    assert "broken@gmail.com" in block and "НЕДОСТУПНИЙ" in block


@pytest.mark.asyncio
async def test_calendar_followup_inherits_trigger(db, monkeypatch, google_on):
    """«Що в календарі…» → agenda; short «а сьогодні?» follow-up → agenda again."""
    calls = {"n": 0}

    async def fake_agenda(_db, _uid, days=7):
        calls["n"] += 1
        return "\nКалендар користувача: подій немає.\n"
    from app.core import briefs as briefs_mod
    monkeypatch.setattr(briefs_mod, "agenda_block", fake_agenda)

    orch = Orchestrator()
    await orch.handle_note(db, user_id=OWNER, text="Що в календарі на завтра?",
                           dedupe_key="cal-1")
    assert calls["n"] == 1
    await orch.handle_note(db, user_id=OWNER, text="а сьогодні?", dedupe_key="cal-2")
    assert calls["n"] == 2  # short follow-up inherited the trigger
    await orch.handle_note(
        db, user_id=OWNER, dedupe_key="cal-3",
        text="Розкажи довгу історію про мандрівки Азією без жодної прив'язки")
    assert calls["n"] == 2  # unrelated long message: no agenda call


def test_scope_status_line():
    from app.telegram.bot import _scope_status
    line, missing = _scope_status(google_client.SCOPES)
    assert not missing and "❌" not in line
    line, missing = _scope_status(
        "openid email https://www.googleapis.com/auth/gmail.readonly")
    assert missing and "📆 календар ❌" in line and "✉️ пошта ✅" in line
