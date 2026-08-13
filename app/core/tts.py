"""Voice replies (R4b): OpenAI TTS — answer voice messages with voice.

Text is ALWAYS sent first; the voice note is an addition, never a replacement.
Long replies stay text-only (listening to a 2-minute robot is worse than
reading). Toggle per user with /voice; default on.
"""
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def should_speak(reply: str, enabled: bool) -> bool:
    """Pure gate: voice replies on, reply short enough, provider configured."""
    return (enabled and bool(settings.openai_api_key)
            and 0 < len(reply) <= settings.tts_max_chars)


async def synthesize(text: str) -> bytes | None:
    """Text -> OGG/Opus bytes (Telegram voice format). None on any failure."""
    if not settings.openai_api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={"model": settings.tts_model, "voice": settings.tts_voice,
                      "input": text[:settings.tts_max_chars],
                      "response_format": "opus"},
            )
        if resp.status_code != 200:
            logger.error("tts failed: %s %s", resp.status_code, resp.text[:150])
            return None
        return resp.content
    except Exception:
        logger.exception("tts failed")
        return None
