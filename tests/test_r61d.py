"""R6.1D — aggregates over OUR orders (the Kalanit case).

«Скільки туристів заброньовано в Туреччину з приймаючою Kalanit» had no tool
at all: `travelon_order` answers one order, `travelon_pulse` a fixed set of
metrics. The model fell back to the knowledge base and honestly found only QA
chat logs. These tests pin the cache, the aggregate and the domain gate.

Owner decision: we only ever ask OUR system. No supplier cabinets.
"""
import json
from datetime import date, timedelta

import pytest

from app.core import chat_tools, travelon
from app.models import TravelonOrderCache

OWNER = 111
TODAY = date(2026, 8, 20)


def _row(no, provider, pax, day, country="Туреччина", status="Confirmed"):
    return TravelonOrderCache(
        user_id=OWNER, domain="travelon", order_no=no, status=status,
        provider=provider, hotel="CESARS SIDE", country=country,
        check_in=day, created=day - timedelta(days=20), nights=7,
        tourists=pax, gross_cost=1200.0, currency="EUR")


async def _seed(db):
    db.add_all([
        _row("64772", "Kalanit Tour Turkey", 4, TODAY + timedelta(days=5)),
        _row("64773", "Kalanit Tour Turkey", 2, TODAY + timedelta(days=40)),
        _row("64774", "Kalanit Tour Turkey", 3, TODAY + timedelta(days=41)),
        _row("64775", "Gepard Travel Turkey", 5, TODAY + timedelta(days=6)),
        _row("64776", "Summer Tour Turkey", 2, TODAY + timedelta(days=7)),
        _row("64777", "Kalanit Tour Turkey", 9, TODAY - timedelta(days=40)),   # past
        _row("64778", "Kalanit Tour Turkey", 7, TODAY + timedelta(days=8),
             status="Cancelled"),                                             # cancelled
        _row("64779", "Melino Travel", 2, TODAY + timedelta(days=9), country="Єгипет"),
    ])
    await db.commit()


# ───────── aggregates ─────────

# 1
async def test_01_provider_filter_counts_tourists(db):
    await _seed(db)
    r = await travelon.stats(db, user_id=OWNER, date_from=TODAY,
                             date_to=TODAY + timedelta(days=90),
                             provider="kalanit")
    # 4 + 2 + 3 = 9; the past order and the cancelled one are excluded
    assert r["total_orders"] == 3 and r["total_tourists"] == 9


# 2
async def test_02_filter_is_case_insensitive_substring(db):
    await _seed(db)
    for q in ("kalanit", "KALANIT", "Kalanit Tour"):
        r = await travelon.stats(db, user_id=OWNER, date_from=TODAY,
                                 date_to=TODAY + timedelta(days=90), provider=q)
        assert r["total_tourists"] == 9, q


# 3
async def test_03_cancelled_excluded_unless_asked(db):
    await _seed(db)
    base = dict(user_id=OWNER, date_from=TODAY, date_to=TODAY + timedelta(days=90),
                provider="kalanit")
    assert (await travelon.stats(db, **base))["total_tourists"] == 9
    withc = await travelon.stats(db, **base, include_cancelled=True)
    assert withc["total_tourists"] == 16          # + the 7-pax cancelled order


# 4
async def test_04_past_checkins_are_outside_the_default_window(db):
    """«Скільки заброньовано» means who is still coming."""
    await _seed(db)
    r = await travelon.stats(db, user_id=OWNER, date_from=TODAY,
                             date_to=TODAY + timedelta(days=90), provider="kalanit")
    assert "64777" not in json.dumps(r)
    back = await travelon.stats(db, user_id=OWNER,
                                date_from=TODAY - timedelta(days=60),
                                date_to=TODAY + timedelta(days=90),
                                provider="kalanit")
    assert back["total_tourists"] == 18           # 9 + the 9-pax past order


# 5
async def test_05_group_by_provider_ranks_by_tourists(db):
    await _seed(db)
    r = await travelon.stats(db, user_id=OWNER, date_from=TODAY,
                             date_to=TODAY + timedelta(days=90),
                             country="туреч", group_by="provider")
    keys = [g["key"] for g in r["groups"]]
    assert keys[0] == "Kalanit Tour Turkey"       # 9 tourists beats Gepard's 5
    assert r["groups"][0]["orders"] == 3
    assert "Melino Travel" not in keys            # Egypt filtered out by country


# 6
async def test_06_group_by_month(db):
    await _seed(db)
    r = await travelon.stats(db, user_id=OWNER, date_from=TODAY,
                             date_to=TODAY + timedelta(days=90),
                             provider="kalanit", group_by="month")
    by = {g["key"]: g["tourists"] for g in r["groups"]}
    assert by["08.2026"] == 4 and by["09.2026"] == 5


# 7
async def test_07_answer_states_its_basis_and_source(db):
    """An aggregate that does not say what it counted is a trap."""
    await _seed(db)
    r = await travelon.stats(db, user_id=OWNER, date_from=TODAY,
                             date_to=TODAY + timedelta(days=90), provider="kalanit")
    assert r["basis"] == "дата заїзду"
    assert "TravelON" in r["note"] and "постачальник" in r["note"]
    assert r["filters"] == {"provider": "kalanit"}


