"""Round 4 tests: Mini App auth, coach lifecycle, Travelon parser, policy."""
import json
import uuid
from datetime import date

import pytest

from app.core import coach, travelon
from app.core.policy import evaluate
from app.webapp.auth import sign_init_data, validate_init_data

BOT_TOKEN = "12345:TEST-token"
NOW = 1_700_000_000


def _init_data(user_id: int = 111, auth_date: int = NOW) -> str:
    return sign_init_data({
        "auth_date": str(auth_date),
        "query_id": "AAF9tOgJ",
        "user": json.dumps({"id": user_id, "first_name": "Dan"}),
    }, BOT_TOKEN)


# ---------- Mini App initData auth ----------

def test_initdata_valid_passes():
    assert validate_init_data(_init_data(), BOT_TOKEN, now=NOW) == 111


def test_initdata_forged_hash_rejected():
    forged = _init_data().replace("auth_date=", "auth_date=9")  # tamper a field
    assert validate_init_data(forged, BOT_TOKEN, now=NOW) is None


def test_initdata_wrong_bot_token_rejected():
    assert validate_init_data(_init_data(), "999:OTHER", now=NOW) is None


def test_initdata_stale_rejected():
    stale = _init_data(auth_date=NOW - 100_000)
    assert validate_init_data(stale, BOT_TOKEN, now=NOW, max_age=3600) is None


def test_initdata_missing_or_empty_rejected():
    assert validate_init_data("", BOT_TOKEN, now=NOW) is None
    assert validate_init_data("user=abc", BOT_TOKEN, now=NOW) is None  # no hash
    assert validate_init_data(_init_data(), "", now=NOW) is None  # no token


def test_initdata_foreign_user_id_comes_back_verbatim():
    # The route layer must compare against the owner id; auth returns who signed in.
    assert validate_init_data(_init_data(user_id=999), BOT_TOKEN, now=NOW) == 999


# ---------- coach: goals ----------

@pytest.mark.asyncio
async def test_goal_lifecycle(db):
    goal = await coach.create_goal(db, user_id=111, title="Запустити зимовий сезон")
    active = await coach.list_goals(db, 111)
    assert [g.id for g in active] == [goal.id]

    assert await coach.set_goal_status(db, user_id=111, goal_id=goal.id,
                                       status="done") == "done"
    assert await coach.list_goals(db, 111) == []
    # idempotent: second click returns the settled status, does not flip anything
    assert await coach.set_goal_status(db, user_id=111, goal_id=goal.id,
                                       status="dropped") == "done"


@pytest.mark.asyncio
async def test_goal_foreign_user_not_found(db):
    goal = await coach.create_goal(db, user_id=111, title="X")
    assert await coach.set_goal_status(db, user_id=222, goal_id=goal.id,
                                       status="done") == "not_found"


# ---------- coach: habits ----------

@pytest.mark.asyncio
async def test_habit_toggle_and_week_count(db):
    habit = await coach.create_habit(db, user_id=111, title="Зарядка")
    days = coach.week_dates()

    assert await coach.toggle_habit(db, user_id=111, habit_id=habit.id) == "done"
    over = await coach.habits_overview(db, 111)
    assert over[0]["done_today"] is True and over[0]["week_count"] == 1

    # mark an earlier weekday too (if the week has one)
    if len(days) > 1:
        assert await coach.toggle_habit(db, user_id=111, habit_id=habit.id,
                                        day=days[0]) == "done"
        over = await coach.habits_overview(db, 111)
        assert over[0]["week_count"] == 2

    # toggle today off -> reversible
    assert await coach.toggle_habit(db, user_id=111, habit_id=habit.id) == "undone"
    over = await coach.habits_overview(db, 111)
    assert over[0]["done_today"] is False


@pytest.mark.asyncio
async def test_habit_unknown_id_not_found(db):
    assert await coach.toggle_habit(db, user_id=111,
                                    habit_id=uuid.uuid4()) == "not_found"


# ---------- policy for new actions ----------

def test_policy_r4_actions():
    assert evaluate("travelon.read").allowed and evaluate("travelon.read").level == "L0"
    for action in ("goal.create", "goal.update", "habit.create", "habit.log"):
        d = evaluate(action)
        assert d.allowed and d.level == "L2"
    assert not evaluate("travelon.write").allowed  # unknown -> denied


# ---------- Travelon XML parser ----------

SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<orders>
  <order>
    <id>59266</id>
    <order>59266</order>
    <status>Confirmed</status>
    <create-date>10.06.2026</create-date>
    <valute>UAH</valute>
    <telephone nil="true"/>
    <hotel>
      <hotel-name>LIMAK ATLANTIS</hotel-name>
      <country>Туреччина</country>
      <nights>7</nights>
      <from-date>21.08.2026</from-date>
      <to-date>28.08.2026</to-date>
    </hotel>
    <customers>
      <customer><name>IVAN</name><surname>PONOMAROV</surname><passport>FT111</passport></customer>
      <customer><name>OLHA</name><surname>PONOMAROVA</surname><passport>FT222</passport></customer>
    </customers>
    <costs>
      <gross-cost>85000.50</gross-cost>
      <amount-of-debt>15000.0</amount-of-debt>
    </costs>
  </order>
  <order>
    <id>59267</id>
    <order>59267</order>
    <status>Cancelled</status>
    <create-date>11.06.2026</create-date>
    <valute>UAH</valute>
    <hotel>
      <hotel-name/>
      <country>Єгипет</country>
      <nights/>
      <from-date>01.09.2026</from-date>
    </hotel>
    <costs><gross-cost/><amount-of-debt/></costs>
  </order>
</orders>"""


def test_travelon_parse_minimal_fields():
    orders = travelon.parse_orders(SAMPLE_XML)
    assert len(orders) == 2
    o = orders[0]
    assert o.order_no == "59266" and o.status == "Confirmed"
    assert o.created == date(2026, 6, 10)
    assert o.hotel == "LIMAK ATLANTIS" and o.country == "Туреччина"
    assert o.check_in == date(2026, 8, 21) and o.nights == 7
    assert o.tourists == 2
    assert o.gross_cost == 85000.50 and o.debt == 15000.0 and o.currency == "UAH"
    # store-minimum: the parsed structure has no passport/name fields at all
    assert not hasattr(o, "passport") and not hasattr(o, "customers")


def test_travelon_parse_tolerates_empty_and_nil():
    orders = travelon.parse_orders(SAMPLE_XML)
    o = orders[1]
    assert o.status == "Cancelled" and o.hotel == "" and o.nights is None
    assert o.gross_cost is None and o.debt is None and o.tourists == 0


def test_travelon_empty_orders_is_zero():
    assert travelon.parse_orders("<orders/>") == []


@pytest.mark.asyncio
async def test_travelon_unconfigured_fetch_is_noop():
    assert travelon.configured() is False  # no TRAVELON_TOKEN in tests
    assert await travelon.fetch_period(date(2026, 1, 1), date(2026, 1, 2)) == []
    assert await travelon.brief_line() is None
    assert await travelon.pulse_text() is None
