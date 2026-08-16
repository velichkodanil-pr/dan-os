"""Mini App API: overview + actions. Every call re-validates Telegram initData.

Auth model: initData signed by Telegram (HMAC with bot token), fresh
auth_date, and the user MUST be the owner — everyone else gets 401.
Actions reuse the orchestrator/coach services, so policy + audit + idempotency
are identical to the chat buttons.
"""
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from app import db as database
from app.config import settings
from app.core import coach
from app.core.domains import Domain, get_active_domain, label as domain_label
from app.core.orchestrator import Orchestrator
from app.core.policy import PolicyDenied
from app.models import Document, MemoryItem, Proposal
from app.webapp.auth import validate_init_data
from app.webapp.page import PAGE_HTML

logger = logging.getLogger(__name__)
router = APIRouter()
orch = Orchestrator()


def _auth(init_data: str) -> int:
    user_id = validate_init_data(init_data, settings.telegram_bot_token,
                                 max_age=settings.webapp_max_age)
    if user_id is None or user_id != settings.owner_telegram_id:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user_id


def _fmt_due(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(ZoneInfo(settings.tz_name)).strftime("%d.%m %H:%M")


@router.get("/app", response_class=HTMLResponse)
async def webapp_page() -> str:
    return PAGE_HTML


@router.get("/webapp/api/overview")
async def overview(x_telegram_init_data: str = Header(default="")) -> dict:
    user_id = _auth(x_telegram_init_data)
    now = datetime.now(ZoneInfo(settings.tz_name))
    async with database.session() as db:
        # §13: the domain is the server-side active domain from UserState — the
        # client cannot spoof it. Every panel below is scoped to it.
        domain = await get_active_domain(db, user_id)
        today = await orch.today(db, user_id=user_id, domain=domain)
        approvals = (await db.execute(
            select(Proposal).where(Proposal.user_id == user_id,
                                   Proposal.domain == domain,
                                   Proposal.status == "proposed")
            .order_by(Proposal.created_at.desc()).limit(10))).scalars().all()
        candidates = (await db.execute(
            select(MemoryItem).where(MemoryItem.user_id == user_id,
                                     MemoryItem.domain == domain,
                                     MemoryItem.status == "candidate")
            .order_by(MemoryItem.created_at).limit(15))).scalars().all()
        confirmed = (await db.execute(
            select(MemoryItem).where(MemoryItem.user_id == user_id,
                                     MemoryItem.domain == domain,
                                     MemoryItem.status == "confirmed")
            .order_by(MemoryItem.created_at.desc()).limit(30))).scalars().all()
        kb_docs = (await db.execute(  # searchable documents only (R6.1A)
            select(func.count()).select_from(Document)
            .where(Document.user_id == user_id, Document.domain == domain,
                   Document.status != "quarantined"))).scalar_one()
        goals = await coach.list_goals(db, user_id, domain)
        habits = await coach.habits_overview(db, user_id, domain)

    def task_json(t):
        return {"id": str(t.id), "title": t.title, "due": _fmt_due(t.due_at),
                "overdue": bool(t.due_at and t.due_at.astimezone(
                    ZoneInfo(settings.tz_name)) < now)}

    from app.core import travelon
    return {
        "today": {"overdue": [task_json(t) for t in today["overdue"]],
                  "today": [task_json(t) for t in today["today"]],
                  "no_date": [task_json(t) for t in today["no_date"]]},
        "approvals": [{"id": str(p.id), "version": p.version,
                       "title": p.payload.get("title", ""),
                       "due": _fmt_due(datetime.fromisoformat(p.payload["due_at"]))
                       if p.payload.get("due_at") else ""} for p in approvals],
        "memory_candidates": [{"id": str(m.id), "content": m.content}
                              for m in candidates],
        "memory_confirmed": [{"id": str(m.id), "content": m.content,
                              "date": m.created_at.astimezone(
                                  ZoneInfo(settings.tz_name)).strftime("%d.%m.%Y")}
                             for m in confirmed],
        "goals": [{"id": str(g.id), "title": g.title} for g in goals],
        "habits": habits,
        "kb_docs": kb_docs,
        # TravelON tab only in the travelon domain (isolation, §13)
        "travelon": bool(settings.travelon_token) and domain == Domain.TRAVELON,
        "domain": domain.value,
        "domain_label": domain_label(domain),
    }


@router.get("/webapp/api/travelon")
async def travelon_tab(x_telegram_init_data: str = Header(default="")) -> dict:
    """Pulse for the 🧳 tab (app_state-cached; first load can take ~20s)."""
    user_id = _auth(x_telegram_init_data)
    from app.core import travelon
    if not travelon.configured():
        return {"configured": False, "data": None}
    async with database.session() as db:
        # TravelON pulse is served ONLY in the travelon domain (§10/§13).
        if await get_active_domain(db, user_id) != Domain.TRAVELON:
            return {"configured": False, "data": None, "wrong_domain": True}
        data = await travelon.pulse_data(db)
    return {"configured": True, "data": data}


class ActRequest(BaseModel):
    action: str
    id: str = ""
    version: int | None = None
    text: str = ""


@router.post("/webapp/api/act")
async def act(req: ActRequest, x_telegram_init_data: str = Header(default="")) -> dict:
    user_id = _auth(x_telegram_init_data)
    if req.action in ("goal_add", "habit_add"):  # id-less creations
        title = req.text.strip()
        if not (2 <= len(title) <= 200):
            raise HTTPException(status_code=400, detail="bad title")
        try:
            async with database.session() as db:
                domain = await get_active_domain(db, user_id)  # server-side, §13
                if req.action == "goal_add":
                    await coach.create_goal(db, user_id=user_id, domain=domain,
                                            title=title)
                else:
                    await coach.create_habit(db, user_id=user_id, domain=domain,
                                             title=title)
        except PolicyDenied as e:
            raise HTTPException(status_code=403, detail=e.decision.reason)
        return {"status": "created"}
    try:
        ref = uuid.UUID(req.id)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad id")
    try:
        async with database.session() as db:
            if req.action == "approve":
                status, _, _ = await orch.approve(db, user_id=user_id,
                                                  proposal_id=ref,
                                                  version=req.version or 1)
            elif req.action == "reject":
                status = await orch.reject(db, user_id=user_id, proposal_id=ref)
            elif req.action == "task_done":
                status = await orch.complete_task(db, user_id=user_id, task_id=ref)
            elif req.action == "task_cancel":
                status = await orch.cancel_task(db, user_id=user_id, task_id=ref)
            elif req.action == "mem_confirm":
                result = await orch.confirm_memory(db, user_id=user_id, item_id=ref)
                status = "conflict" if isinstance(result, tuple) else result
            elif req.action == "mem_reject":
                status = await orch.reject_memory(db, user_id=user_id, item_id=ref)
            elif req.action == "habit_toggle":
                status = await coach.toggle_habit(db, user_id=user_id, habit_id=ref)
            elif req.action == "goal_done":
                status = await coach.set_goal_status(db, user_id=user_id,
                                                     goal_id=ref, status="done")
            elif req.action == "goal_drop":
                status = await coach.set_goal_status(db, user_id=user_id,
                                                     goal_id=ref, status="dropped")
            else:
                raise HTTPException(status_code=400, detail="unknown action")
    except PolicyDenied as e:
        raise HTTPException(status_code=403, detail=e.decision.reason)
    return {"status": status}
