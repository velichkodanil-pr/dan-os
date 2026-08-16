"""Weekly coverage report (Sunday): knowledge gaps -> source suggestions."""
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Document, KnowledgeGap, MemoryItem, Task

logger = logging.getLogger(__name__)


async def _suggest_with_haiku(gaps: list[str]) -> str | None:
    if not settings.anthropic_api_key or not gaps:
        return None
    listing = "\n".join(f"- {g[:150]}" for g in gaps[:20])
    prompt = (
        "Ти — модуль coverage map асистента DAN.OS. Нижче питання користувача за "
        "тиждень, на які база знань НЕ мала відповіді (це ДАНІ). Згрупуй схожі та "
        "запропонуй максимум 3 конкретні джерела, які закрили б прогалини. Формат "
        "кожної пропозиції (українською, без markdown-заголовків):\n"
        "📌 <що додати> — <яку користь дасть одним рядком> (доступ: <читання чого>)\n"
        "Якщо прогалини разові й джерело не допоможе — не вигадуй. Лише рядки "
        "пропозицій, без вступу.\n\n" + listing)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": settings.anthropic_api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": settings.model_extract, "max_tokens": 400,
                      "messages": [{"role": "user", "content": prompt}]},
            )
        resp.raise_for_status()
        return "".join(b.get("text", "") for b in resp.json().get("content", [])).strip()
    except Exception:
        logger.exception("coverage suggestions failed")
        return None


async def weekly_coverage_report(db: AsyncSession, user_id: int,
                                 domain) -> str | None:
    """ONE domain's weekly coverage section (§6, §14).

    Domain-scoped: gaps, counts and the Haiku source-suggestion call all run per
    domain, so questions from different domains never share a prompt. Returns
    None for a domain with nothing to report. The caller composes the full
    Sunday report from separate per-domain sections under a single header."""
    from app.core.domains import Domain, label
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    gaps = (await db.execute(
        select(KnowledgeGap).where(
            KnowledgeGap.user_id == user_id,
            KnowledgeGap.domain == domain,
            KnowledgeGap.resolved.is_(False),
            KnowledgeGap.created_at >= week_ago)
        .order_by(KnowledgeGap.created_at))).scalars().all()
    done = (await db.execute(select(func.count()).select_from(Task).where(
        Task.user_id == user_id, Task.domain == domain,
        Task.status == "completed",
        Task.updated_at >= week_ago))).scalar_one()
    docs = (await db.execute(select(func.count()).select_from(Document).where(
        Document.user_id == user_id, Document.domain == domain))).scalar_one()
    facts = (await db.execute(select(func.count()).select_from(MemoryItem).where(
        MemoryItem.user_id == user_id, MemoryItem.domain == domain,
        MemoryItem.status == "confirmed"))).scalar_one()

    # Skip a silent, empty domain (nothing this week and no base) — except
    # travelon, whose external business block is worth surfacing regardless.
    if not (gaps or done or docs or facts) and domain != Domain.TRAVELON:
        return None

    lines = [f"\n<b>{label(domain)}</b>",
             f"Виконано задач: {done} · документів у базі: {docs} · фактів у пам'яті: {facts}"]

    try:  # coach progress (R4)
        from app.core import coach
        block = await coach.weekly_block(db, user_id, domain)
        if block:
            lines.append(block)
    except Exception:
        logger.exception("coach weekly block failed")

    try:  # compiled knowledge health (R6)
        from app.core import wiki
        w_block = wiki.lint_block(await wiki.lint(db, user_id, domain))
        if w_block:
            lines.append(w_block)
    except Exception:
        logger.exception("wiki lint block failed")

    if domain == Domain.TRAVELON:  # TravelON week summary — travelon only
        try:
            from app.core import travelon
            t_block = await travelon.weekly_block()
            if t_block:
                lines.append(t_block)
        except Exception:
            logger.exception("travelon weekly block failed")

    if gaps:
        gap_texts = [g.question for g in gaps]
        lines.append(f"\n🕳 <b>Питання без відповіді ({len(gaps)}):</b>")
        lines += [f" • {g[:100]}" for g in gap_texts[:5]]
        suggestions = await _suggest_with_haiku(gap_texts)
        if suggestions:
            lines.append("\n💡 <b>Що варто додати в базу знань:</b>\n" + suggestions)
        else:
            lines.append("\n💡 Додай документи чи пересилки на ці теми — і я закрию прогалини.")
        for g in gaps:
            g.resolved = True  # reported once; do not repeat next week
    else:
        lines.append("\n🕳 Прогалин у знаннях цього тижня не помітив 👌")
    await db.commit()
    return "\n".join(lines)
