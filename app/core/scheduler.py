"""Reminder scheduler + daily rituals (morning brief, evening check-in).

One 30s DB-polling loop: survives restarts, no in-memory jobs. Rituals are
claimed in app_state per day (run-then-claim: a crash between send and claim
may repeat once next tick — preferred over silently losing a brief).
"""
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.core.audit import audit
from app.models import AppState, Reminder, Task
from app import db as database

logger = logging.getLogger(__name__)

POLL_SECONDS = 30
_task: asyncio.Task | None = None


def ritual_due(last_run_date: str | None, now_local: datetime, time_str: str) -> bool:
    """Pure decision: is the daily ritual due now? (testable)"""
    try:
        hh, mm = (int(x) for x in time_str.split(":"))
    except ValueError:
        return False
    target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now_local >= target and last_run_date != now_local.date().isoformat()


async def _fire_due(send_message) -> None:
    async with database.session() as db:
        due = (await db.execute(
            select(Reminder).where(
                Reminder.status == "scheduled",
                Reminder.fire_at <= datetime.now(timezone.utc),
            ).with_for_update(skip_locked=True)
        )).scalars().all()
        for reminder in due:
            task = await db.get(Task, reminder.task_id)
            if task is None or task.status != "open":
                reminder.status = "cancelled"
                continue
            late = (datetime.now(timezone.utc) - reminder.fire_at).total_seconds() > 120
            local = reminder.fire_at.astimezone(ZoneInfo(settings.tz_name))
            # §9: a reminder fires regardless of the active domain, but the
            # message is LABELLED with the reminder's own (stored) domain, so a
            # travelon reminder arriving while active=personal is unambiguous.
            dom_tag = ""
            try:
                from app.core.domains import label as _dlabel
                dom_tag = f" · {_dlabel(reminder.domain)}"
            except Exception:
                pass
            text = (f"⏰ <b>Нагадування{' (запізніле)' if late else ''}:</b>{dom_tag}\n"
                    f"{task.title}\n🕐 {local.strftime('%H:%M %d.%m')}")
            try:
                await send_message(reminder.user_id, text, task_id=str(task.id))
                reminder.status = "fired"
                await audit(db, actor="system:scheduler", action="reminder.fired",
                            resource_type="reminder", resource_id=reminder.id, late=late)
            except Exception:
                logger.exception("reminder send failed; will retry next tick")
        await db.commit()


async def _run_rituals(run_brief, run_checkin, run_digest, run_weekly,
                       run_debts=None) -> None:
    owner = settings.owner_telegram_id
    if not owner:
        return
    now_local = datetime.now(ZoneInfo(settings.tz_name))
    rituals = [("brief", settings.brief_time, run_brief, None),
               ("checkin", settings.checkin_time, run_checkin, None),
               ("weekly", settings.weekly_time, run_weekly, 6)]  # Sunday
    for t in [x.strip() for x in settings.digest_times.split(",") if x.strip()]:
        rituals.append((f"digest_{t}", t, run_digest, None))
    if run_debts is not None and settings.debt_alert_time.strip():
        rituals.append(("debts", settings.debt_alert_time.strip(), run_debts, None))
    for key, time_str, fn, weekday in rituals:
        if weekday is not None and now_local.weekday() != weekday:
            continue
        async with database.session() as db:
            state = await db.get(AppState, f"last_{key}")
            if not ritual_due(state.value if state else None, now_local, time_str):
                continue
            try:
                await fn(owner)
            except Exception:
                logger.exception("ritual %s failed; retry next tick", key)
                continue
            state = state or AppState(key=f"last_{key}")
            state.value = now_local.date().isoformat()
            db.add(state)
            await audit(db, actor="system:scheduler", action=f"ritual.{key}",
                        resource_type="ritual", resource_id=key)
            await db.commit()


async def _loop(send_message, run_brief, run_checkin, run_digest, run_weekly,
                run_debts=None) -> None:
    while True:
        try:
            await _fire_due(send_message)
            await _run_rituals(run_brief, run_checkin, run_digest, run_weekly,
                               run_debts)
        except Exception:
            logger.exception("scheduler tick failed")
        await asyncio.sleep(POLL_SECONDS)


def start(send_message, run_brief, run_checkin, run_digest, run_weekly,
          run_debts=None) -> None:
    global _task
    _task = asyncio.create_task(
        _loop(send_message, run_brief, run_checkin, run_digest, run_weekly,
              run_debts))
    logger.info("Reminder scheduler started (poll every %ss)", POLL_SECONDS)


def stop() -> None:
    if _task:
        _task.cancel()
