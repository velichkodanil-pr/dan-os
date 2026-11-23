"""Home power status (R8): Deye Cloud «grid lost / restored» mails -> one
pinned status card in the owner's chat, plus a silent push on a settled change.

Deterministic, no model: mails are untrusted data, parsed by regex for three
things only — station name, device SN, on/off. Personal domain: only Google
accounts assigned to `personal` are read (no cross-domain fallback).

State lives in app_state["power_state"] as JSON (no migration):
  stations  {sn: {name, status on|off, since_ms}}
  history   [[sn, off_start_ms, off_end_ms|null], ...]   (35 days)
  seen      recent Gmail message ids (dedupe)
  hw_ms     high-water mark of processed internalDate
  polled_ms last successful poll
  notified  overall state the owner was last pushed about (+ notified_since_ms)
  pin       {message_id} + pin_text
"""
import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from app.config import settings
from app.models import AppState

logger = logging.getLogger(__name__)

STATE_KEY = "power_state"
HISTORY_DAYS = 35
SEEN_KEEP = 300
STALE_MINUTES = 15
DAY_MS = 86_400_000

_ON_MARKERS = ("повторно", "відновл", "восстановл", "reconnect", "restor")
_OFF_MARKERS = ("відключ", "отключ", "disconnect", "off-grid", "offline from the grid")
_STATION_RE = re.compile(
    r"(?:станц(?:ії|ии|ія|ия)|station)\s+(.+?)\s+(?:пристр|устр|device|повторно|"
    r"наразі|сейчас|тепер|відключ|отключ|підключ|подключ|is\b|has\b|was\b)",
    re.IGNORECASE)
_STATION_FALLBACK_RE = re.compile(r"(?:станц(?:ії|ии|ія|ия)|station)\s+([^\s,.;:]+)",
                                  re.IGNORECASE)
_SN_RE = re.compile(r"\b(\d{8,14})\b")


@dataclass(frozen=True)
class PowerEvent:
    msg_id: str
    ts_ms: int
    sn: str
    station: str
    status: str  # "on" | "off"


class Sender(Protocol):
    async def send(self, text: str, silent: bool = True) -> int: ...
    async def edit(self, message_id: int, text: str) -> bool: ...
    async def pin(self, message_id: int) -> None: ...


# ---------- parsing (pure) ----------

def parse_event(msg_id: str, ts_ms: int, subject: str, snippet: str) -> PowerEvent | None:
    """One Deye mail -> event, or None for anything else (alarms, codes...)."""
    text = f"{subject} {snippet}".replace("​", "").replace("\xa0", " ")
    low = text.lower()
    if any(m in low for m in _ON_MARKERS):
        status = "on"
    elif any(m in low for m in _OFF_MARKERS):
        status = "off"
    else:
        return None
    m = _STATION_RE.search(snippet) or _STATION_FALLBACK_RE.search(snippet)
    station = (m.group(1).strip(" ,.;:") if m else "").strip()
    sn_m = _SN_RE.search(snippet)
    sn = sn_m.group(1) if sn_m else ""
    if not station and not sn:
        return None
    station = station[:24] or f"SN {sn[-4:]}"
    return PowerEvent(msg_id=msg_id, ts_ms=int(ts_ms), sn=sn or station.casefold(),
                      station=station, status=status)


# ---------- state machine (pure) ----------

def empty_state() -> dict:
    return {"stations": {}, "history": [], "seen": [], "hw_ms": 0,
            "polled_ms": 0, "notified": None, "notified_since_ms": 0,
            "pin": None, "pin_text": "", "last_try_ms": 0}


def apply_events(state: dict, events: list[PowerEvent]) -> bool:
    """Fold events in time order. Returns True if any station changed status.
    Idempotent: a message id is applied once; a repeated status is a no-op;
    an event older than the station's current status is ignored."""
    changed = False
    seen = set(state["seen"])
    for ev in sorted(events, key=lambda e: e.ts_ms):
        if ev.msg_id in seen:
            continue
        seen.add(ev.msg_id)
        state["seen"].append(ev.msg_id)
        state["hw_ms"] = max(state["hw_ms"], ev.ts_ms)
        st = state["stations"].get(ev.sn)
        if st is not None:
            st["name"] = ev.station
            if ev.ts_ms < st["since_ms"] or st["status"] == ev.status:
                continue
        state["stations"][ev.sn] = {"name": ev.station, "status": ev.status,
                                    "since_ms": ev.ts_ms}
        changed = True
        hist = state["history"]
        open_iv = next((iv for iv in reversed(hist)
                        if iv[0] == ev.sn and iv[2] is None), None)
        if ev.status == "off" and open_iv is None:
            hist.append([ev.sn, ev.ts_ms, None])
        elif ev.status == "on" and open_iv is not None:
            open_iv[2] = ev.ts_ms
    state["seen"] = state["seen"][-SEEN_KEEP:]
    return changed


