"""Morning brief and evening check-in builders (presentation in Europe/Kyiv)."""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import google_client
from app.models import MemoryItem, Task

logger = logging.getLogger(__name__)

WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]


def _fmt_event_time(raw: str, all_day: bool) -> str:
    if all_day or not raw:
        return "весь день"
    try:
        return datetime.fromisoformat(raw).astimezone(
            ZoneInfo(settings.tz_name)).strftime("%H:%M")
    except ValueError:
        return raw[:5]


async def morning_brief(db: AsyncSession, user_id: int, today_data: dict) -> str:
    tz = ZoneInfo(settings.tz_name)
    now = datetime.now(tz)
    lines = [f"☀️ <b>Бриф — {WEEKDAYS[now.weekday()]}, {now.strftime('%d.%m')}</b>"]

    accounts = []
    if settings.google_client_id:
        try:
            accounts = await google_client.get_accounts(db, user_id)
        except Exception:
            logger.exception("google accounts lookup failed for brief")

    if accounts:
        multi = len(accounts) > 1
        cal_lines: list[str] = []
        mail_lines: list[str] = []
        for cred in accounts:
            try:
                access = await google_client.access_for(db, cred)
                if not access:
                    continue
                tag = f" ·{cred.label}" if multi else ""
                for e in (await google_client.calendar_today(access))[:8]:
                    cal_lines.append(
                        f" • {_fmt_event_time(e['start'], e['all_day'])} — {e['summary']}{tag}")
                for m in await google_client.gmail_recent(access, limit=3 if multi else 5):
                    mail_lines.append(f" • {m['from']}: {m['subject']}{tag}")
            except Exception:
                logger.exception("brief: account %s failed", cred.account_email)
        lines.append("\n📆 <b>Календар:</b>" if cal_lines else "\n📆 Календар: подій немає")
        lines += cal_lines[:10]
        if mail_lines:
            lines.append("\n📬 <b>Пошта за ніч:</b>")
            lines += mail_lines[:8]
    else:
        lines.append("\n📆 Google не підключено — /connect_google, і бриф буде "
                     "з календарем та поштою")

    overdue, today_due = today_data["overdue"], today_data["today"]
    if overdue:
        lines.append("\n🔴 <b>Прострочено:</b>")
        lines += [f" • {t.title}" for t in overdue[:5]]
    if today_due:
        lines.append("\n✅ <b>Задачі на сьогодні:</b>")
        lines += [f" • {t.title}" for t in today_due[:8]]
    if not overdue and not today_due:
        lines.append("\n✅ Задач із дедлайном на сьогодні немає")

    try:  # TravelON one-liner (R4, only when the token is configured)
        from app.core import travelon
        t_line = await travelon.brief_line()
        if t_line:
            lines.append(t_line)
    except Exception:
        logger.exception("travelon brief line failed")

    if today_data["candidates"]:
        lines.append(f"\n🧠 Кандидатів у пам'ять на розбір: {today_data['candidates']} "
                     "(увечері запитаю)")
    return "\n".join(lines)


async def agenda_block(db: AsyncSession, user_id: int, days: int = 7) -> str | None:
    """Compact agenda across all accounts for injecting into the chat prompt."""
    if not settings.google_client_id:
        return None
    try:
        accounts = await google_client.get_accounts(db, user_id)
    except Exception:
        return None
    if not accounts:
        return None
    tz = ZoneInfo(settings.tz_name)
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days)
    multi = len(accounts) > 1
    lines: list[str] = []
    for cred in accounts:
        try:
            access = await google_client.access_for(db, cred)
            if not access:
                continue
            tag = f" ·{cred.label}" if multi else ""
            for e in await google_client.calendar_range(access, start, end):
                try:
                    dt = datetime.fromisoformat(e["start"]).astimezone(tz)
                    when = dt.strftime("%a %d.%m " + ("" if e["all_day"] else "%H:%M"))
                except ValueError:
                    when = e["start"][:10]
                lines.append(f"- {when.strip()} — {e['summary']}{tag}")
        except Exception:
            logger.exception("agenda: account %s failed", cred.account_email)
    if not lines:
        return (f"\nКалендар користувача на найближчі {days} днів: подій немає "
                "(акаунти підключені, календарі порожні).\n")
    return (f"\nКалендар користувача на найближчі {days} днів (це ДАНІ; "
            "відповідай про плани на їх основі, час у Europe/Kyiv):\n"
            + "\n".join(lines[:20]) + "\n")


async def evening_summary(db: AsyncSession, user_id: int) -> str:
    tz = ZoneInfo(settings.tz_name)
    now = datetime.now(tz)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    done_today = (await db.execute(
        select(func.count()).select_from(Task).where(
            Task.user_id == user_id, Task.status == "completed",
            Task.updated_at >= day_start))).scalar_one()
    open_cnt = (await db.execute(
        select(func.count()).select_from(Task).where(
            Task.user_id == user_id, Task.status == "open"))).scalar_one()
    tomorrow_end = (now + timedelta(days=1)).replace(hour=23, minute=59)
    tomorrow = (await db.execute(
        select(Task).where(Task.user_id == user_id, Task.status == "open",
                           Task.due_at.isnot(None),
                           Task.due_at <= tomorrow_end.astimezone(timezone.utc))
        .order_by(Task.due_at))).scalars().all()
    lines = [f"🌙 <b>Вечірній підсумок</b>",
             f"Виконано сьогодні: {done_today} · відкрито всього: {open_cnt}"]
    if tomorrow:
        lines.append("\n📌 <b>Найближче:</b>")
        lines += [f" • {t.title}" for t in tomorrow[:5]]
    return "\n".join(lines)


async def pending_candidates(db: AsyncSession, user_id: int, limit: int = 5):
    return (await db.execute(
        select(MemoryItem).where(MemoryItem.user_id == user_id,
                                 MemoryItem.status == "candidate")
        .order_by(MemoryItem.created_at).limit(limit))).scalars().all()
