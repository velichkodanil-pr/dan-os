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
                live_row = parse_latest(key, data) if _ok(data) else StationLive(name=key)
                fresh[key] = merge_listing(live_row, _listing.get(key, {}))
        _cache.clear()
        _cache.update(fresh)
        _cache_at = time.monotonic()
    except Exception as e:  # never log tokens/bodies
        logger.warning("deye api unavailable: %s", type(e).__name__)
        _token = "" if "token" in str(e) else _token
    return _cache


def reset() -> None:
    """Tests only."""
    global _token, _token_exp, _cache_at
    _token, _token_exp, _cache_at = "", 0.0, 0.0
    _stations.clear()
    _listing.clear()
    _cache.clear()