def grid_events(state: dict, presence: dict[str, dict], now_ms: int) -> list[PowerEvent]:
    """Inverter readings that contradict the current station status → events
    stamped with the reading time. Offline inverters and unclear voltages are
    ignored (no news is not «grid lost»)."""
    out = []
    for sn, rec in (presence or {}).items():
        st = state["stations"].get(sn)
        if st is None or rec.get("grid") is None or rec.get("state") == 3:
            continue
        status = "on" if rec["grid"] else "off"
        if status == st["status"]:
            continue
        ts = int((rec.get("ts") or now_ms / 1000) * 1000)
        ts = min(max(ts, st["since_ms"] + 1), now_ms)
        out.append(PowerEvent(msg_id=f"api:{sn}:{ts}", ts_ms=ts, sn=sn,
                              station=st["name"], status=status))
    return out


def prune(state: dict, now_ms: int) -> None:
    cutoff = now_ms - HISTORY_DAYS * DAY_MS
    state["history"] = [iv for iv in state["history"]
                        if iv[2] is None or iv[2] >= cutoff]


def overall(state: dict) -> tuple[str, int]:
    """("on"|"off"|"partial"|"unknown", since_ms of that overall state)."""
    sts = list(state["stations"].values())
    if not sts:
        return "unknown", 0
    statuses = {s["status"] for s in sts}
    if statuses == {"on"}:
        return "on", max(s["since_ms"] for s in sts)
    if statuses == {"off"}:
        return "off", min(s["since_ms"] for s in sts)
    return "partial", max(s["since_ms"] for s in sts)


def off_minutes(state: dict, start_ms: int, end_ms: int) -> tuple[int, int]:
    """(minutes without power, outage count) within [start, end): union of
    every station's off-intervals — «хоча б одна станція без мережі»."""
    ivs = sorted((max(iv[1], start_ms), min(iv[2] or end_ms, end_ms))
                 for iv in state["history"])
    ivs = [(a, b) for a, b in ivs if b > a]
    total, count, cur_a, cur_b = 0, 0, None, None
    for a, b in ivs:
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b, count = a, b, count + 1
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return total // 60_000, count


def decide_push(state: dict, now_ms: int) -> str | None:
    """Overall state to push about, or None. First observation never pushes;
    a change pushes only once it has held for the debounce window."""
    ov, _ = overall(state)
    if ov == "unknown":
        return None
    if state["notified"] is None:
        state["notified"], state["notified_since_ms"] = ov, overall(state)[1]
        return None
    if ov == state["notified"]:
        return None
    last_change = max(s["since_ms"] for s in state["stations"].values())
    if now_ms - last_change < settings.power_debounce_minutes * 60_000:
        return None
    return ov


# ---------- rendering (pure) ----------

def _tz() -> ZoneInfo:
    return ZoneInfo(settings.tz_name)


def _at(ms: int, now_ms: int) -> str:
    t = datetime.fromtimestamp(ms / 1000, _tz())
    n = datetime.fromtimestamp(now_ms / 1000, _tz())
    return t.strftime("%H:%M") if t.date() == n.date() else t.strftime("%d.%m %H:%M")


def dur(minutes: int) -> str:
    if minutes < 1:
        return "щойно"
    d, rem = divmod(minutes, 1440)
    h, m = divmod(rem, 60)
    parts = ([f"{d} д"] if d else []) + ([f"{h} год"] if h else []) + \
            ([f"{m} хв"] if m and not d else [])
    return " ".join(parts)


def _kw(w: float | None) -> str:
    return "—" if w is None else f"{w / 1000:.1f} кВт"


