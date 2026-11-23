"""Grid pre-charge before an outage (R8.2) — the one feature that WRITES to
an inverter. Policy L3: every change is previewed and applied only on the
owner's button; the original time-of-use table is kept and restored on time.

How: Deye inverters charge from the grid inside a time-of-use slot that has
«grid charge» on and a target SOC. We read the table, switch grid charge on
(target 100 %) for the slots that overlap [now, now + hours], write it, and
put the original back at the end. Stations whose SOC is estimated from
voltage (no BMS link) are never controlled — their SOC target is meaningless.
"""
import html
import json
import logging
import secrets

from sqlalchemy import select

from app.config import settings
from app.core import energy
from app.models import AppState

logger = logging.getLogger(__name__)

STATE_KEY = "power_precharge"   # own row: short locks, no race with the card
PENDING_TTL_MS = 10 * 60_000
MAX_RESTORE_TRIES = 3
_ALLOWED = ("time", "power", "soc", "voltage", "enableGridCharge",
            "enableGeneration", "enableSell")
_OFF_WORDS = {"off", "close", "closed", "false", "0", "disable", "disabled"}


# ---------- pure ----------

def _hm(t: str) -> int | None:
    try:
        hh, mm = (int(x) for x in str(t).strip()[:5].split(":"))
    except ValueError:
        return None
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return hh * 60 + mm


def normalize(raw: list[dict]) -> list[dict] | None:
    """Deye's table -> write-ready items, or None if anything looks off
    (refusing is safer than writing a table we did not understand)."""
    out = []
    for r in raw or []:
        t = r.get("time")
        if _hm(t) is None:
            return None
        item = {k: r[k] for k in _ALLOWED if k in r and r[k] is not None}
        item["time"] = str(t).strip()[:5]
        for b in ("enableGridCharge", "enableGeneration", "enableSell"):
            if b in item:
                item[b] = item[b] in (True, 1, "1", "true", "True", "on")
        item.setdefault("enableGridCharge", False)
        item.setdefault("enableGeneration", True)
        item.setdefault("power", int(r.get("power") or 0))
        out.append(item)
    if not 2 <= len(out) <= 6:
        return None
    return sorted(out, key=lambda i: _hm(i["time"]))


def slots_for(items: list[dict], start_min: int, minutes: int) -> list[int]:
    """Indexes of slots overlapping [start, start+minutes) — minutes of day,
    the table wraps around midnight."""
    starts = [_hm(i["time"]) for i in items]

    def slot_at(m: int) -> int:
        m %= 1440
        idx = len(starts) - 1
        for k, s in enumerate(starts):
            if s <= m:
                idx = k
        return idx if starts[0] <= m else len(starts) - 1

    hit = []
    for step in range(0, max(minutes, 5), 5):
        k = slot_at(start_min + step)
        if k not in hit:
            hit.append(k)
    return hit


def plan(items: list[dict], start_min: int, hours: int) -> tuple[list[dict], list[int]]:
    new = [dict(i) for i in items]
    idx = slots_for(items, start_min, hours * 60)
    for k in idx:
        new[k]["enableGridCharge"] = True
        new[k]["soc"] = 100
    return new, idx


def describe(items: list[dict], idx: list[int]) -> str:
    rows = []
    for k in idx:
        nxt = items[(k + 1) % len(items)]["time"]
        was = items[k]
        rows.append(f"• слот {was['time']}–{nxt}: заряд від мережі "
                    f"{'✅' if was.get('enableGridCharge') else '❌'} → ✅, "
                    f"SOC {was.get('soc', '—')}% → 100%")
    return "\n".join(rows)


def controllable(stations: dict) -> dict[str, str]:
    """{station name casefold: inverter SN} that may be controlled.
    `stations` is power_state["stations"] ({sn: {name, status, since_ms}})."""
    return {st["name"].casefold(): sn for sn, st in (stations or {}).items()
            if sn.isdigit() and not energy.voltage_soc(st["name"])}


# ---------- persistence (row lock: the scheduler and a button may race) ----------

async def load(db) -> dict:
    row = (await db.execute(select(AppState).where(AppState.key == STATE_KEY)
                            .with_for_update())).scalar_one_or_none()
    if row is None or not row.value:
        return {}
    try:
        return json.loads(row.value)
    except ValueError:
        return {}


async def save(db, pc: dict) -> None:
    row = await db.get(AppState, STATE_KEY) or AppState(key=STATE_KEY)
    row.value = json.dumps(pc, ensure_ascii=False)
    db.add(row)
    await db.commit()


# ---------- flow ----------

