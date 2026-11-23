"""R7.1 — rituals survive hostile text and stop hammering when they fail.

2026-11-22: a mail sender «Joyce Tours <receivable@joyce-tours.net>» went into
the morning brief unescaped; Telegram rejected the HTML, and the scheduler
rebuilt and resent the brief every tick for six hours. These tests pin the
three fixes: escaping at the source, a plain-text fallback at the adapter,
and backoff + a daily cap + one alert in the scheduler.
"""
import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from sqlalchemy import select

from app.config import settings
from app.core import briefs, google_client, scheduler
from app.core.domains import Domain
from app.core.orchestrator import Orchestrator
from app.models import AppState, AuditRecord, Task
from app.telegram.safe_send import plain_from_html, send_html

OWNER = 111
KYIV = ZoneInfo("Europe/Kyiv")
SENDER = "Joyce Tours <receivable@joyce-tours.net>"
# Telegram HTML accepts only its own tags; anything else in <> is fatal.
_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)")
_ALLOWED = {"b", "i", "u", "s", "code", "pre", "a", "blockquote"}


def _only_telegram_tags(html: str) -> bool:
    return all(m.group(2).lower() in _ALLOWED for m in _TAG.finditer(html))


@pytest.fixture
def hostile_google(monkeypatch):
    """One connected account whose calendar and inbox are full of markup."""
    cred = SimpleNamespace(label="work<1>", account_email="me@example.com")
    monkeypatch.setattr(settings, "google_client_id", "test-client")

    async def _accounts(db, user_id, domain):
        return [cred, SimpleNamespace(label="home", account_email="h@example.com")]

    async def _access(db, c):
        return "token"

    async def _calendar(access):
        return [{"start": "", "all_day": True, "summary": "Зустріч з <Юрою> & Co"}]

    async def _mail(access, hours=16, limit=5):
        return [{"from": SENDER, "subject": "Invoice <44> & reminder",
                 "snippet": "please pay"}]

    monkeypatch.setattr(google_client, "get_accounts", _accounts)
    monkeypatch.setattr(google_client, "access_for", _access)
    monkeypatch.setattr(google_client, "calendar_today", _calendar)
    monkeypatch.setattr(google_client, "gmail_recent", _mail)


# 1. The exact 22.11 sender no longer breaks the brief
async def test_brief_escapes_mail_calendar_and_labels(db, hostile_google):
    td = await Orchestrator().today(db, user_id=OWNER, domain=Domain.PERSONAL)
    text = await briefs.morning_brief(db, OWNER, Domain.PERSONAL, td)
    assert "&lt;receivable@joyce-tours.net&gt;" in text
    assert "Invoice &lt;44&gt; &amp; reminder" in text
    assert "Зустріч з &lt;Юрою&gt; &amp; Co" in text
    assert "·work&lt;1&gt;" in text                      # account label too
    assert "<receivable" not in text
    assert _only_telegram_tags(text)


# 2. Task titles are his own text — escaped in the brief and the evening summary
async def test_task_titles_escaped_in_brief_and_evening(db):
    from datetime import timezone
    db.add(Task(user_id=OWNER, domain="personal", title="Відповісти <Юрі> & Ко",
                due_at=datetime.now(timezone.utc)))
    await db.commit()
    td = await Orchestrator().today(db, user_id=OWNER, domain=Domain.PERSONAL)
    brief = await briefs.morning_brief(db, OWNER, Domain.PERSONAL, td)
    evening = await briefs.evening_summary(db, OWNER, Domain.PERSONAL)
    for text in (brief, evening):
        assert "Відповісти &lt;Юрі&gt; &amp; Ко" in text
        assert _only_telegram_tags(text)