def _day_start_ms(now_ms: int) -> int:
    n = datetime.fromtimestamp(now_ms / 1000, _tz())
    return int(n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


HEAD = {"on": "🟢 <b>ЕЛЕКТРО Є</b>", "off": "🔴 <b>ЕЛЕКТРО НЕМА</b>",
        "partial": "🟡 <b>ЧАСТКОВО</b>", "unknown": "⚪ <b>СТАН НЕВІДОМИЙ</b>"}


def render_card(state: dict, now_ms: int, live: dict | None = None,
                week: bool = False) -> str:
    ov, since = overall(state)
    if ov == "unknown":
        lines = [HEAD[ov], "Ще не було жодного листа Deye про мережу — або пошта домену 🏠 Особисте не підключена (/accounts)."]
    else:
        mins = (now_ms - since) // 60_000
        head = HEAD[ov] + (f" · {dur(mins)} (з {_at(since, now_ms)})" if ov == "off"
                           else f" · з {_at(since, now_ms)}")
        lines = [head]
        cells = []
        for st in sorted(state["stations"].values(), key=lambda s: s["name"]):
            mark = "✅" if st["status"] == "on" else "❌"
            cells.append(f"{html.escape(st['name'])} {mark} з {_at(st['since_ms'], now_ms)}")
        lines.append(" · ".join(cells))
        if ov == "off":
            lines.append("Живимось від батарей.")
    from app.core import energy
    for st in sorted(state["stations"].values(), key=lambda s: s["name"]):
        lv = (live or {}).get(st["name"].casefold())
        if lv is None:
            continue
        stale = bool(lv.updated_ts and now_ms / 1000 - lv.updated_ts
                     > settings.deye_offline_minutes * 60)
        if lv.online is False or stale:
            since_txt = (f" з {_at(int(lv.updated_ts * 1000), now_ms)}"
                         if lv.updated_ts else "")
            lines.append(f"⚪ {html.escape(st['name'])}: інвертор не на зв'язку{since_txt}"
                         " — статус може бути застарілим")
            continue
        lines.append(energy.battery_line(st["name"], lv, st["status"] == "off"))
    if ov != "unknown":
        t_min, t_cnt = off_minutes(state, _day_start_ms(now_ms), now_ms)
        lines.append(f"Сьогодні без світла: {dur(t_min) if t_min else '0'}"
                     + (f" ({t_cnt} р.)" if t_cnt else ""))
        if week:
            w_min, w_cnt = off_minutes(state, _day_start_ms(now_ms) - 6 * DAY_MS, now_ms)
            lines.append(f"За 7 днів: {dur(w_min) if w_min else '0'}"
                         + (f" ({w_cnt} р.)" if w_cnt else ""))
    if state["polled_ms"] and now_ms - state["polled_ms"] > STALE_MINUTES * 60_000:
        lines.append(f"⚠️ пошту не вдалося перевірити з {_at(state['polled_ms'], now_ms)}")
    elif state["polled_ms"]:
        lines.append(f"<i>перевірено {_at(state['polled_ms'], now_ms)}</i>")
    return "\n".join(lines)


def push_text(state: dict, ov: str, now_ms: int) -> str:
    _, since = overall(state)
    names = ", ".join(html.escape(s["name"]) for s in state["stations"].values())
    if ov == "off":
        return f"🔴 Світло зникло о {_at(since, now_ms)} — {names}. Живимось від батарей."
    if ov == "on":
        prev = state.get("notified_since_ms") or 0
        gone = f" (не було {dur((since - prev) // 60_000)})" if (
            state.get("notified") == "off" and prev and since > prev) else ""
        return f"🟢 Світло є з {_at(since, now_ms)}{gone}."
    cells = [f"{html.escape(s['name'])} {'є' if s['status'] == 'on' else 'нема'}"
             for s in state["stations"].values()]
    return "🟡 Світло частково: " + " · ".join(cells) + "."


# ---------- persistence ----------

async def load(db) -> dict:
    row = await db.get(AppState, STATE_KEY)
    if row is None or not row.value:
        return empty_state()
    try:
        return {**empty_state(), **json.loads(row.value)}
    except ValueError:
        logger.warning("power_state unreadable — starting fresh")
        return empty_state()


async def save(db, state: dict) -> None:
    row = await db.get(AppState, STATE_KEY) or AppState(key=STATE_KEY)
    row.value = json.dumps(state, ensure_ascii=False)
    db.add(row)
    await db.commit()


# ---------- I/O ----------

def _query(state: dict) -> str:
    base = f"from:{settings.power_mail_from}"
    if state["hw_ms"]:
        return f"{base} after:{max(0, state['hw_ms'] // 1000 - 600)}"
    return f"{base} newer_than:4d"


async def fetch_events(db, owner: int, state: dict) -> list[PowerEvent] | None:
    """New events from every PERSONAL Google account; None if none readable."""
    from app.core import google_client
    from app.core.domains import Domain
    accounts = await google_client.get_accounts(db, owner, Domain.PERSONAL)
    if not accounts:
        return None
    events: list[PowerEvent] = []
    ok = False
    for cred in accounts:
        try:
            access = await google_client.access_for(db, cred)
            if not access:
                continue
            msgs = await google_client.gmail_messages(access, _query(state), limit=50)
            ok = True
        except Exception:
            logger.exception("power: gmail read failed for one account")
            continue
        for m in msgs:
            ev = parse_event(m["id"], m["internal_ms"], m["subject"], m["snippet"])
            if ev:
                events.append(ev)
    return events if ok else None


async def tick(db, sender: Sender, now_ms: int | None = None,
               force: bool = False) -> dict:
    """One scheduler step. Throttled to POWER_POLL_SECONDS unless forced."""
    owner = settings.owner_telegram_id
    if not owner or not settings.power_mail_from:
        return {}
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    state = await load(db)
    if not force and now_ms - state.get("last_try_ms", 0) < settings.power_poll_seconds * 1000:
        return state
    state["last_try_ms"] = now_ms
    events = await fetch_events(db, owner, state)
    if events is not None:
        n_new = len([e for e in events if e.msg_id not in set(state["seen"])])
        apply_events(state, events)
        state["polled_ms"] = now_ms
        if n_new:
            logger.info("power: %d new event(s), overall=%s", n_new, overall(state)[0])
    elif not state.get("warned_no_mail"):
        logger.warning("power: no readable PERSONAL Gmail account — card not updated")
        state["warned_no_mail"] = True
    prune(state, now_ms)
    if not state["stations"] and events is None:
        await save(db, state)   # nothing to show yet and no mailbox — stay quiet
        return state

    from app.core import deye_api
    live = await deye_api.live() if deye_api.enabled() else {}
    # R8.2.1: the inverter's own grid voltage is the primary signal — Deye's
    # mails can be late or missing (23.11: no «restored» mail for Дом). An
    # API reading that differs from the mail state becomes a synthetic event,
    # so history, the card and the push all follow it.
    sns = [sn for sn in state["stations"] if sn.isdigit()]
    if sns and deye_api.enabled():
        try:
            api_events = grid_events(state, await deye_api.grid_presence(sns), now_ms)
            if api_events:
                apply_events(state, api_events)
                for e in api_events:
                    logger.info("power: grid %s for %s from inverter voltage", e.status, e.station)
        except Exception:
            logger.exception("power: grid presence check failed")
    if deye_api.enabled() and bool(live) != state.get("deye_ok"):
        logger.info("power: deye api %s (%d station(s))",
                    "ok" if live else "unavailable", len(live))
        state["deye_ok"] = bool(live)
    text = render_card(state, now_ms, live)
    await _sync_pin(state, sender, text)

    target = decide_push(state, now_ms)
    if target is not None:
        logger.info("power: overall changed to %s — push", target)
        if settings.power_push:
            try:
                await sender.send(push_text(state, target, now_ms), silent=True)
            except Exception:
                logger.exception("power: push failed")
        state["notified"] = target
        state["notified_since_ms"] = overall(state)[1]

    # R8.2: energy features + a pending pre-charge restore. Isolated — a
    # failure here never costs the card or the grid push above.
    async def _send(text: str, silent: bool = True) -> None:
        await sender.send(text, silent=silent)
    if live:
        grid_status = {st["name"].casefold(): (st["status"], st["since_ms"])
                       for st in state["stations"].values()}
        sn_by_name = {st["name"].casefold(): sn for sn, st in state["stations"].items()
                      if sn.isdigit()}
        try:
            from app.core import energy
            await energy.run(state, now_ms, live, grid_status, _send, sn_by_name)
        except Exception:
            logger.exception("power: energy pass failed")
    await save(db, state)
    try:
        from app.core import precharge
        pc = await precharge.load(db)
        if pc.get("precharge"):
            await precharge.tick(pc, now_ms, _send)
            await precharge.save(db, pc)
        else:
            await db.commit()   # release the row lock
    except Exception:
        logger.exception("power: precharge pass failed")
    return state


async def _sync_pin(state: dict, sender: Sender, text: str) -> None:
    pin = state.get("pin") or {}
    mid = pin.get("message_id")
    try:
        if mid and text == state.get("pin_text"):
            return
        if mid and await sender.edit(mid, text):
            state["pin_text"] = text
            return
        mid = await sender.send(text, silent=True)
        await sender.pin(mid)
        state["pin"] = {"message_id": mid}
        state["pin_text"] = text
        logger.info("power: status card posted and pinned")
    except Exception:
        logger.exception("power: pinned card update failed")


async def status_text(db, now_ms: int | None = None) -> str:
    """For /power: the same card plus the 7-day line."""
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    state = await load(db)
    from app.core import deye_api
    live = await deye_api.live() if deye_api.enabled() else {}
    return render_card(state, now_ms, live, week=True)
