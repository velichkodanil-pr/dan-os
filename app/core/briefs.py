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


def brief_date_header() -> str:
    now = datetime.now(ZoneInfo(settings.tz_name))
    return f"☀️ <b>Бриф — {WEEKDAYS[now.weekday()]}, {now.strftime('%d.%m')}</b>"


async def morning_brief(db: AsyncSession, user_id: int, domain,
                        today_data: dict) -> str | None:
    """ONE domain's morning-brief section (calendar + mail + tasks), labeled.

    A scheduled brief is a permitted cross-domain ritual (§14), but it is built
    from SEPARATE domain-scoped calls like this one — each section uses only its
    own domain's Google accounts and tasks, under an explicit domain header,
    with no unlabelled mixing. Returns None when the domain has nothing worth
    showing (so the composed brief skips silent, empty domains)."""
    from app.core.domains import Domain, label
    lines: list[str] = []

    accounts = []
    if settings.google_client_id:
        try:
            accounts = await google_client.get_accounts(db, user_id, domain)
        except Exception:
            logger.exception("google accounts lookup failed for brief")

    cal_lines: list[str] = []
    mail_lines: list[str] = []
    cal_broken: list[str] = []
    if accounts:
        multi = len(accounts) > 1
        for cred in accounts:
            tag = f" ·{cred.label}" if multi else ""
            access = None
            try:
                access = await google_client.access_for(db, cred)
            except Exception:
                logger.exception("brief: token refresh failed %s", cred.account_email)
            if not access:
                cal_broken.append(cred.account_email)
                continue
            try:
                for e in (await google_client.calendar_today(access))[:8]:
                    cal_lines.append(
                        f" • {_fmt_event_time(e['start'], e['all_day'])} — {e['summary']}{tag}")
            except google_client.CalendarAccessError:
                cal_broken.append(cred.account_email)
            except Exception:
                logger.exception("brief calendar: %s failed", cred.account_email)
            try:
                for m in await google_client.gmail_recent(access, limit=3 if multi else 5):
                    mail_lines.append(f" • {m['from']}: {m['subject']}{tag}")
            except Exception:
                logger.exception("brief mail: %s failed", cred.account_email)

    overdue, today_due = today_data["overdue"], today_data["today"]

    travelon_line = None
    if domain == Domain.TRAVELON:  # TravelON pulse is its OWN block, travelon only
        try:
            from app.core import travelon
            travelon_line = await travelon.brief_line()
        except Exception:
            logger.exception("travelon brief line failed")

    # A section is worth showing only if this domain has something in it.
    if not (accounts or overdue or today_due or travelon_line
            or today_data.get("candidates")):
        return None

    lines.append(f"\n<b>{label(domain)}</b>")
    if cal_lines:
        lines.append("📆 <b>Календар:</b>")
        lines += cal_lines[:10]
    elif accounts and not cal_broken:
        lines.append("📆 Календар: подій немає")
    if cal_broken:
        lines.append("⚠️ Календар недоступний: " + ", ".join(cal_broken)
                     + "\nПерепідключи /connect_google — і постав галочку «Календар»")
    if mail_lines:
        lines.append("📬 <b>Пошта за ніч:</b>")
        lines += mail_lines[:8]
    if overdue:
        lines.append("🔴 <b>Прострочено:</b>")
        lines += [f" • {t.title}" for t in overdue[:5]]
    if today_due:
        lines.append("✅ <b>Задачі на сьогодні:</b>")
        lines += [f" • {t.title}" for t in today_due[:8]]
    if travelon_line:
        lines.append(travelon_line)
    if today_data.get("candidates"):
        lines.append(f"🧠 Кандидатів у пам'ять: {today_data['candidates']} "
                     "(увечері запитаю)")
    return "\n".join(lines)


