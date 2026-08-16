"""The domain boundary (R6.1B): personal | travelon | tech as real isolation.

One module owns the model. Every core service takes `domain` as a mandatory
keyword-only argument; nothing in production code defaults to "personal".
The ACTIVE domain is decided by the server at the start of a request (from
UserState), travels down as an immutable snapshot, and is never taken from
model output, tool arguments or client JSON.

Fail-closed: an unknown or empty domain is an error, not a silent fallback.
"""
from __future__ import annotations

from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession


class Domain(StrEnum):
    PERSONAL = "personal"
    TRAVELON = "travelon"
    TECH = "tech"


ALLOWED_DOMAINS: tuple[str, ...] = tuple(d.value for d in Domain)
DEFAULT_DOMAIN = Domain.PERSONAL  # for BACKFILL/bootstrap only, never parsing

LABELS = {Domain.PERSONAL: "🏠 Особисте",
          Domain.TRAVELON: "🧳 TravelON",
          Domain.TECH: "🛠 Tech"}
DESCRIPTIONS = {
    Domain.PERSONAL: "особисті задачі, нотатки, знання",
    Domain.TRAVELON: "бізнес: заявки, партнери, доступи, пульс",
    Domain.TECH: "технічне: проєкти, інструменти, розробка",
}


class DomainError(ValueError):
    """Invalid domain at an entry point. Message is safe to show the user."""

    def __init__(self, value):
        self.value = str(value)[:40]
        super().__init__(f"invalid domain: {self.value!r} "
                         f"(allowed: {', '.join(ALLOWED_DOMAINS)})")


def parse_domain(value) -> Domain:
    """Fail-closed validation. Empty/unknown/model-generated -> DomainError,
    NOT a silent 'personal'."""
    if isinstance(value, Domain):
        return value
    raw = str(value or "").strip().lower()
    try:
        return Domain(raw)
    except ValueError:
        raise DomainError(value) from None


def label(domain) -> str:
    return LABELS[parse_domain(domain)]


async def get_active_domain(db: AsyncSession, user_id: int) -> Domain:
    """The server-side active domain for this user (bootstraps to personal).

    This is the ONLY place a missing value becomes personal — a user who has
    never switched genuinely is in the default domain; that is bootstrap, not
    guessing.
    """
    from app.models import UserState
    state = await db.get(UserState, user_id)
    if state is None or not getattr(state, "active_domain", None):
        return DEFAULT_DOMAIN
    try:
        return parse_domain(state.active_domain)
    except DomainError:
        # corrupt value in DB: fail closed to the safest domain and log loudly
        import logging
        logging.getLogger(__name__).error(
            "invalid active_domain in user_state for %s — using personal", user_id)
        return DEFAULT_DOMAIN


async def set_active_domain(db: AsyncSession, user_id: int, domain) -> Domain:
    """Switch the active domain (reversible internal action; caller audits)."""
    from app.models import UserState
    new = parse_domain(domain)
    state = await db.get(UserState, user_id) or None
    if state is None:
        from app.models import UserState as _US
        state = _US(user_id=user_id)
        db.add(state)
    state.active_domain = new.value
    # a pending edit staged in another domain must not leak into this one
    state.pending_edit_proposal = None
    return new