# 8
async def test_08_empty_result_is_zero_not_an_error(db):
    await _seed(db)
    r = await travelon.stats(db, user_id=OWNER, date_from=TODAY,
                             date_to=TODAY + timedelta(days=90), provider="няма")
    assert r["total_orders"] == 0 and r["groups"] == []


# ───────── cache hygiene ─────────

# 9
async def test_09_cache_is_travelon_domain_only(db):
    """Business data lives in the travelon domain — R6.1B invariant."""
    await _seed(db)
    rows = (await db.execute(__import__("sqlalchemy").select(TravelonOrderCache))).scalars().all()
    assert rows and all(r.domain == "travelon" for r in rows)


# 10
async def test_10_cache_stores_no_tourist_names(db):
    """Store-minimum: a COUNT, never the passenger list."""
    cols = set(TravelonOrderCache.__table__.columns.keys())
    for forbidden in ("name", "surname", "passport", "dob", "idno", "customers"):
        assert not any(forbidden in c for c in cols), forbidden
    assert "tourists" in cols


# 11
async def test_11_cache_span_reports_coverage(db):
    await _seed(db)
    span = await travelon.cache_span(db, user_id=OWNER)
    assert span["orders"] == 8
    assert span["from"] == TODAY - timedelta(days=40)
    assert span["to"] == TODAY + timedelta(days=41)


# 12
async def test_12_provider_is_parsed_from_order_xml():
    xml = """<?xml version="1.0"?><orders><order><order>1</order>
      <hotel><hotel-name>H</hotel-name><provider>Kalanit Tour Turkey</provider>
      <country>Туреччина</country><from-date>12.08.2026</from-date></hotel>
      <customers><customer><name>A</name></customer></customers></order></orders>"""
    o = travelon.parse_orders(xml)[0]
    assert o.provider == "Kalanit Tour Turkey"


# ───────── the agent tool ─────────

# 13
def test_13_stats_tool_is_travelon_only():
    tv = {t["name"] for t in chat_tools.tools_for_domain("travelon")}
    pers = {t["name"] for t in chat_tools.tools_for_domain("personal")}
    assert "travelon_stats" in tv and "travelon_stats" not in pers


# 14
@pytest.mark.asyncio
async def test_14_stats_tool_fails_closed_outside_travelon(db, monkeypatch):
    called = []

    async def _boom(*a, **k):
        called.append(1)
        return {}
    monkeypatch.setattr(travelon, "stats", _boom)
    for dom in ("personal", "tech"):
        res = json.loads(await chat_tools.run_tool(
            db, OWNER, dom, "travelon_stats", {"provider": "kalanit"}))
        assert res.get("error") == "wrong_domain"
    assert called == []


# 15
@pytest.mark.asyncio
async def test_15_stats_tool_answers_the_kalanit_question(db, monkeypatch):
    """The default window is 7 months out, so mark it covered and check the
    arithmetic — on-demand filling is covered by test 16."""
    await _seed(db)

    async def _covered(*a, **k):
        return []
    monkeypatch.setattr(travelon, "missing_days", _covered)
    raw = await chat_tools.run_tool(db, OWNER, "travelon", "travelon_stats",
                                    {"provider": "Kalanit", "country": "туреч"})
    data = json.loads(raw)
    assert data["total_tourists"] == 9 and data["total_orders"] == 3
    assert data["basis"] == "дата заїзду"
    assert data["coverage"] == "повне"


# 16
@pytest.mark.asyncio
async def test_16_uncovered_period_is_fetched_on_demand(db, monkeypatch):
    """The point of «розумний і гнучкий»: ask about a month nobody prepared,
    and the bot fetches it once instead of returning a caveat."""
    await _seed(db)
    from app.config import settings
    monkeypatch.setattr(settings, "travelon_token", "T")
    calls = []

    async def _fake_sync(_db, *, user_id, date_from, date_to, basis="check_in"):
        calls.append((date_from, date_to, basis))
        return {"status": "ok", "days": (date_to - date_from).days + 1,
                "days_failed": 0, "orders": 0, "added": 0, "updated": 0}
    monkeypatch.setattr(travelon, "sync_orders", _fake_sync)
    data = json.loads(await chat_tools.run_tool(
        db, OWNER, "travelon", "travelon_stats",
        {"provider": "kalanit", "date_from": "01.05.2026", "date_to": "31.05.2026"}))
    assert calls, "вікно поза кешем має добиратись автоматично"
    assert data.get("just_fetched_days")


# 17
@pytest.mark.asyncio
async def test_17_window_far_too_large_is_refused_not_truncated(db, monkeypatch):
    """A silently truncated window would look like a complete answer."""
    await _seed(db)
    from app.config import settings
    monkeypatch.setattr(settings, "travelon_token", "T")

    async def _boom(*a, **k):
        raise AssertionError("must not fetch a window this large")
    monkeypatch.setattr(travelon, "sync_orders", _boom)
    data = json.loads(await chat_tools.run_tool(
        db, OWNER, "travelon", "travelon_stats",
        {"date_from": "01.01.2020", "date_to": "31.12.2020"}))
    assert data["found"] is False and data["reason"] == "window_too_large"


