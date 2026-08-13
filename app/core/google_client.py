"""Google OAuth + read-only Calendar/Gmail client (typed, minimal scopes).

OAuth runs through the bot's own public domain (web flow): /connect_google in
Telegram gives a URL; Google redirects back to /google/oauth/callback.
State is HMAC-signed and short-lived; only the owner's user_id is accepted.
The refresh token is stored Fernet-encrypted. Read-only scopes only (L0).
"""
import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import GoogleCredential

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ("https://www.googleapis.com/auth/calendar.readonly "
          "https://www.googleapis.com/auth/gmail.readonly")


def _fernet() -> Fernet:
    return Fernet(settings.cred_key.encode())


# ---------- signed state ----------

def sign_state(user_id: int, ttl: int = 900) -> str:
    exp = int(time.time()) + ttl
    msg = f"{user_id}.{exp}"
    sig = hmac.new(settings.webhook_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{msg}.{sig}"


def verify_state(state: str) -> int | None:
    try:
        uid_s, exp_s, sig = state.split(".")
        msg = f"{uid_s}.{exp_s}"
        expected = hmac.new(settings.webhook_secret.encode(), msg.encode(),
                            hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected):
            return None
        if int(exp_s) < time.time():
            return None
        return int(uid_s)
    except (ValueError, AttributeError):
        return None


# ---------- oauth ----------

def redirect_uri() -> str:
    return f"{settings.public_url}/google/oauth/callback"


def auth_url(user_id: int) -> str:
    params = httpx.QueryParams({
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": sign_state(user_id),
    })
    return f"{AUTH_URL}?{params}"


async def exchange_code(code: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        })
    resp.raise_for_status()
    return resp.json()


async def store_tokens(db: AsyncSession, user_id: int, tokens: dict) -> None:
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise ValueError("Google did not return a refresh_token")
    cred = await db.get(GoogleCredential, user_id) or GoogleCredential(user_id=user_id)
    cred.refresh_token_enc = _fernet().encrypt(refresh.encode()).decode()
    cred.access_token = tokens.get("access_token", "")
    cred.access_expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(tokens.get("expires_in", 3600)) - 60)
    cred.scopes = tokens.get("scope", SCOPES)
    db.add(cred)
    await db.commit()


async def get_access_token(db: AsyncSession, user_id: int) -> str | None:
    cred = await db.get(GoogleCredential, user_id)
    if cred is None:
        return None
    now = datetime.now(timezone.utc)
    if cred.access_token and cred.access_expires_at and cred.access_expires_at > now:
        return cred.access_token
    refresh = _fernet().decrypt(cred.refresh_token_enc.encode()).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, data={
            "refresh_token": refresh,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "grant_type": "refresh_token",
        })
    if resp.status_code != 200:
        logger.error("Google token refresh failed: %s %s", resp.status_code, resp.text[:200])
        return None
    tokens = resp.json()
    cred.access_token = tokens["access_token"]
    cred.access_expires_at = now + timedelta(seconds=int(tokens.get("expires_in", 3600)) - 60)
    await db.commit()
    return cred.access_token


# ---------- read-only data ----------

async def calendar_today(access_token: str) -> list[dict]:
    tz = ZoneInfo(settings.tz_name)
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                    "singleEvents": "true", "orderBy": "startTime", "maxResults": 10},
        )
    if resp.status_code != 200:
        logger.error("calendar_today failed: %s", resp.status_code)
        return []
    events = []
    for item in resp.json().get("items", []):
        start_raw = item.get("start", {})
        events.append({
            "summary": item.get("summary", "(без назви)"),
            "start": start_raw.get("dateTime") or start_raw.get("date", ""),
            "all_day": "date" in start_raw,
        })
    return events


async def gmail_recent(access_token: str, hours: int = 16, limit: int = 5) -> list[dict]:
    query = f"in:inbox newer_than:{max(1, hours // 24 + 1)}d -category:promotions -category:social"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers, params={"q": query, "maxResults": limit},
        )
        if resp.status_code != 200:
            logger.error("gmail list failed: %s", resp.status_code)
            return []
        out = []
        for ref in resp.json().get("messages", [])[:limit]:
            m = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{ref['id']}",
                headers=headers,
                params={"format": "metadata", "metadataHeaders": ["From", "Subject"]},
            )
            if m.status_code != 200:
                continue
            data = m.json()
            hdrs = {h["name"]: h["value"] for h in data.get("payload", {}).get("headers", [])}
            sender = hdrs.get("From", "?")
            if "<" in sender:
                sender = sender.split("<")[0].strip().strip('"') or sender
            out.append({"from": sender[:40], "subject": hdrs.get("Subject", "(без теми)")[:70],
                        "snippet": data.get("snippet", "")[:100]})
    return out
