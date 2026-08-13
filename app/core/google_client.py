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
SCOPES = ("openid email "
          "https://www.googleapis.com/auth/calendar.readonly "
          "https://www.googleapis.com/auth/calendar.events "
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


def _decode_id_token_email(id_token: str) -> str:
    """Email from the id_token payload (token came straight from Google over TLS)."""
    import base64
    import json as jsonlib
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = jsonlib.loads(base64.urlsafe_b64decode(payload))
        return (data.get("email") or "").lower()
    except Exception:
        return ""


async def _account_email(tokens: dict) -> str:
    """Resolve the account email robustly: id_token -> userinfo -> gmail profile."""
    email = _decode_id_token_email(tokens.get("id_token", ""))
    if email:
        return email
    access = tokens.get("access_token", "")
    for url in ("https://www.googleapis.com/oauth2/v2/userinfo",
                "https://gmail.googleapis.com/gmail/v1/users/me/profile"):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {access}"})
            if resp.status_code == 200:
                d = resp.json()
                email = (d.get("email") or d.get("emailAddress") or "").lower()
                if email:
                    return email
            else:
                logger.warning("account email via %s -> %s", url, resp.status_code)
        except Exception:
            logger.exception("account email lookup failed via %s", url)
    return ""


async def store_tokens(db: AsyncSession, user_id: int, tokens: dict) -> str:
    """Adds/updates ONE Google account (multi-account). Returns the account email."""
    from sqlalchemy import select
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise ValueError("Google did not return a refresh_token")
    email = await _account_email(tokens)
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

class CalendarAccessError(Exception):
    """Calendar API refused (401/403): missing scope or revoked grant.
    Callers must surface this — an access problem is NOT an empty calendar."""


async def _calendar_list(client: httpx.AsyncClient, headers: dict) -> list[dict]:
    """Visible calendars of the account (primary + selected). Raises on 401/403."""
    resp = await client.get(
        "https://www.googleapis.com/calendar/v3/users/me/calendarList",
        headers=headers,
        params={"maxResults": 20, "fields": "items(id,summary,primary,selected)"})
    if resp.status_code in (401, 403):
        logger.error("calendarList denied: %s %s", resp.status_code, resp.text[:150])
        raise CalendarAccessError(str(resp.status_code))
    if resp.status_code != 200:
        logger.error("calendarList failed: %s %s", resp.status_code, resp.text[:150])
        return [{"id": "primary", "primary": True}]
    items = resp.json().get("items", [])
    cals = [c for c in items if c.get("primary") or c.get("selected")]
    return cals or [{"id": "primary", "primary": True}]


async def calendar_range(access_token: str, start: datetime, end: datetime) -> list[dict]:
    """Events across ALL visible calendars of the account (not just primary).

    Raises CalendarAccessError when the token has no calendar scope, so the
    caller can tell the user the truth instead of claiming "no events".
    """
    from urllib.parse import quote
    headers = {"Authorization": f"Bearer {access_token}"}
    events: list[dict] = []
    seen: set[tuple] = set()
    async with httpx.AsyncClient(timeout=30) as client:
        for cal in (await _calendar_list(client, headers))[:10]:
            resp = await client.get(
                "https://www.googleapis.com/calendar/v3/calendars/"
                f"{quote(cal.get('id', 'primary'), safe='')}/events",
                headers=headers,
                params={"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                        "singleEvents": "true", "orderBy": "startTime",
                        "maxResults": 25},
            )
            if resp.status_code in (401, 403) and cal.get("primary"):
                logger.error("calendar events denied: %s %s",
                             resp.status_code, resp.text[:150])
                raise CalendarAccessError(str(resp.status_code))
            if resp.status_code != 200:
                logger.warning("calendar %s failed: %s", cal.get("summary", "?"),
                               resp.status_code)
                continue
            for item in resp.json().get("items", []):
                start_raw = item.get("start", {})
                ev = {
                    "summary": item.get("summary", "(без назви)"),
                    "start": start_raw.get("dateTime") or start_raw.get("date", ""),
                    "all_day": "date" in start_raw,
                }
                key = (ev["summary"], ev["start"])
                if key not in seen:  # same event can live in several calendars
                    seen.add(key)
                    events.append(ev)
    events.sort(key=lambda e: e["start"])
    return events[:25]


async def calendar_today(access_token: str) -> list[dict]:
    tz = ZoneInfo(settings.tz_name)
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return await calendar_range(access_token, start, start + timedelta(days=1))


# ---------- calendar: respond to own participation (L3 flow) ----------

def match_score(summary: str, query: str) -> float:
    """Share of query content-words found in the event summary (0..1, pure)."""
    import re as _re
    words = [w for w in _re.findall(r"\w+", (query or "").lower()) if len(w) > 2]
    if not words:
        return 0.0
    s = (summary or "").lower()
    hit = sum(1 for w in words if w in s or any(
        w[:5] == sw[:5] for sw in _re.findall(r"\w+", s) if len(sw) > 2))
    return hit / len(words)


async def calendar_find_events(access_token: str, query: str,
                               day: datetime | None = None,
                               days: int = 14) -> list[dict]:
    """Upcoming events matching the query words (all visible calendars, ≤3)."""
    from urllib.parse import quote
    tz = ZoneInfo(settings.tz_name)
    if day is not None:
        start = day.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    else:
        start = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(days=days)
    headers = {"Authorization": f"Bearer {access_token}"}
    found: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for cal in (await _calendar_list(client, headers))[:10]:
            cal_id = cal.get("id", "primary")
            resp = await client.get(
                "https://www.googleapis.com/calendar/v3/calendars/"
                f"{quote(cal_id, safe='')}/events",
                headers=headers,
                params={"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                        "singleEvents": "true", "orderBy": "startTime",
                        "maxResults": 50},
            )
            if resp.status_code in (401, 403) and cal.get("primary"):
                raise CalendarAccessError(str(resp.status_code))
            if resp.status_code != 200:
                continue
            for item in resp.json().get("items", []):
                score = match_score(item.get("summary", ""), query)
                if score >= 0.5:
                    start_raw = item.get("start", {})
                    found.append({
                        "calendar_id": cal_id,
                        "event_id": item.get("id", ""),
                        "summary": item.get("summary", "(без назви)"),
                        "start": start_raw.get("dateTime") or start_raw.get("date", ""),
                        "all_day": "date" in start_raw,
                        "score": score,
                    })
    found.sort(key=lambda e: (-e["score"], e["start"]))
    seen: set[tuple] = set()
    unique = []
    for e in found:
        key = (e["summary"], e["start"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique[:3]


async def calendar_create_event(access_token: str, *, title: str,
                                start: datetime, end: datetime,
                                calendar_id: str = "primary") -> str:
    """Create a simple event (no invitees) on the account's calendar.
    Returns the event htmlLink ('' if absent). Raises CalendarAccessError on
    401/403 so the caller can point at the missing consent checkbox."""
    from urllib.parse import quote
    body = {
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": settings.tz_name},
        "end": {"dateTime": end.isoformat(), "timeZone": settings.tz_name},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://www.googleapis.com/calendar/v3/calendars/"
            f"{quote(calendar_id, safe='')}/events",
            headers={"Authorization": f"Bearer {access_token}"}, json=body)
    if resp.status_code in (401, 403):
        logger.error("calendar create denied: %s %s", resp.status_code,
                     resp.text[:150])
        raise CalendarAccessError(str(resp.status_code))
    if resp.status_code not in (200, 201):
        logger.error("calendar create failed: %s %s", resp.status_code,
                     resp.text[:150])
        raise RuntimeError(f"calendar create {resp.status_code}")
    return resp.json().get("htmlLink", "")


def respond_in_attendees(attendees: list[dict], response: str,
                         email: str = "") -> list[dict] | None:
    """New attendees list with MY responseStatus changed; None if I'm not there."""
    out, mine = [], False
    for a in attendees or []:
        a = dict(a)
        if a.get("self") or (email and a.get("email", "").lower() == email.lower()):
            a["responseStatus"] = response
            mine = True
        out.append(a)
    return out if mine else None


async def calendar_respond(access_token: str, calendar_id: str, event_id: str,
                           response: str, email: str = "") -> str:
    """Set own attendance (declined/accepted/tentative). Organizer is notified —
    same as pressing No/Yes in the Calendar UI. Returns done|not_attendee|error."""
    from urllib.parse import quote
    status_map = {"decline": "declined", "accept": "accepted",
                  "tentative": "tentative"}
    target = status_map.get(response, response)
    headers = {"Authorization": f"Bearer {access_token}"}
    base = ("https://www.googleapis.com/calendar/v3/calendars/"
            f"{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(base, headers=headers)
        if resp.status_code in (401, 403):
            raise CalendarAccessError(str(resp.status_code))
        if resp.status_code != 200:
            logger.error("calendar_respond get failed: %s", resp.status_code)
            return "error"
        event = resp.json()
        new_attendees = respond_in_attendees(event.get("attendees", []), target, email)
        if new_attendees is None:
            return "not_attendee"
        patch = await client.patch(
            base, headers=headers, params={"sendUpdates": "all"},
            json={"attendees": new_attendees})
        if patch.status_code in (401, 403):
            raise CalendarAccessError(str(patch.status_code))
        if patch.status_code != 200:
            logger.error("calendar_respond patch failed: %s %s",
                         patch.status_code, patch.text[:150])
            return "error"
    return "done"


# ---------- drive (read-only) ----------

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DRIVE_FILE_EXT = (".pdf", ".docx", ".txt", ".md", ".csv", ".tsv", ".xlsx")


def drive_indexable(f: dict) -> bool:
    """Is this Drive file worth indexing? Docs/Sheets natively; plus known
    text formats under 15MB. Everything else (media, binaries) is skipped."""
    mime = f.get("mimeType", "")
    if mime in (GOOGLE_DOC_MIME, GOOGLE_SHEET_MIME):
        return True
    if not f.get("name", "").lower().endswith(DRIVE_FILE_EXT):
        return False
    try:
        return int(f.get("size") or 0) <= 15 * 1024 * 1024
    except (TypeError, ValueError):
        return False


class DriveAccessError(Exception):
    """Drive API refused (401/403): API disabled in the Cloud project or the
    token lacks the drive scope. api_disabled tells the two apart."""

    def __init__(self, status: str, api_disabled: bool):
        self.api_disabled = api_disabled
        super().__init__(status)


async def drive_list_all(access_token: str, max_files: int = 300) -> list[dict]:
    """Whole-Drive listing (newest first), filtered to indexable files.
    Raises DriveAccessError on 401/403 — an access problem is NOT an empty
    Drive, and the caller must say so honestly."""
    files: list[dict] = []
    page_token: str | None = None
    async with httpx.AsyncClient(timeout=60) as client:
        while len(files) < max_files:
            params = {"q": "trashed=false",
                      "fields": "nextPageToken,files(id,name,mimeType,size)",
                      "pageSize": 100, "orderBy": "modifiedTime desc"}
            if page_token:
                params["pageToken"] = page_token
            resp = await client.get(
                "https://www.googleapis.com/drive/v3/files",
                headers={"Authorization": f"Bearer {access_token}"}, params=params)
            if resp.status_code in (401, 403):
                logger.error("drive_list_all denied: %s %s",
                             resp.status_code, resp.text[:200])
                raise DriveAccessError(
                    str(resp.status_code),
                    api_disabled="has not been used in project" in resp.text
                    or "it is disabled" in resp.text)
            if resp.status_code != 200:
                logger.error("drive_list_all failed: %s %s",
                             resp.status_code, resp.text[:120])
                break
            data = resp.json()
            for f in data.get("files", []):
                if drive_indexable(f):
                    files.append(f)
                    if len(files) >= max_files:
                        break
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return files


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
    """Returns (effective_filename, raw_bytes). Google Docs exported as txt;
    Google Sheets exported as XLSX — that carries ALL tabs with the existing
    drive.readonly scope (no Sheets API / extra consent needed). If the
    workbook is too big for export (~10MB cap), falls back to csv (first tab
    only — better than nothing)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=120) as client:
        mime = file.get("mimeType")
        if mime == GOOGLE_DOC_MIME:
            resp = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file['id']}/export",
                headers=headers, params={"mimeType": "text/plain"})
            resp.raise_for_status()
            return file["name"] + ".txt", resp.content
        if mime == GOOGLE_SHEET_MIME:
            resp = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file['id']}/export",
                headers=headers, params={"mimeType": XLSX_MIME})
            if resp.status_code == 200:
                return file["name"] + ".xlsx", resp.content
            logger.warning("sheet xlsx export failed (%s) for %s — csv fallback",
                           resp.status_code, file.get("name", "?"))
            resp = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file['id']}/export",
                headers=headers, params={"mimeType": "text/csv"})
            resp.raise_for_status()
            return file["name"] + ".csv", resp.content
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
