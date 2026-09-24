"""Provider health: retry transient failures, tell the owner about fatal ones.

Two failure classes need opposite handling:

- **Transient** (429 rate limit, 5xx, Anthropic 529 overloaded): retry a
  couple of times with a short backoff. On 2026-09-12 a burst of embedding
  calls hit OpenAI's rate limit and five knowledge-base questions silently
  lost semantic search, although the account itself was fine.
- **Fatal until a human acts** (out of credits, rejected key): retrying is
  pointless. Between 2026-09-12 and 2026-09-23 the OpenAI balance ran dry and
  voice, TTS and semantic search failed with nothing but a line in the Railway
  log. Now the owner gets ONE Telegram message per provider/problem per
  cooldown window, and one more when the provider answers again.

Core stays adapter-free: the Telegram sender is injected at startup
(`set_alert_sink`), exactly like the scheduler's `send_message`. Alert text
never carries response bodies, keys or request URLs.
"""
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3                 # 1 call + 2 retries
BACKOFF_BASE_SECONDS = 1.5       # 1.5 s, then 3 s
MAX_RETRY_AFTER_SECONDS = 20.0   # never park a chat reply longer than this
ALERT_COOLDOWN_SECONDS = 6 * 3600
TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})

BILLING = "billing"
AUTH = "auth"

_PROVIDER_LABEL = {"openai": "OpenAI", "anthropic": "Anthropic"}
_BILLING_HINT = {
    "openai": ("Не працюють: голосові, озвучка відповідей і пошук за змістом "
               "у базі знань. Поповни баланс: platform.openai.com → Billing."),
    "anthropic": ("Не працюють: чат, розбір нотаток, дайджести й англійська. "
                  "Поповни баланс: console.anthropic.com → Billing."),
}
_KEY_VAR = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}

AlertSink = Callable[[str], Awaitable[None]]

_sink: AlertSink | None = None
_last_alert: dict[tuple[str, str], float] = {}
_down: set[str] = set()


def set_alert_sink(sink: AlertSink | None) -> None:
    """Register the owner notifier (the Telegram adapter does this at startup)."""
    global _sink
    _sink = sink


def reset_state() -> None:
    """Forget cooldowns and outages (tests; a restart does the same)."""
    _last_alert.clear()
    _down.clear()


def classify(provider: str, status: int, body: str) -> str | None:
    """Return BILLING / AUTH for failures only a human can fix, else None."""
    low = (body or "").lower()
    if status == 401:
        return AUTH
    if status == 402:
        return BILLING
    if provider == "openai" and status == 429 and (
            "insufficient_quota" in low or "no credits" in low
            or "exceeded your current quota" in low):
        return BILLING
    if provider == "anthropic" and status == 400 and "credit balance" in low:
        return BILLING
    return None


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    header = resp.headers.get("retry-after", "")
    try:
        if header:
            return max(0.0, min(float(header), MAX_RETRY_AFTER_SECONDS))
    except ValueError:
        pass
    return BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))


def _alert_text(provider: str, kind: str, status: int) -> str:
    name = _PROVIDER_LABEL.get(provider, provider)
    if kind == BILLING:
        return (f"⚠️ <b>{name}: закінчились кредити</b> (HTTP {status}).\n"
                f"{_BILLING_HINT.get(provider, '')}")
    return (f"⚠️ <b>{name}: ключ API відхилено</b> (HTTP {status}).\n"
            f"Перевір змінну {_KEY_VAR.get(provider, 'API key')} у Railway.")


async def _notify(text: str) -> None:
    if _sink is None:
        return
    try:
        await _sink(text)
    except Exception:
        logger.exception("provider alert could not be delivered")


async def _on_fatal(provider: str, kind: str, status: int) -> None:
    _down.add(provider)
    key = (provider, kind)
    now = time.monotonic()
    last = _last_alert.get(key)
    if last is not None and now - last < ALERT_COOLDOWN_SECONDS:
        return
    _last_alert[key] = now
    logger.error("provider %s unavailable: %s (HTTP %s) — owner alerted",
                 provider, kind, status)
    await _notify(_alert_text(provider, kind, status))


async def _on_success(provider: str) -> None:
    if provider not in _down:
        return
    _down.discard(provider)
    for key in [k for k in _last_alert if k[0] == provider]:
        del _last_alert[key]
    name = _PROVIDER_LABEL.get(provider, provider)
    logger.info("provider %s recovered", provider)
    await _notify(f"✅ <b>{name} знову відповідає.</b> Усе, що від нього "
                  "залежить, працює.")


async def post(client: httpx.AsyncClient, provider: str, url: str,
               **kwargs) -> httpx.Response:
    """`client.post` with provider-aware retries and owner alerts.

    Returns the final response exactly like `client.post` would, so callers
    keep their own status handling. Network exceptions propagate unchanged.
    """
    resp: httpx.Response | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        resp = await client.post(url, **kwargs)
        status = resp.status_code
        if 200 <= status < 300:
            await _on_success(provider)
            return resp
        kind = classify(provider, status, resp.text)
        if kind is not None:
            await _on_fatal(provider, kind, status)
            return resp
        if status not in TRANSIENT_STATUSES or attempt == MAX_ATTEMPTS:
            return resp
        delay = _retry_delay(resp, attempt)
        logger.warning("%s HTTP %s, retry %d/%d in %.1fs", provider, status,
                       attempt, MAX_ATTEMPTS - 1, delay)
        await asyncio.sleep(delay)
    return resp  # pragma: no cover — the loop always returns
