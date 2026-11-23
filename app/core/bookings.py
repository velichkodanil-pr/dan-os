"""TravelON booking feed: a daily digest of new orders and alerts on exceptions.

Owner request (23.11.2026): every new order sends an e-mail «Ваше бронювання
№ … готове!» (~40 a day). He wants to know what is being booked without
reading them. The e-mail is only a rendering of the order report, so this
module reads the report directly — it also sees cancellations, which no
e-mail announces.

Two outputs, both deterministic (no LLM, no prompt — nothing to inject into):

- ``digest_text(start, end)`` — one summary for a window: new orders,
  tourists, turnover, countries, agencies, booking depth, the biggest order,
  possible duplicates, cancellations; compared with the same window a week
  earlier. Sent once a day by the scheduler and on demand by /bookings.
- ``poll_alerts(db, now)`` — run every few minutes in working hours; returns
  ONE message listing only the orders worth a look right now (big amount, big
  group, departure within days, first order of an agency, a big cancellation)
  or None. Each order is announced at most once per kind.

Store-minimum, as in ``travelon``: tourist names are read only to recognise a
test order («STAFF STAFF») and never kept; passports, dates of birth and free
texts are not parsed at all. What persists in app_state is order numbers,
agency ids and dates — no personal data.
"""
import asyncio
import hashlib
import html
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx

from app.config import settings
from app.core import travelon
from app.core.travelon import _date, _num, _text

logger = logging.getLogger(__name__)

# The report reads URL datetimes as fixed UTC+3 wall-clock time (Kyiv is
# UTC+2 in winter), so every window is converted explicitly and padded; the
# exact cut is then made in code on the order's own creation time.
_REPORT_OFFSET = timedelta(hours=3)
_PAD = timedelta(hours=1)

# Approximate EUR value, used ONLY to compare against alert thresholds.
# Totals in messages are always shown per currency, never converted.
_TO_EUR = {"EUR": 1.0, "USD": 0.86}

TEST_REASONS = {"тестова"}
REBOOK_REASONS = {"перебронювання"}
AGENCY_SEED_DAYS = 120

_STATE_ALERTS = "bookings_alerts"      # {"last": iso, "sent": {key: iso_date}}
_STATE_AGENCIES = "bookings_agencies"  # {"seeded": bool, "ids": {id: iso_date}}
_seeding = False


@dataclass
class FeedOrder:
    order_no: str
    status: str
    created_at: datetime | None      # Europe/Kyiv, aware
    agency_id: str
    agency: str
    country: str
    city: str
    hotel: str
    check_in: date | None
    tourists: int
    gross: float | None
    currency: str
    is_test: bool
    rebooked_from: str
    rebooked_to: str
    cancelled_on: date | None
    cancel_reason: str               # dictionary reason only, never free text
    people_key: str = ""             # hash of the tourist list — never names

    @property
    def cancelled(self) -> bool:
        return travelon._is_cancelled(self.status)

    @property
    def is_rebooking(self) -> bool:
        """A copy made by «rebook»: not a new sale."""
        return bool(self.rebooked_from)

    @property
    def lost_by_rebooking(self) -> bool:
        """The old half of a rebooking: not a lost sale."""
        return bool(self.rebooked_to) or (
            self.cancel_reason.lower() in REBOOK_REASONS)

    @property
    def approx_eur(self) -> float | None:
        k = _TO_EUR.get(self.currency.upper())
        return None if k is None or self.gross is None else self.gross * k

    @property
    def where(self) -> str:
        return ", ".join(x for x in (self.country, self.city) if x) or "—"


def _is_test(order_el, reason: str) -> bool:
    if reason.lower() in TEST_REASONS:
        return True
    customers = order_el.findall("customers/customer")
    return bool(customers) and all(
        _text(c, "name").upper() == "STAFF" and _text(c, "surname").upper() == "STAFF"
        for c in customers)


