"""R4b extras: TravelON pulse cache, TTS gate, webapp act request shape."""
from datetime import date

import pytest

from app.config import settings
from app.core import travelon, tts

OWNER = 111


@pytest.mark.asyncio
async def test_pulse_data_uses_cache(db, monkeypatch):
    monkeypatch.setattr(settings, "travelon_token", "T")
    calls = {"n": 0}

    async def fake_period(d_from, d_to, by_entry_date=False):
        calls["n"] += 1
        return [travelon.TravelonOrder(
            order_no=f"O{calls['n']}", status="Confirmed", created=date.today(),
            hotel="H", country="Т", check_in=date.today(), nights=7, tourists=2,
            gross_cost=100.0, currency="EUR", debt=None)]
    monkeypatch.setattr(travelon, "fetch_period", fake_period)

    d1 = await travelon.pulse_data(db)
    assert d1 is not None and calls["n"] == 9  # 2 created days + 7 arrival days
    d2 = await travelon.pulse_data(db)
    assert calls["n"] == 9  # served from app_state cache — no new fetches
    assert d2 == d1
    d3 = await travelon.pulse_data(db, max_age=0)  # stale -> refetch
    assert calls["n"] == 18 and d3 is not None


@pytest.mark.asyncio
async def test_pulse_text_renders_from_data(db, monkeypatch):
    monkeypatch.setattr(settings, "travelon_token", "T")

    async def fake_data(_db=None, max_age=600):
        return {"date": "13.08", "created_today": 3, "created_yesterday": 5,
                "sum_2d": "1 000 EUR", "arrivals_today": 2, "arrivals_tomorrow": 4,
                "arrivals_week": 20, "tourists": 55, "debt_count": 1,
                "debt_total": "300 EUR",
                "debtors": [{"when": "14.08", "order_no": "1", "where": "Т",
                             "amount": "300 EUR"}],
                "generated_at": "10:00"}
    monkeypatch.setattr(travelon, "pulse_data", fake_data)
    text = await travelon.pulse_text(db)
    assert "сьогодні 3 · вчора 5" in text and "300 EUR" in text
    assert "Станом на 10:00" in text


def test_tts_gate(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-x")
    assert tts.should_speak("Коротка відповідь", True)
    assert not tts.should_speak("Коротка відповідь", False)  # toggled off
    assert not tts.should_speak("", True)
    assert not tts.should_speak("а" * (settings.tts_max_chars + 1), True)
    monkeypatch.setattr(settings, "openai_api_key", "")
    assert not tts.should_speak("Текст", True)  # no provider


def test_act_request_id_optional():
    from app.webapp.routes import ActRequest
    r = ActRequest(action="goal_add", text="Ціль")
    assert r.id == "" and r.text == "Ціль"
