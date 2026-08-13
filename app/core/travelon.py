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


async def pulse_text() -> str | None:
    """Aggregated pulse for /travelon (HTML). None when not configured.

    Volume-aware: this token sees the WHOLE operator flow (100+ orders/day),
    so the card shows aggregates, and per-order lines only for the most
    actionable thing — unpaid balances among the nearest check-ins."""
    if not configured():
        return None
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
        return "🧳 TravelON: сервіс звітів зараз недоступний, спробуй пізніше."

    ct, cy = _active(created_today), _active(created_yest)
    arr = _active(arrivals)
    arr_today = [o for o in arr if o.check_in == today]
    arr_tomorrow = [o for o in arr if o.check_in == today + timedelta(days=1)]
    tourists = sum(o.tourists for o in arr)

    lines = [f"🧳 <b>TravelON пульс — {today.strftime('%d.%m')}</b>",
             f"\n📈 <b>Нові заявки:</b> сьогодні {len(ct)} · вчора {len(cy)}",
             f"💰 Сума за ці два дні: "
             f"{_sum_by_currency((o.gross_cost, o.currency) for o in ct + cy)}",
             f"\n🛬 <b>Заїзди:</b> сьогодні {len(arr_today)} · завтра "
             f"{len(arr_tomorrow)} · за 7 днів {len(arr)} ({tourists} тур.)"]

    debtors = sorted((o for o in arr if o.debt and o.debt > 0),
                     key=lambda x: x.check_in or today)
    if debtors:
        total = _sum_by_currency((o.debt, o.currency) for o in debtors)
        lines.append(f"\n💸 <b>Борг у найближчих заїздах:</b> {len(debtors)} "
                     f"заявок · {total}")
        for o in debtors[:5]:
            when = o.check_in.strftime("%d.%m") if o.check_in else "—"
            lines.append(f" • {when} №{o.order_no} · {o.country or o.hotel or '—'} · "
                         f"{_fmt_money(o.debt, o.currency)}")
        if len(debtors) > 5:
            lines.append(f" • … і ще {len(debtors) - 5}")
    else:
        lines.append("\n💸 Боргів у найближчих заїздах немає 👌")
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