def _people_key(order_el) -> str:
    """A short hash of the tourist list, so two orders can be compared for
    «same people» without keeping a single name (store-minimum)."""
    names = sorted(f"{_text(c, 'surname').upper()} {_text(c, 'name').upper()}"
                   for c in order_el.findall("customers/customer"))
    if not names:
        return ""
    return hashlib.sha256("|".join(names).encode()).hexdigest()[:16]


def _created_at(order_el, tz: ZoneInfo) -> datetime | None:
    d = _date(_text(order_el, "create-date"))
    if d is None:
        return None
    try:
        t = datetime.strptime(_text(order_el, "create-hour") or "00:00:00",
                              "%H:%M:%S").time()
    except ValueError:
        t = datetime.min.time()
    return datetime.combine(d, t, tzinfo=tz)


def parse_feed(xml_text: str) -> list[FeedOrder]:
    tz = ZoneInfo(settings.tz_name)
    out: list[FeedOrder] = []
    for o in ElementTree.fromstring(xml_text).findall("order"):
        hotel_el, agency_el = o.find("hotel"), o.find("agency")
        transport_el, costs_el = o.find("transport"), o.find("costs")
        reason_el = o.find("cancel-reason")
        reason = _text(reason_el, "reason") if reason_el is not None else ""
        status = _text(o, "status")
        check_in = _date(_text(hotel_el, "from-date")) if hotel_el is not None else None
        if check_in is None and transport_el is not None:
            check_in = _date(_text(transport_el, "depart-departure-date"))
        country = _text(hotel_el, "country") if hotel_el is not None else ""
        city = ""
        if hotel_el is not None:
            city = _text(hotel_el, "city-name") or _text(hotel_el, "region-name")
        if not country and transport_el is not None:
            charter = _text(transport_el, "depart-charter-name")
            country = f"✈️ {charter[:30]}" if charter else ""
        cancelled_on = None
        if travelon._is_cancelled(status):
            cancelled_on = (_date(_text(o, "cancellation-date"))
                            or _date(_text(o, "update-date")))
        out.append(FeedOrder(
            order_no=_text(o, "order") or _text(o, "id"),
            status=status,
            created_at=_created_at(o, tz),
            agency_id=_text(agency_el, "id") if agency_el is not None else "",
            agency=_text(agency_el, "name") if agency_el is not None else "",
            country=country, city=city,
            hotel=_text(hotel_el, "hotel-name") if hotel_el is not None else "",
            check_in=check_in,
            tourists=len(o.findall("customers/customer")),
            gross=_num(_text(costs_el, "gross-cost")) if costs_el is not None else None,
            currency=_text(o, "valute"),
            is_test=_is_test(o, reason),
            rebooked_from=_text(o, "rebooked-from"),
            rebooked_to=_text(o, "rebooked-to"),
            cancelled_on=cancelled_on,
            cancel_reason=reason,
            people_key=_people_key(o),
        ))
    return out


# ---------- fetching ----------

def report_wallclock(dt: datetime) -> datetime:
    """An aware datetime → the naive UTC+3 wall clock the report expects."""
    return (dt.astimezone(timezone.utc) + _REPORT_OFFSET).replace(
        tzinfo=None, microsecond=0)


async def _get(url: str) -> list[FeedOrder]:
    async with travelon._semaphore():
        async with httpx.AsyncClient(timeout=travelon._TIMEOUT,
                                     headers=travelon._UA) as client:
            resp = await client.get(url)
    if resp.status_code != 200:
        logger.error("bookings: report returned %s", resp.status_code)  # no URL: token
        raise RuntimeError(f"report status {resp.status_code}")
    return parse_feed(resp.text)


async def fetch_created(start: datetime, end: datetime) -> list[FeedOrder]:
    """Orders CREATED in [start, end), every status."""
    url = travelon._period_url(report_wallclock(start - _PAD),
                               report_wallclock(end + _PAD), status="all")
    return [o for o in await _get(url)
            if o.created_at is not None and start <= o.created_at < end]


