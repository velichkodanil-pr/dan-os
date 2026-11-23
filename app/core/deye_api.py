"""Deye Cloud OpenAPI — read-only live numbers for the power card (R8 stage 2).

Optional: active only when DEYE_APP_ID / DEYE_APP_SECRET / DEYE_EMAIL /
DEYE_PASSWORD_SHA256 are all set (Railway variables). The access token is held
in process memory only — never written to the database, logs or audit.

Grid presence still comes from Deye's own mails (power.py); this module adds
battery SOC, PV, load and «the inverter stopped reporting» (lastUpdateTime).
"""
import logging
import time
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_token: str = ""
_token_exp: float = 0.0
_stations: dict[str, str] = {}      # station name (casefold) -> station id
_listing: dict[str, dict] = {}      # station name (casefold) -> station/list row
_cache: dict[str, "StationLive"] = {}
_cache_at: float = 0.0
CACHE_SECONDS = 240
_shapes_logged: set[str] = set()


def _log_shape(path: str, data: dict) -> None:
    """Once per process: which fields Deye actually returned (names only, no
    values) — lets a deploy be checked against the swagger from the logs."""
    if path in _shapes_logged or not isinstance(data, dict):
        return
    _shapes_logged.add(path)
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    keys = sorted(k for k in body.keys() if k not in ("requestId",))
    for list_key in ("stationDataItems", "stationAlertItems", "alertList",
                     "timeUseSettingItems", "stationList"):
        rows = body.get(list_key)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            keys.append(f"{list_key}[0]:" + ",".join(sorted(rows[0].keys())))
    logger.info("deye shape %s: %s", path, "; ".join(keys)[:900])


@dataclass
class StationLive:
    name: str
    soc: float | None = None          # battery %
    pv_w: float | None = None         # generation
    load_w: float | None = None       # consumption
    grid_w: float | None = None       # wire power
    battery_w: float | None = None    # + charge / - discharge (sign as Deye gives)
    updated_ts: float | None = None   # epoch seconds of the inverter's last data
    online: bool | None = None        # station/list connectionStatus (NORMAL)
    charge_w: float | None = None
    discharge_w: float | None = None
    station_id: str = ""
    capacity_kwp: float | None = None  # installed PV, station/list
    lat: float | None = None
    lng: float | None = None


def enabled() -> bool:
    return all([settings.deye_app_id, settings.deye_app_secret,
                settings.deye_email, settings.deye_password_sha256])


