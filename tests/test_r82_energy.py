"""R8.2 — energy on top of the power card.

Pins:
  1. runtime math (cut-off SOC, sun offsets the load, «≈» for voltage SOC);
  2. low-battery alerts: once per threshold per outage, the last one rings;
  3. capacity estimate only from a meaningful discharge swing;
  4. panel check calibrates first, then flags a clear drop;
  5. surplus uses installed kWp × irradiance (a full battery curtails PV),
     daytime only, with a cooldown;
  6. pre-charge: table parsing refuses the unknown, slot planning wraps
     midnight, voltage-SOC stations are never controllable, apply needs a
     fresh token, restore puts the original table back;
  7. home_power is a PERSONAL tool: hidden and refused elsewhere.
"""
import json

import pytest

from app.config import settings
from app.core import chat_tools, deye_api, energy, precharge

MIN = 60_000
T0 = 1_795_417_693_000          # 23.11.2026 09:08 Kyiv


def lv(**kw):
    return deye_api.StationLive(name=kw.pop("name", "дом"), **kw)


# ---------- 1. runtime ----------

def test_runtime_hours():
    # 32 kWh, 64 % → (64-10)% = 17.28 kWh at 1.2 kW net = 14.4 h
    assert energy.runtime_hours(32, 64, 1200, 0) == pytest.approx(14.4)
    assert energy.runtime_hours(32, 64, 1200, 1300) is None      # sun covers
    assert energy.runtime_hours(32, None, 1200, 0) is None


def test_battery_line_marks_voltage_soc():
    line = energy.battery_line("Баня", lv(name="баня", soc=55, load_w=500, pv_w=0), True)
    assert "≈55%" in line and "вистачить ≈" in line
    line = energy.battery_line("Дом", lv(soc=64, load_w=1200, pv_w=0), False)
    assert "64%" in line and "запас ~14 год" in line


# ---------- 2. low battery ----------

def test_low_battery_once_per_threshold_per_outage():
    st = {}
    assert energy.low_battery_due(st, "дом", 45, T0) is None
    assert energy.low_battery_due(st, "дом", 29, T0) == 30
    assert energy.low_battery_due(st, "дом", 28, T0) is None
    assert energy.low_battery_due(st, "дом", 14, T0) == 15
    assert energy.low_battery_due(st, "дом", 12, T0) is None
    assert energy.low_battery_due(st, "дом", 25, T0 + 600 * MIN) == 30   # new outage
    assert energy.low_battery_due(st, "дом", 25, None) is None           # grid on


# ---------- 3. capacity ----------

def test_capacity_estimate_from_outage():
    st = {}
    energy.capacity_step(st, "дом", lv(soc=90, discharge_w=4000), True, T0, T0)
    for i in range(1, 31):     # 30 polls × 5 min × 4 kW = 10 kWh, SOC 90 → 60
        energy.capacity_step(st, "дом", lv(soc=90 - i, discharge_w=4000), True, T0,
                             T0 + i * 5 * MIN)
    est = energy.capacity_step(st, "дом", lv(soc=60), False, None, T0 + 160 * MIN)
    assert est == pytest.approx(10 / 0.30, rel=0.01)
    assert "Ємність Дом: ~33.3 кВт·год з 32" in energy.capacity_line(st, "дом")
    # a small swing says nothing
    energy.capacity_step(st, "дом", lv(soc=80, discharge_w=1000), True, T0 + 999 * MIN, T0 + 999 * MIN)
    energy.capacity_step(st, "дом", lv(soc=75, discharge_w=1000), True, T0 + 999 * MIN, T0 + 1004 * MIN)
    assert energy.capacity_step(st, "дом", lv(soc=75), False, None, T0 + 1010 * MIN) is None


# ---------- 4. panels ----------

def test_panel_check_calibrates_then_flags():
    st = {}
    for d in range(5):
        msg = energy.panel_verdict(st, "дом", f"2026-11-0{d + 1}", 20.0, 2.0)
        assert "калібрую" in msg
    assert "у нормі" in energy.panel_verdict(st, "дом", "2026-11-10", 19.0, 2.0)
    assert "⚠️" in energy.panel_verdict(st, "дом", "2026-11-11", 8.0, 2.0)
    assert energy.panel_verdict(st, "дом", "2026-11-12", 1.0, 0.3) is None   # dull day


# ---------- 5. surplus ----------

def test_surplus_from_potential_not_curtailed_pv():
    full = lv(soc=100, pv_w=900, load_w=900, capacity_kwp=15)
    spare = energy.surplus_watts(full, 600)            # 15 kWp × 0.6 × 0.8 = 7.2 kW
    assert spare == pytest.approx(6300)
    assert energy.surplus_watts(lv(soc=80, pv_w=9000, load_w=900), 900) is None
    st = {}
    noon = T0 + 3 * 60 * MIN                           # 12:08 Kyiv
    assert not energy.surplus_due(st, "дом", spare, noon)          # needs 2 hits
    assert energy.surplus_due(st, "дом", spare, noon + 4 * MIN)
    assert not energy.surplus_due(st, "дом", spare, noon + 8 * MIN)   # cooldown
    night = T0 + 12 * 60 * MIN
    st2 = {}
    energy.surplus_due(st2, "дом", spare, night)
    assert not energy.surplus_due(st2, "дом", spare, night + 4 * MIN)


