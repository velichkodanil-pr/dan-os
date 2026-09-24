"""Provider health: transient failures are retried, fatal ones alert the owner once."""
import httpx
import pytest

from app.core import provider_health as ph

OPENAI_NO_CREDITS = ('{"error": {"message": "You have no credits remaining. Add credits '
                     'to continue using the API.", "type": "insufficient_quota", '
                     '"code": "insufficient_quota"}}')
OPENAI_RATE_LIMIT = '{"error": {"message": "Rate limit reached", "type": "requests"}}'
ANTHROPIC_NO_CREDITS = ('{"type": "error", "error": {"type": "invalid_request_error", '
                        '"message": "Your credit balance is too low to access the '
                        'Anthropic API."}}')


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ph.reset_state()
    sent: list[str] = []

    async def sink(text: str) -> None:
        sent.append(text)
    ph.set_alert_sink(sink)
    monkeypatch.setattr(ph, "BACKOFF_BASE_SECONDS", 0.0)
    yield sent
    ph.set_alert_sink(None)
    ph.reset_state()


def _client(responses: list[tuple[int, str]], calls: list):
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, text=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_classify():
    assert ph.classify("openai", 429, OPENAI_NO_CREDITS) == ph.BILLING
    assert ph.classify("openai", 429, OPENAI_RATE_LIMIT) is None
    assert ph.classify("anthropic", 400, ANTHROPIC_NO_CREDITS) == ph.BILLING
    assert ph.classify("anthropic", 400, '{"error": {"message": "bad schema"}}') is None
    assert ph.classify("openai", 401, "") == ph.AUTH
    assert ph.classify("anthropic", 529, "overloaded") is None


@pytest.mark.asyncio
async def test_rate_limit_is_retried_then_succeeds(_clean):
    calls: list = []
    async with _client([(429, OPENAI_RATE_LIMIT), (200, "{}")], calls) as c:
        resp = await ph.post(c, "openai", "https://api.openai.com/v1/embeddings", json={})
    assert resp.status_code == 200 and len(calls) == 2
    assert _clean == []  # a transient blip is not worth waking the owner


@pytest.mark.asyncio
async def test_overloaded_gives_up_after_max_attempts(_clean):
    calls: list = []
    async with _client([(529, "overloaded")], calls) as c:
        resp = await ph.post(c, "anthropic", "https://api.anthropic.com/v1/messages", json={})
    assert resp.status_code == 529 and len(calls) == ph.MAX_ATTEMPTS
    assert _clean == []


@pytest.mark.asyncio
async def test_no_credits_is_not_retried_and_alerts_once(_clean):
    calls: list = []
    async with _client([(429, OPENAI_NO_CREDITS)], calls) as c:
        for _ in range(3):
            resp = await ph.post(c, "openai", "https://api.openai.com/v1/embeddings", json={})
    assert resp.status_code == 429
    assert len(calls) == 3            # one call per request — no retries
    assert len(_clean) == 1           # cooldown: a single alert, not three
    assert "OpenAI" in _clean[0] and "кредити" in _clean[0]
    assert "no credits" not in _clean[0]  # response bodies never reach the chat


@pytest.mark.asyncio
async def test_recovery_is_announced_once(_clean):
    calls: list = []
    async with _client([(400, ANTHROPIC_NO_CREDITS)], calls) as c:
        await ph.post(c, "anthropic", "https://api.anthropic.com/v1/messages", json={})
    async with _client([(200, "{}")], calls) as c:
        await ph.post(c, "anthropic", "https://api.anthropic.com/v1/messages", json={})
        await ph.post(c, "anthropic", "https://api.anthropic.com/v1/messages", json={})
    assert len(_clean) == 2
    assert "Anthropic" in _clean[0] and "знову відповідає" in _clean[1]


@pytest.mark.asyncio
async def test_rejected_key_names_the_variable_not_the_key(_clean):
    calls: list = []
    async with _client([(401, '{"error": "invalid x-api-key"}')], calls) as c:
        await ph.post(c, "anthropic", "https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": "sk-ant-SECRET"}, json={})
    assert len(calls) == 1 and len(_clean) == 1
    assert "ANTHROPIC_API_KEY" in _clean[0] and "SECRET" not in _clean[0]


@pytest.mark.asyncio
async def test_broken_sink_never_breaks_the_call(_clean):
    async def boom(_text: str) -> None:
        raise RuntimeError("telegram down")
    ph.set_alert_sink(boom)
    calls: list = []
    async with _client([(429, OPENAI_NO_CREDITS)], calls) as c:
        resp = await ph.post(c, "openai", "https://api.openai.com/v1/audio/speech", json={})
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_stt_surfaces_billing_as_alert(_clean, monkeypatch):
    """End-to-end through a real call site: the STT provider."""
    from app.config import settings
    from app.core import transcription
    monkeypatch.setattr(settings, "openai_api_key", "k")
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda req: httpx.Response(429, text=OPENAI_NO_CREDITS))
        return real_client(*args, **kwargs)
    monkeypatch.setattr(transcription.httpx, "AsyncClient", fake_client)
    with pytest.raises(transcription.TranscriptionError):
        await transcription.OpenAITranscriptionProvider().transcribe(b"ogg")
    assert len(_clean) == 1 and "OpenAI" in _clean[0]
