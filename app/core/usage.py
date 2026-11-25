"""Token & cost meter (R9). Every successful provider call → one row of numbers.

Hooked into provider_health.post, the single door every Anthropic / OpenAI
call already goes through, so nothing can bypass it. Stores model, the
feature that made the call (from the call stack), token counts, web searches
and the cost in USD. Never the prompt, the reply or any content.

Prices are list prices per million tokens (Claude API, Nov 2026). OpenAI audio
is billed per minute upstream and is only approximated here (flag `approx`).
The meter must never break a call: every failure is logged and swallowed.
"""
import html
import inspect
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.config import settings

logger = logging.getLogger(__name__)

M = 1_000_000

# $/MTok: input, output, cache read, cache write (5 min). Prefix-matched,
# longest prefix wins. Source: platform.claude.com pricing, 2026-11.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.5),
    "claude-sonnet-4": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),   # prompt ≤100k tokens
    "claude-haiku-4": (1.0, 5.0, 0.10, 1.25),
    "claude-fable-5": (8.0, 40.0, 0.20, 10.0),
    "claude-mythos-5": (8.0, 40.0, 0.20, 10.0),
    "text-embedding-3-small": (0.02, 0.0, 0.0, 0.0),
    "text-embedding-3-large": (0.13, 0.0, 0.0, 0.0),
    # OpenAI audio: token prices where the API reports tokens (≈)
    "gpt-4o-mini-transcribe": (1.25, 5.0, 0.0, 0.0),
    "gpt-4o-transcribe": (2.5, 10.0, 0.0, 0.0),
}
HAIKU55_LONG = (0.50, 2.50, 0.05, 0.625)            # prompt >100k tokens
WEB_SEARCH_USD = 10.0 / 1000                         # per search
TTS_USD_PER_1K_CHARS = 0.015                         # ≈ gpt-4o-mini-tts

FEATURE_LABEL = {
    "chat": "чат", "english": "англійська", "digest": "дайджест пошти",
    "extraction": "розбір нотаток", "memory": "пам'ять", "reports": "звіти",
    "wiki": "вікі", "meetings": "зустрічі", "energy": "аварії Deye",
    "orchestrator": "оркестратор", "transcription": "голос → текст",
    "tts": "озвучка", "ingest": "база знань (індекс)", "rag": "пошук у базі",
    "briefs": "брифи", "coach": "коуч", "bookings": "заявки TravelON",
}

# frames that only relay a call for somebody else
_WRAPPERS = {("app.core.provider_health", None), ("app.core.extraction", "haiku_text"),
             ("app.core.embeddings", None), ("app.core.usage", None)}


def price_for(model: str, prompt_tokens: int = 0) -> tuple[tuple, bool]:
    """((in, out, read, write), known). Unknown models cost 0 and are flagged."""
    m = (model or "").casefold()
    if m.startswith("claude-haiku-5-5") and prompt_tokens > 100_000:
        return HAIKU55_LONG, True
    best = max((p for p in PRICES if m.startswith(p)), key=len, default=None)
    if best is None:
        return (0.0, 0.0, 0.0, 0.0), False
    return PRICES[best], True


def feature_from_stack() -> str:
    """Short module name of the first app frame that is not a relay."""
    via_embed = False
    for fr in inspect.stack()[1:40]:
        mod = fr.frame.f_globals.get("__name__", "")
        if not mod.startswith("app."):
            continue
        if (mod, None) in _WRAPPERS or (mod, fr.function) in _WRAPPERS:
            via_embed = via_embed or mod == "app.core.embeddings"
            continue
        name = mod.rsplit(".", 1)[-1]
        if mod.startswith("app.telegram") or mod.startswith("app.webapp"):
            name = "telegram" if mod.startswith("app.telegram") else "webapp"
        return f"{name}" + (" (ембединги)" if via_embed and name != "embeddings" else "")
    return "embeddings" if via_embed else "unknown"


