# NEXT — the one authorized round

## Round 6.1C — Order context: insurance & documents (CURRENT, code complete)

Code is complete locally and NOT deployed. Steps, in order:

1. Review the commit `feat(r6.1c): read order insurance and documents`.
2. Deploy it (no migration this round).
3. **Set `CHAT_MODEL=claude-opus-5` in Railway variables** — the env var
   overrides the code default, and production is currently on
   `claude-opus-4-5`. Without this step the model does not change.
4. Verify live in the travelon domain: forward the insurance letter again —
   the bot must NOT answer «заявку 3490138 не знайшов», must open order 64772,
   read the policy, and answer the coverage questions citing its terms.
5. Spot-check that plain «заявка 59266» still returns the order card.

### Deferred out of this round (deliberately — do NOT start)

- Supplier-cabinet integration (Proxymo / SAMO / OBS) and underpayment
  registries — covered by the `travelon-supplier-cabinets` skill, and a round
  of its own.
- Insurer general terms (Генеральний договір 14/25) in the knowledge base, so
  the bot can answer insurance questions with no order at hand.
- Reading documents of orders the owner has not named (bulk document indexing).

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
