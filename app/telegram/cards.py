"""Telegram card formatting (presentation only; Europe/Kyiv at the boundary)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.models import Proposal, Task


def _fmt_dt(iso_or_dt) -> str | None:
    if not iso_or_dt:
        return None
    dt = datetime.fromisoformat(iso_or_dt) if isinstance(iso_or_dt, str) else iso_or_dt
    return dt.astimezone(ZoneInfo(settings.tz_name)).strftime("%H:%M %a %d.%m")


def proposal_card(p: Proposal) -> str:
    pl = p.payload
    lines = ["📝 <b>Нова задача</b>", f"<b>Назва:</b> {pl.get('title')}"]
    due = _fmt_dt(pl.get("due_at"))
    remind = _fmt_dt(pl.get("remind_at"))
    if due:
        lines.append(f"<b>Термін:</b> {due}")
    if remind and remind != due:
        lines.append(f"<b>Нагадаю:</b> {remind}")
    elif remind:
        lines.append("<b>Нагадаю:</b> у вказаний час")
    if pl.get("memory_text"):
        lines.append(f"🧠 <b>Запам'ятати:</b> {pl['memory_text']}")
    if p.version > 1:
        lines.append(f"<i>(версія {p.version})</i>")
    return "\n".join(lines)


def task_created_card(task: Task, reminder_at=None) -> str:
    lines = [f"✅ <b>Задача створена:</b> {task.title}"]
    due = _fmt_dt(task.due_at)
    if due:
        lines.append(f"🕐 Термін: {due}")
    r = _fmt_dt(reminder_at)
    if r:
        lines.append(f"⏰ Нагадаю: {r}")
    return "\n".join(lines)


def today_card(data: dict) -> str:
    lines = ["📅 <b>Сьогодні</b>"]
    if data["overdue"]:
        lines.append("\n🔴 <b>Прострочено:</b>")
        lines += [f" • {t.title} ({_fmt_dt(t.due_at)})" for t in data["overdue"][:5]]
    if data["today"]:
        lines.append("\n🟢 <b>На сьогодні:</b>")
        lines += [f" • {t.title} ({_fmt_dt(t.due_at)})" for t in data["today"][:10]]
    if data["no_date"]:
        lines.append("\n▫️ <b>Без дати:</b>")
        lines += [f" • {t.title}" for t in data["no_date"][:5]]
    if not (data["overdue"] or data["today"] or data["no_date"]):
        lines.append("\nВідкритих задач немає. Чистий горизонт 🙌")
    if data["candidates"]:
        lines.append(f"\n🧠 Кандидатів у пам'ять: {data['candidates']} (розбір — у раунді 2)")
    return "\n".join(lines)
