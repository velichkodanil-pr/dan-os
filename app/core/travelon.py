"""TravelON read-only gateway (R4): business pulse from the XML report API.

Endpoint: https://travelon.to/book/report/xml/{TOKEN}/{FROM}/{TO}[?by_entry_date]
URL dates are YYYY-MM-DD; dates INSIDE the XML are DD.MM.YYYY.
Empty <orders/> is a valid "zero orders" answer.

Security (store-minimum): the token is a full-access secret — never logged,
never stored in DB. Responses contain passports/tax IDs — we parse ONLY the
operational minimum (order no, status, dates, hotel, country, tourist COUNT,
totals) and keep nothing else. Read-only: this module never writes to Travelon.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BASE = "https://travelon.to/book/report/xml"
# The report endpoint is slow on big days (100+ orders/day, ~1MB XML) —
# browser-like UA + generous timeout + day-sized windows fetched concurrently.
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 DAN.OS/1.0"}
_TIMEOUT = 90
_MAX_PARALLEL = 4


def configured() -> bool:
    return bool(settings.travelon_token)


@dataclass
class TravelonOrder:
    order_no: str
    status: str
    created: date | None
    hotel: str
    country: str
    check_in: date | None
    nights: int | None
    tourists: int
    gross_cost: float | None
    currency: str  # order currency (valute), e.g. EUR — gross_cost and debt
    debt: float | None  # debt-agency-in-currency: debt in the ORDER currency
    debt_local: float | None = None  # amount-of-debt: same debt in UAH/local
    local_currency: str = ""  # e.g. UAH

MIN_DEBT = 1.0  # order-currency units; below this it's rounding dust


def has_debt(o: TravelonOrder) -> bool:
    return o.debt is not None and o.debt >= MIN_DEBT


def _text(el, name: str) -> str:
    child = el.find(name)
    if child is None or child.text is None:
        return ""
    if child.get("nil") == "true":
        return ""
    return child.text.strip()


def _date(raw: str) -> date | None:
    """XML dates are DD.MM.YYYY."""
    try:
        return datetime.strptime(raw, "%d.%m.%Y").date()
    except ValueError:
        return None


def _num(raw: str) -> float | None:
    try:
        return float(raw)
    except ValueError:
        return None


def parse_orders(xml_text: str) -> list[TravelonOrder]:
    """Minimal parse; tolerates missing blocks and empty <orders/>.

    Flight-only orders (no hotel block — a big share of the operator's flow)
    fall back to the transport block for the check-in date and direction."""
    root = ElementTree.fromstring(xml_text)
    orders: list[TravelonOrder] = []
    for o in root.findall("order"):
        hotel_el = o.find("hotel")
        transport_el = o.find("transport")
        costs_el = o.find("costs")
        customers = o.findall("customers/customer")
        check_in = _date(_text(hotel_el, "from-date")) if hotel_el is not None else None
        country = _text(hotel_el, "country") if hotel_el is not None else ""
        if transport_el is not None:
            if check_in is None:
                check_in = _date(_text(transport_el, "depart-departure-date"))
            if not country:
                charter = _text(transport_el, "depart-charter-name")
                country = f"✈️ {charter[:30]}" if charter else ""
        # debt semantics (verified on live orders): debt-agency-in-currency is
        # in the ORDER currency (valute); amount-of-debt is the SAME debt in
        # local currency (UAH), amount = debt * rate. Never mix the two.
        debt_cur = debt_local = None
        if costs_el is not None:
            debt_cur = _num(_text(costs_el, "debt-agency-in-currency"))
            debt_local = _num(_text(costs_el, "amount-of-debt"))
            if debt_cur is None and debt_local is not None:
                rate = _num(_text(o, "rate"))
                debt_cur = round(debt_local / rate, 2) if rate else None
        orders.append(TravelonOrder(
            order_no=_text(o, "order") or _text(o, "id"),
            status=_text(o, "status"),
            created=_date(_text(o, "create-date")),
            hotel=_text(hotel_el, "hotel-name") if hotel_el is not None else "",
            country=country,
            check_in=check_in,
            nights=int(n) if hotel_el is not None
            and (n := _text(hotel_el, "nights")).isdigit() else None,
            tourists=len(customers),
            gross_cost=_num(_text(costs_el, "gross-cost")) if costs_el is not None else None,
            currency=_text(o, "valute"),
            debt=debt_cur,
            debt_local=debt_local,
            local_currency=_text(o, "local-currency"),
        ))
    return orders


async def fetch_period(date_from: date, date_to: date,
                       by_entry_date: bool = False) -> list[TravelonOrder]:
    """One report request. FROM==TO is a valid single-day window."""
    if not configured():
        return []
    url = f"{BASE}/{settings.travelon_token}/{date_from.isoformat()}/{date_to.isoformat()}"
    if by_entry_date:
        url += "?by_entry_date"
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_UA) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        # never log the URL — it carries the token
        logger.error("travelon report returned %s", resp.status_code)
        return []
    return parse_orders(resp.text)


_sem: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(_MAX_PARALLEL)
    return _sem


async def fetch_days(days: list[date],
                     by_entry_date: bool = False) -> list[TravelonOrder]:
    """Day-sized windows fetched concurrently (big-volume tokens: 100+/day),
    merged and de-duplicated by order number."""
    async def one(d: date) -> list[TravelonOrder]:
        async with _semaphore():
            try:
                return await fetch_period(d, d, by_entry_date)
            except Exception:
                logger.exception("travelon day %s failed", d)
                return []
    results = await asyncio.gather(*(one(d) for d in days))
    seen: set[str] = set()
    merged: list[TravelonOrder] = []
    for chunk in results:
        for o in chunk:
            if o.order_no not in seen:
                seen.add(o.order_no)
                merged.append(o)
    return merged


def _active(orders: list[TravelonOrder]) -> list[TravelonOrder]:
    return [o for o in orders if "cancel" not in o.status.lower()]


def _fmt_money(v: float | None, cur: str) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}".replace(",", " ") + (f" {cur}" if cur else "")


def _sum_by_currency(pairs) -> str:
    """pairs: iterable of (amount, currency)."""
    sums: dict[str, float] = {}
    for v, cur in pairs:
        if v:
            sums[cur or "?"] = sums.get(cur or "?", 0) + v
    return " + ".join(_fmt_money(v, cur) for cur, v in sorted(sums.items())) or "—"


def _fmt_debt(o: TravelonOrder) -> str:
    """Debt in the order currency, with the local (UAH) equivalent."""
    out = _fmt_money(o.debt, o.currency)
    if o.debt_local and o.local_currency and o.local_currency != o.currency:
        out += f" (≈{_fmt_money(o.debt_local, o.local_currency)})"
    return out


async def _pulse_compute() -> dict | None:
    """Fetch + aggregate the pulse (no cache). None on total failure."""
    tz = ZoneInfo(settings.tz_name)
    today = datetime.now(tz).date()
    week = [today + timedelta(days=i) for i in range(7)]
    try:
        created_today, created_yest, arrivals = await asyncio.gather(
            fetch_days([today]),
            fetch_days([today - timedelta(days=1)]),
            fetch_days(week, by_entry_date=True))
    except Exception:
        logger.exception("travelon pulse failed")
        return None
    ct, cy = _active(created_today), _active(created_yest)
    arr = _active(arrivals)
    arr_today = [o for o in arr if o.check_in == today]
    arr_tomorrow = [o for o in arr if o.check_in == today + timedelta(days=1)]
    debtors = sorted((o for o in arr if has_debt(o)),
                     key=lambda x: x.check_in or today)
    return {
        "date": today.strftime("%d.%m"),
        "created_today": len(ct), "created_yesterday": len(cy),
        "sum_2d": _sum_by_currency((o.gross_cost, o.currency) for o in ct + cy),
        "arrivals_today": len(arr_today), "arrivals_tomorrow": len(arr_tomorrow),
        "arrivals_week": len(arr), "tourists": sum(o.tourists for o in arr),
        "debt_count": len(debtors),
        "debt_total": _sum_by_currency((o.debt, o.currency) for o in debtors),
        "debtors": [{
            "when": o.check_in.strftime("%d.%m") if o.check_in else "—",
            "order_no": o.order_no,
            "where": o.country or o.hotel or "—",
            "amount": _fmt_debt(o),
        } for o in debtors[:8]],
        "generated_at": datetime.now(tz).strftime("%H:%M"),
    }


async def pulse_data(db=None, max_age: int = 600) -> dict | None:
    """Aggregated pulse with an app_state cache (the fetch takes ~20s).

    db=None -> no cache (compute directly). Stale or missing cache -> refetch
    and store. None when not configured or the report API is down."""
    if not configured():
        return None
    if db is None:
        return await _pulse_compute()
    import json as _json
    import time as _time
    from app.models import AppState
    state = await db.get(AppState, "travelon_pulse")
    if state is not None:
        try:
            cached = _json.loads(state.value)
            if _time.time() - cached["ts"] <= max_age:
                return cached["data"]
        except (ValueError, KeyError, TypeError):
            pass
    data = await _pulse_compute()
    if data is None:
        return None
    state = state or AppState(key="travelon_pulse")
    state.value = _json.dumps({"ts": _time.time(), "data": data},
                              ensure_ascii=False)
    db.add(state)
    await db.commit()
    return data


async def pulse_text(db=None) -> str | None:
    """Pulse card for /travelon (HTML). None when not configured."""
    if not configured():
        return None
    data = await pulse_data(db)
    if data is None:
        return "🧳 TravelON: сервіс звітів зараз недоступний, спробуй пізніше."
    lines = [f"🧳 <b>TravelON пульс — {data['date']}</b>",
             f"\n📈 <b>Нові заявки:</b> сьогодні {data['created_today']} · "
             f"вчора {data['created_yesterday']}",
             f"💰 Сума за ці два дні: {data['sum_2d']}",
             f"\n🛬 <b>Заїзди:</b> сьогодні {data['arrivals_today']} · завтра "
             f"{data['arrivals_tomorrow']} · за 7 днів {data['arrivals_week']} "
             f"({data['tourists']} тур.)"]
    if data["debt_count"]:
        lines.append(f"\n💸 <b>Борг у найближчих заїздах:</b> "
                     f"{data['debt_count']} заявок · {data['debt_total']}")
        for d in data["debtors"][:5]:
            lines.append(f" • {d['when']} №{d['order_no']} · {d['where']} · "
                         f"{d['amount']}")
        if data["debt_count"] > 5:
            lines.append(f" • … і ще {data['debt_count'] - 5}")
    else:
        lines.append("\n💸 Боргів у найближчих заїздах немає 👌")
    lines.append(f"\n🕓 Станом на {data['generated_at']}")
    return "\n".join(lines)


async def fetch_order(order_id: str) -> TravelonOrder | None:
    """Single order by number: BASE/{TOKEN}/{ORDER_ID}. None if not found."""
    if not configured() or not order_id.isdigit():
        return None
    url = f"{BASE}/{settings.travelon_token}/{order_id}"
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_UA) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        logger.error("travelon order lookup returned %s", resp.status_code)
        return None
    orders = parse_orders(resp.text)
    return orders[0] if orders else None


def order_card(o: TravelonOrder) -> str:
    """Plain-text order card (the chat layer escapes HTML)."""
    lines = [f"🧳 Заявка №{o.order_no} · {o.status or '—'}"]
    where = " · ".join(x for x in (o.hotel, o.country) if x)
    if where:
        lines.append(f"🏨 {where}")
    when = o.check_in.strftime("%d.%m.%Y") if o.check_in else "—"
    details = [f"🛬 Заїзд {when}"]
    if o.nights:
        details.append(f"{o.nights} ноч.")
    if o.tourists:
        details.append(f"{o.tourists} тур.")
    lines.append(" · ".join(details))
    money = f"💰 {_fmt_money(o.gross_cost, o.currency)}"
    if has_debt(o):
        money += f" · борг {_fmt_debt(o)} ⚠️"
    elif o.debt is not None:
        money += " · оплачено ✅"
    lines.append(money)
    if o.created:
        lines.append(f"📅 Створена {o.created.strftime('%d.%m.%Y')}")
    return "\n".join(lines)


async def debt_alert_text() -> str | None:
    """Morning debt alert: TOMORROW's check-ins with unpaid balance.
    None => nothing to say (no debts / not configured / API down)."""
    if not configured():
        return None
    tz = ZoneInfo(settings.tz_name)
    tomorrow = datetime.now(tz).date() + timedelta(days=1)
    try:
        arrivals = await fetch_days([tomorrow], by_entry_date=True)
    except Exception:
        logger.exception("travelon debt alert failed")
        return None
    debtors = [o for o in _active(arrivals) if has_debt(o)]
    if not debtors:
        return None
    debtors.sort(key=lambda o: -(o.debt or 0))
    total = _sum_by_currency((o.debt, o.currency) for o in debtors)
    lines = [f"🚨 <b>Завтра заїзд із боргом:</b> {len(debtors)} заявок · {total}"]
    for o in debtors[:10]:
        lines.append(f" • №{o.order_no} · {o.country or o.hotel or '—'} · "
                     f"{_fmt_debt(o)}")
    if len(debtors) > 10:
        lines.append(f" • … і ще {len(debtors) - 10}")
    lines.append("Деталі: «заявка №…» або /travelon")
    return "\n".join(lines)


async def weekly_block() -> str | None:
    """TravelON week summary for the Sunday report (HTML). None => skip."""
    if not configured():
        return None
    tz = ZoneInfo(settings.tz_name)
    today = datetime.now(tz).date()
    week = [today - timedelta(days=i) for i in range(7)]
    try:
        created = await fetch_days(week)
    except Exception:
        logger.exception("travelon weekly failed")
        return None
    active = _active(created)
    by_country: dict[str, int] = {}
    for o in active:
        key = o.country or "інше"
        by_country[key] = by_country.get(key, 0) + 1
    top = sorted(by_country.items(), key=lambda kv: -kv[1])[:3]
    lines = [f"\n🧳 <b>TravelON за тиждень:</b> {len(active)} нових заявок · "
             f"{_sum_by_currency((o.gross_cost, o.currency) for o in active)}"]
    if top:
        lines.append(" • топ: " + " · ".join(f"{c} ({n})" for c, n in top))
    return "\n".join(lines)


async def brief_line() -> str | None:
    """One compact line for the morning brief. None = skip silently."""
    if not configured():
        return None
    tz = ZoneInfo(settings.tz_name)
    today = datetime.now(tz).date()
    try:
        created, arrivals = await asyncio.wait_for(
            asyncio.gather(fetch_days([today - timedelta(days=1)]),
                           fetch_days([today], by_entry_date=True)),
            timeout=75)
    except Exception:
        logger.exception("travelon brief line failed")
        return None
    parts = []
    if arrivals:
        parts.append(f"заїздів сьогодні: {len(_active(arrivals))}")
    new_cnt = len(_active(created))
    if new_cnt:
        parts.append(f"нових заявок учора: {new_cnt}")
    if not parts:
        return "\n🧳 TravelON: тихо — без нових заявок і заїздів"
    return "\n🧳 <b>TravelON:</b> " + " · ".join(parts) + " (деталі: /travelon)"


# ---------- single-order DETAIL (R6.1C) ----------
# The bulk path above stays store-minimum on purpose: a period fetch parses
# hundreds of orders and must not touch names or documents. The detail path
# below is for ONE order the owner explicitly asked about — it parses the
# blocks an answer actually needs (insurance, tourists, documents) and still
# persists NOTHING. Document URLs carry the full-access token: they stay inside
# this module and are never returned to the model, the UI or the log.

MAX_DOC_CHARS = 20000   # a policy PDF is ~68k chars; the model needs the terms,
                        # not the whole booklet, and prompts must stay sane


@dataclass
class Insurance:
    included: bool
    provider: str          # e.g. "ЄТС"
    policy_nr: str         # e.g. "KM 3490138"
    category: str          # programme/class, e.g. "А"
    sum_insured: float | None
    territory: str
    from_date: date | None
    to_date: date | None


@dataclass
class Tourist:
    name: str              # "MUTSAK MYKHAILO"
    dob: date | None


@dataclass
class OrderDoc:
    kind: str              # insurance | voucher | confirmation | passenger | invoice | attached
    title: str
    url: str               # SECRET (carries the token) — never expose


@dataclass
class OrderDetail:
    order: TravelonOrder
    insurance: Insurance | None
    tourists: list[Tourist]
    documents: list[OrderDoc]

    def doc(self, kind: str) -> OrderDoc | None:
        kind = (kind or "").strip().lower()
        for d in self.documents:
            if d.kind == kind:
                return d
        for d in self.documents:   # allow matching an attached file by title
            if kind and kind in d.title.lower():
                return d
        return None


def _bool(raw: str) -> bool:
    return (raw or "").strip().lower() == "true"


def parse_order_detail(xml_text: str) -> OrderDetail | None:
    """Rich parse of a SINGLE order. Returns None for an empty <orders/>."""
    base = parse_orders(xml_text)
    if not base:
        return None
    root = ElementTree.fromstring(xml_text)
    o = root.find("order")

    ins_el = o.find("insurance")
    insurance = None
    if ins_el is not None and _text(ins_el, "policy-nr"):
        insurance = Insurance(
            included=_bool(_text(ins_el, "included")),
            provider=_text(ins_el, "provider"),
            policy_nr=_text(ins_el, "policy-nr"),
            category=_text(ins_el, "category"),
            sum_insured=_num(_text(ins_el, "insurance-sum")),
            territory=_text(ins_el, "territory"),
            from_date=_date(_text(ins_el, "from-date")),
            to_date=_date(_text(ins_el, "to-date")),
        )

    tourists = []
    for c in o.findall("customers/customer"):
        full = " ".join(x for x in (_text(c, "surname"), _text(c, "name")) if x)
        if full:
            tourists.append(Tourist(name=full, dob=_date(_text(c, "dob"))))

    docs: list[OrderDoc] = []
    d_el = o.find("documents")
    if d_el is not None:
        for kind in ("insurance", "confirmation", "voucher", "passenger"):
            url = _text(d_el, kind)
            if url:
                docs.append(OrderDoc(kind=kind, title=kind, url=url))
        for inv in d_el.findall("invoices/invoice"):
            u = _text(inv, "url")
            if u:
                docs.append(OrderDoc(kind="invoice",
                                     title=_text(inv, "title") or "invoice", url=u))
        for af in d_el.findall("attached-files/attached-file"):
            u = _text(af, "url")
            if u:
                docs.append(OrderDoc(kind="attached",
                                     title=_text(af, "title") or "file", url=u))
    return OrderDetail(order=base[0], insurance=insurance,
                       tourists=tourists, documents=docs)


async def fetch_order_detail(order_id: str) -> OrderDetail | None:
    """Single order with insurance, tourists and document list."""
    if not configured() or not str(order_id).strip().isdigit():
        return None
    url = f"{BASE}/{settings.travelon_token}/{str(order_id).strip()}"
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_UA) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        logger.error("travelon order detail returned %s", resp.status_code)
        return None
    try:
        return parse_order_detail(resp.text)
    except ElementTree.ParseError:
        logger.exception("travelon order detail: malformed XML")
        return None


def _pdf_text(data: bytes) -> str:
    from io import BytesIO
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


async def fetch_document_text(order_id: str, kind: str) -> tuple[str, str]:
    """Text of ONE document of an order. Returns (status, text).

    status: ok | no_order | no_document | unsupported | error.
    The document URL is never part of the return value — it carries the
    TravelON token, and the caller's output goes to a model."""
    detail = await fetch_order_detail(order_id)
    if detail is None:
        return "no_order", ""
    doc = detail.doc(kind)
    if doc is None:
        return "no_document", ""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_UA,
                                     follow_redirects=True) as client:
            resp = await client.get(doc.url)
        if resp.status_code != 200:
            logger.error("travelon document %s -> %s", kind, resp.status_code)
            return "error", ""
        ctype = (resp.headers.get("content-type") or "").lower()
        if "pdf" in ctype or resp.content[:5] == b"%PDF-":
            text = _pdf_text(resp.content)
        elif "text" in ctype or "html" in ctype:
            import re as _r
            text = _r.sub(r"<[^>]+>", " ", resp.text)
        else:
            return "unsupported", ""
    except Exception:
        logger.exception("travelon document fetch failed (%s)", kind)
        return "error", ""
    text = " ".join(text.split())
    return ("ok", text[:MAX_DOC_CHARS]) if text.strip() else ("unsupported", "")


def insurance_card(d: OrderDetail) -> str:
    """Plain-text insurance summary (the chat layer escapes HTML)."""
    i = d.insurance
    if i is None:
        return f"Заявка №{d.order.order_no}: страхування у заявці не вказане."
    when = " – ".join(x.strftime("%d.%m.%Y") for x in (i.from_date, i.to_date) if x)
    lines = [f"🛡 Страхування заявки №{d.order.order_no}",
             f"Страховик: {i.provider or '—'} · поліс {i.policy_nr or '—'}"]
    if i.category:
        lines.append(f"Програма/клас: {i.category}")
    if i.sum_insured:
        lines.append(f"Страхова сума: {i.sum_insured:,.0f}".replace(",", " "))
    if i.territory:
        lines.append(f"Територія: {i.territory}")
    if when:
        lines.append(f"Період: {when}")
    if d.tourists:
        who = ", ".join(t.name for t in d.tourists[:8])
        lines.append(f"Застраховані ({len(d.tourists)}): {who}")
    return "\n".join(lines)
