"""Reminder scheduler + daily rituals (morning brief, evening check-in).

One 30s DB-polling loop: survives restarts, no in-memory jobs. Rituals are
claimed in app_state per day (run-then-claim: a crash between send and claim
may repeat once next tick — preferred over silently losing a brief).

R7.1: a failing ritual backs off instead of retrying every tick. On
2026-11-22 the brief failed on bad markup and was rebuilt ~500 times in six
hours — Calendar, Gmail and TravelON each time — and never arrived, with
nothing but a log line to show for it. Now: a pause after each failure
(RETRY_DELAYS_MIN), at most MAX_ATTEMPTS a day, then ONE alert to the owner
and silence until tomorrow. The failure state lives in app_state as
`ritual_fail_<key>` (JSON), so it survives restarts like everything else here.
"""
import asyncio
import functools
import html as _html
import json
import logging
from datetime import datetime, timedelta, timezone
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


def slot_missed(now_local: datetime, time_str: str, grace_min: int) -> bool:
    """Pure: is it too late for this slot today? Booking digests are a
    sequence of windows — a slot found long after its time (a deploy, an
    outage) is skipped instead of firing several stale digests at once."""
    try:
        hh, mm = (int(x) for x in time_str.split(":"))
    except ValueError:
        return True
    target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return (now_local - target).total_seconds() > grace_min * 60


def interval_due(last_run_iso: str | None, now_utc: datetime, minutes: int) -> bool:
    """Pure decision for a repeating job (testable). minutes <= 0 = disabled."""
    if minutes <= 0:
        return False
    try:
        last = datetime.fromisoformat(last_run_iso) if last_run_iso else None
    except ValueError:
        last = None
    return last is None or (now_utc - last).total_seconds() >= minutes * 60 - 5


# Pause after the 1st, 2nd, 3rd, 4th failure; the 5th failure ends the day.
RETRY_DELAYS_MIN = (5, 15, 60, 60)
MAX_ATTEMPTS = len(RETRY_DELAYS_MIN) + 1

# What the owner reads in the give-up alert, and what to run by hand.
RITUAL_NAMES = {
    "brief": ("Ранковий бриф", "/brief"),
    "checkin": ("Вечірній чек-ін", "/checkin"),
    "weekly": ("Тижневий звіт", None),
    "debts": ("Алерт боргів TravelON", "/travelon"),
    "tv_sync": ("Нічна синхронізація заявок", "/travelon_sync"),
    "english": ("Нагадування про англійську", "/english"),
    "bookings_digest": ("Дайджест бронювань TravelON", "/bookings"),
}


def _fail_key(key: str) -> str:
    return f"ritual_fail_{key}"


def retry_state(raw: str | None, today: str) -> dict:
    """Pure: today's failure record for a ritual. Yesterday's record, a missing
    one or a corrupt one all mean a clean slate — a bad day never leaks into
    the next one."""
    try:
        state = json.loads(raw) if raw else {}
    except ValueError:
        state = {}
    if not isinstance(state, dict) or state.get("date") != today:
        return {"date": today, "count": 0, "next_at": None, "gave_up": False}
    return state


def may_attempt(state: dict, now_local: datetime) -> bool:
    """Pure: may the ritual run on this tick, given today's failures?"""
    if state.get("gave_up"):
        return False
    next_at = state.get("next_at")
    if not next_at:
        return True
    try:
        return now_local >= datetime.fromisoformat(next_at)
    except ValueError:
        return True


def after_failure(state: dict, now_local: datetime) -> dict:
    """Pure: the record after one more failure — a pause, or giving up."""
    count = int(state.get("count", 0)) + 1
    if count >= MAX_ATTEMPTS:
        return {"date": state["date"], "count": count, "next_at": None,
                "gave_up": True}
    pause = timedelta(minutes=RETRY_DELAYS_MIN[count - 1])
    return {"date": state["date"], "count": count,
            "next_at": (now_local + pause).isoformat(), "gave_up": False}


