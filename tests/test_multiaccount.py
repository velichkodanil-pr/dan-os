"""Multi-account Google tests."""
from cryptography.fernet import Fernet

from sqlalchemy import func, select

from app.config import settings
from app.core import google_client
from app.models import GoogleCredential

OWNER = 111


async def _fake_email(monkeypatch, email: str):
    async def fake(_access):
        return email
    monkeypatch.setattr(google_client, "_account_email", fake)


def _tokens():
    return {"refresh_token": "r1", "access_token": "a1", "expires_in": 3600,
            "scope": google_client.SCOPES}


# 1. Two different accounts -> two rows; same account twice -> upsert (still one)
async def test_store_tokens_multi_upsert(db, monkeypatch):
    settings.cred_key = Fernet.generate_key().decode()
    await _fake_email(monkeypatch, "personal@gmail.com")
    e1 = await google_client.store_tokens(db, OWNER, _tokens(), domain="personal")
    await _fake_email(monkeypatch, "work@travelon.ua")
    e2 = await google_client.store_tokens(db, OWNER, _tokens(), domain="personal")
    await _fake_email(monkeypatch, "personal@gmail.com")
    await google_client.store_tokens(db, OWNER, _tokens(),
                                     domain="personal")  # re-consent same acc
    assert e1 == "personal@gmail.com" and e2 == "work@travelon.ua"
    count = (await db.execute(
        select(func.count()).select_from(GoogleCredential))).scalar_one()
    assert count == 2
    accounts = await google_client.get_accounts(db, OWNER, "personal")
    assert [a.account_email for a in accounts] == ["personal@gmail.com", "work@travelon.ua"]
    assert accounts[0].label == "personal"


# 2. Fresh access token is returned without refresh when not expired
async def test_access_for_uses_cached_token(db, monkeypatch):
    settings.cred_key = Fernet.generate_key().decode()
    await _fake_email(monkeypatch, "one@gmail.com")
    await google_client.store_tokens(db, OWNER, _tokens(), domain="personal")
    cred = (await google_client.get_accounts(db, OWNER, "personal"))[0]
    assert await google_client.access_for(db, cred) == "a1"


# 3. Calendar-question detector (deterministic trigger for agenda context)
def test_calendar_trigger():
    from app.core.orchestrator import _CALENDAR_RE
    for t in ("Що в календарі на завтра?", "які плани на тиждень",
              "що у мене завтра", "коли наступна зустріч", "розклад на пʼятницю"):
        assert _CALENDAR_RE.search(t), t
    for t in ("нагадай завтра о 10 подзвонити", "запам'ятай: Юра любить каву"):
        assert not _CALENDAR_RE.search(t), t
