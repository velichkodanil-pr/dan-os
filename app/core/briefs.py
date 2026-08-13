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

    access = None
    if settings.google_client_id:
        try:
            access = await google_client.get_access_token(db, user_id)
        except Exception:
            logger.exception("google access failed for brief")

    if access:
        events = await google_client.calendar_today(access)
        if events:
            lines.append("\n📆 <b>Календар:</b>")
            lines += [f" • {_fmt_event_time(e['start'], e['all_day'])} — {e['summary']}"
                      for e in events[:8]]
        else:
            lines.append("\n📆 Календар: подій немає")
        emails = await google_client.gmail_recent(access)
        if emails:
            lines.append("\n📬 <b>Пошта за ніч:</b>")
            lines += [f" • {m['from']}: {m['subject']}" for m in emails[:5]]
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
    if today_data["candidates"]:
        lines.append(f"\n🧠 Кандидатів у пам'ять на розбір: {today_data['candidates']} "
                     "(увечері запитаю)")
    return "\n".join(lines)


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
