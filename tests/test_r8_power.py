"""R8 — home power status from Deye Cloud mails.

Pins, in the order it matters:
  1. the four real mail shapes (UA/RU × lost/restored) parse; alarms and
     verification codes do not;
  2. the state machine is idempotent (same mail twice, repeated «restored»,
     out-of-order mail) and keeps an outage history;
  3. a blink shorter than the debounce never pushes; a settled change pushes
     once; the very first observation never pushes;
  4. the pinned card is created once, then edited in place; a deleted card is
     reposted; nothing is re-sent when the text did not change;
  5. only PERSONAL-domain Google accounts are read;
  6. the Deye OpenAPI mapping is tolerant and its token never lands in state.
"""
import json

import pytest

from app.config import settings
from app.core import deye_api, power
from app.models import AppState, GoogleCredential

OWNER = 111
MIN = 60_000
T0 = 1_795_417_693_000  # 2026-11-23 09:08 Kyiv

RU_OFF = ("Уведомление об отключении от сети",
          "Уважаемый пользователь Deye Cloud, устройство 2602222277 на вашей станции "
          "Дом сейчас отключено от электросети. Рекомендуем своевременно проверить "
          "оборудование для обеспечения стабильной работы.")
RU_ON = ("Уведомление о восстановлении подключения к сети​",
         "Уважаемый пользователь Deye Cloud, устройство с SN: 2601253438 на вашей "
         "станции Баня повторно подключено к электросети.")
UA_OFF = ("Повідомлення про відключення від електромережі",
          "Шановний користувачу Deye Cloud, на вашій станції Баня пристрій 2601253438 "
          "наразі відключений від електромережі. Рекомендуємо негайно перевірити його.")
UA_ON = ("Сповіщення про відновлення електропостачання.",
         "Шановний користувачу Deye Cloud, на вашій станції Баня пристрій із SN: "
         "2601253438 тепер повторно підключений до електромережі.")


def ev(i, ts, sn, name, status):
    return power.PowerEvent(f"m{i}", ts, sn, name, status)


# ---------- 1. parsing ----------

@pytest.mark.parametrize("mail,sn,name,status", [
    (RU_OFF, "2602222277", "Дом", "off"),
    (RU_ON, "2601253438", "Баня", "on"),
    (UA_OFF, "2601253438", "Баня", "off"),
    (UA_ON, "2601253438", "Баня", "on"),
])
def test_real_mails_parse(mail, sn, name, status):
    e = power.parse_event("x", T0, *mail)
    assert (e.sn, e.station, e.status) == (sn, name, status)


@pytest.mark.parametrize("subject,snippet", [
    ("В станции Дом произошла тревога с названием W3 Grid_Phase_Wrong.",
     "В станции Дом произошла тревога W3 Grid_Phase_Wrong."),
    ("Code 340176,This verification code is used for registration.", "Code 340176"),
])
def test_other_deye_mail_ignored(subject, snippet):
    assert power.parse_event("x", T0, subject, snippet) is None


# ---------- 2. state machine ----------

def test_apply_is_idempotent_and_ordered():
    st = power.empty_state()
    evs = [ev(1, T0, "A", "Дом", "off"), ev(2, T0 + 60 * MIN, "A", "Дом", "on")]
    assert power.apply_events(st, list(reversed(evs)))       # order-independent
    assert st["stations"]["A"]["status"] == "on"
    assert not power.apply_events(st, evs)                   # same ids again
    assert not power.apply_events(st, [ev(3, T0 + 70 * MIN, "A", "Дом", "on")])
    assert not power.apply_events(st, [ev(4, T0 + 10 * MIN, "A", "Дом", "off")])  # stale
    assert st["history"] == [["A", T0, T0 + 60 * MIN]]
    assert power.off_minutes(st, T0 - MIN, T0 + 200 * MIN) == (60, 1)


