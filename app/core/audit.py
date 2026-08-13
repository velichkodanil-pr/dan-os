"""Append-only audit log. Application code must never update or delete rows.

Details must stay short: ids, truncated titles, counters. Never full message
bodies, voice payloads, tokens or secrets.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditRecord


def _trim(value, limit: int = 80):
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1] + "…"
    return value


async def audit(
    db: AsyncSession,
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str = "",
    outcome: str = "ok",
    policy_level: str | None = None,
    **details,
) -> None:
    db.add(
        AuditRecord(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            outcome=outcome,
            policy_level=policy_level,
            details={k: _trim(v) for k, v in details.items() if v is not None},
        )
    )
