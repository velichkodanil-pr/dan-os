"""Tools the chat engine can call on its own (agentic bot, R5).

The model DECIDES what it needs and reaches for it — no more hardcoded
regex triggers deciding for it. READ-ONLY tools only: every executor passes
the deterministic policy (L0) before touching data; anything that WRITES
stays behind the existing preview-card flows. Tool output is DATA.
"""
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.policy import evaluate

logger = logging.getLogger(__name__)

TOOL_DEFS = [
    {"name": "wiki_index",
     "description": "Карта бази знань Данила: перелік сторінок (сутності — "
                    "партнери/люди/інструменти, концепції — процеси/правила, "
                    "архів відповідей). ПОЧИНАЙ з цього інструменту, коли "
                    "питання про його справи, партнерів, доступи чи домовленості "
                    "— щоб побачити, що взагалі відомо.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "wiki_page",
     "description": "Повна сторінка знань за назвою, слагом або будь-яким "
                    "аліасом (ТОКО / Toco UA / toco-tour.com.ua). Тут лежать "
                    "зібрані факти: доступи, реквізити, умови, контакти, з "
                    "джерелами.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "назва/аліас/слаг"}},
         "required": ["name"]}},
    {"name": "wiki_save_answer",
     "description": "Зберегти складну синтезовану відповідь як сторінку архіву, "
                    "щоб наступного разу вона була миттєвою. Використовуй, коли "
                    "зібрав відповідь з кількох джерел і вона буде потрібна ще.",
     "input_schema": {"type": "object", "properties": {
         "title": {"type": "string"}, "summary": {"type": "string"},
         "body": {"type": "string", "description": "повний текст, markdown"}},
         "required": ["title", "summary", "body"]}},
    {"name": "search_knowledge",
     "description": "Пошук по СИРИХ документах (таблиці, файли Drive, транскрипти, "
                    "пересилки; семантика + точні збіги). Використовуй, коли у "
                    "вікі немає сторінки або треба знайти конкретний рядок/цитату "
                    "у документі.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "запит у вільній формі"}},
         "required": ["query"]}},
    {"name": "get_calendar",
     "description": "Події з УСІХ підключених Google-календарів на N днів уперед.",
     "input_schema": {"type": "object", "properties": {
         "days": {"type": "integer", "minimum": 1, "maximum": 30, "default": 7}},
         "required": []}},
    {"name": "get_recent_mail",
     "description": "Останні листи з усіх Gmail-акаунтів (відправник, тема, "
                    "фрагмент). Використовуй на «що нового в пошті?».",
     "input_schema": {"type": "object", "properties": {
         "limit": {"type": "integer", "minimum": 1, "maximum": 15, "default": 8}},
         "required": []}},
    {"name": "search_mail",
     "description": "Знайти конкретний лист у Gmail за запитом (відправник, тема, "
                    "слова). Повертає повний текст найрелевантнішого листа.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_tasks",
     "description": "Відкриті задачі Данила (з дедлайнами) і цілі/звички.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "travelon_pulse",
     "description": "Бізнес-пульс TravelON: нові заявки, заїзди на 7 днів, борги.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "travelon_order",
     "description": "Картка заявки TravelON за номером (статус, готель, дати, "
                    "сума, борг).",
     "input_schema": {"type": "object", "properties": {
         "order_no": {"type": "string"}}, "required": ["order_no"]}},
]

_POLICY = {"wiki_index": "wiki.read", "wiki_page": "wiki.read",
           "wiki_save_answer": "wiki.archive", "search_knowledge": "note.read", "get_calendar": "calendar.read",
           "get_recent_mail": "gmail.read", "search_mail": "gmail.read",
           "get_tasks": "today.read", "travelon_pulse": "travelon.read",
           "travelon_order": "travelon.read"}


async def _t_wiki_index(db, user_id, args):
    from app.core import wiki
    return {"index": await wiki.render_index(db, user_id)}


async def _t_wiki_page(db, user_id, args):
    from app.core import wiki
    name = str(args.get("name", ""))[:120]
    page = await wiki.find_page(db, user_id, name)
    if page is None:
        found = await wiki.search_pages(db, user_id, name, limit=5)
        if not found:
            return {"found": False,
                    "note": "сторінки немає — спробуй search_knowledge по сирих документах"}
        if len(found) == 1:
            page = found[0]
        else:
            return {"found": False, "candidates": [
                {"title": p.title, "slug": p.slug, "summary": p.summary[:150]}
                for p in found]}
    return {"found": True, "page": wiki.page_text(page)[:6000]}


async def _t_wiki_save_answer(db, user_id, args):
    from app.core import wiki
    title = str(args.get("title", "")).strip()[:200]
    body = str(args.get("body", "")).strip()
    if len(title) < 3 or len(body) < 40:
        return {"saved": False, "note": "замало змісту для архівної сторінки"}
    page = await wiki.save_archive(db, user_id=user_id, title=title,
                                   summary=str(args.get("summary", ""))[:600],
                                   body=body[:10000])
    return {"saved": True, "slug": page.slug}


