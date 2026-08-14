# NEXT — the one authorized round

## Round 6.1B — Domain isolation (AUTHORIZED NEXT, not started)

R6.1A closed the credential leak. The next round closes the second half of
the same architectural gap: `domain` (personal | travelon | tech) exists on
almost every table but is not enforced as a boundary — retrieval, wiki lookup
and chat context mix domains freely, and a personal note can surface inside a
business answer.

Scope sketch (to be specified before implementation):

- domain as an enforced filter on retrieval, wiki lookup and compilation, not
  just a stored column;
- an explicit active-domain concept for chat and tools;
- domain-aware provenance in answers;
- migration + tests; no UI beyond what the Telegram flow needs.

Blocked on nothing. Do NOT start it in the same round as anything else.

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