def _num(d: dict, *keys) -> float | None:
    for k in keys:
        v = d.get(k)
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _epoch(v) -> float | None:
    """Deye returns epoch seconds, milliseconds or an ISO date-time (the spec
    says date-time, the examples show epoch seconds); normalise to seconds."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f / 1000 if f > 1e11 else f
    except (TypeError, ValueError):
        pass
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_latest(name: str, data: dict) -> StationLive:
    """Tolerant mapping of /station/latest. Pure — covered by tests."""
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    return StationLive(
        name=name,
        soc=_num(body, "batterySOC", "batterySoc", "soc"),
        pv_w=_num(body, "generationPower", "pvPower"),
        load_w=_num(body, "consumptionPower", "loadPower"),
        grid_w=_num(body, "wirePower", "gridPower"),
        battery_w=_num(body, "batteryPower"),
        updated_ts=_epoch(body.get("lastUpdateTime") or body.get("updateTime")),
        charge_w=_num(body, "chargePower"),
        discharge_w=_num(body, "dischargePower"),
    )


def merge_listing(lv: StationLive, row: dict) -> StationLive:
    """Fill gaps from the station/list row and take its connection status."""
    status = str(row.get("connectionStatus") or "").upper()
    if status:
        lv.online = status == "NORMAL"
    if lv.soc is None:
        lv.soc = _num(row, "batterySOC")
    if lv.pv_w is None:
        lv.pv_w = _num(row, "generationPower")
    if lv.updated_ts is None:
        lv.updated_ts = _epoch(row.get("lastUpdateTime"))
    if row.get("id") is not None:
        lv.station_id = str(row["id"])
    lv.capacity_kwp = _num(row, "installedCapacity")
    lv.lat = _num(row, "locationLat")
    lv.lng = _num(row, "locationLng")
    return lv


def _ok(data: dict) -> bool:
    return bool(data.get("success", True)) and str(data.get("code", "1000000")) in (
        "1000000", "0", "200")


async def _post(client: httpx.AsyncClient, path: str, payload: dict,
                params: dict | None = None, auth: bool = True) -> dict:
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {_token}"
    resp = await client.post(settings.deye_base_url.rstrip("/") + path,
                             json=payload, params=params, headers=headers)
    if resp.status_code != 200:
        # status only — the body may echo request fields
        raise RuntimeError(f"deye {path} http {resp.status_code}")
    return resp.json()


async def _ensure_token(client: httpx.AsyncClient) -> None:
    global _token, _token_exp
    if _token and time.time() < _token_exp:
        return
    data = await _post(client, "/account/token",
                       {"appSecret": settings.deye_app_secret,
                        "email": settings.deye_email,
                        "password": settings.deye_password_sha256.lower()},
                       params={"appId": settings.deye_app_id}, auth=False)
    tok = data.get("accessToken") or (data.get("data") or {}).get("accessToken")
    if not tok:
        raise RuntimeError(f"deye token refused (code {data.get('code')})")
    _token = tok
    ttl = _num(data, "expiresIn") or 5 * 86400
    _token_exp = time.time() + min(ttl, 30 * 86400) - 3600


async def _list_stations(client: httpx.AsyncClient) -> None:
    """station/list each refresh: it carries connectionStatus (NORMAL /
    ALL_OFFLINE / PARTIAL_OFFLINE / NO_DEVICE) and lastUpdateTime."""
    data = await _post(client, "/station/list", {"page": 1, "size": 50})
    if not _ok(data):
        raise RuntimeError(f"deye station/list code {data.get('code')}"
                           + (" token" if str(data.get("code")) != "1000000" else ""))
    rows = data.get("stationList") or (data.get("data") or {}).get("stationList") or []
    _listing.clear()
    for row in rows:
        if row.get("id") is not None and row.get("name"):
            key = str(row["name"]).strip().casefold()
            _stations[key] = str(row["id"])
            _listing[key] = row


async def live() -> dict[str, StationLive]:
    """{station name casefold: StationLive}; {} when disabled or on any error.
    Cached for a few minutes — Deye itself refreshes every 3–5 min."""
    global _cache_at, _token
    if not enabled():
        return {}
    if _cache and time.monotonic() - _cache_at < CACHE_SECONDS:
        return _cache
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            await _ensure_token(client)
            await _list_stations(client)
            fresh: dict[str, StationLive] = {}
            for key, sid in _stations.items():
                data = await _post(client, "/station/latest", {"stationId": int(sid)
                                                               if sid.isdigit() else sid})
                _log_shape("/station/latest", data)
                live_row = parse_latest(key, data) if _ok(data) else StationLive(name=key)
                fresh[key] = merge_listing(live_row, _listing.get(key, {}))
        _cache.clear()
        _cache.update(fresh)
        _cache_at = time.monotonic()
    except Exception as e:  # never log tokens/bodies
        logger.warning("deye api unavailable: %s", type(e).__name__)
        _token = "" if "token" in str(e) else _token
    return _cache


# ---------- R8.2: history, alerts, time-of-use ----------

async def call(path: str, payload: dict) -> dict | None:
    """One authenticated POST; None when disabled or on any failure (logged
    by type only). A refused token is dropped so the next call re-logs in."""
    global _token
    if not enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            await _ensure_token(client)
            if not _stations:
                await _list_stations(client)
            data = await _post(client, path, payload)
    except Exception as e:
        logger.warning("deye %s unavailable: %s", path, type(e).__name__)
        _token = "" if "token" in str(e) else _token
        return None
    _log_shape(path, data)
    if not _ok(data):
        logger.warning("deye %s refused: code %s", path, data.get("code"))
        if str(data.get("code")) not in ("1000000",):
            _token = ""
        return None
    return data


def station_id(name: str) -> int | None:
    sid = _stations.get(name.casefold())
    return int(sid) if sid and sid.isdigit() else None


def station_names() -> list[str]:
    return list(_stations.keys())


def _items(data: dict | None, key: str) -> list[dict]:
    if not data:
        return []
    rows = data.get(key)
    if rows is None and isinstance(data.get("data"), dict):
        rows = data["data"].get(key)
    return [r for r in (rows or []) if isinstance(r, dict)]


def parse_daily(row: dict) -> dict:
    """One StationDataItem at day granularity -> kWh figures."""
    return {"day": row.get("day"), "month": row.get("month"), "year": row.get("year"),
            "generation": _num(row, "generationValue") or 0.0,
            "consumption": _num(row, "consumptionValue") or 0.0,
            "purchase": _num(row, "purchaseValue") or 0.0,
            "grid": _num(row, "gridValue") or 0.0,
            "charge": _num(row, "chargeValue") or 0.0,
            "discharge": _num(row, "dischargeValue") or 0.0}


async def daily(name: str, start: str, end: str) -> list[dict]:
    """Daily energy (kWh) for [start, end) — dates yyyy-MM-dd, max 31 days."""
    sid = station_id(name)
    if sid is None:
        return []
    data = await call("/station/history", {"stationId": sid, "granularity": 2,
                                           "startAt": start, "endAt": end})
    return [parse_daily(r) for r in _items(data, "stationDataItems")]


async def frames(name: str, day: str) -> list[dict]:
    """Intra-day power curve (granularity 1) for one date yyyy-MM-dd."""
    sid = station_id(name)
    if sid is None:
        return []
    data = await call("/station/history", {"stationId": sid, "granularity": 1,
                                           "startAt": day})
    out = []
    for r in _items(data, "stationDataItems"):
        ts = _epoch(r.get("timeStamp"))
        if ts is None:
            continue
        out.append({"ts": ts, "pv": _num(r, "generationPower"),
                    "load": _num(r, "consumptionPower"),
                    "soc": _num(r, "batterySOC"), "grid": _num(r, "wirePower")})
    return out


async def station_alerts(name: str, start_s: int, end_s: int) -> list[dict]:
    sid = station_id(name)
    if sid is None:
        return []
    data = await call("/station/alertList", {"stationId": sid, "startTimestamp": start_s,
                                             "endTimestamp": end_s, "page": 1, "size": 50})
    return _items(data, "stationAlertItems")


async def device_alerts(sn: str, start_s: int, end_s: int) -> list[dict]:
    data = await call("/device/alertList", {"deviceSn": sn, "deviceType": "INVERTER",
                                            "startTimestamp": start_s,
                                            "endTimestamp": end_s, "page": 1, "size": 50})
    return _items(data, "alertList")


async def tou_read(sn: str) -> dict | None:
    """{'action': str, 'items': [...]} or None."""
    data = await call("/config/tou", {"deviceSn": sn})
    if data is None:
        return None
    return {"action": str(data.get("touAction") or ""),
            "items": _items(data, "timeUseSettingItems")}


async def tou_update(sn: str, items: list[dict]) -> int | None:
    """Writes the inverter's time-of-use table. Returns the async order id.
    Callers MUST have the owner's explicit confirmation (policy L3)."""
    data = await call("/order/sys/tou/update", {"deviceSn": sn,
                                               "timeUseSettingItems": items})
    if data is None:
        return None
    oid = data.get("orderId")
    return int(oid) if oid is not None else None