async def _t_search_knowledge(db, user_id, args):
    from app.core import rag
    chunks = await rag.retrieve(db, user_id=user_id,
                                query=str(args.get("query", ""))[:300], k=8)
    if not chunks:
        return {"found": 0, "note": "нічого не знайдено в базі знань"}
    return {"found": len(chunks),
            "chunks": [{"source": c.title,
                        "date": c.created_at.strftime("%d.%m.%Y"),
                        "text": c.text[:700]} for c in chunks]}


async def _t_get_calendar(db, user_id, args):
    from app.core.briefs import agenda_block
    days = min(max(int(args.get("days") or 7), 1), 30)
    block = await agenda_block(db, user_id, days=days)
    return {"agenda": block or "Google-акаунти не підключені"}


async def _t_get_recent_mail(db, user_id, args):
    from app.core import google_client
    limit = min(max(int(args.get("limit") or 8), 1), 15)
    accounts = await google_client.get_accounts(db, user_id)
    if not accounts:
        return {"error": "Google не підключено (/connect_google)"}
    out, problems = [], []
    for cred in accounts:
        try:
            access = await google_client.access_for(db, cred)
            if not access:
                problems.append(f"{cred.account_email}: токен потребує "
                                "перепідключення (/connect_google)")
                continue
            for m in await google_client.gmail_recent(access, hours=48, limit=limit):
                out.append({**m, "account": cred.account_email})
        except google_client.GmailAccessError as e:
            problems.append(
                f"{cred.account_email}: Gmail API ВИМКНЕНИЙ у Cloud-проєкті — "
                "власнику треба увімкнути console.cloud.google.com/apis/library/"
                "gmail.googleapis.com" if e.api_disabled else
                f"{cred.account_email}: немає дозволу на Gmail — /connect_google "
                "з галочками пошти")
        except Exception:
            logger.exception("mail tool: %s failed", cred.account_email)
            problems.append(f"{cred.account_email}: тимчасова помилка")
    result = {"messages": out[:limit * 2]}
    if problems:
        result["problems"] = problems
    return result


async def _t_search_mail(db, user_id, args):
    from app.core import google_client
    query = str(args.get("query", ""))[:200]
    accounts = await google_client.get_accounts(db, user_id)
    if not accounts:
        return {"error": "Google не підключено (/connect_google)"}
    for cred in accounts:
        try:
            access = await google_client.access_for(db, cred)
            if not access:
                continue
            email = await google_client.gmail_find_message(access, query)
            if email:
                return {"account": cred.account_email, "from": email["from"],
                        "subject": email["subject"], "body": email["body"][:2000]}
        except Exception:
            logger.exception("search_mail: %s failed", cred.account_email)
    return {"found": False, "note": "лист не знайдено"}


async def _t_get_tasks(db, user_id, args):
    from app.core import coach
    from app.models import Task
    tasks = (await db.execute(
        select(Task).where(Task.user_id == user_id, Task.status == "open")
        .order_by(Task.due_at.asc().nulls_last()).limit(20))).scalars().all()
    goals = await coach.list_goals(db, user_id)
    habits = await coach.habits_overview(db, user_id)
    return {"open_tasks": [{"title": t.title,
                            "due": t.due_at.isoformat() if t.due_at else None}
                           for t in tasks],
            "goals": [g.title for g in goals],
            "habits": [{"title": h["title"], "week": f"{h['week_count']}/{h['week_days']}"}
                       for h in habits]}


async def _t_travelon_pulse(db, user_id, args):
    from app.core import travelon
    if not travelon.configured():
        return {"error": "TravelON не підключено"}
    data = await travelon.pulse_data(db)
    return data or {"error": "звіт тимчасово недоступний"}


async def _t_travelon_order(db, user_id, args):
    from app.core import travelon
    if not travelon.configured():
        return {"error": "TravelON не підключено"}
    order = await travelon.fetch_order(str(args.get("order_no", "")).strip())
    return {"card": travelon.order_card(order)} if order else {"found": False}


_EXECUTORS = {"wiki_index": _t_wiki_index, "wiki_page": _t_wiki_page,
              "wiki_save_answer": _t_wiki_save_answer,
              "search_knowledge": _t_search_knowledge,
              "get_calendar": _t_get_calendar,
              "get_recent_mail": _t_get_recent_mail,
              "search_mail": _t_search_mail,
              "get_tasks": _t_get_tasks,
              "travelon_pulse": _t_travelon_pulse,
              "travelon_order": _t_travelon_order}


async def run_tool(db: AsyncSession, user_id: int, name: str, args: dict) -> str:
    """Execute one read-tool under policy; ALWAYS returns a JSON string."""
    action = _POLICY.get(name)
    if action is None or not evaluate(action).allowed:
        return json.dumps({"error": f"інструмент {name} не дозволено"},
                          ensure_ascii=False)
    try:
        result = await _EXECUTORS[name](db, user_id, args or {})
    except Exception:
        logger.exception("tool %s failed", name)
        result = {"error": "інструмент тимчасово не спрацював"}
    return json.dumps(result, ensure_ascii=False, default=str)[:8000]