# 3. Digest: both the model ranking and the plain fallback are escaped
@pytest.mark.parametrize("ranked", [None, f"⚡ {SENDER} — рахунок"])
async def test_digest_escapes_ranked_and_fallback(db, hostile_google, monkeypatch,
                                                  ranked):
    from app.core import digest

    async def _rank(emails):
        return ranked

    monkeypatch.setattr(digest, "_rank_with_haiku", _rank)
    text = await digest.build_digest(db, OWNER, Domain.PERSONAL)
    assert "&lt;receivable@joyce-tours.net&gt;" in text
    assert _only_telegram_tags(text)


# 4. Supplier names in TravelON texts are escaped
async def test_travelon_debt_alert_escapes_supplier_names(monkeypatch):
    from app.core import travelon
    order = SimpleNamespace(order_no="64772", country=None, hotel="Barut <B&B>",
                            debt=100.0, currency="EUR", status="Confirmed",
                            debt_local=None, local_currency="")
    monkeypatch.setattr(travelon, "configured", lambda: True)

    async def _days(days, **kw):
        return [order]

    monkeypatch.setattr(travelon, "fetch_days", _days)
    monkeypatch.setattr(travelon, "_active", lambda orders: orders)
    monkeypatch.setattr(travelon, "has_debt", lambda o: True)
    text = await travelon.debt_alert_text()
    assert "Barut &lt;B&amp;B&gt;" in text
    assert _only_telegram_tags(text)


# 5. Plain-text fallback keeps content and drops only Telegram's own tags
def test_plain_from_html_keeps_addresses():
    html = f"☀️ <b>Бриф</b>\n • {SENDER}: <i>Тема</i> &amp; ще"
    plain = plain_from_html(html)
    assert plain == f"☀️ Бриф\n • {SENDER}: Тема & ще"


class _FakeBot:
    """Rejects the first send as bad markup (like Telegram on 22.11)."""

    def __init__(self, error: str = "Bad Request: can't parse entities: "
                                    "Unsupported start tag \"x\" at byte offset 4"):
        self.calls: list[dict] = []
        self.error = error

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append({"text": text, **kwargs})
        if len(self.calls) == 1 and self.error:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=chat_id, text=text), message=self.error)
        return "sent"


# 6. A markup rejection is resent once as plain text — the brief arrives
async def test_send_html_falls_back_to_plain_text():
    bot = _FakeBot()
    result = await send_html(bot, OWNER, f"<b>Бриф</b> {SENDER}", reply_markup="kb")
    assert result == "sent"
    assert len(bot.calls) == 2
    assert bot.calls[1]["parse_mode"] is None
    assert bot.calls[1]["text"] == f"Бриф {SENDER}"
    assert bot.calls[1]["reply_markup"] == "kb"          # buttons survive


# 7. Other Telegram errors are NOT swallowed (callers keep their retry logic)
async def test_send_html_reraises_other_errors():
    bot = _FakeBot(error="Bad Request: chat not found")
    with pytest.raises(TelegramBadRequest):
        await send_html(bot, OWNER, "<b>x</b>")
    assert len(bot.calls) == 1


# 8. Backoff state machine: pauses, cap, give-up, next-day reset
def test_retry_state_machine():
    day = datetime(2026, 11, 22, 7, 30, tzinfo=KYIV)
    st = scheduler.retry_state(None, "2026-11-22")
    assert scheduler.may_attempt(st, day)
    expected = []
    now = day
    for i, pause in enumerate(scheduler.RETRY_DELAYS_MIN, start=1):
        st = scheduler.after_failure(st, now)
        assert st["count"] == i and not st["gave_up"]
        assert not scheduler.may_attempt(st, now + timedelta(minutes=pause - 1))
        now = now + timedelta(minutes=pause)
        assert scheduler.may_attempt(st, now)
        expected.append(pause)
    st = scheduler.after_failure(st, now)
    assert st["gave_up"] and st["count"] == scheduler.MAX_ATTEMPTS == 5
    assert not scheduler.may_attempt(st, now + timedelta(hours=10))
    # a new day is a clean slate; corrupt state is a clean slate too
    assert scheduler.retry_state(json.dumps(st), "2026-11-23")["count"] == 0
    assert scheduler.retry_state("{not json", "2026-11-22")["count"] == 0
    assert expected == [5, 15, 60, 60]