def test_overall_and_union_stats():
    st = power.empty_state()
    power.apply_events(st, [ev(1, T0, "A", "Дом", "off"),
                            ev(2, T0 + 2 * MIN, "B", "Баня", "off")])
    assert power.overall(st) == ("off", T0)
    power.apply_events(st, [ev(3, T0 + 30 * MIN, "A", "Дом", "on")])
    assert power.overall(st)[0] == "partial"
    power.apply_events(st, [ev(4, T0 + 40 * MIN, "B", "Баня", "on")])
    assert power.overall(st) == ("on", T0 + 40 * MIN)
    assert power.off_minutes(st, T0 - MIN, T0 + 99 * MIN) == (40, 1)  # union, one outage


# ---------- 3. push decisions ----------

def test_first_observation_never_pushes_then_debounce():
    st = power.empty_state()
    power.apply_events(st, [ev(1, T0, "A", "Дом", "on")])
    assert power.decide_push(st, T0 + MIN) is None and st["notified"] == "on"
    power.apply_events(st, [ev(2, T0 + 10 * MIN, "A", "Дом", "off")])
    assert power.decide_push(st, T0 + 11 * MIN) is None           # still settling
    power.apply_events(st, [ev(3, T0 + 12 * MIN, "A", "Дом", "on")])
    assert power.decide_push(st, T0 + 20 * MIN) is None           # blink: back to on
    power.apply_events(st, [ev(4, T0 + 30 * MIN, "A", "Дом", "off")])
    assert power.decide_push(st, T0 + 30 * MIN + settings.power_debounce_minutes * MIN) == "off"


def test_push_text_says_how_long_it_was_gone():
    st = power.empty_state()
    power.apply_events(st, [ev(1, T0 + 135 * MIN, "A", "Дом", "on")])
    st["notified"], st["notified_since_ms"] = "off", T0
    assert "не було 2 год 15 хв" in power.push_text(st, "on", T0 + 140 * MIN)


def test_render_card_states():
    st = power.empty_state()
    assert "СТАН НЕВІДОМИЙ" in power.render_card(st, T0)
    power.apply_events(st, [ev(1, T0, "A", "Дом", "off"), ev(2, T0, "B", "<b>", "off")])
    st["polled_ms"] = T0 + 135 * MIN
    card = power.render_card(st, T0 + 135 * MIN)
    assert card.startswith("🔴 <b>ЕЛЕКТРО НЕМА</b> · 2 год 15 хв (з 09:08)")
    assert "&lt;b&gt;" in card                       # station names are escaped
    assert "Сьогодні без світла: 2 год 15 хв (1 р.)" in card
    st["polled_ms"] = T0
    assert "⚠️ пошту не вдалося перевірити" in power.render_card(st, T0 + 135 * MIN)


# ---------- 4. tick: pinned card + push (fake mailbox, fake Telegram) ----------

class FakeSender:
    def __init__(self):
        self.sent, self.edits, self.pins, self.gone = [], [], [], set()

    async def send(self, text, silent=True):
        self.sent.append((text, silent))
        return len(self.sent)

    async def edit(self, mid, text):
        if mid in self.gone:
            return False
        self.edits.append((mid, text))
        return True

    async def pin(self, mid):
        self.pins.append(mid)


@pytest.fixture
def mailbox(monkeypatch):
    box: list[dict] = []

    async def fake_fetch(db, owner, state):
        return [e for m in box if (e := power.parse_event(
            m["id"], m["ts"], m["subject"], m["snippet"]))]
    monkeypatch.setattr(power, "fetch_events", fake_fetch)
    monkeypatch.setattr(deye_api, "enabled", lambda: False)
    return box


