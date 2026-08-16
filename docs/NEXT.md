# NEXT — the one authorized round

## Round 6.1B — Domain isolation (CURRENT, code complete, NOT deployed)

The authorized round right now. Code is complete locally and NOT deployed; the
migration has run only against local databases. **"Code complete" is not "live".**
Do not call production done, and do NOT start the next round, until every step
below has happened and been verified in production:

1. Review the single commit `feat(r6.1b): enforce domain isolation end to end`.
2. Deploy it, then run the Alembic migration on production
   (`a7b1c2d3e4f5`, down_revision `f6a1b2c3d4e7`). It is idempotent and does no
   provider calls, no embeddings, no content reads — pure schema + parent-based
   backfill.
3. **Assign Google accounts to domains** in `/accounts`. After the migration
   every existing account is UNASSIGNED (domain = NULL) and therefore used by no
   domain-scoped tool. Until the owner assigns each account, Gmail / Calendar /
   Drive will honestly report "no account for this domain". This is deliberate —
   domain is never guessed from the email.
4. Verify live: `/domain` switches and persists; a personal note never surfaces
   in a travelon answer and vice-versa; TravelON tools work only in the travelon
   domain; `/domain_audit` looks sane; the R6.1A.1 secret behaviour is intact
   (a password question still returns «не зберігаю»; a hard token is still
   blocked).
5. Understand that any legacy resource that landed in the wrong domain is NOT
   moved automatically. Mis-classified material must be consciously re-uploaded
   in the correct domain (see the runbook note in README).

Only after all of that is the next round authorized.

### Deferred out of this round (deliberately — do NOT start)

- **R6.2** wiki revisions / durable compile queue (below).
- **R6.3** confirmed save-answer flow (below).
- **Local STT.** Voice audio still reaches the external transcription provider
  before any scan can run — unchanged by R6.1B.
- Any semantic/LLM re-classification of legacy rows into domains. The migration
  is deterministic and parent-based on purpose; guessing domains from content is
  explicitly out of scope, now and later.

## Round 6.2 — Wiki revisions & durable compile queue (later, separate round)

- immutable page revisions (history + rollback) instead of in-place overwrite;
- normalized fact/source tables so provenance is per-fact, not per-page;
- a durable compile queue with retries, replacing best-effort inline calls;
- a full section compiler for oversized sources — this is what turns R6.1A's
  honest `deferred_large` status into an actually-complete compilation.

## Round 6.3 — Confirmed save-answer flow (later, separate round)

R6.1A removed `wiki_save_answer`: the model can no longer write to long-term
memory on its own initiative. The capability itself is still worth having, but
as a user-confirmed action — a preview card («зберегти цю відповідь як сторінку
архіву?») with an explicit button, the same pattern as calendar and drafts, and
the secret gate on the way in. `wiki.save_archive()` already exists in core and
already passes the gate; only the confirmation flow is missing.

## Immediately after deploying R6.1A (owner actions, not code)

1. Deploy the reviewed commit.
2. Run `/kb_security_scan` and keep the counts-only result.
3. Rotate any credentials that could have been in indexed sources.
4. Verify: a password question returns the safe «не зберігаю» answer; an
   ordinary business document still indexes and is findable.
5. Leave `AUTO_WIKI_COMPILE_ENABLED=false` until the scan result is reviewed.

## Idea backlog (NOT authorized)

Monthly TravelON analytics, task auto-carryover proposals, /reply v2 (thread
context), memory browser in the Mini App, a vault/password-manager connector
(explicitly out of scope for R6.1A).

## Out of scope

WhatsApp/Instagram, Health, crypto, payments, email send; Zoom API integration
(dropped by Danylo); Android (postponed); editing/deleting existing calendar
events.