async def fetch_changed(start: datetime, end: datetime, *,
                        status: str = "all") -> list[FeedOrder]:
    """Orders whose LATEST change falls in [start, end] (only_updated)."""
    url = travelon._period_url(report_wallclock(start), report_wallclock(end),
                               status=status, only_updated=True)
    return await _get(url)


# ---------- digest ----------

def _short_agency(name: str) -> str:
    n = name.strip()
    for prefix in ("ФОП ", "ТОВ ", "ПП "):
        if n.upper().startswith(prefix):
            n = n[len(prefix):]
    n = n.replace(" - Мережа TAT", " (TAT)").replace("- Мережа TAT", "(TAT)")
    return n if len(n) <= 40 else n[:39] + "…"


def _delta(cur: int, prev: int | None) -> str:
    if not prev:
        return ""
    pct = round((cur - prev) * 100 / prev)
    return f" ({'+' if pct >= 0 else ''}{pct}% до тижня тому)"


NO_HOTEL = "без готелю"   # insurance-only, ticket-only without a charter name


def possible_duplicates(orders: list[FeedOrder]) -> list[list[FeedOrder]]:
    """The SAME tourists in the same hotel and check-in, more than once.
    Same price alone is not a duplicate: on 23.11.2026 a TAT agency booked
    eight families into one hotel and date — identical prices, different
    people (checked against the CRM)."""
    groups: dict[tuple, list[FeedOrder]] = {}
    for o in orders:
        if not (o.people_key and o.hotel and o.check_in):
            continue
        groups.setdefault((o.people_key, o.hotel.upper(), o.check_in),
                          []).append(o)
    return [g for g in groups.values() if len(g) > 1]


SERIES_MIN = 3


def series(orders: list[FeedOrder]) -> list[list[FeedOrder]]:
    """One agency booking several different families into the same hotel
    and check-in — a group sale, worth seeing as one line."""
    groups: dict[tuple, list[FeedOrder]] = {}
    for o in orders:
        if o.agency_id and o.hotel and o.check_in:
            groups.setdefault((o.agency_id, o.hotel.upper(), o.check_in),
                              []).append(o)
    out = [g for g in groups.values() if len(g) >= SERIES_MIN]
    return sorted(out, key=lambda g: -len(g))


def sales(orders: list[FeedOrder]) -> list[FeedOrder]:
    """New sales: no tests, no rebooking copies. Any current status — the
    week-ago window is counted the same way, so the comparison is fair."""
    return [o for o in orders if not o.is_test and not o.is_rebooking]


def lost(orders: list[FeedOrder], start: datetime, end: datetime) -> list[FeedOrder]:
    """Real cancellations in the window: no tests, no rebookings."""
    d0, d1 = start.date(), end.date()
    return [o for o in orders
            if o.cancelled and not o.is_test and not o.lost_by_rebooking
            and o.cancelled_on is not None and d0 <= o.cancelled_on <= d1]


