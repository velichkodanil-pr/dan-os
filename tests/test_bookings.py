"""R8.1 booking feed: digest of new orders + alerts on exceptions.

The fixture mirrors real orders of 23.11.2026 (anonymised): a TAT agency
booking the same hotel four times (two with the same amount), two test orders
on «STAFF STAFF», an Egypt order, a rebooking copy and cancellations."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.core import bookings, scheduler, travelon

TZ = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 11, 23, 15, 40, tzinfo=TZ)


def _order(no, *, created="23.11.2026", hour="12:00:00", status="Confirmed",
           agency=("101", "ФОП КОМАРОВА ОЛЕКСАНДРА АНДРІЇВНА - Мережа TAT"),
           country="Туреччина", city="Аланія", hotel="MERYAN HOTEL",
           check_in="29.06.2027", gross="2296.21", cur="EUR", pax=2,
           staff=False, reason="", rebooked_from="", rebooked_to="",
           cancelled="", family=None):
    # every order gets its own family unless `family` makes two the same
    fam = family or f"FAM{no}"
    people = "".join(
        f"<customer><name>{'STAFF' if staff else f'IVAN{i}'}</name>"
        f"<surname>{'STAFF' if staff else fam}</surname>"
        f"<passport>XX000000</passport><dob>01.01.1990</dob></customer>"
        for i in range(pax))
    cancel = (f"<cancellation-date>{cancelled}</cancellation-date>"
              f"<cancel-reason><requested>false</requested><text nil=\"true\"/>"
              f"<reason>{reason}</reason></cancel-reason>") if cancelled else ""
    rf = f"<rebooked-from>{rebooked_from}</rebooked-from>" if rebooked_from \
        else '<rebooked-from nil="true"/>'
    rt = f"<rebooked-to>{rebooked_to}</rebooked-to>" if rebooked_to \
        else '<rebooked-to nil="true"/>'
    return (f"<order><id>{no}</id><order>{no}</order><status>{status}</status>"
            f"<create-date>{created}</create-date><create-hour>{hour}</create-hour>"
            f"<valute>{cur}</valute>{cancel}{rf}{rt}"
            f"<agency><id>{agency[0]}</id><name>{agency[1]}</name></agency>"
            f"<hotel><hotel-name>{hotel}</hotel-name><country>{country}</country>"
            f"<city-name>{city}</city-name><from-date>{check_in}</from-date>"
            f"<nights>9</nights></hotel>"
            f"<customers>{people}</customers>"
            f"<costs><gross-cost>{gross}</gross-cost></costs></order>")


def _xml(*orders):
    return "<orders>" + "".join(orders) + "</orders>"


DAY = _xml(
    _order("75024", hour="14:10:12", gross="2595.24", pax=3),
    _order("75025", hour="14:16:24", gross="3327.81", pax=4),
    _order("75027", hour="14:22:20"),
    _order("75028", hour="14:28:05"),
    _order("75032", hour="14:37:01", agency=("202", "ТОВ КЛУБ ДАТУР"),
           country="Єгипет", city="Хадаба", hotel="SUNRISE DIAMOND BEACH",
           check_in="20.12.2026", gross="3113.18", pax=3),
    _order("75033", hour="14:38:28", agency=("1", "Транспортал ТОВ"),
           hotel="SummerON", gross="358.8", pax=1, staff=True),
    _order("75036", hour="15:33:43", agency=("1", "Транспортал ТОВ"),
           hotel="SummerON", gross="369.8", pax=1, staff=True),
    _order("75040", hour="15:35:00", agency=("303", "ФОП ПЛУГАТИРЬОВА"),
           rebooked_from="74900", gross="1595.28", check_in="26.12.2026"),
)


# ---------- parsing ----------

def test_parse_feed_fields_and_flags():
    by_no = {o.order_no: o for o in bookings.parse_feed(DAY)}
    o = by_no["75032"]
    assert o.agency == "ТОВ КЛУБ ДАТУР" and o.agency_id == "202"
    assert o.where == "Єгипет, Хадаба" and o.tourists == 3
    assert o.created_at == datetime(2026, 11, 23, 14, 37, 1, tzinfo=TZ)
    assert o.check_in == date(2026, 12, 20) and o.approx_eur == pytest.approx(3113.18)
    assert by_no["75033"].is_test and by_no["75036"].is_test
    assert not o.is_test
    assert by_no["75040"].is_rebooking
    assert not o.is_rebooking and o.rebooked_to == ""   # nil → empty
    # store-minimum: nothing personal on the object
    assert not any(hasattr(o, f) for f in ("passport", "dob", "names"))


def test_test_order_by_dictionary_reason():
    o = bookings.parse_feed(_xml(_order(
        "1", status="Canceled", cancelled="23.11.2026", reason="Тестова")))[0]
    assert o.is_test and o.cancelled


def test_rebooking_cancellation_is_not_lost():
    a, b = bookings.parse_feed(_xml(
        _order("1", status="Canceled", cancelled="23.11.2026",
               reason="Перебронювання"),
        _order("2", status="Canceled", cancelled="23.11.2026", rebooked_to="9")))
    assert a.lost_by_rebooking and b.lost_by_rebooking
    start = NOW - timedelta(days=1)
    assert bookings.lost([a, b], start, NOW) == []


# ---------- digest ----------

def test_digest_counts_sales_only_series_not_duplicates():
    cur = bookings.parse_feed(DAY)
    prev = bookings.parse_feed(_xml(*(_order(str(i), created="16.11.2026")
                                      for i in range(10))))
    cancelled = bookings.parse_feed(_xml(
        _order("74000", status="Canceled", cancelled="23.11.2026",
               reason="Відмова агента", gross="1200")))
    text = bookings.build_digest(cur, prev, cancelled, title="Заявки 23.11")
    # 8 orders: 2 tests and 1 rebooking copy are not sales
    assert "Нових заявок: 5</b> (-50% до тижня тому)" in text
    assert "туристів 14" in text
    assert "Туреччина 4 · Єгипет 1" in text
    assert "КОМАРОВА ОЛЕКСАНДРА АНДРІЇВНА (TAT) 4" in text
    # 23.11.2026 live: same agency + hotel + date + price, DIFFERENT families
    # = a group sale, not a duplicate
    assert "Можливий дубль" not in text
    assert ("👥 Серія: КОМАРОВА ОЛЕКСАНДРА АНДРІЇВНА (TAT) — 4 заявок, "
            "MERYAN HOTEL, заїзд 29.06, 11 тур.") in text
    assert "💶 Сума: 13 629 EUR" in text
    assert "№75025" in text and "Найбільша: №75025" in text
    assert "Анульовано: 1" in text and "Відмова агента 1" in text
    assert "перебронювань 1, тестових 2" in text
    assert "раннє бронювання (виїзд за 90+ днів): 4".lower() in text.lower()
    # no personal data reaches the message
    assert "XX000000" not in text and "IVAN" not in text and "STAFF" not in text


def test_digest_is_html_safe():
    o = bookings.parse_feed(_xml(_order("1", agency=("5", "ТОВ &lt;b&gt;X&lt;/b&gt;"),
                                        hotel="A&amp;B")))
    text = bookings.build_digest(o, [], [], title="t")
    assert "<b>X</b>" not in text and "&lt;b&gt;X" in text


def test_digest_empty_and_missing_cancellations():
    text = bookings.build_digest([], None, None, title="Заявки")
    assert "Нових заявок не було" in text
    assert "Ануляції зараз не вдалося прочитати" in text


def test_report_wallclock_is_fixed_utc_plus_3():
    # Kyiv is UTC+2 in November: 20:30 Kyiv = 18:30 UTC = 21:30 on the report
    assert bookings.report_wallclock(datetime(2026, 11, 23, 20, 30, tzinfo=TZ)) \
        == datetime(2026, 11, 23, 21, 30)
    # summer: Kyiv is UTC+3, no shift
    assert bookings.report_wallclock(datetime(2026, 7, 1, 10, 0, tzinfo=TZ)) \
        == datetime(2026, 7, 1, 10, 0)


@pytest.mark.asyncio
async def test_fetch_created_cuts_the_window_on_creation_time(monkeypatch):
    monkeypatch.setattr(settings, "travelon_token", "T")
    urls = []

    async def fake_get(url):
        urls.append(url)
        return bookings.parse_feed(DAY)
    monkeypatch.setattr(bookings, "_get", fake_get)
    start = datetime(2026, 11, 23, 14, 15, tzinfo=TZ)
    got = await bookings.fetch_created(start, datetime(2026, 11, 23, 14, 30, tzinfo=TZ))
    assert [o.order_no for o in got] == ["75025", "75027", "75028"]
    # padded window in the report's UTC+3 wall clock, every status
    assert urls[0].endswith("/T/2026-11-23T14:15:00/2026-11-23T16:30:00/all")


@pytest.mark.asyncio
async def test_digest_text_survives_report_failure(monkeypatch):
    monkeypatch.setattr(settings, "travelon_token", "T")

    async def boom(url):
        raise RuntimeError("report status 524")
    monkeypatch.setattr(bookings, "_get", boom)
    text = await bookings.digest_text(NOW - timedelta(days=1), NOW, title="x")
    assert "недоступний" in text


# ---------- alerts ----------

def _one(xml_order):
    return bookings.parse_feed(_xml(xml_order))[0]


def test_alert_rules():
    ws = NOW - timedelta(minutes=40)
    kw = dict(now=NOW, window_start=ws, known_agencies={"101": "2026-01-01"})
    assert bookings.alert_reasons(_one(_order("1", hour="15:30:00")), **kw) == []
    big = _one(_order("2", hour="15:30:00", gross="7200"))
    assert ("new", "💶 велика сума") in bookings.alert_reasons(big, **kw)
    group = _one(_order("3", hour="15:30:00", pax=9))
    assert ("new", "👥 група 9 тур.") in bookings.alert_reasons(group, **kw)
    soon = _one(_order("4", hour="15:30:00", check_in="25.11.2026"))
    assert ("new", "⏱ виїзд через 2 дн.") in bookings.alert_reasons(soon, **kw)
    fresh = _one(_order("5", hour="15:30:00", agency=("999", "ФОП НОВИЙ")))
    assert ("new", "🌱 перша заявка агенції") in bookings.alert_reasons(fresh, **kw)
    # history not seeded yet → no «new agency» guesses
    assert bookings.alert_reasons(fresh, now=NOW, window_start=ws,
                                  known_agencies=None) == []
    # created before the window: an old order that merely changed now
    old = _one(_order("6", created="20.11.2026", gross="9000"))
    assert bookings.alert_reasons(old, **kw) == []
    # tests and rebooking copies never alert
    assert bookings.alert_reasons(_one(_order("7", hour="15:30:00", gross="9000",
                                              staff=True)), **kw) == []
    assert bookings.alert_reasons(_one(_order("8", hour="15:30:00", gross="9000",
                                              rebooked_from="1")), **kw) == []
    # USD counts approximately, UAH never triggers the EUR threshold
    assert bookings.alert_reasons(_one(_order("9", hour="15:30:00", gross="6500",
                                              cur="USD")), **kw)
    assert bookings.alert_reasons(_one(_order("10", hour="15:30:00",
                                              gross="300000", cur="UAH")), **kw) == []


def test_cancel_alert_skips_rebookings():
    kw = dict(now=NOW, window_start=NOW - timedelta(minutes=40), known_agencies={})
    lost = _one(_order("1", created="01.11.2026", status="Canceled",
                       cancelled="23.11.2026", reason="Відмова по готелю",
                       gross="4100"))
    assert bookings.alert_reasons(lost, **kw) == [
        ("cancel", "❌ анульовано: Відмова по готелю")]
    rebook = _one(_order("2", created="01.11.2026", status="Canceled",
                         cancelled="23.11.2026", reason="Перебронювання",
                         gross="4100"))
    assert bookings.alert_reasons(rebook, **kw) == []


def test_alert_hours():
    assert bookings.in_alert_hours(NOW)
    assert not bookings.in_alert_hours(NOW.replace(hour=22, minute=5))
    assert not bookings.in_alert_hours(NOW.replace(hour=7, minute=59))


@pytest.mark.asyncio
async def test_poll_alerts_once_per_order_and_kind(db, monkeypatch):
    monkeypatch.setattr(settings, "travelon_token", "T")
    batch = _xml(_order("80001", hour="15:30:00", gross="7200"),
                 _order("80002", hour="15:31:00"),
                 _order("80003", hour="15:32:00", staff=True, gross="9000"))
    calls = []

    async def fake_changed(start, end, *, status="all"):
        calls.append((start, end))
        return bookings.parse_feed(batch)
    monkeypatch.setattr(bookings, "fetch_changed", fake_changed)
    monkeypatch.setattr(bookings, "seed_agencies", _noop_seed)

    first = await bookings.poll_alerts(db, NOW)
    assert first.count("• №") == 1 and "№80001" in first and "велика сума" in first
    assert "STAFF" not in first and "XX000000" not in first
    # first run looks back 30 minutes only — never replays history
    assert calls[0][0] == NOW - timedelta(minutes=30)

    again = await bookings.poll_alerts(db, NOW + timedelta(minutes=10))
    assert again is None                                  # already announced
    # the next window overlaps the previous poll by 30 minutes
    assert calls[1][0] == NOW - timedelta(minutes=30)

    # night: silent, report not even asked
    n = len(calls)
    assert await bookings.poll_alerts(db, NOW.replace(hour=23)) is None
    assert len(calls) == n


async def _noop_seed(*a, **k):
    return 0


@pytest.mark.asyncio
async def test_new_agency_after_seed(db, monkeypatch):
    monkeypatch.setattr(settings, "travelon_token", "T")
    seeded = bookings.parse_feed(_xml(_order("1", created="01.10.2026")))
    await bookings._remember_agencies(db, seeded)
    state = await bookings._load(db, bookings._STATE_AGENCIES)
    state["seeded"] = True
    await bookings._save(db, bookings._STATE_AGENCIES, state)

    batch = _xml(_order("2", hour="15:30:00"),                      # known 101
                 _order("3", hour="15:31:00", agency=("777", "ФОП НОВА")),
                 _order("4", hour="15:32:00", agency=("777", "ФОП НОВА")))

    async def fake_changed(start, end, *, status="all"):
        return bookings.parse_feed(batch)
    monkeypatch.setattr(bookings, "fetch_changed", fake_changed)
    text = await bookings.poll_alerts(db, NOW)
    # one alert for the new agency, not one per order
    assert text.count("перша заявка агенції") == 1 and "№80" not in text
    assert "НОВА" in text
    ids = (await bookings._load(db, bookings._STATE_AGENCIES))["ids"]
    assert ids["777"] == "2026-11-23" and ids["101"] == "2026-10-01"

    # the digest names it too
    new = await bookings._new_agencies(
        db, bookings.sales(bookings.parse_feed(batch)), before=NOW - timedelta(days=1))
    assert [o.agency_id for o in new] == ["777"]


# ---------- scheduler ----------

def test_interval_due():
    now = datetime(2026, 11, 23, 13, 0)
    assert scheduler.interval_due(None, now, 10)
    assert not scheduler.interval_due((now - timedelta(minutes=5)).isoformat(), now, 10)
    assert scheduler.interval_due((now - timedelta(minutes=10)).isoformat(), now, 10)
    assert not scheduler.interval_due(None, now, 0)        # 0 disables
    assert scheduler.interval_due("garbage", now, 10)


def test_token_never_in_digest(monkeypatch):
    monkeypatch.setattr(settings, "travelon_token", "SECRET-TOKEN")
    text = bookings.build_digest(bookings.parse_feed(DAY), [], [], title="x")
    assert "SECRET-TOKEN" not in text
    assert travelon.configured()


def test_duplicate_means_same_tourists():
    orders = bookings.parse_feed(_xml(
        _order("1", family="KOVAL"), _order("2", family="KOVAL", gross="2310"),
        _order("3", family="SHEVCHENKO")))
    dups = bookings.possible_duplicates(bookings.sales(orders))
    assert [[o.order_no for o in g] for g in dups] == [["1", "2"]]
    text = bookings.build_digest(orders, None, [], title="t")
    assert "Можливий дубль (ті самі туристи): №1, №2" in text
    assert "KOVAL" not in text            # names never reach the message
    # the hash is all that is kept, and it is not a name
    assert all(len(o.people_key) == 16 and "KOVAL" not in o.people_key
               for o in orders)


def test_countries_label_no_hotel_and_rest():
    orders = bookings.parse_feed(_xml(
        _order("1"), _order("2"), _order("3", country="", city="", hotel=""),
        *(_order(str(10 + i), country=c) for i, c in enumerate(
            ["Єгипет", "Греція", "Чехія", "ОАЕ", "Болгарія", "Кіпр", "Іспанія"]))))
    line = bookings._countries(bookings.sales(orders))
    assert line.startswith("🌍 Туреччина 2 · ")
    assert "решта 3" in line and line.endswith("без готелю 1")
    assert "інше" not in line


def test_cancellations_without_amounts_show_no_dash():
    gone = bookings.parse_feed(_xml(_order(
        "1", status="Canceled", cancelled="23.11.2026", reason="Відмова по готелю",
        gross="0")))
    text = bookings.build_digest([], None, gone, title="t")
    assert "❌ Анульовано: 1 · Відмова по готелю 1" in text


def test_day_total_line():
    day = bookings.parse_feed(DAY)
    window = [o for o in day if o.created_at >= datetime(2026, 11, 23, 14, 20, tzinfo=TZ)]
    text = bookings.build_digest(window, [], [], title="t", day=day,
                                 day_prev=bookings.parse_feed(_xml(_order("9"))))
    assert "Нових заявок: 3</b>" in text          # 75027, 75028, 75032
    assert "📈 <b>За день: 5 заявок</b> (+400% до тижня тому) · туристів 14" in text


def test_slot_window_every_three_hours():
    slots = "12:00,15:00,18:00,21:00,00:00"
    at = lambda d, h: datetime(2026, 11, d, h, 0, tzinfo=TZ)
    assert bookings.slot_window(at(23, 15), slots) == (at(23, 12), at(23, 0))
    # the first slot of the day covers the morning since midnight
    assert bookings.slot_window(at(23, 12), slots) == (at(23, 0), at(23, 0))
    # the midnight slot closes the PREVIOUS day
    assert bookings.slot_window(at(24, 0), slots) == (at(23, 21), at(23, 0))
    assert bookings.window_title(at(23, 21), at(24, 0)) == "Заявки 23.11, 21:00–24:00"
    assert bookings.window_title(at(23, 12), at(23, 15)) == "Заявки 23.11, 12:00–15:00"


def test_scheduler_slots_skip_stale_and_name_failures():
    now = datetime(2026, 11, 23, 16, 40, tzinfo=TZ)
    assert scheduler.slot_missed(now, "15:00", 50)          # deploy at 16:40
    assert not scheduler.slot_missed(now.replace(hour=15, minute=20), "15:00", 50)
    assert not scheduler.slot_missed(now.replace(hour=0, minute=1), "00:00", 50)
    msg = scheduler.give_up_text("bookings_digest_15:00", 5)
    assert "Дайджест бронювань 15:00" in msg and "/bookings" in msg


@pytest.mark.asyncio
async def test_slot_rituals_fire_once_with_their_slot(db, monkeypatch):
    calls = []

    async def fake(owner, slot=None):
        calls.append(slot)
    monkeypatch.setattr(settings, "bookings_digest_times", "12:00,15:00,00:00")
    async def nop(*a, **k):
        return None
    for now in (datetime(2026, 11, 23, 16, 40, tzinfo=TZ),     # deploy: all stale
                datetime(2026, 11, 24, 0, 0, 30, tzinfo=TZ),   # midnight
                datetime(2026, 11, 24, 0, 1, 0, tzinfo=TZ),    # next tick: done
                datetime(2026, 11, 24, 12, 0, 30, tzinfo=TZ)):
        await scheduler._run_rituals(nop, nop, nop, nop, run_bookings_digest=fake,
                                     now_local=now)
    assert [c for c in calls if c] == ["00:00", "12:00"]
