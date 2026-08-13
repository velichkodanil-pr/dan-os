"""Coach (R4): goals and daily habits. Deterministic, no LLM.

Goals are mid-term aims with an active|done|dropped lifecycle.
Habits are daily; one habit_log row per (habit, local date) = done that day.
Toggling off deletes the day row — reversible by design (L2).
"""
import logging
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.audit import audit
from app.core.policy import PolicyDenied, evaluate
from app.models import Goal, Habit, HabitLog

logger = logging.getLogger(__name__)


def _check(action: str) -> None:
    d = evaluate(action)
    if not d.allowed:
        raise PolicyDenied(action, d)


def _today_local() -> str:
    return datetime.now(ZoneInfo(settings.tz_name)).date().isoformat()


def week_dates(today: str | None = None) -> list[str]:
    """ISO dates of the current week, Monday..today (Europe/Kyiv)."""
    d = datetime.fromisoformat(today or _today_local()).date()
    monday = d - timedelta(days=d.weekday())
    return [(monday + timedelta(days=i)).isoformat()
            for i in range((d - monday).days + 1)]


# ---------- goals ----------

async def create_goal(db: AsyncSession, *, user_id: int, title: str,
                      domain: str = "personal") -> Goal:
    _check("goal.create")
    goal = Goal(user_id=user_id, title=title.strip()[:300], domain=domain)
    db.add(goal)
    await db.flush()
    await audit(db, actor=f"user:{user_id}", action="goal.created",
                resource_type="goal", resource_id=goal.id, policy_level="L2",
                title=goal.title[:80])
    await db.commit()
    return goal


async def list_goals(db: AsyncSession, user_id: int, status: str = "active") -> list[Goal]:
    return (await db.execute(
        select(Goal).where(Goal.user_id == user_id, Goal.status == status)
        .order_by(Goal.created_at))).scalars().all()


async def set_goal_status(db: AsyncSession, *, user_id: int, goal_id: uuid.UUID,
                          status: str) -> str:
    _check("goal.update")
    goal = await db.get(Goal, goal_id, with_for_update=True)
    if goal is None or goal.user_id != user_id:
        return "not_found"
    if goal.status != "active":
        await db.commit()
        return goal.status  # idempotent
    goal.status = status
    await audit(db, actor=f"user:{user_id}", action=f"goal.{status}",
                resource_type="goal", resource_id=goal.id, policy_level="L2")
    await db.commit()
    return status


# ---------- habits ----------

async def create_habit(db: AsyncSession, *, user_id: int, title: str) -> Habit:
    _check("habit.create")
    habit = Habit(user_id=user_id, title=title.strip()[:200])
    db.add(habit)
    await db.flush()
    await audit(db, actor=f"user:{user_id}", action="habit.created",
                resource_type="habit", resource_id=habit.id, policy_level="L2",
                title=habit.title[:80])
    await db.commit()
    return habit


async def list_habits(db: AsyncSession, user_id: int) -> list[Habit]:
    return (await db.execute(
        select(Habit).where(Habit.user_id == user_id, Habit.active.is_(True))
        .order_by(Habit.created_at))).scalars().all()


async def archive_habit(db: AsyncSession, *, user_id: int, habit_id: uuid.UUID) -> str:
    _check("habit.create")
    habit = await db.get(Habit, habit_id, with_for_update=True)
    if habit is None or habit.user_id != user_id:
        return "not_found"
    habit.active = False
    await audit(db, actor=f"user:{user_id}", action="habit.archived",
                resource_type="habit", resource_id=habit.id, policy_level="L2")
    await db.commit()
    return "archived"


async def toggle_habit(db: AsyncSession, *, user_id: int, habit_id: uuid.UUID,
                       day: str | None = None) -> str:
    """Mark/unmark the habit for the local date. Returns "done"|"undone"|"not_found"."""
    _check("habit.log")
    habit = await db.get(Habit, habit_id)
    if habit is None or habit.user_id != user_id:
        return "not_found"
    day = day or _today_local()
    existing = (await db.execute(
        select(HabitLog).where(HabitLog.habit_id == habit_id,
                               HabitLog.log_date == day))).scalar_one_or_none()
    if existing is not None:
        await db.execute(delete(HabitLog).where(HabitLog.id == existing.id))
        await audit(db, actor=f"user:{user_id}", action="habit.unlogged",
                    resource_type="habit", resource_id=habit_id, policy_level="L2",
                    day=day)
        await db.commit()
        return "undone"
    db.add(HabitLog(habit_id=habit_id, user_id=user_id, log_date=day))
    try:
        await db.flush()
    except IntegrityError:  # race on double-tap: already logged — fine
        await db.rollback()
        return "done"
    await audit(db, actor=f"user:{user_id}", action="habit.logged",
                resource_type="habit", resource_id=habit_id, policy_level="L2", day=day)
    await db.commit()
    return "done"


async def habits_overview(db: AsyncSession, user_id: int) -> list[dict]:
    """Active habits with today-status and current-week count."""
    habits = await list_habits(db, user_id)
    if not habits:
        return []
    days = week_dates()
    today = days[-1]
    rows = (await db.execute(
        select(HabitLog.habit_id, HabitLog.log_date).where(
            HabitLog.user_id == user_id, HabitLog.log_date.in_(days)))).all()
    by_habit: dict = {}
    for habit_id, log_date in rows:
        by_habit.setdefault(habit_id, set()).add(log_date)
    return [{
        "id": str(h.id),
        "title": h.title,
        "done_today": today in by_habit.get(h.id, set()),
        "week_count": len(by_habit.get(h.id, set())),
        "week_days": len(days),
    } for h in habits]


async def weekly_block(db: AsyncSession, user_id: int) -> str | None:
    """Goals + habits progress lines for the Sunday report (HTML)."""
    goals = await list_goals(db, user_id)
    overview = await habits_overview(db, user_id)
    if not goals and not overview:
        return None
    lines: list[str] = []
    if goals:
        lines.append("\n🎯 <b>Цілі в роботі:</b>")
        lines += [f" • {g.title}" for g in goals[:5]]
    if overview:
        lines.append("\n🏃 <b>Звички за тиждень:</b>")
        lines += [f" • {h['title']}: {h['week_count']}/{h['week_days']}"
                  for h in overview[:8]]
    return "\n".join(lines)
