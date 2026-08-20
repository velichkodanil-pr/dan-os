"""TravelON owner pack: order lookup trigger, debt alert, weekly block."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.core import travelon
from app.core.orchestrator import _order_lookup_no, Orchestrator

OWNER = 111


def _order(no="59266", status="Confirmed", debt=None, cost=85000.0, cur="UAH",
           country="Туреччина", check_in=None, tourists=2):
    return travelon.TravelonOrder(
        order_no=no, status=status, created=date(2026, 6, 10), hotel="LIMAK",
        country=country, check_in=check_in or date(2026, 8, 21), nights=7,
        tourists=tourists, gross_cost=cost, currency=cur, debt=debt)


# ---------- order regex ----------

def test_order_regex():
    assert _order_lookup_no("заявка 59266") == "59266"
    assert _order_lookup_no("Що там по заявці №66422?") == "66422"
    assert _order_lookup_no("покажи №66784") == "66784"
    assert _order_lookup_no("order 12345") == "12345"
    assert _order_lookup_no("нагадай завтра о 10 подзвонити") is None
    assert _order_lookup_no("додай 5 задач") is None  # short numbers ignored


# ---------- order card ----------

def test_order_card_render():
    card = travelon.order_card(_order(debt=15000.0))
    assert "Заявка №59266" in card and "Confirmed" in card
    assert "LIMAK · Туреччина" in card
    assert "борг 15 000 UAH ⚠️" in card
    paid = travelon.order_card(_order(debt=0.0))
    assert "оплачено ✅" in paid
    assert "<" not in card  # plain text — the chat layer escapes


# ---------- orchestrator trigger ----------

@pytest.mark.asyncio
async def test_order_lookup_in_chat(db, monkeypatch):
    monkeypatch.setattr(settings, "travelon_token", "T")

    async def fake_fetch(no):
        return _order(no=no) if no == "59266" else None
    monkeypatch.setattr(travelon, "fetch_order", fake_fetch)

    # the deterministic order lookup fires only in the travelon domain
    from app.core.domains import set_active_domain
    await set_active_domain(db, OWNER, "travelon")
    await db.commit()

    orch = Orchestrator()
    out = await orch.handle_note(db, user_id=OWNER, text="що по заявці 59266?",
                                 dedupe_key="ord-1")
    assert out.kind == "chat" and "Заявка №59266" in out.reply
    # R6.1C: a MISS is no longer a dead end. The shortcut guessed a number and
    # guessed wrong, so the message falls through to the normal chat path
    # (which has travelon_order/_document) instead of answering «не знайшов»
    # and burying whatever the owner actually asked.
    out2 = await orch.handle_note(db, user_id=OWNER, text="заявка 11111",
                                  dedupe_key="ord-2")
    assert out2.kind == "chat"
    assert "Заявка №11111" not in (out2.reply or "")


@pytest.mark.asyncio
async def test_order_lookup_skipped_when_unconfigured(db, monkeypatch):
    async def boom(_no):
        raise AssertionError("must not fetch when unconfigured")
    monkeypatch.setattr(travelon, "fetch_order", boom)
    orch = Orchestrator()
    out = await orch.handle_note(db, user_id=OWNER, text="заявка 59266",
                                 dedupe_key="ord-3")
    assert out.kind == "chat"  # falls through to the normal chat path


# ---------- debt alert ----------

@pytest.mark.asyncio
async def test_debt_alert(monkeypatch):
    monkeypatch.setattr(settings, "travelon_token", "T")
    tomorrow = datetime.now(ZoneInfo(settings.tz_name)).date() + timedelta(days=1)

    async def with_debts(days, by_entry_date=False):
        assert days == [tomorrow] and by_entry_date
        return [_order(no="1", debt=500.0, cur="EUR", check_in=tomorrow),
                _order(no="2", debt=None, check_in=tomorrow),
                _order(no="3", status="Cancelled", debt=900.0, check_in=tomorrow)]
    monkeypatch.setattr(travelon, "fetch_days", with_debts)
    text = await travelon.debt_alert_text()
    assert "Завтра заїзд із боргом:</b> 1" in text and "500 EUR" in text
    assert "№3" not in text  # cancelled excluded

    async def clean(days, by_entry_date=False):
        return [_order(no="1", debt=0.0, check_in=tomorrow)]
    monkeypatch.setattr(travelon, "fetch_days", clean)
    assert await travelon.debt_alert_text() is None  # silence when all paid


# ---------- weekly block ----------

@pytest.mark.asyncio
async def test_weekly_block(monkeypatch):
    monkeypatch.setattr(settings, "travelon_token", "T")

    async def week(days, by_entry_date=False):
        assert len(days) == 7 and not by_entry_date
        return [_order(no="1", country="Туреччина", cost=100.0, cur="EUR"),
                _order(no="2", country="Туреччина", cost=200.0, cur="EUR"),
                _order(no="3", country="Єгипет", cost=300.0, cur="EUR"),
                _order(no="4", status="Cancelled", cost=999.0, cur="EUR")]
    monkeypatch.setattr(travelon, "fetch_days", week)
    block = await travelon.weekly_block()
    assert "3 нових заявок" in block and "600 EUR" in block
    assert "Туреччина (2)" in block