async def order_status(order_id: int) -> dict | None:
    """GET /order/{id} — status of an async control order."""
    global _token
    if not enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            await _ensure_token(client)
            resp = await client.get(
                settings.deye_base_url.rstrip("/") + f"/order/{int(order_id)}",
                headers={"Authorization": f"Bearer {_token}"})
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        logger.warning("deye order status unavailable: %s", type(e).__name__)
        return None


# ---------- grid presence from the inverter itself (R8.2.1) ----------

_grid_cache: dict = {"at": 0.0, "data": {}}
_grid_keys_logged = {"done": False}


def grid_from_values(values: dict[str, tuple[str, str]]) -> bool | None:
    """Grid voltage measure points → True (grid present) / False (absent) /
    None (no grid voltage reported). Keys differ by model («Grid Voltage L1»,
    «Grid Voltage L1-L2», …), so match on the words, not the exact key."""
    volts = []
    for key, (val, unit) in values.items():
        k = key.casefold()
        if "grid" in k and "volt" in k and "load" not in k and "gen" not in k:
            try:
                volts.append(abs(float(val)))
            except (TypeError, ValueError):
                continue
    if not volts:
        return None
    if max(volts) >= 150:
        return True
    if max(volts) <= 30:
        return False
    return None


async def grid_presence(sns: list[str]) -> dict[str, dict]:
    """{sn: {"grid": bool|None, "state": 1 online|2 alert|3 offline,
    "ts": collection time s}} from /device/latest; cached like live()."""
    if not sns or not enabled():
        return {}
    if _grid_cache["data"] and time.monotonic() - _grid_cache["at"] < CACHE_SECONDS:
        return _grid_cache["data"]
    data = await call("/device/latest", {"deviceList": list(sns), "deviceType": "INVERTER"})
    out: dict[str, dict] = {}
    for row in _items(data, "deviceDataList"):
        sn = str(row.get("deviceSn") or "")
        values = {}
        for d in row.get("dataList") or []:
            if isinstance(d, dict) and d.get("key"):
                values[str(d["key"])] = (d.get("value"), d.get("unit"))
        if not _grid_keys_logged["done"]:
            _grid_keys_logged["done"] = True
            gk = sorted(k for k in values if "grid" in k.casefold() or "volt" in k.casefold())
            logger.info("deye device/latest grid keys: %s", ", ".join(gk)[:600] or "none")
        out[sn] = {"grid": grid_from_values(values),
                   "state": row.get("deviceState"),
                   "ts": _epoch(row.get("collectionTime"))}
    if out:
        _grid_cache.update(at=time.monotonic(), data=out)
    return out


def reset() -> None:
    """Tests only."""
    global _token, _token_exp, _cache_at
    _token, _token_exp, _cache_at = "", 0.0, 0.0
    _stations.clear()
    _listing.clear()
    _cache.clear()
    _grid_cache.update(at=0.0, data={})
