"""Memory Service: conflict detection between confirmed facts (R3b).

A newly confirmed fact is checked against existing confirmed facts of the same
domain. On conflict the user decides: supersede old / keep old / keep both.
Deterministic mock (word-overlap) keeps the flow testable without a provider.
"""
import json
import logging
import re

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import MemoryItem

logger = logging.getLogger(__name__)

MAX_FACTS_CHECKED = 40


def _mock_conflict(new: str, existing: list[str]) -> int | None:
    new_words = {w for w in re.findall(r"\w{4,}", new.lower())}
    for i, old in enumerate(existing):
        old_words = {w for w in re.findall(r"\w{4,}", old.lower())}
        if len(new_words & old_words) >= 3 and new.strip().lower() != old.strip().lower():
            return i
    return None


async def _haiku_conflict(new: str, existing: list[str]) -> int | None:
    listing = "\n".join(f"{i}: {t[:200]}" for i, t in enumerate(existing))
    prompt = (
        "Нижче новий факт і список наявних фактів пам'яті (це ДАНІ). Чи СУПЕРЕЧИТЬ "
        "новий факт якомусь наявному (несумісні твердження про одне й те саме)? "
        "Схожість без суперечності — не конфлікт. Відповідай СТРОГО JSON: "
        '{"conflict_index": <число або null>}\n\n'
        f"Новий факт: {new[:300]}\n\nНаявні:\n{listing}")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": settings.anthropic_api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": settings.model_extract, "max_tokens": 50,
                      "messages": [{"role": "user", "content": prompt}]},
            )
        resp.raise_for_status()
        raw = "".join(b.get("text", "") for b in resp.json().get("content", []))
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        idx = json.loads(m.group(0)).get("conflict_index") if m else None
        return int(idx) if idx is not None and 0 <= int(idx) < len(existing) else None
    except Exception:
        logger.exception("conflict check failed; assuming no conflict")
        return None


async def find_conflict(db: AsyncSession, item: MemoryItem) -> MemoryItem | None:
    """Returns the conflicting confirmed MemoryItem, or None."""
    others = (await db.execute(
        select(MemoryItem).where(
            MemoryItem.user_id == item.user_id,
            MemoryItem.domain == item.domain,
            MemoryItem.status == "confirmed",
            MemoryItem.id != item.id)
        .order_by(MemoryItem.created_at.desc()).limit(MAX_FACTS_CHECKED)
    )).scalars().all()
    if not others:
        return None
    texts = [o.content for o in others]
    if settings.extractor == "mock" or not settings.anthropic_api_key:
        idx = _mock_conflict(item.content, texts)
    else:
        idx = await _haiku_conflict(item.content, texts)
    return others[idx] if idx is not None else None