# 18
def test_18_tool_schema_has_no_domain():
    for t in chat_tools.TOOL_DEFS:
        assert "domain" not in (t["input_schema"].get("properties") or {})


# 19
def test_19_date_parser_accepts_both_formats():
    from app.core.chat_tools import _parse_day
    assert _parse_day("12.08.2026") == date(2026, 8, 12)
    assert _parse_day("2026-08-12") == date(2026, 8, 12)
    assert _parse_day("не дата") is None and _parse_day("") is None


# 20
def test_20_nightly_sync_is_wired_into_the_scheduler():
    import inspect
    from app.core import scheduler
    from app.config import settings
    assert "run_tv_sync" in inspect.signature(scheduler.start).parameters
    assert settings.travelon_sync_time


# ───────── flexibility (R6.1D refinement) ─────────

# 21
async def test_21_coverage_distinguishes_empty_from_unfetched(db):
    """A day with zero orders is NOT a gap — otherwise the bot re-fetches
    forever, or worse, reports 0 for a period it never looked at."""
    gaps = await travelon.missing_days(db, user_id=OWNER, date_from=TODAY,
                                       date_to=TODAY + timedelta(days=2))
    assert len(gaps) == 3                      # nothing fetched yet
    from app.models import TravelonSyncDay
    db.add(TravelonSyncDay(user_id=OWNER, basis="check_in", day=TODAY, orders=0))
    await db.commit()
    gaps = await travelon.missing_days(db, user_id=OWNER, date_from=TODAY,
                                       date_to=TODAY + timedelta(days=2))
    assert TODAY not in gaps and len(gaps) == 2


# 22
async def test_22_coverage_is_tracked_per_basis(db):
    from app.models import TravelonSyncDay
    db.add(TravelonSyncDay(user_id=OWNER, basis="check_in", day=TODAY, orders=3))
    await db.commit()
    assert await travelon.missing_days(db, user_id=OWNER, date_from=TODAY,
                                       date_to=TODAY, basis="check_in") == []
    assert await travelon.missing_days(db, user_id=OWNER, date_from=TODAY,
                                       date_to=TODAY, basis="created") == [TODAY]


# 23
async def test_23_created_basis_counts_a_different_question(db):
    """«Скільки їде в серпні» and «скільки продали в серпні» are not the same."""
    await _seed(db)
    by_checkin = await travelon.stats(db, user_id=OWNER, date_from=TODAY,
                                      date_to=TODAY + timedelta(days=90),
                                      provider="kalanit", basis="check_in")
    by_created = await travelon.stats(db, user_id=OWNER,
                                      date_from=TODAY - timedelta(days=30),
                                      date_to=TODAY, provider="kalanit",
                                      basis="created")
    assert by_checkin["basis"] == "дата заїзду"
    assert by_created["basis"] == "дата створення заявки"
    assert by_created["total_orders"] and by_created["total_orders"] != 0


# 24
async def test_24_money_is_summed_per_currency_never_mixed(db):
    await _seed(db)
    db.add(_row("64999", "Kalanit Tour Turkey", 2, TODAY + timedelta(days=5)))
    await db.commit()
    r = await travelon.stats(db, user_id=OWNER, date_from=TODAY,
                             date_to=TODAY + timedelta(days=90), provider="kalanit")
    assert set(r["total_money"]) == {"EUR"}
    assert r["total_money"]["EUR"] == 4 * 1200.0
    assert r["groups"][0]["money"]["EUR"] == 4 * 1200.0


# 25
async def test_25_group_truncation_is_reported(db):
    await _seed(db)
    r = await travelon.stats(db, user_id=OWNER, date_from=TODAY,
                             date_to=TODAY + timedelta(days=90),
                             group_by="provider", limit=1)
    assert r["groups_shown"] == 1 and r["groups_total"] > 1


# 26a
@pytest.mark.asyncio
async def test_26a_no_token_still_answers_from_cache_but_says_so(db):
    """Losing the connector must degrade the answer, not refuse it."""
    await _seed(db)
    data = json.loads(await chat_tools.run_tool(
        db, OWNER, "travelon", "travelon_stats", {"provider": "kalanit"}))
    assert data["total_tourists"] == 9          # cache still answers
    assert data["coverage"].startswith("неповне")
    assert "недоступний" in data["warning"]


# 26
def test_26_tool_exposes_basis_and_cancelled_controls():
    t = [x for x in chat_tools.TOOL_DEFS if x["name"] == "travelon_stats"][0]
    props = t["input_schema"]["properties"]
    assert set(props["basis"]["enum"]) == {"check_in", "created"}
    assert "include_cancelled" in props
    assert "АВТОМАТИЧНО" in t["description"]      # the model must know it can fetch