async def agenda_block(db: AsyncSession, user_id: int, domain,
                       days: int = 7) -> str | None:
    """Compact agenda across THIS DOMAIN's accounts for the chat prompt.

    Domain-scoped: this string is injected into the model's context, so it must
    only ever contain the active domain's calendar — never another domain's."""
    if not settings.google_client_id:
        return None
    try:
        accounts = await google_client.get_accounts(db, user_id, domain)
    except Exception:
        return None
    if not accounts:
        return None
    tz = ZoneInfo(settings.tz_name)
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days)
    multi = len(accounts) > 1
    lines: list[str] = []
    broken: list[str] = []
    for cred in accounts:
        access = None
        try:
            access = await google_client.access_for(db, cred)
        except Exception:
            logger.exception("agenda: token refresh failed %s", cred.account_email)
        if not access:
            broken.append(cred.account_email)
            continue
        tag = f" ·{cred.label}" if multi else ""
        try:
            for e in await google_client.calendar_range(access, start, end):
                try:
                    dt = datetime.fromisoformat(e["start"]).astimezone(tz)
                    when = dt.strftime("%a %d.%m " + ("" if e["all_day"] else "%H:%M"))
                except ValueError:
                    when = e["start"][:10]
                lines.append(f"- {when.strip()} — {e['summary']}{tag}")
        except google_client.CalendarAccessError:
            broken.append(cred.account_email)
        except Exception:
            logger.exception("agenda: account %s failed", cred.account_email)
            broken.append(cred.account_email)
    parts: list[str] = []
    if broken:
        parts.append(
            "\nВАЖЛИВО: календар акаунтів " + ", ".join(broken) + " ЗАРАЗ НЕДОСТУПНИЙ "
            "(токен без дозволу на календар або доступ відкликано). НЕ стверджуй, що "
            "календар порожній чи синхронізований — чесно скажи Данилу, що не бачиш "
            "цих календарів, і порадь перепідключити акаунт через /connect_google, "
            "поставивши галочку «Переглядати календарі».\n")
    if lines:
        parts.append(f"\nКалендар користувача на найближчі {days} днів (це ДАНІ; "
                     "відповідай про плани на їх основі, час у Europe/Kyiv):\n"
                     + "\n".join(lines[:20]) + "\n")
    elif not broken:
        parts.append(f"\nКалендар користувача на найближчі {days} днів: подій немає "
                     "(акаунти підключені, календарі порожні).\n")
    return "".join(parts)


async def evening_summary(db: AsyncSession, user_id: int, domain) -> str | None:
    """ONE domain's evening counts, labeled. Returns None for an empty domain."""
    from app.core.domains import label
    tz = ZoneInfo(settings.tz_name)
    now = datetime.now(tz)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    done_today = (await db.execute(
        select(func.count()).select_from(Task).where(
            Task.user_id == user_id, Task.domain == domain,
            Task.status == "completed",
            Task.updated_at >= day_start))).scalar_one()
    open_cnt = (await db.execute(
        select(func.count()).select_from(Task).where(
            Task.user_id == user_id, Task.domain == domain,
            Task.status == "open"))).scalar_one()
    tomorrow_end = (now + timedelta(days=1)).replace(hour=23, minute=59)
    tomorrow = (await db.execute(
        select(Task).where(Task.user_id == user_id, Task.domain == domain,
                           Task.status == "open", Task.due_at.isnot(None),
                           Task.due_at <= tomorrow_end.astimezone(timezone.utc))
        .order_by(Task.due_at))).scalars().all()
    if not (done_today or open_cnt or tomorrow):
        return None
    lines = [f"\n<b>{label(domain)}</b>",
             f"Виконано сьогодні: {done_today} · відкрито всього: {open_cnt}"]
    if tomorrow:
        lines.append("📌 <b>Найближче:</b>")
        lines += [f" • {t.title}" for t in tomorrow[:5]]
    return "\n".join(lines)


async def pending_candidates(db: AsyncSession, user_id: int, domain,
                             limit: int = 5):
    return (await db.execute(
        select(MemoryItem).where(MemoryItem.user_id == user_id,
                                 MemoryItem.domain == domain,
                                 MemoryItem.status == "candidate")
        .order_by(MemoryItem.created_at).limit(limit))).scalars().all()
