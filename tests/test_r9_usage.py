"""R9 — token & cost meter.

Pins: prices per model (incl. Haiku 5.5 long-prompt tier, cache, web search);
the feature is the real caller, not the relay; provider_health.post records a
row after a 2xx and never on failure; the meter never breaks a call; rows hold
numbers only; the daily alert fires once; the report renders.
"""
import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.core import provider_health, usage
from app.models import AppState, TokenUsage


def test_prices():
    r = usage.parse("anthropic", "https://api.anthropic.com/v1/messages",
                    {"model": "claude-opus-5-5"},
                    {"usage": {"input_tokens": 10_000, "output_tokens": 2_000,
                               "cache_read_input_tokens": 50_000,
                               "cache_creation_input_tokens": 4_000,
                               "server_tool_use": {"web_search_requests": 2}}})
    # 10k×4 + 2k×20 + 50k×0.2 + 4k×5 = 40k+40k+10k+20k = 110k /1M = 0.11 + 2×0.01
    assert r["cost_usd"] == pytest.approx(0.13)
    assert r["web_searches"] == 2 and not r["approx"]
    short = usage.parse("anthropic", "x", {"model": "claude-haiku-5-5"},
                        {"usage": {"input_tokens": 1000, "output_tokens": 100}})
    assert short["cost_usd"] == pytest.approx((1000 * 0.10 + 100 * 0.50) / 1e6)
    long = usage.parse("anthropic", "x", {"model": "claude-haiku-5-5"},
                       {"usage": {"input_tokens": 120_000, "output_tokens": 100}})
    assert long["cost_usd"] == pytest.approx((120_000 * 0.50 + 100 * 2.50) / 1e6)
    unknown = usage.parse("anthropic", "x", {"model": "claude-new-9"},
                          {"usage": {"input_tokens": 5}})
    assert unknown["approx"] and unknown["cost_usd"] == 0


def test_openai_shapes():
    emb = usage.parse("openai", "https://api.openai.com/v1/embeddings",
                      {"model": "text-embedding-3-small"},
                      {"usage": {"prompt_tokens": 1_000_000, "total_tokens": 1_000_000}})
    assert emb["cost_usd"] == pytest.approx(0.02)
    tts = usage.parse("openai", "https://api.openai.com/v1/audio/speech",
                      {"model": "gpt-4o-mini-tts", "input": "x" * 2000}, None)
    assert tts["approx"] and tts["cost_usd"] == pytest.approx(0.03)


def test_feature_is_the_real_caller():
    """A call relayed through extraction.haiku_text / embeddings is booked on
    the module that asked for it."""
    def fake(module, src):
        g = {"__name__": module, "usage": usage}
        exec(src, g)
        return g
    relay = fake("app.core.extraction",
                 "def haiku_text():\n    return usage.feature_from_stack()\n")
    caller = fake("app.core.energy", "def explain():\n    return haiku_text()\n")
    caller["haiku_text"] = relay["haiku_text"]
    assert caller["explain"]() == "energy"
    emb = fake("app.core.embeddings", "def embed():\n    return usage.feature_from_stack()\n")
    rag = fake("app.core.rag", "def search():\n    return embed()\n")
    rag["embed"] = emb["embed"]
    assert rag["search"]() == "rag (ембединги)"
    assert usage.feature_from_stack() == "unknown"     # tests are not app code
    assert usage.label("chat") == "чат" and usage.label("rag (ембединги)") == "пошук у базі (ембединги)"


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, status, payload):
        self.status, self.payload = status, payload

    async def handle_async_request(self, request):
        return httpx.Response(self.status, json=self.payload)


async def test_post_records_after_success_only(db):
    ok = {"usage": {"input_tokens": 100, "output_tokens": 10}, "content": [{"text": "SECRET-REPLY"}]}
    async with httpx.AsyncClient(transport=_Transport(200, ok)) as c:
        await provider_health.post(c, "anthropic", "https://api.anthropic.com/v1/messages",
                                   json={"model": "claude-opus-5-5",
                                         "messages": [{"role": "user", "content": "SECRET-PROMPT"}]})
    async with httpx.AsyncClient(transport=_Transport(400, {"error": "bad"})) as c:
        await provider_health.post(c, "anthropic", "https://api.anthropic.com/v1/messages",
                                   json={"model": "claude-opus-5-5"})
    rows = (await db.execute(select(TokenUsage))).scalars().all()
    assert len(rows) == 1
    r = rows[0]
    assert (r.model, r.input_tokens, r.output_tokens) == ("claude-opus-5-5", 100, 10)
    assert r.cost_usd == pytest.approx((100 * 4 + 10 * 20) / 1e6)
    dump = " ".join(str(getattr(r, c.name)) for c in TokenUsage.__table__.columns)
    assert "SECRET" not in dump


async def test_meter_never_breaks_a_call(db, monkeypatch):
    async def boom(rec):
        raise RuntimeError("db down")
    monkeypatch.setattr(usage, "_store", boom)
    async with httpx.AsyncClient(transport=_Transport(200, {"usage": {}})) as c:
        resp = await provider_health.post(c, "anthropic", "https://api.anthropic.com/v1/messages",
                                          json={"model": "claude-opus-5-5"})
    assert resp.status_code == 200


async def test_daily_alert_once(db, monkeypatch):
    sent = []

    async def sink(text):
        sent.append(text)
    provider_health.set_alert_sink(sink)
    monkeypatch.setattr(settings, "usage_alert_usd_day", 0.5)
    big = {"usage": {"input_tokens": 100_000, "output_tokens": 10_000}}   # $0.60 on Opus 5.5
    try:
        for _ in range(2):
            async with httpx.AsyncClient(transport=_Transport(200, big)) as c:
                await provider_health.post(c, "anthropic", "https://api.anthropic.com/v1/messages",
                                           json={"model": "claude-opus-5-5"})
    finally:
        provider_health.set_alert_sink(None)
    assert len(sent) == 1 and "$0.60" in sent[0]
    assert (await db.get(AppState, "usage_alert_date")) is not None


async def test_report_renders(db):
    assert "даних ще немає" in await usage.report(db, 7)
    db.add(TokenUsage(provider="anthropic", model="claude-opus-5-5", feature="chat",
                      input_tokens=20_000, output_tokens=3_000, cache_read=10_000,
                      cost_usd=0.142))
    db.add(TokenUsage(provider="anthropic", model="claude-haiku-5-5", feature="digest",
                      input_tokens=5_000, output_tokens=300, cost_usd=0.00065))
    await db.commit()
    text = await usage.report(db, 7)
    assert "чат: 1 · $0.142" in text and "дайджест пошти" in text
    assert "opus-5-5" in text and "кеш 33%" in text and "Найдорожчі" in text
