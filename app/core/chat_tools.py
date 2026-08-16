"""Tools the chat engine can call on its own (agentic bot, R5).

The model DECIDES what it needs and reaches for it — no more hardcoded
regex triggers deciding for it. READ-ONLY tools only: every executor passes
the deterministic policy (L0) before touching data; anything that WRITES
stays behind the existing preview-card flows. Tool output is DATA.

R6.1A tightened two things here:

- `wiki_save_answer` is GONE. It was the one tool that let the model write to
  long-term memory on its own initiative, and «save the answer you just
  assembled» is how partner credentials became a permanent, retrievable wiki
  page. A user-confirmed save flow comes back as R6.3.
- Every tool result is scanned before it is handed to the model. Tools read
  from places DAN.OS does not control — mailboxes, spreadsheets, chunks
  indexed before this round — so the last checkpoint before the model's
  context is here, not in the callers.
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
                    "аліасом (ТОКО / Toco UA / toco-tour.com.ua). Тут зібрані "
                    "факти: сервіс, посилання, логін, умови, реквізити, "
                    "контакти — з джерелами. Паролів і токенів тут немає.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "назва/аліас/слаг"}},
         "required": ["name"]}},
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
           "search_knowledge": "note.read", "get_calendar": "calendar.read",
           "get_recent_mail": "gmail.read", "search_mail": "gmail.read",
           "get_tasks": "today.read", "travelon_pulse": "travelon.read",
           "travelon_order": "travelon.read"}

# §10: TravelON tools are a domain-scoped capability, not a general one. They
# are hidden from the model outside the travelon domain (tools_for_domain) AND
# refused at dispatch (run_tool) — two independent layers, so a stale or
# hallucinated tool_use cannot reach the TravelON network from personal/tech.
_TRAVELON_TOOLS = {"travelon_pulse", "travelon_order"}

_TRAVELON_WRONG_DOMAIN = {
    "error": "wrong_domain",
    "note": "Інструменти TravelON доступні лише в домені TravelON. Зараз "
            "активний інший домен — бізнес-дані звідси недоступні. Якщо треба "
            "пульс/заявки, хай Данило перемкне домен: /domain travelon."}


def tools_for_domain(domain) -> list[dict]:
    """Tool definitions offered to the model for THIS domain.

    The model can only ask for a tool it was shown, so the TravelON business
    API is invisible in personal/tech — the definitions are simply absent from
    the list. Fail-closed: an unparseable domain gets the non-TravelON set.
    run_tool re-checks at dispatch regardless (defence in depth)."""
    from app.core.domains import Domain, DomainError, parse_domain
    try:
        active = parse_domain(domain)
    except DomainError:
        active = None
    if active is Domain.TRAVELON:
        return list(TOOL_DEFS)
    return [t for t in TOOL_DEFS if t["name"] not in _TRAVELON_TOOLS]


async def _t_wiki_index(db, user_id, domain, args):
    from app.core import wiki
    return {"index": await wiki.render_index(db, user_id, domain)}


async def _t_wiki_page(db, user_id, domain, args):
    from app.core import wiki
    name = str(args.get("name", ""))[:120]
    page = await wiki.find_page(db, user_id, domain, name)
    if page is None:
        found = await wiki.search_pages(db, user_id, domain, name, limit=5)
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


async def _t_search_knowledge(db, user_id, domain, args):
    from app.core import rag
    chunks = await rag.retrieve(db, user_id=user_id, domain=domain,
                                query=str(args.get("query", ""))[:300], k=8)
    if not chunks:
        return {"found": 0, "note": "нічого не знайдено в базі знань"}
    return {"found": len(chunks),
            "chunks": [{"source": c.title,
                        "date": c.created_at.strftime("%d.%m.%Y"),
                        "text": c.text[:700]} for c in chunks]}


async def _t_get_calendar(db, user_id, domain, args):
    from app.core.briefs import agenda_block
    days = min(max(int(args.get("days") or 7), 1), 30)
    block = await agenda_block(db, user_id, domain, days=days)
    return {"agenda": block or "Для цього домену не призначено Google-акаунтів "
                               "(/accounts)"}


async def _t_get_recent_mail(db, user_id, domain, args):
    from app.core import google_client
    limit = min(max(int(args.get("limit") or 8), 1), 15)
    accounts = await google_client.get_accounts(db, user_id, domain)
    if not accounts:
        return {"error": "Для цього домену не призначено жодного Google-акаунта "
                         "(/accounts)"}
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


async def _t_search_mail(db, user_id, domain, args):
    from app.core import google_client
    query = str(args.get("query", ""))[:200]
    accounts = await google_client.get_accounts(db, user_id, domain)
    if not accounts:
        return {"error": "Для цього домену не призначено жодного Google-акаунта "
                         "(/accounts)"}
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


async def _t_get_tasks(db, user_id, domain, args):
    from app.core import coach
    from app.models import Task
    tasks = (await db.execute(
        select(Task).where(Task.user_id == user_id, Task.domain == domain,
                           Task.status == "open")
        .order_by(Task.due_at.asc().nulls_last()).limit(20))).scalars().all()
    goals = await coach.list_goals(db, user_id, domain)
    habits = await coach.habits_overview(db, user_id, domain)
    return {"open_tasks": [{"title": t.title,
                            "due": t.due_at.isoformat() if t.due_at else None}
                           for t in tasks],
            "goals": [g.title for g in goals],
            "habits": [{"title": h["title"], "week": f"{h['week_count']}/{h['week_days']}"}
                       for h in habits]}


async def _t_travelon_pulse(db, user_id, domain, args):
    # Defence in depth: run_tool already refused this outside travelon, but the
    # network call itself lives behind the domain check too — zero TravelON
    # traffic in personal/tech even if this is reached directly.
    from app.core.domains import Domain
    if domain != Domain.TRAVELON:
        return dict(_TRAVELON_WRONG_DOMAIN)
    from app.core import travelon
    if not travelon.configured():
        return {"error": "TravelON не підключено"}
    data = await travelon.pulse_data(db)
    return data or {"error": "звіт тимчасово недоступний"}


async def _t_travelon_order(db, user_id, domain, args):
    from app.core.domains import Domain
    if domain != Domain.TRAVELON:
        return dict(_TRAVELON_WRONG_DOMAIN)
    from app.core import travelon
    if not travelon.configured():
        return {"error": "TravelON не підключено"}
    order = await travelon.fetch_order(str(args.get("order_no", "")).strip())
    return {"card": travelon.order_card(order)} if order else {"found": False}


_EXECUTORS = {"wiki_index": _t_wiki_index, "wiki_page": _t_wiki_page,
              "search_knowledge": _t_search_knowledge,
              "get_calendar": _t_get_calendar,
              "get_recent_mail": _t_get_recent_mail,
              "search_mail": _t_search_mail,
              "get_tasks": _t_get_tasks,
              "travelon_pulse": _t_travelon_pulse,
              "travelon_order": _t_travelon_order}


_REFUSED_ARGS = {"refused": True, "reason": "secret_in_arguments",
                 "note": "У запиті до інструмента був ключ/токен. Я його "
                         "нікуди не відправив. Скажи Данилу, що шукати за "
                         "секретом я не буду; значення не повторюй."}

_WITHHELD = {"withheld": True, "reason": "secret_detected",
             "note": "У знайденому фрагменті є пароль/токен/ключ. DAN.OS не "
                     "передає такі значення. Скажи Данилу, ЩО саме знайшлось "
                     "(сервіс, документ) і що доступ треба взяти в менеджері "
                     "паролів; значення не називай — ти його не бачив."}


async def run_tool(db: AsyncSession, user_id: int, domain, name: str,
                   args: dict) -> str:
    """Execute one read-tool under policy, in a fixed domain; ALWAYS returns a
    JSON string.

    `domain` is the server-side active-domain snapshot for this request — never
    a value the model chose (it is not in any tool's input schema). It scopes
    every data lookup the tool makes, so the model cannot read another domain
    by naming a tool.

    The last checkpoint before the model's context: whatever a tool dug up —
    a mailbox thread, a spreadsheet row, a chunk indexed before R6.1A — is
    scanned here. A blocked result is replaced wholesale, not redacted in
    place, because a partial redaction still leaks structure.
    """
    from app.core import security
    from app.core.domains import Domain, DomainError, parse_domain
    action = _POLICY.get(name)
    if action is None or not evaluate(action).allowed:
        return json.dumps({"error": f"інструмент {name} не дозволено"},
                          ensure_ascii=False)
    # Fail-closed on the domain itself: the snapshot must be valid before any
    # data tool runs. An unparseable domain is a server bug, never the model's
    # doing — refuse rather than let a lookup default to the wrong scope.
    try:
        active = parse_domain(domain)
    except DomainError:
        logger.error("run_tool: invalid domain %r for tool %s", domain, name)
        return json.dumps({"error": "внутрішня помилка домену"},
                          ensure_ascii=False)
    # §10: TravelON tools are domain-gated. Refuse BEFORE the executor so there
    # is zero TravelON network activity outside the travelon domain, even if a
    # stale/replayed tool_use for one arrives here.
    if name in _TRAVELON_TOOLS and active is not Domain.TRAVELON:
        logger.info("tool %s: refused, active domain is not travelon", name)
        return json.dumps(_TRAVELON_WRONG_DOMAIN, ensure_ascii=False)
    # R6.1A.1: ARGUMENTS are provider input. A search query, a mail query or a
    # page name goes straight out to Gmail / Calendar / the embedder, so it is
    # scanned before the executor runs — not only on the way back.
    arg_scan = security.scan_envelope(args or {})
    if arg_scan.blocked:
        logger.info("tool %s: refused, arguments carry a blocked secret", name)
        await security.record_finding(db, user_id=user_id,
                                      resource_type="tool_args",
                                      resource_id=name, result=arg_scan)
        await security.audit_blocked(db, user_id=user_id,
                                     action="tool.args_refused",
                                     resource_type="tool", resource_id=name,
                                     result=arg_scan)
        await db.commit()
        return json.dumps(_REFUSED_ARGS, ensure_ascii=False)
    try:
        result = await _EXECUTORS[name](db, user_id, active, args or {})
    except Exception:
        logger.exception("tool %s failed", name)
        result = {"error": "інструмент тимчасово не спрацював"}
    payload = json.dumps(result, ensure_ascii=False, default=str)[:8000]
    scan = security.scan(payload)
    if scan.blocked:
        logger.info("tool %s: output withheld by secret scan (categories=%s)",
                    name, ",".join(str(c) for c in scan.categories))
        await security.record_finding(db, user_id=user_id,
                                      resource_type="tool_output",
                                      resource_id=name, result=scan)
        await security.audit_blocked(db, user_id=user_id,
                                     action="tool.output_withheld",
                                     resource_type="tool", resource_id=name,
                                     result=scan)
        await db.commit()
        return json.dumps(_WITHHELD, ensure_ascii=False)
    return payload
