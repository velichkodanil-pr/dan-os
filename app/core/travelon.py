"""TravelON read-only gateway (R4): business pulse from the XML report API.

Endpoint: https://travelon.to/book/report/xml/{TOKEN}/{FROM}/{TO}[?by_entry_date]
URL dates are YYYY-MM-DD; dates INSIDE the XML are DD.MM.YYYY.
Empty <orders/> is a valid "zero orders" answer.

Security (store-minimum): the token is a full-access secret — never logged,
never stored in DB. Responses contain passports/tax IDs — we parse ONLY the
operational minimum (order no, status, dates, hotel, country, tourist COUNT,
totals) and keep nothing else. Read-only: this module never writes to Travelon.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BASE = "https://travelon.to/book/report/xml"


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
    """Minimal parse; tolerates missing blocks and empty <orders/>."""
    root = ElementTree.fromstring(xml_text)
    orders: list[TravelonOrder] = []
    for o in root.findall("order"):
        hotel_el = o.find("hotel")
        costs_el = o.find("costs")
        customers = o.findall("customers/customer")
        orders.append(TravelonOrder(
            order_no=_text(o, "order") or _text(o, "id"),
            status=_text(o, "status"),
            created=_date(_text(o, "create-date")),
            hotel=_text(hotel_el, "hotel-name") if hotel_el is not None else "",
            country=_text(hotel_el, "country") if hotel_el is not None else "",
            check_in=_date(_text(hotel_el, "from-date")) if hotel_el is not None else None,
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
    if not configured():
        return []
    url = f"{BASE}/{settings.travelon_token}/{date_from.isoformat()}/{date_to.isoformat()}"
    if by_entry_date:
        url += "?by_entry_date"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        # never log the URL — it carries the token
        logger.error("travelon report returned %s", resp.status_code)
        return []
    return parse_orders(resp.text)


def _fmt_money(v: float | None, cur: str) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}".replace(",", " ") + (f" {cur}" if cur else "")


async def pulse_text() -> str | None:
    """Full pulse card for /travelon (HTML). None when not configured."""
    if not configured():
        return None
    tz = ZoneInfo(settings.tz_name)
    today = datetime.now(tz).date()
    week_ago, week_ahead = today - timedelta(days=7), today + timedelta(days=7)
    try:
        recent = await fetch_period(week_ago, today)
        arrivals = await fetch_period(today, week_ahead, by_entry_date=True)
    except Exception:
        logger.exception("travelon pulse failed")
        return "🧳 TravelON: сервіс звітів зараз недоступний, спробуй пізніше."

    lines = [f"🧳 <b>TravelON пульс — {today.strftime('%d.%m')}</b>"]

    active = [o for o in recent if o.status.lower() != "cancelled"]
    lines.append(f"\n📈 <b>Нові заявки за 7 днів:</b> {len(active)}")
    for o in active[:6]:
        when = o.created.strftime("%d.%m") if o.created else "—"
        lines.append(f" • №{o.order_no} {when} · {o.country or o.hotel or '—'} · "
                     f"{o.tourists} тур. · {_fmt_money(o.gross_cost, o.currency)} "
                     f"({o.status})")
    if len(active) > 6:
        lines.append(f" • … і ще {len(active) - 6}")

    if arrivals:
        lines.append(f"\n🛬 <b>Заїзди в найближчі 7 днів:</b> {len(arrivals)}")
        for o in sorted(arrivals, key=lambda x: x.check_in or today)[:6]:
            when = o.check_in.strftime("%d.%m") if o.check_in else "—"
            nights = f" · {o.nights} ноч." if o.nights else ""
            lines.append(f" • {when} — {o.hotel or o.country or '—'}{nights} · "
                         f"{o.tourists} тур. (№{o.order_no})")
    else:
        lines.append("\n🛬 Заїздів у найближчі 7 днів немає")

    debtors = [o for o in recent + arrivals
               if o.debt and o.debt > 0 and o.status.lower() != "cancelled"]
    seen: set[str] = set()
    debtors = [o for o in debtors if not (o.order_no in seen or seen.add(o.order_no))]
    if debtors:
        lines.append(f"\n💸 <b>Із боргом:</b> {len(debtors)}")
        for o in debtors[:5]:
            lines.append(f" • №{o.order_no} · борг {_fmt_money(o.debt, o.currency)}")
    return "\n".join(lines)


async def brief_line() -> str | None:
    """One compact line for the morning brief. None = skip silently."""
    if not configured():
        return None
    tz = ZoneInfo(settings.tz_name)
    today = datetime.now(tz).date()
    try:
        arrivals = await fetch_period(today, today + timedelta(days=1),
                                      by_entry_date=True)
        created = await fetch_period(today - timedelta(days=1), today)
    except Exception:
        logger.exception("travelon brief line failed")
        return None
    new_cnt = len([o for o in created if o.status.lower() != "cancelled"])
    parts = []
    if arrivals:
        parts.append(f"заїздів сьогодні/завтра: {len(arrivals)}")
    if new_cnt:
        parts.append(f"нових заявок за добу: {new_cnt}")
    if not parts:
        return "\n🧳 TravelON: тихо — без нових заявок і заїздів"
    return "\n🧳 <b>TravelON:</b> " + " · ".join(parts) + " (деталі: /travelon)"