async def preview(state: dict, stations: dict, name: str | None, hours: int,
                  now_ms: int) -> dict:
    """{'ok': bool, 'text': str, 'token': str|None}. Stores the plan as
    pending; nothing is written to the inverter here."""
    from app.core import deye_api
    if not deye_api.enabled():
        return {"ok": False, "text": "Deye API не підключено (змінні DEYE_* у Railway)."}
    if state.get("precharge"):
        p = state["precharge"]
        return {"ok": False, "text": f"Дозаряд уже йде на станції {html.escape(p['name'].capitalize())} "
                                     f"до {energy.local(p['restore_at']).strftime('%H:%M')}. "
                                     "Зупинити: /precharge stop"}
    ctrl = controllable(stations)
    if not ctrl:
        return {"ok": False, "text": "Немає станції, якою можна керувати (потрібен SN з листів Deye "
                                     "і батарея зі зв'язком BMS)."}
    key = (name or "").casefold() or next(iter(ctrl))
    if key not in ctrl:
        skip = " (SOC Бані рахується з напруги — нею не керую)" if energy.voltage_soc(key) else ""
        return {"ok": False, "text": f"Станцією «{html.escape(key)}» керувати не можу{skip}. "
                                     f"Доступні: {', '.join(ctrl)}"}
    hours = max(1, min(int(hours), settings.precharge_max_hours))
    sn = ctrl[key]
    tou = await deye_api.tou_read(sn)
    if tou is None:
        return {"ok": False, "text": "Не вдалося прочитати розклад (TOU) з інвертора — спробуй пізніше."}
    if tou["action"].strip().casefold() in _OFF_WORDS:
        return {"ok": False, "text": "Розклад TOU на інверторі вимкнений — зміна слотів не подіє. "
                                     "Увімкни Time of Use у застосунку Deye і повтори /precharge."}
    items = normalize(tou["items"])
    if items is None:
        return {"ok": False, "text": "Розклад TOU має незнайомий формат — нічого не міняю."}
    now = energy.local(now_ms)
    new, idx = plan(items, now.hour * 60 + now.minute, hours)
    if all(items[k].get("enableGridCharge") and int(items[k].get("soc") or 0) >= 100 for k in idx):
        return {"ok": False, "text": "Заряд від мережі до 100% у цих слотах уже ввімкнено — нічого міняти."}
    token = secrets.token_hex(4)
    restore_at = now_ms + hours * 3_600_000
    state["precharge_pending"] = {"token": token, "name": key, "sn": sn, "orig": items,
                                  "new": new, "created": now_ms, "restore_at": restore_at}
    text = (f"🔌 <b>Дозаряд {html.escape(key.capitalize())} від мережі до 100%</b> на {hours} год "
            f"(до {energy.local(restore_at).strftime('%H:%M')}).\n"
            f"Змінюю розклад TOU інвертора:\n{describe(items, idx)}\n"
            "Після цього поверну розклад як був. Мережа має бути — без неї заряду не буде.")
    return {"ok": True, "text": text, "token": token}


async def apply(state: dict, token: str, now_ms: int) -> str:
    """Owner pressed ✅. Writes the planned table; returns a status line."""
    from app.core import deye_api
    from app.core.policy import evaluate
    pend = state.get("precharge_pending")
    if not pend or pend["token"] != token or now_ms - pend["created"] > PENDING_TTL_MS:
        return "⏱ Пропозиція застаріла — запусти /precharge ще раз."
    if not evaluate("inverter.control").allowed:
        return "Політика не дозволяє керувати інвертором."
    state.pop("precharge_pending", None)
    order = await deye_api.tou_update(pend["sn"], pend["new"])
    if order is None:
        return "❌ Deye не прийняв зміну розкладу — нічого не змінено."
    shift = now_ms - pend["created"]
    state["precharge"] = {"name": pend["name"], "sn": pend["sn"], "orig": pend["orig"],
                          "restore_at": pend["restore_at"] + shift, "order": order, "tries": 0}
    logger.info("precharge: applied on %s (order %s)", pend["name"], order)
    return (f"✅ Дозаряд увімкнено (наказ Deye №{order}). Поверну розклад о "
            f"{energy.local(state['precharge']['restore_at']).strftime('%H:%M')}.")


async def restore(state: dict) -> bool:
    from app.core import deye_api
    p = state.get("precharge")
    if not p:
        return True
    order = await deye_api.tou_update(p["sn"], p["orig"])
    if order is None:
        p["tries"] = p.get("tries", 0) + 1
        return False
    logger.info("precharge: restored on %s (order %s)", p["name"], order)
    state.pop("precharge", None)
    return True


async def tick(state: dict, now_ms: int, send) -> None:
    p = state.get("precharge")
    if not p or now_ms < p["restore_at"]:
        return
    name = html.escape(p["name"].capitalize())
    if await restore(state):
        await send(f"↩️ Розклад TOU {name} повернуто як був — дозаряд завершено.", True)
        return
    if p.get("tries", 0) >= MAX_RESTORE_TRIES:
        state.pop("precharge", None)
        await send(f"⚠️ <b>Не вдалося повернути розклад TOU {name}</b> після "
                   f"{MAX_RESTORE_TRIES} спроб. Перевір у застосунку Deye: слоти з "
                   "зарядом від мережі зараз увімкнені.", False)


async def stop(state: dict) -> str:
    if not state.get("precharge"):
        state.pop("precharge_pending", None)
        return "Активного дозаряду немає."
    ok = await restore(state)
    return "↩️ Розклад повернуто, дозаряд зупинено." if ok else \
        "⚠️ Deye не прийняв повернення — спробую ще раз автоматично."