async def test_tick_pins_once_edits_in_place_and_pushes(db, mailbox):
    s = FakeSender()
    mailbox.append({"id": "1", "ts": T0, "subject": RU_OFF[0], "snippet": RU_OFF[1]})
    await power.tick(db, s, now_ms=T0 + MIN)
    assert len(s.sent) == 1 and s.pins == [1] and s.sent[0][1] is True
    assert "ЕЛЕКТРО НЕМА" in s.sent[0][0]

    await power.tick(db, s, now_ms=T0 + 30 * 1000)          # throttled
    assert len(s.sent) == 1 and not s.edits

    on_ts = T0 + 90 * MIN
    mailbox.append({"id": "2", "ts": on_ts, "subject": "Уведомление о восстановлении",
                    "snippet": RU_OFF[1].replace("сейчас отключено", "повторно подключено")})
    await power.tick(db, s, now_ms=on_ts + MIN)              # settling: edit only
    assert s.edits and "ЕЛЕКТРО Є" in s.edits[-1][1] and len(s.sent) == 1
    await power.tick(db, s, now_ms=on_ts + 5 * MIN)          # settled: one silent push
    assert len(s.sent) == 2 and s.sent[1][0].startswith("🟢 Світло є з 10:38")
    assert "не було 1 год 30 хв" in s.sent[1][0] and s.sent[1][1] is True

    s.gone.add(1)                                            # owner deleted the card
    await power.tick(db, s, now_ms=on_ts + 9 * MIN)
    assert s.pins == [1, 3]
    saved = json.loads((await db.get(AppState, power.STATE_KEY)).value)
    assert saved["pin"] == {"message_id": 3} and saved["notified"] == "on"


async def test_tick_quiet_without_mailbox(db, monkeypatch):
    async def none(db, owner, state):
        return None
    monkeypatch.setattr(power, "fetch_events", none)
    s = FakeSender()
    await power.tick(db, s, now_ms=T0)
    assert s.sent == []


# ---------- 5. personal domain only ----------

async def test_only_personal_accounts_are_read(db, monkeypatch):
    from app.core import google_client
    for email, dom in (("home@x.com", "personal"), ("work@x.com", "travelon"),
                       ("new@x.com", None)):
        db.add(GoogleCredential(user_id=OWNER, account_email=email, domain=dom,
                                refresh_token_enc="x", access_token="", scopes=""))
    await db.commit()
    read = []

    async def access_for(db, cred):
        return cred.account_email

    async def msgs(access, query, limit=50):
        read.append((access, query))
        return [{"id": "1", "internal_ms": T0, "subject": UA_ON[0], "snippet": UA_ON[1]}]
    monkeypatch.setattr(google_client, "access_for", access_for)
    monkeypatch.setattr(google_client, "gmail_messages", msgs)
    events = await power.fetch_events(db, OWNER, power.empty_state())
    assert [a for a, _ in read] == ["home@x.com"]
    assert read[0][1] == "from:noreply@deye.cloud newer_than:4d"
    assert events[0].status == "on"
    st = power.empty_state()
    st["hw_ms"] = T0
    assert power._query(st) == f"from:noreply@deye.cloud after:{T0 // 1000 - 600}"


# ---------- 6. Deye OpenAPI ----------