async def _tick(fn, now, alerts):
    async def _alert(user_id, html):
        alerts.append((user_id, html))

    noop = _noop_ritual
    await scheduler._run_rituals(fn, noop, noop, noop, alert=_alert, now_local=now)


async def _noop_ritual(owner):
    return None


async def _row(key):
    from app import db as database
    async with database.session() as s:
        row = await s.get(AppState, key)
        return row.value if row else None


# 9. A failing brief: 5 attempts with pauses, one alert, then silence for the day
async def test_failing_ritual_backs_off_and_alerts_once(db, monkeypatch):
    monkeypatch.setattr(settings, "digest_times", "")
    monkeypatch.setattr(settings, "checkin_time", "23:59")
    monkeypatch.setattr(settings, "weekly_time", "23:59")
    calls, alerts = [], []

    async def _broken_brief(owner):
        calls.append(owner)
        raise RuntimeError("can't parse entities")

    now = datetime(2026, 11, 22, 7, 30, 10, tzinfo=KYIV)
    end = now + timedelta(hours=6)
    while now < end:                      # six hours of 30-second ticks
        await _tick(_broken_brief, now, alerts)
        now += timedelta(seconds=30)

    assert len(calls) == scheduler.MAX_ATTEMPTS        # was ~700 before R7.1
    assert len(alerts) == 1
    assert alerts[0][0] == OWNER
    assert "Ранковий бриф" in alerts[0][1] and "/brief" in alerts[0][1]
    assert "parse" not in alerts[0][1]                 # never the error text
    assert await _row("last_brief") is None            # not claimed as sent
    audits = (await db.execute(select(AuditRecord).where(
        AuditRecord.action == "ritual.failed"))).scalars().all()
    outcomes = sorted(a.outcome for a in audits)
    assert outcomes == ["gave_up"] + ["retry"] * 4

    # next morning it runs again from a clean slate
    async def _ok(owner):
        calls.append("ok")

    await _tick(_ok, datetime(2026, 11, 23, 7, 31, tzinfo=KYIV), alerts)
    assert calls[-1] == "ok"
    assert await _row("last_brief") == "2026-11-23"
    assert await _row("ritual_fail_brief") is None     # recovered → cleared
    assert len(alerts) == 1


# 10. A ritual that recovers on a retry is claimed, and its failures are cleared
async def test_ritual_recovers_on_retry(db, monkeypatch):
    monkeypatch.setattr(settings, "digest_times", "")
    monkeypatch.setattr(settings, "checkin_time", "23:59")
    monkeypatch.setattr(settings, "weekly_time", "23:59")
    attempts, alerts = [], []

    async def _flaky(owner):
        attempts.append(owner)
        if len(attempts) == 1:
            raise RuntimeError("google timeout")

    t0 = datetime(2026, 11, 22, 7, 30, 5, tzinfo=KYIV)
    await _tick(_flaky, t0, alerts)
    await _tick(_flaky, t0 + timedelta(minutes=1), alerts)   # inside the pause
    assert len(attempts) == 1
    await _tick(_flaky, t0 + timedelta(minutes=5), alerts)   # pause is over
    assert len(attempts) == 2
    assert await _row("last_brief") == "2026-11-22"
    assert await _row("ritual_fail_brief") is None
    await _tick(_flaky, t0 + timedelta(minutes=6), alerts)   # claimed: no rerun
    assert len(attempts) == 2 and alerts == []


# 11. Give-up text names every ritual sensibly and escapes nothing it shouldn't
def test_give_up_text_names():
    assert "Дайджест пошти 13:00" in scheduler.give_up_text("digest_13:00", 5)
    assert "/travelon_sync" in scheduler.give_up_text("tv_sync", 5)
    assert _only_telegram_tags(scheduler.give_up_text("weird<key>", 5))
