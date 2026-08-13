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
          "https://www.googleapis.com/auth/gmail.readonly "
          "https://www.googleapis.com/auth/gmail.compose "
          "https://www.googleapis.com/auth/drive.readonly")


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


async def _account_email(access_token: str) -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers={"Authorization": f"Bearer {access_token}"})
    resp.raise_for_status()
    return resp.json().get("emailAddress", "").lower()


async def store_tokens(db: AsyncSession, user_id: int, tokens: dict) -> str:
    """Adds/updates ONE Google account (multi-account). Returns the account email."""
    from sqlalchemy import select
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise ValueError("Google did not return a refresh_token")
    email = await _account_email(tokens.get("access_token", ""))
    if not email:
        raise ValueError("Could not resolve account email")
    cred = (await db.execute(select(GoogleCredential).where(
        GoogleCredential.user_id == user_id,
        GoogleCredential.account_email == email))).scalar_one_or_none()
    if cred is None:
        cred = GoogleCredential(user_id=user_id, account_email=email,
                                label=email.split("@")[0][:32])
    cred.refresh_token_enc = _fernet().encrypt(refresh.encode()).decode()
    cred.access_token = tokens.get("access_token", "")
    cred.access_expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(tokens.get("expires_in", 3600)) - 60)
    cred.scopes = tokens.get("scope", SCOPES)
    db.add(cred)
    await db.commit()
    return email


async def get_accounts(db: AsyncSession, user_id: int) -> list[GoogleCredential]:
    from sqlalchemy import select
    return list((await db.execute(
        select(GoogleCredential).where(GoogleCredential.user_id == user_id)
        .order_by(GoogleCredential.created_at))).scalars().all())


async def access_for(db: AsyncSession, cred: GoogleCredential) -> str | None:
    """Fresh access token for one account (refreshes if needed)."""
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
        logger.error("Google token refresh failed (%s): %s %s",
                     cred.account_email, resp.status_code, resp.text[:200])
        return None
    tokens = resp.json()
    cred.access_token = tokens["access_token"]
    cred.access_expires_at = now + timedelta(seconds=int(tokens.get("expires_in", 3600)) - 60)
    await db.commit()
    return cred.access_token


async def get_access_token(db: AsyncSession, user_id: int) -> str | None:
    """Back-compat: token of the FIRST connected account."""
    accounts = await get_accounts(db, user_id)
    return await access_for(db, accounts[0]) if accounts else None


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


# ---------- drive (read-only) ----------

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
DRIVE_FILE_EXT = (".pdf", ".docx", ".txt", ".md")


async def drive_list_folders(access_token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://www.googleapis.com/drive/v3/files",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"q": "mimeType='application/vnd.google-apps.folder' and trashed=false",
                    "fields": "files(id,name)", "pageSize": 20,
                    "orderBy": "modifiedTime desc"},
        )
    if resp.status_code != 200:
        logger.error("drive_list_folders failed: %s %s", resp.status_code, resp.text[:150])
        resp.raise_for_status()
    return resp.json().get("files", [])


async def drive_list_files(access_token: str, folder_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://www.googleapis.com/drive/v3/files",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"q": f"'{folder_id}' in parents and trashed=false",
                    "fields": "files(id,name,mimeType,size)", "pageSize": 50},
        )
    resp.raise_for_status()
    out = []
    for f in resp.json().get("files", []):
        name, mime = f.get("name", ""), f.get("mimeType", "")
        if mime == GOOGLE_DOC_MIME or name.lower().endswith(DRIVE_FILE_EXT):
            out.append(f)
    return out


async def drive_download_text_source(access_token: str, file: dict) -> tuple[str, bytes]:
    """Returns (effective_filename, raw_bytes); Google Docs are exported as txt."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=120) as client:
        if file.get("mimeType") == GOOGLE_DOC_MIME:
            resp = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file['id']}/export",
                headers=headers, params={"mimeType": "text/plain"})
            resp.raise_for_status()
            return file["name"] + ".txt", resp.content
        resp = await client.get(
            f"https://www.googleapis.com/drive/v3/files/{file['id']}",
            headers=headers, params={"alt": "media"})
        resp.raise_for_status()
        return file["name"], resp.content


# ---------- gmail: full message + draft creation ----------

def _walk_text(payload: dict) -> str:
    import base64 as b64
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return b64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", "ignore")
    for part in payload.get("parts", []) or []:
        text = _walk_text(part)
        if text:
            return text
    return ""


async def gmail_find_message(access_token: str, query: str) -> dict | None:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers, params={"q": f"in:inbox {query}", "maxResults": 1})
        if resp.status_code != 200 or not resp.json().get("messages"):
            return None
        mid = resp.json()["messages"][0]["id"]
        m = await client.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}",
            headers=headers, params={"format": "full"})
        if m.status_code != 200:
            return None
    data = m.json()
    hdrs = {h["name"].lower(): h["value"]
            for h in data.get("payload", {}).get("headers", [])}
    return {
        "id": mid, "thread_id": data.get("threadId", ""),
        "from": hdrs.get("from", ""), "subject": hdrs.get("subject", ""),
        "message_id": hdrs.get("message-id", ""),
        "references": hdrs.get("references", ""),
        "body": _walk_text(data.get("payload", {}))[:6000] or data.get("snippet", ""),
    }


async def gmail_create_draft(access_token: str, *, to_addr: str, subject: str,
                             body: str, thread_id: str = "", in_reply_to: str = "",
                             references: str = "") -> str:
    import base64 as b64
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["To"] = to_addr
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip()
    msg.set_content(body)
    raw = b64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload: dict = {"message": {"raw": raw}}
    if thread_id:
        payload["message"]["threadId"] = thread_id
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
            headers={"Authorization": f"Bearer {access_token}"}, json=payload)
    resp.raise_for_status()
    return resp.json().get("id", "")


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
