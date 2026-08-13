"""Voice transcription boundary (provider-neutral)."""
import logging
from typing import Protocol

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MAX_VOICE_BYTES = 20 * 1024 * 1024


class TranscriptionError(Exception):
    pass


class TranscriptionProvider(Protocol):
    async def transcribe(self, audio: bytes, filename: str) -> str: ...


class OpenAITranscriptionProvider:
    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        if len(audio) > MAX_VOICE_BYTES:
            raise TranscriptionError("Голосове завелике (ліміт 20 МБ)")
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                data={"model": settings.stt_model},
                files={"file": (filename, audio, "audio/ogg")},
            )
        if resp.status_code != 200:
            logger.error("STT failed: %s %s", resp.status_code, resp.text[:200])
            raise TranscriptionError("Не вдалося розшифрувати голосове")
        text = resp.json().get("text", "").strip()
        if not text:
            raise TranscriptionError("Порожня розшифровка")
        return text


class MockTranscriptionProvider:
    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        return "нагадай завтра о 10 подзвонити в банк"


def get_transcriber() -> TranscriptionProvider:
    if settings.transcriber == "mock" or not settings.openai_api_key:
        return MockTranscriptionProvider()
    return OpenAITranscriptionProvider()