def parse(provider: str, url: str, request_json: dict | None,
          response_json: dict | None) -> dict:
    """Numbers from one call. Pure — covered by tests."""
    req = request_json or {}
    data = response_json or {}
    u = data.get("usage") or {}
    rec = {"provider": provider, "model": str(req.get("model") or data.get("model") or "")[:64],
           "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0,
           "web_searches": 0, "cost_usd": 0.0, "approx": False}
    if provider == "anthropic":
        rec["input_tokens"] = int(u.get("input_tokens") or 0)
        rec["output_tokens"] = int(u.get("output_tokens") or 0)
        rec["cache_read"] = int(u.get("cache_read_input_tokens") or 0)
        rec["cache_write"] = int(u.get("cache_creation_input_tokens") or 0)
        rec["web_searches"] = int((u.get("server_tool_use") or {}).get("web_search_requests") or 0)
    elif "/audio/speech" in url:
        chars = len(str(req.get("input") or ""))
        rec["output_tokens"] = chars          # characters, for TTS
        rec["cost_usd"] = chars / 1000 * TTS_USD_PER_1K_CHARS
        rec["approx"] = True
        return rec
    else:   # embeddings / transcription
        rec["input_tokens"] = int(u.get("input_tokens") or u.get("prompt_tokens")
                                  or u.get("total_tokens") or 0)
        rec["output_tokens"] = int(u.get("output_tokens") or 0)
        rec["approx"] = "/audio/" in url
    prompt = rec["input_tokens"] + rec["cache_read"] + rec["cache_write"]
    (pin, pout, pread, pwrite), known = price_for(rec["model"], prompt)
    rec["cost_usd"] = (rec["input_tokens"] * pin + rec["output_tokens"] * pout
                       + rec["cache_read"] * pread + rec["cache_write"] * pwrite) / M \
        + rec["web_searches"] * WEB_SEARCH_USD
    rec["approx"] = rec["approx"] or not known
    return rec


async def capture(provider: str, url: str, request_kwargs: dict, resp) -> None:
    """Called by provider_health.post after a 2xx. Never raises."""
    try:
        feature = feature_from_stack()
        body = None
        if "/audio/speech" not in url:
            try:
                body = resp.json()
            except Exception:
                body = None
        rec = parse(provider, url, request_kwargs.get("json"), body)
        rec["feature"] = feature[:48]
        await _store(rec)
    except Exception:
        logger.exception("usage meter failed (call itself succeeded)")


async def _store(rec: dict) -> None:
    from app import db as database
    from app.models import TokenUsage
    if database.SessionLocal is None:
        return
    async with database.session() as s:
        s.add(TokenUsage(**rec))
        await s.commit()
        await _maybe_alert(s)


async def _maybe_alert(s) -> None:
    """One owner alert per day when the day's spend crosses the limit."""
    from app.models import AppState, TokenUsage
    limit = settings.usage_alert_usd_day
    if limit <= 0:
        return
    start = _day_start_utc(0)
    total = (await s.execute(select(func.coalesce(func.sum(TokenUsage.cost_usd), 0.0))
                             .where(TokenUsage.at >= start))).scalar_one()
    if total < limit:
        return
    today = _local_today().isoformat()
    row = await s.get(AppState, "usage_alert_date")
    if row is not None and row.value == today:
        return
    s.add(AppState(key="usage_alert_date", value=today) if row is None else row)
    if row is not None:
        row.value = today
    await s.commit()
    from app.core import provider_health
    await provider_health._notify(
        f"💸 <b>Токени сьогодні: ${total:.2f}</b> — вище ліміту ${limit:g}/день. "
        "Деталі: /usage 1")


# ---------- report ----------

def _tz() -> ZoneInfo:
    return ZoneInfo(settings.tz_name)


def _local_today() -> date:
    return datetime.now(_tz()).date()


def _day_start_utc(days_back: int) -> datetime:
    d = _local_today() - timedelta(days=days_back)
    return datetime(d.year, d.month, d.day, tzinfo=_tz()).astimezone(timezone.utc)


def label(feature: str) -> str:
    base, _, rest = feature.partition(" ")
    return FEATURE_LABEL.get(base, base) + (f" {rest}" if rest else "")


def short_model(model: str) -> str:
    return (model or "?").replace("claude-", "").replace("text-embedding-", "emb-")


def money(x: float) -> str:
    return f"${x:.2f}" if x >= 0.995 or x == 0 else f"${x:.3f}"