def _countries(new: list[FeedOrder]) -> str:
    by_country: dict[str, int] = {}
    for o in new:
        k = o.country or NO_HOTEL
        by_country[k] = by_country.get(k, 0) + 1
    top = sorted(by_country.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = [kv for kv in top if kv[0] != NO_HOTEL][:5]
    rest = sum(n for k, n in top if k != NO_HOTEL and (k, n) not in shown)
    parts = [f"{html.escape(c)} {n}" for c, n in shown]
    if rest:
        parts.append(f"решта {rest}")
    if by_country.get(NO_HOTEL):
        parts.append(f"{NO_HOTEL} {by_country[NO_HOTEL]}")
    return "🌍 " + " · ".join(parts)


def _money(orders: list[FeedOrder]) -> str:
    return travelon._sum_by_currency((o.gross, o.currency) for o in orders)


def build_digest(cur: list[FeedOrder], prev: list[FeedOrder] | None,
                 cancelled: list[FeedOrder] | None, *, title: str,
                 new_agencies: list[FeedOrder] | None = None,
                 day: list[FeedOrder] | None = None,
                 day_prev: list[FeedOrder] | None = None) -> str:
    """`cur`/`prev` = the window and the same window a week earlier;
    `day`/`day_prev` (optional) = the day so far, for the running total."""
    new = sales(cur)
    tests = sum(1 for o in cur if o.is_test)
    rebooks = sum(1 for o in cur if o.is_rebooking and not o.is_test)
    lines = [f"📊 <b>{html.escape(title)}</b>"]
    if not new:
        lines.append("Нових заявок не було."
                     + (f" Тиждень тому за цей час: {len(sales(prev))}."
                        if prev else ""))
    else:
        confirmed = sum(1 for o in new if travelon._is_confirmed(o.status))
        lines.append(
            f"🆕 <b>Нових заявок: {len(new)}</b>"
            f"{_delta(len(new), len(sales(prev)) if prev is not None else None)}"
            f" · туристів {sum(o.tourists for o in new)}")
        lines.append(f"💶 Сума: {_money(new)} · підтверджено {confirmed} з {len(new)}")
        lines.append(_countries(new))

        by_agency: dict[str, list[FeedOrder]] = {}
        for o in new:
            by_agency.setdefault(o.agency_id or o.agency, []).append(o)
        top_ag = sorted(by_agency.values(), key=lambda g: (-len(g), g[0].agency))[:3]
        lines.append(f"🏢 Агенцій: {len(by_agency)} · топ: " + "; ".join(
            f"{html.escape(_short_agency(g[0].agency) or '—')} {len(g)}"
            for g in top_ag))

        if new_agencies:
            names = ", ".join(html.escape(_short_agency(o.agency))
                              for o in new_agencies[:5])
            more = len(new_agencies) - 5
            lines.append(f"🌱 Нові агенції: {names}"
                         + (f" і ще {more}" if more > 0 else ""))

        early = soon = 0
        for o in new:
            if o.check_in and o.created_at:
                days = (o.check_in - o.created_at.date()).days
                early += days > 90
                soon += days <= 14
        lines.append(f"📅 Раннє бронювання (виїзд за 90+ днів): {early} · "
                     f"виїзд протягом 14 днів: {soon}")

        big = max((o for o in new if o.gross), key=lambda o: o.approx_eur or 0,
                  default=None)
        if big is not None:
            lines.append(
                f"🏆 Найбільша: №{big.order_no} · "
                f"{travelon._fmt_money(big.gross, big.currency)} · "
                f"{html.escape(big.where)} · {big.tourists} тур. · "
                f"{html.escape(_short_agency(big.agency))}")

        for g in series(new)[:3]:
            o = g[0]
            lines.append(
                f"👥 Серія: {html.escape(_short_agency(o.agency))} — {len(g)} "
                f"заявок, {html.escape(o.hotel)}, заїзд {o.check_in:%d.%m}, "
                f"{sum(x.tourists for x in g)} тур.")
        for g in possible_duplicates(new)[:3]:
            o = g[0]
            lines.append(
                "⚠️ Можливий дубль (ті самі туристи): "
                + ", ".join(f"№{x.order_no}" for x in g)
                + f" — {html.escape(o.hotel)}, {o.check_in:%d.%m}")

    if cancelled is not None:
        if cancelled:
            reasons: dict[str, int] = {}
            for o in cancelled:
                r = o.cancel_reason or "без причини"
                reasons[r] = reasons.get(r, 0) + 1
            money = _money(cancelled)
            lines.append(
                f"❌ Анульовано: {len(cancelled)}"
                + (f" · {money}" if money != "—" else "")
                + " · " + ", ".join(f"{html.escape(r)} {n}" for r, n in
                                    sorted(reasons.items(), key=lambda kv: -kv[1])[:3]))
        else:
            lines.append("❌ Анульовано: 0")
    else:
        lines.append("❌ Ануляції зараз не вдалося прочитати.")

    if day is not None:
        dn = sales(day)
        lines.append(
            f"\n📈 <b>За день: {len(dn)} заявок</b>"
            f"{_delta(len(dn), len(sales(day_prev)) if day_prev is not None else None)}"
            f" · туристів {sum(o.tourists for o in dn)} · {_money(dn)}")

    extra = []
    if rebooks:
        extra.append(f"перебронювань {rebooks}")
    if tests:
        extra.append(f"тестових {tests}")
    if extra:
        lines.append("<i>Не враховано: " + ", ".join(extra) + "</i>")
    return "\n".join(lines)


async def digest_text(start: datetime, end: datetime, *, title: str,
                      db=None, day_start: datetime | None = None) -> str | None:
    """None when not configured. Never raises: a failed report is said.
    With `day_start` earlier than `start`, one fetch covers the whole day and
    the message ends with the running day total."""
    if not travelon.configured():
        return None
    week = timedelta(days=7)
    with_day = day_start is not None and day_start < start
    first = day_start if with_day else start
    cur_all, prev_all, changed = await asyncio.gather(
        fetch_created(first, end), fetch_created(first - week, end - week),
        fetch_changed(start, end, status="cancelled"), return_exceptions=True)
    if isinstance(cur_all, BaseException):
        logger.error("bookings digest failed: %s", type(cur_all).__name__)
        return "📊 Дайджест заявок: звіт TravelON зараз недоступний."
    prev_all = None if isinstance(prev_all, BaseException) else prev_all

    def in_window(orders, lo):
        return [o for o in orders if o.created_at and o.created_at >= lo]
    cur = in_window(cur_all, start)
    prev = in_window(prev_all, start - week) if prev_all is not None else None
    new_ag = None
    if db is not None:
        try:
            new_ag = await _new_agencies(db, sales(cur), before=start)
        except Exception:
            logger.exception("bookings: new-agency check failed")
    return build_digest(
        cur, prev,
        None if isinstance(changed, BaseException) else lost(changed, start, end),
        title=title, new_agencies=new_ag,
        day=cur_all if with_day else None,
        day_prev=prev_all if with_day else None)


def parse_slots(raw: str) -> list[tuple[int, int]]:
    out = []
    for t in (x.strip() for x in raw.split(",") if x.strip()):
        try:
            hh, mm = (int(x) for x in t.split(":"))
        except ValueError:
            continue
        out.append((hh % 24, mm))
    return sorted(set(out))


def slot_window(end: datetime, raw_slots: str) -> tuple[datetime, datetime]:
    """(start, day_start) for a digest ending at `end`: start = the previous
    slot (wrapping over midnight), day_start = 00:00 of the day the window
    belongs to — the 00:00 digest closes the PREVIOUS day."""
    slots = parse_slots(raw_slots) or [(end.hour, end.minute)]
    candidates = []
    for back in (0, 1):
        d = end.date() - timedelta(days=back)
        for hh, mm in slots:
            t = end.replace(year=d.year, month=d.month, day=d.day, hour=hh,
                            minute=mm, second=0, microsecond=0)
            if t < end - timedelta(minutes=1):
                candidates.append(t)
    start = max(candidates) if candidates else end - timedelta(hours=24)
    day = (end - timedelta(minutes=1))
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return max(start, end - timedelta(hours=24)), day_start


def window_title(start: datetime, end: datetime) -> str:
    """«Заявки 23.11, 12:00–15:00»; a window ending at midnight reads 24:00
    of the day it closes."""
    last = end - timedelta(minutes=1)
    to = "24:00" if (end.hour, end.minute) == (0, 0) and end > start else f"{end:%H:%M}"
    if start.date() == last.date():
        return f"Заявки {start:%d.%m}, {start:%H:%M}–{to}"
    return f"Заявки за добу: {start:%d.%m %H:%M} – {end:%d.%m %H:%M}"


# ---------- known agencies (for «перша заявка агенції») ----------

async def _load(db, key: str) -> dict:
    from app.models import AppState
    st = await db.get(AppState, key)
    try:
        return json.loads(st.value) if st and st.value else {}
    except ValueError:
        return {}


async def _save(db, key: str, data: dict) -> None:
    from app.models import AppState
    st = await db.get(AppState, key) or AppState(key=key)
    st.value = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    db.add(st)
    await db.commit()


async def _new_agencies(db, orders: list[FeedOrder], *,
                        before: datetime) -> list[FeedOrder] | None:
    """Agencies in `orders` not seen before `before`. None until the
    history is seeded — a guess would call every agency «new»."""
    state = await _load(db, _STATE_AGENCIES)
    if not state.get("seeded"):
        return None
    ids = state.get("ids", {})
    cutoff = before.date().isoformat()
    seen, out = set(), []
    for o in sorted(orders, key=lambda x: x.created_at or before):
        if not o.agency_id or o.agency_id in seen:
            continue
        seen.add(o.agency_id)
        first = ids.get(o.agency_id)
        if first is None or first >= cutoff:
            out.append(o)
    return out


async def _remember_agencies(db, orders: list[FeedOrder]) -> None:
    state = await _load(db, _STATE_AGENCIES)
    ids = state.setdefault("ids", {})
    changed = False
    for o in orders:
        if o.agency_id and o.created_at:
            d = o.created_at.date().isoformat()
            if o.agency_id not in ids or d < ids[o.agency_id]:
                ids[o.agency_id] = d       # first time seen
                changed = True
    if changed:
        await _save(db, _STATE_AGENCIES, state)


async def seed_agencies(days: int = AGENCY_SEED_DAYS) -> int:
    """One-time history scan so «new agency» means something on day one.
    Runs in the background (a few minutes); idempotent — ids are a union."""
    global _seeding
    if _seeding or not travelon.configured():
        return 0
    _seeding = True
    try:
        tz = ZoneInfo(settings.tz_name)
        today = datetime.now(tz).date()

        async def one(d: date) -> list[FeedOrder]:
            try:
                return await _get(travelon._period_url(d, d, status="all"))
            except Exception:
                return []
        chunks = await asyncio.gather(
            *(one(today - timedelta(days=i)) for i in range(1, days + 1)))
        from app import db as database
        async with database.session() as db:
            await _remember_agencies(db, [o for c in chunks for o in c])
            state = await _load(db, _STATE_AGENCIES)
            failed = sum(1 for c in chunks if not c)
            # mostly-empty history means the report was down: try again later
            state["seeded"] = failed < days // 2
            await _save(db, _STATE_AGENCIES, state)
            return len(state.get("ids", {}))
    finally:
        _seeding = False


# ---------- alerts ----------

def in_alert_hours(now_local: datetime) -> bool:
    try:
        a, b = (x.strip() for x in settings.bookings_alert_hours.split("-"))
        start = datetime.strptime(a, "%H:%M").time()
        end = datetime.strptime(b, "%H:%M").time()
    except ValueError:
        return False
    return start <= now_local.time() < end


def alert_reasons(o: FeedOrder, *, now: datetime, window_start: datetime,
                  known_agencies: dict | None) -> list[tuple[str, str]]:
    """(kind, label) pairs. kind 'new' = once per order on creation, 'cancel'
    = once per order on cancellation. Pure — tested directly."""
    if o.is_test:
        return []
    out: list[tuple[str, str]] = []
    eur = o.approx_eur
    created_now = (o.created_at is not None and o.created_at >= window_start
                   and not o.is_rebooking)
    if created_now and not o.cancelled:
        if eur is not None and eur >= settings.bookings_alert_big_eur:
            out.append(("new", "💶 велика сума"))
        if o.tourists >= settings.bookings_alert_group:
            out.append(("new", f"👥 група {o.tourists} тур."))
        if o.check_in is not None:
            days = (o.check_in - now.date()).days
            if 0 <= days <= settings.bookings_alert_lastminute_days:
                out.append(("new", "⏱ виїзд " + ("сьогодні" if days == 0 else
                            "завтра" if days == 1 else f"через {days} дн.")))
        if (known_agencies is not None and o.agency_id
                and o.agency_id not in known_agencies):
            out.append(("new", "🌱 перша заявка агенції"))
    if (o.cancelled and not o.lost_by_rebooking and eur is not None
            and eur >= settings.bookings_alert_cancel_eur
            and o.cancelled_on == now.date()):
        out.append(("cancel", "❌ анульовано" + (
            f": {o.cancel_reason}" if o.cancel_reason else "")))
    return out


def alert_line(o: FeedOrder, labels: list[str]) -> str:
    when = f" · виїзд {o.check_in:%d.%m}" if o.check_in else ""
    return (f"• №{o.order_no} · {travelon._fmt_money(o.gross, o.currency)} · "
            f"{html.escape(o.where)}{when} · {o.tourists} тур. · "
            f"{html.escape(_short_agency(o.agency) or '—')}\n   "
            + html.escape(" · ".join(labels)))


async def poll_alerts(db, now: datetime | None = None) -> str | None:
    """One message with the orders worth a look since the last poll, or None.
    Kyiv quiet hours are respected by the caller-independent hour gate."""
    if not travelon.configured():
        return None
    tz = ZoneInfo(settings.tz_name)
    now = (now or datetime.now(tz)).astimezone(tz)
    if not in_alert_hours(now):
        return None
    state = await _load(db, _STATE_ALERTS)
    try:
        last = datetime.fromisoformat(state["last"]).astimezone(tz)
    except (KeyError, ValueError, TypeError):
        last = None
    # first run: only what is fresh — never replay a day of history
    start = (now - timedelta(minutes=30) if last is None
             else max(last - timedelta(minutes=30), now - timedelta(hours=24)))
    orders = await fetch_changed(start, now)   # raises → scheduler retries

    agencies = await _load(db, _STATE_AGENCIES)
    known = agencies.get("ids", {}) if agencies.get("seeded") else None
    if not agencies.get("seeded") and not _seeding:
        asyncio.create_task(seed_agencies())

    sent: dict = state.setdefault("sent", {})
    lines: list[str] = []
    for o in sorted(orders, key=lambda x: x.order_no):
        reasons = alert_reasons(o, now=now, window_start=start,
                                known_agencies=known)
        if known is not None and o.agency_id and not o.is_test:
            known = {**known, o.agency_id: now.date().isoformat()}
        fresh: list[str] = []
        for kind in ("new", "cancel"):
            labels = [lab for k, lab in reasons if k == kind]
            key = f"{o.order_no}:{kind}"
            if labels and key not in sent:
                sent[key] = now.date().isoformat()
                fresh += labels
        if fresh:
            lines.append(alert_line(o, fresh))
    await _remember_agencies(db, [o for o in orders if not o.is_test])

    horizon = (now.date() - timedelta(days=14)).isoformat()
    state["sent"] = {k: v for k, v in sent.items() if v >= horizon}
    state["last"] = now.isoformat()
    await _save(db, _STATE_ALERTS, state)
    if not lines:
        return None
    head = "🔔 <b>Заявки, на які варто глянути</b>"
    if len(lines) > 12:
        lines = lines[:12] + [f"… і ще {len(lines) - 12} — /bookings"]
    return head + "\n" + "\n".join(lines)