def test_deye_latest_mapping_and_offline_line():
    lv = deye_api.parse_latest("дом", {"code": "1000000", "success": True,
                                       "batterySOC": 64, "generationPower": 1234.0,
                                       "consumptionPower": 800, "wirePower": 0,
                                       "lastUpdateTime": T0 // 1000})
    assert (lv.soc, lv.pv_w, lv.load_w, lv.updated_ts) == (64, 1234.0, 800, T0 // 1000)
    st = power.empty_state()
    power.apply_events(st, [ev(1, T0, "A", "Дом", "on")])
    assert ("🔋 Дом 64% · сонце покриває споживання · ☀️ 1.2 кВт · 🏠 0.8 кВт"
            in power.render_card(st, T0 + MIN, {"дом": lv}))
    late = T0 + (settings.deye_offline_minutes + 5) * MIN
    assert "інвертор не на зв'язку з 09:08" in power.render_card(st, late, {"дом": lv})


def test_deye_listing_fills_gaps_and_flags_offline():
    row = {"id": 10, "name": "Дом", "batterySOC": 55.0, "connectionStatus": "ALL_OFFLINE",
           "generationPower": 0.0, "lastUpdateTime": T0 // 1000}
    lv = deye_api.merge_listing(deye_api.StationLive(name="дом"), row)
    assert (lv.soc, lv.online, lv.updated_ts) == (55.0, False, T0 // 1000)
    st = power.empty_state()
    power.apply_events(st, [ev(1, T0, "A", "Дом", "on")])
    assert "⚪ Дом: інвертор не на зв'язку з 09:08" in power.render_card(st, T0 + MIN, {"дом": lv})
    assert deye_api._epoch("2026-11-23T07:08:13Z") == T0 / 1000
    assert deye_api._epoch("1795417693") == T0 / 1000


def test_deye_disabled_without_all_four_vars():
    assert not deye_api.enabled()


# ---------- R8.2.1: inverter grid voltage overrides a missing mail ----------

def test_grid_from_values():
    assert deye_api.grid_from_values({"Grid Voltage L1": ("228.1", "V")}) is True
    assert deye_api.grid_from_values({"Grid Voltage L1-L2": ("0", "V"),
                                      "Grid Voltage L2-L3": ("0.4", "V")}) is False
    assert deye_api.grid_from_values({"Load Voltage L1": ("230", "V")}) is None
    assert deye_api.grid_from_values({"Battery Voltage": ("52", "V")}) is None


def test_grid_events_follow_inverter_not_stale_mail():
    st = power.empty_state()
    power.apply_events(st, [ev(1, T0, "2602222277", "Дом", "off"),
                            ev(2, T0, "2601253438", "Баня", "off")])
    presence = {"2602222277": {"grid": True, "state": 1, "ts": (T0 + 60 * MIN) / 1000},
                "2601253438": {"grid": None, "state": 1, "ts": None},
                "999": {"grid": True, "state": 1, "ts": None}}
    evs = power.grid_events(st, presence, T0 + 70 * MIN)
    assert [(e.sn, e.status, e.ts_ms) for e in evs] == [("2602222277", "on", T0 + 60 * MIN)]
    power.apply_events(st, evs)
    assert st["stations"]["2602222277"]["status"] == "on"
    assert st["history"][0][2] == T0 + 60 * MIN
    # an offline inverter is no news
    assert power.grid_events(st, {"2602222277": {"grid": False, "state": 3, "ts": None}},
                             T0 + 80 * MIN) == []
    # a late «restored» mail for the same change is a no-op
    assert not power.apply_events(st, [ev(9, T0 + 75 * MIN, "2602222277", "Дом", "on")])


def test_station_grid_uses_any_online_inverter():
    readings = {"A": {"grid": False, "state": 3, "ts": 1},       # offline logger
                "B": {"grid": True, "state": 1, "ts": 2},
                "C": {"grid": False, "state": 1, "ts": 3}}
    assert deye_api.station_grid(["A", "B"], readings) is True
    assert deye_api.station_grid(["A", "C"], readings) is False
    assert deye_api.station_grid(["A"], readings) is None
    st = power.empty_state()
    power.apply_events(st, [ev(1, T0, "A", "Дом", "off")])
    pres = power.station_presence(st, {"дом": ["B"]}, readings)
    assert pres["A"]["grid"] is True
    evs = power.grid_events(st, pres, T0 + 60 * MIN)
    assert evs and evs[0].sn == "A" and evs[0].status == "on"


def test_partial_offline_station_is_alive():
    lv = deye_api.merge_listing(deye_api.StationLive(name="дом"),
                                {"id": 1, "name": "Дом", "connectionStatus": "PARTIAL_OFFLINE"})
    assert lv.online is True
    lv = deye_api.merge_listing(deye_api.StationLive(name="дом"),
                                {"id": 1, "name": "Дом", "connectionStatus": "ALL_OFFLINE"})
    assert lv.online is False
