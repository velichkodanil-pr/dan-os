"""Report statuses: the period endpoint returns CONFIRMED orders unless a
status segment is given. New orders, cancellations and the local cache must
not silently lose every other status."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.core import travelon

D = date(2026, 10, 30)


def _o(no, status="Confirmed", cost=100.0, cancelled_on=None):
    return travelon.TravelonOrder(
        order_no=no, status=status, created=D, hotel="H", country="Т",
        check_in=D, nights=7, tourists=2, gross_cost=cost, currency="EUR",
        debt=None, cancelled_on=cancelled_on)


def test_period_url_segments(monkeypatch):
    monkeypatch.setattr(settings, "travelon_token", "T")
    u = travelon._period_url
    assert u(D, D).endswith("/T/2026-10-30/2026-10-30")
    assert u(D, D, status="all").endswith("/2026-10-30/2026-10-30/all")
    assert u(D, D, status="cancelled", only_updated=True).endswith(
        "/2026-10-30/2026-10-30/cancelled/1")
    assert u(D, D, by_entry_date=True, status="all").endswith(
        "/2026-10-30/2026-10-30/all?by_entry_date=1")
    # only_updated without a status segment would be dropped silently — so
    # it is not emitted at all
    assert u(D, D, only_updated=True).endswith("/2026-10-30/2026-10-30")
    with pytest.raises(ValueError):
        u(D, D, status="canceld")  # the server would answer «confirmed»


def test_parse_cancellation_date():
    xml = ("<orders><order><order>1</order><status>Canceled</status>"
           "<create-date>01.10.2026</create-date><update-date>05.10.2026</update-date>"
           "<cancellation-date>04.10.2026</cancellation-date></order>"
           "<order><order>2</order><status>Canceled</status>"
           "<update-date>06.10.2026</update-date><cancellation-date/></order>"
           "<order><order>3</order><status>Confirmed</status>"
           "<update-date>07.10.2026</update-date></order></orders>")
    a, b, c = travelon.parse_orders(xml)
    assert a.cancelled_on == date(2026, 10, 4)
    assert b.cancelled_on == date(2026, 10, 6)   # empty → update-date
    assert c.cancelled_on is None


@pytest.mark.asyncio
async def test_pulse_counts_every_new_order(monkeypatch):
    monkeypatch.setattr(settings, "travelon_token", "T")
    today = datetime.now(ZoneInfo(settings.tz_name)).date()
    seen = []

    async def fake_period(d_from, d_to, by_entry_date=False, *, status="",
                          only_updated=False):
        seen.append((by_entry_date, status, only_updated))
        if only_updated:
            return [_o("C1", "Canceled", cancelled_on=today),
                    _o("C2", "Canceled", cancelled_on=today - timedelta(days=3))]
        if by_entry_date:
            return []
        return [_o("1"), _o("2", "Confirmed Print"), _o("3", "In Work"),
                _o("4", "Not Confirmed"), _o("5", "Accepted For Processing"),
                _o("6", "Canceled")]
    monkeypatch.setattr(travelon, "fetch_period", fake_period)

    text = await travelon.pulse_text()
    # created days are fetched with /all, arrivals keep the confirmed default
    assert (False, "all", False) in seen and (True, "", False) in seen
    assert "сьогодні 5 · вчора 5 (підтверджено 2 · 2)" in text
    assert "Анульовано сьогодні: 1" in text   # C2 was cancelled days ago


@pytest.mark.asyncio
async def test_stats_default_counts_confirmed_only(db):
    from app.models import TravelonOrderCache
    today = date.today()
    owner = 111

    def row(no, status, pax):
        return TravelonOrderCache(
            user_id=owner, domain="travelon", order_no=no, status=status,
            provider="Kalanit Tour Turkey", hotel="H", country="Туреччина",
            check_in=today + timedelta(days=5), created=today, nights=7,
            tourists=pax, gross_cost=100.0, currency="EUR")
    db.add_all([row("1", "Confirmed", 2), row("2", "Confirmed Print", 3),
                row("3", "In Work", 4), row("4", "Not Confirmed", 5),
                row("5", "Canceled", 6)])
    await db.commit()
    base = dict(user_id=owner, date_from=today, date_to=today + timedelta(days=30))
    assert (await travelon.stats(db, **base))["total_tourists"] == 5
    every = await travelon.stats(db, **base, include_cancelled=True,
                                 group_by="status")
    assert every["total_tourists"] == 20 and every["groups_total"] == 5