def tok(n: int) -> str:
    if n >= M:
        return f"{n / M:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(n)


async def report(db, days: int = 7) -> str:
    """Owner-only, cross-domain technical report (numbers only)."""
    from app.models import TokenUsage
    days = max(1, min(int(days), 90))
    T = TokenUsage

    async def total_since(back: int) -> float:
        return float((await db.execute(select(func.coalesce(func.sum(T.cost_usd), 0.0))
                                       .where(T.at >= _day_start_utc(back)))).scalar_one())

    today, week, month = await total_since(0), await total_since(6), await total_since(29)
    start = _day_start_utc(days - 1)
    first = (await db.execute(select(func.min(T.at)))).scalar_one()
    lines = [f"💸 <b>Вартість роботи бота</b>",
             f"Сьогодні {money(today)} · 7 днів {money(week)} · 30 днів {money(month)}"]
    if first is None:
        lines.append("Лічильник щойно увімкнено — даних ще немає.")
        return "\n".join(lines)
    span_days = max(1.0, min(30.0, (datetime.now(timezone.utc) - first).total_seconds() / 86400))
    lines.append(f"Прогноз на місяць: ~{money(month / span_days * 30)} "
                 f"(за {span_days:.0f} дн. даних)" if span_days < 30 else
                 f"Прогноз на місяць: ~{money(month)}")

    rows = (await db.execute(
        select(T.feature, func.count(), func.sum(T.cost_usd), func.sum(T.input_tokens),
               func.sum(T.output_tokens), func.sum(T.cache_read), func.sum(T.web_searches))
        .where(T.at >= start).group_by(T.feature)
        .order_by(func.sum(T.cost_usd).desc()))).all()
    period_total = sum(float(r[2] or 0) for r in rows) or 1e-9
    lines.append(f"\n<b>За {days} дн. по функціях</b> (виклики · $ · частка · $/виклик):")
    for feat, n, cost, tin, tout, tread, tws in rows[:12]:
        cost = float(cost or 0)
        ws = f" · 🔎{tws}" if tws else ""
        lines.append(f"• {html.escape(label(feat))}: {n} · {money(cost)} · "
                     f"{cost / period_total * 100:.0f}% · {money(cost / n)}"
                     f" · in {tok(int(tin or 0) + int(tread or 0))}/out {tok(int(tout or 0))}{ws}")

    mrows = (await db.execute(
        select(T.model, func.count(), func.sum(T.cost_usd), func.sum(T.input_tokens),
               func.sum(T.output_tokens), func.sum(T.cache_read), func.sum(T.cache_write),
               func.bool_or(T.approx))
        .where(T.at >= start).group_by(T.model).order_by(func.sum(T.cost_usd).desc()))).all()
    lines.append("\n<b>По моделях:</b>")
    for model, n, cost, tin, tout, tread, twrite, approx in mrows:
        tin, tread, twrite = int(tin or 0), int(tread or 0), int(twrite or 0)
        prompt = tin + tread + twrite
        hit = f" · кеш {tread / prompt * 100:.0f}%" if prompt and model.startswith("claude") else ""
        lines.append(f"• {html.escape(short_model(model))}: {n} викл. · {money(float(cost or 0))}"
                     f"{' ≈' if approx else ''} · in {tok(prompt)} / out {tok(int(tout or 0))}{hit}")

    top = (await db.execute(select(T).where(T.at >= start)
                            .order_by(T.cost_usd.desc()).limit(5))).scalars().all()
    if top:
        lines.append("\n<b>Найдорожчі виклики:</b>")
        for r in top:
            when = r.at.astimezone(_tz()).strftime("%d.%m %H:%M")
            lines.append(f"• {when} {html.escape(label(r.feature))}, {html.escape(short_model(r.model))}: "
                         f"in {tok(r.input_tokens + r.cache_read + r.cache_write)} / "
                         f"out {tok(r.output_tokens)} → {money(r.cost_usd)}")
    lines.append("\n<i>Ціни — прайс Claude API (Opus 5.5 $4/$20 за 1M, Haiku 5.5 $0.10/$0.50); "
                 "аудіо OpenAI — приблизно (≈). Вихідні токени включають «обдумування».</i>")
    return "\n".join(lines)