# ---------- daily report gate ----------

def test_report_due_once_after_time():
    st = {}
    evening = T0 + (21 * 60 + 5 - 9 * 60 - 8) * MIN   # 21:05 Kyiv
    assert not energy.report_due(st, T0)
    assert energy.report_due(st, evening)
    st["report_date"] = energy.local(evening).date().isoformat()
    assert not energy.report_due(st, evening + 30 * MIN)


def test_savings_use_own_energy_only():
    f = energy.DayFigures(generation=12, consumption=20, purchase=5, grid=1)
    assert energy.savings_uah(f) == pytest.approx(15 * settings.power_tariff_uah)


# ---------- 6. pre-charge ----------

TOU = [{"time": "00:00", "power": 5000, "soc": 30, "enableGridCharge": False,
        "enableGeneration": True},
       {"time": "06:00", "power": 5000, "soc": 30, "enableGridCharge": False,
        "enableGeneration": True},
       {"time": "12:00", "power": 5000, "soc": 40, "enableGridCharge": False,
        "enableGeneration": True},
       {"time": "18:00", "power": 5000, "soc": 50, "enableGridCharge": "1",
        "enableGeneration": True}]


def test_normalize_refuses_unknown_tables():
    assert precharge.normalize(TOU)[3]["enableGridCharge"] is True
    assert precharge.normalize([{"time": "25:00"}, {"time": "01:00"}]) is None
    assert precharge.normalize([{"time": "00:00"}]) is None          # one slot only


def test_plan_wraps_midnight():
    items = precharge.normalize(TOU)
    new, idx = precharge.plan(items, 23 * 60, 2)        # 23:00 → 01:00
    assert idx == [3, 0]
    assert new[3]["soc"] == 100 and new[0]["enableGridCharge"] is True
    assert items[0]["enableGridCharge"] is False         # original untouched
    assert precharge.plan(items, 9 * 60, 2)[1] == [1]


def test_voltage_soc_station_is_never_controllable():
    stations = {"2602222277": {"name": "Дом", "status": "on", "since_ms": T0},
                "2601253438": {"name": "Баня", "status": "on", "since_ms": T0}}
    assert precharge.controllable(stations) == {"дом": "2602222277"}


async def test_preview_apply_restore(monkeypatch):
    writes = []

    async def tou_read(sn):
        return {"action": "on", "items": TOU}

    async def tou_update(sn, items):
        writes.append((sn, json.loads(json.dumps(items))))
        return 4242
    monkeypatch.setattr(deye_api, "enabled", lambda: True)
    monkeypatch.setattr(deye_api, "tou_read", tou_read)
    monkeypatch.setattr(deye_api, "tou_update", tou_update)
    stations = {"2602222277": {"name": "Дом", "status": "on", "since_ms": T0}}
    pc = {}
    res = await precharge.preview(pc, stations, None, 2, T0)          # 09:08
    assert res["ok"] and "до 11:08" in res["text"] and not writes
    assert "⏱" in await precharge.apply(pc, "wrong", T0 + MIN)
    msg = await precharge.apply(pc, res["token"], T0 + MIN)
    assert "4242" in msg and writes[0][1][1]["soc"] == 100
    sent = []

    async def send(text, silent=True):
        sent.append(text)
    await precharge.tick(pc, T0 + 60 * MIN, send)                    # not yet
    assert len(writes) == 1
    await precharge.tick(pc, T0 + 125 * MIN, send)
    assert writes[1][1] == precharge.normalize(TOU) and "повернуто" in sent[0]
    assert "precharge" not in pc


async def test_preview_refuses_when_tou_off(monkeypatch):
    async def tou_read(sn):
        return {"action": "off", "items": TOU}
    monkeypatch.setattr(deye_api, "enabled", lambda: True)
    monkeypatch.setattr(deye_api, "tou_read", tou_read)
    res = await precharge.preview({}, {"1": {"name": "Дом", "status": "on", "since_ms": 0}},
                                  None, 2, T0)
    assert not res["ok"] and "вимкнений" in res["text"]


# ---------- 7. personal-only tool ----------

def test_home_power_is_personal_only():
    names = lambda d: {t["name"] for t in chat_tools.tools_for_domain(d)}
    assert "home_power" in names("personal")
    assert "home_power" not in names("travelon") | names("tech")


async def test_home_power_refused_outside_personal(db):
    out = json.loads(await chat_tools.run_tool(db, 111, "travelon", "home_power", {}))
    assert out["error"] == "wrong_domain"
