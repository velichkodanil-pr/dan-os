"""Embedding boundary (provider-neutral)."""
import hashlib
import logging
import math
from typing import Protocol

import httpx

from app.config import settings
from app.models import EMBED_DIM

logger = logging.getLogger(__name__)


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingProvider:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=60) as client:
            for i in range(0, len(texts), 64):
                batch = [t[:6000] for t in texts[i:i + 64]]
                resp = await client.post(
                    "https://api.openai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                    json={"model": settings.embed_model, "input": batch},
                )
                resp.raise_for_status()
                data = sorted(resp.json()["data"], key=lambda d: d["index"])
                out.extend(d["embedding"] for d in data)
        return out


class MockEmbeddingProvider:
    """Deterministic bag-of-words embeddings for tests: word overlap ~ cosine
    similarity, so retrieval semantics are testable without a real provider."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for t in texts:
            vec = [0.0] * EMBED_DIM
            for word in t.lower().split():
                word = word.strip(".,!?:;()«»\"'")
                if len(word) < 3:
                    continue
                idx = int.from_bytes(hashlib.md5(word.encode()).digest()[:4], "big")
                vec[idx % EMBED_DIM] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


def get_embedder() -> EmbeddingProvider:
    if settings.embedder == "mock" or not settings.openai_api_key:
        return MockEmbeddingProvider()
    return OpenAIEmbeddingProvider()
