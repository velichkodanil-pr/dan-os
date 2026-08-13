"""Reminder scheduler: polls the DB every 30s and fires due reminders.

DB polling (instead of in-memory jobs) survives restarts and redeploys by
design: a reminder missed while the service was down fires on the next tick,
marked as late. Firing is idempotent via the status transition UPDATE.
"""
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.core.audit import audit
from app.models import Reminder, Task
from app import db as database

logger = logging.getLogger(__name__)

POLL_SECONDS = 30
_task: asyncio.Task | None = None


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
            text = (f"⏰ <b>Нагадування{' (запізніле)' if late else ''}:</b> {task.title}\n"
                    f"🕐 {local.strftime('%H:%M %d.%m')}")
            try:
                await send_message(reminder.user_id, text, task_id=str(task.id))
                reminder.status = "fired"
                await audit(db, actor="system:scheduler", action="reminder.fired",
                            resource_type="reminder", resource_id=reminder.id,
                            late=late)
            except Exception:
                logger.exception("reminder send failed; will retry next tick")
        await db.commit()


async def _loop(send_message) -> None:
    while True:
        try:
            await _fire_due(send_message)
        except Exception:
            logger.exception("scheduler tick failed")
        await asyncio.sleep(POLL_SECONDS)


def start(send_message) -> None:
    """send_message: async (user_id, html_text, task_id) -> None"""
    global _task
    _task = asyncio.create_task(_loop(send_message))
    logger.info("Reminder scheduler started (poll every %ss)", POLL_SECONDS)


def stop() -> None:
    if _task:
        _task.cancel()
