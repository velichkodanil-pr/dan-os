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
    currency: str
    debt: float | None


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
            debt=_num(_text(costs_el, "amount-of-debt")) if costs_el is not None else None,
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
    debtors = sorted((o for o in arr if o.debt and o.debt > 0),
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
            "amount": _fmt_money(o.debt, o.currency),
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