def give_up_text(key: str, attempts: int) -> str:
    """The one alert after the last attempt. Says what and what to do; never
    the error itself (it can quote mail or data)."""
    if key.startswith("bookings_digest_"):
        name = f"Дайджест бронювань {key.removeprefix('bookings_digest_')}"
        cmd = "/bookings"
    elif key.startswith("digest_"):
        name, cmd = f"Дайджест пошти {key.removeprefix('digest_')}", None
    else:
        name, cmd = RITUAL_NAMES.get(key, (key, None))
    hint = f" Спробувати вручну: {cmd}." if cmd else ""
    return (f"⚠️ <b>{_html.escape(name, quote=False)}</b> сьогодні не вдався "
            f"{attempts} разів — до завтра більше не пробую.{hint} "
            "Причина в логах Railway.")


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
                    f"{_html.escape(task.title or '', quote=False)}\n"
                    f"🕐 {local.strftime('%H:%M %d.%m')}")
            try:
                await send_message(reminder.user_id, text, task_id=str(task.id))
                reminder.status = "fired"
                await audit(db, actor="system:scheduler", action="reminder.fired",
                            resource_type="reminder", resource_id=reminder.id, late=late)
            except Exception:
                logger.exception("reminder send failed; will retry next tick")
        await db.commit()


async def _run_interval(key: str, minutes: int, fn) -> None:
    """A repeating owner job (R8.1 booking alerts). Claimed BEFORE running:
    a failing job retries on its next interval instead of every 30 s tick."""
    owner = settings.owner_telegram_id
    if not owner or fn is None:
        return
    now = datetime.now(timezone.utc)
    async with database.session() as db:
        state = await db.get(AppState, f"last_{key}")
        if not interval_due(state.value if state else None, now, minutes):
            return
        state = state or AppState(key=f"last_{key}")
        state.value = now.isoformat()
        db.add(state)
        await db.commit()
    try:
        await fn(owner)
    except Exception:
        logger.exception("interval job %s failed; next try in %s min", key, minutes)


async def _run_rituals(run_brief, run_checkin, run_digest, run_weekly,
                       run_debts=None, run_tv_sync=None,
                       run_english=None, run_bookings_digest=None, *,
                       alert=None, now_local: datetime | None = None) -> None:
    """`alert(user_id, html)` gets the one give-up message; `now_local` is for
    tests (production always uses the clock)."""
    owner = settings.owner_telegram_id
    if not owner:
        return
    now_local = now_local or datetime.now(ZoneInfo(settings.tz_name))
    today = now_local.date().isoformat()
    rituals = [("brief", settings.brief_time, run_brief, None),
               ("checkin", settings.checkin_time, run_checkin, None),
               ("weekly", settings.weekly_time, run_weekly, 6)]  # Sunday
    for t in [x.strip() for x in settings.digest_times.split(",") if x.strip()]:
        rituals.append((f"digest_{t}", t, run_digest, None))
    if run_debts is not None and settings.debt_alert_time.strip():
        rituals.append(("debts", settings.debt_alert_time.strip(), run_debts, None))
    # R6.1D: warm the local order cache overnight so aggregate questions are
    # answered from Postgres in milliseconds instead of timing out on the
    # period report. Silent by design — it reports nothing unless it breaks.
    if run_tv_sync is not None and settings.travelon_sync_time.strip():
        rituals.append(("tv_sync", settings.travelon_sync_time.strip(),
                        run_tv_sync, None))
    # R7: the English nudge. It fires the ritual every evening; the handler
    # itself stays silent when the session is already done, so the reminder
    # never turns into noise the owner learns to ignore.
    if run_english is not None and settings.english_time.strip():
        rituals.append(("english", settings.english_time.strip(),
                        run_english, None))
    # R8.1: booking digests every few hours (owner: 12:00…00:00 every 3 h).
    # Deterministic, no LLM. Each slot is its own ritual and knows its time,
    # so the window is «since the previous slot» even when it fires late.
    late_ok: dict[str, int] = {}
    if run_bookings_digest is not None:
        for t in [x.strip() for x in settings.bookings_digest_times.split(",")
                  if x.strip()]:
            key = f"bookings_digest_{t}"
            rituals.append((key, t, functools.partial(run_bookings_digest,
                                                      slot=t), None))
            late_ok[key] = settings.bookings_digest_grace_min
    for key, time_str, fn, weekday in rituals:
        if weekday is not None and now_local.weekday() != weekday:
            continue
        if key in late_ok and slot_missed(now_local, time_str, late_ok[key]):
            continue
        async with database.session() as db:
            state = await db.get(AppState, f"last_{key}")
            if not ritual_due(state.value if state else None, now_local, time_str):
                continue
            fail_row = await db.get(AppState, _fail_key(key))
            fails = retry_state(fail_row.value if fail_row else None, today)
            if not may_attempt(fails, now_local):
                continue
            try:
                await fn(owner)
            except Exception:
                fails = after_failure(fails, now_local)
                logger.exception("ritual %s failed (attempt %d of %d)%s", key,
                                 fails["count"], MAX_ATTEMPTS,
                                 "; giving up for today" if fails["gave_up"]
                                 else f"; next try after {fails['next_at']}")
                fail_row = fail_row or AppState(key=_fail_key(key))
                fail_row.value = json.dumps(fails)
                db.add(fail_row)
                await audit(db, actor="system:scheduler", action="ritual.failed",
                            resource_type="ritual", resource_id=key,
                            outcome="gave_up" if fails["gave_up"] else "retry",
                            attempt=fails["count"])
                await db.commit()
                if fails["gave_up"] and alert is not None:
                    try:
                        await alert(owner, give_up_text(key, fails["count"]))
                    except Exception:
                        logger.exception("ritual give-up alert failed")
                continue
            state = state or AppState(key=f"last_{key}")
            state.value = today
            db.add(state)
            if fail_row is not None:  # recovered: today's failures are history
                await db.delete(fail_row)
            await audit(db, actor="system:scheduler", action=f"ritual.{key}",
                        resource_type="ritual", resource_id=key,
                        attempt=(fails["count"] + 1) if fails["count"] else None)
            await db.commit()


async def _loop(send_message, run_brief, run_checkin, run_digest, run_weekly,
                run_debts=None, run_tv_sync=None, run_english=None,
                run_power=None, run_bookings_digest=None,
                run_bookings_alerts=None) -> None:
    while True:
        try:
            await _fire_due(send_message)
            await _run_rituals(run_brief, run_checkin, run_digest, run_weekly,
                               run_debts, run_tv_sync, run_english,
                               run_bookings_digest, alert=send_message)
            await _run_interval("bookings_alerts",
                                settings.bookings_alert_interval_min,
                                run_bookings_alerts)
        except Exception:
            logger.exception("scheduler tick failed")
        # R8: power status is not a daily ritual — it self-throttles to
        # POWER_POLL_SECONDS and must never block reminders if it breaks.
        if run_power is not None:
            try:
                await run_power()
            except Exception:
                logger.exception("power tick failed")
        await asyncio.sleep(POLL_SECONDS)


def start(send_message, run_brief, run_checkin, run_digest, run_weekly,
          run_debts=None, run_tv_sync=None, run_english=None,
          run_power=None, run_bookings_digest=None,
          run_bookings_alerts=None) -> None:
    global _task
    _task = asyncio.create_task(
        _loop(send_message, run_brief, run_checkin, run_digest, run_weekly,
              run_debts, run_tv_sync, run_english, run_power,
              run_bookings_digest, run_bookings_alerts))
    logger.info("Reminder scheduler started (poll every %ss)", POLL_SECONDS)


def stop() -> None:
    if _task:
        _task.cancel()
