# NEXT — the one authorized round

## Round 6.1D — Order aggregates (CURRENT, code complete)

Code is complete locally and NOT deployed. Steps, in order:

1. Review the commit `feat(r6.1d): aggregate our own orders`.
2. Deploy it. The migration `b8c2d3e4f5a6` adds ONE empty table — no backfill.
3. Optionally run `/travelon_sync` to pre-warm; not required — the bot fills
   any window it needs on demand the first time it is asked.
4. Ask in Telegram: «скільки туристів у Туреччину з приймаючою Gepard за
   серпень» — expect 627 заявок / 1 610 туристів, basis «дата заїзду». The
   first such question takes ~40 s while it fetches; the next is instant.
5. Try «а за датою створення» and «на яку суму» — both are supported.
6. From then on the cache also refreshes itself nightly at 04:30.

### Deferred out of this round (deliberately — do NOT start)

- **Supplier cabinets.** Owner decision: we ask only our own system. Logging
  into Kalanit / TOCO / E.Line to cross-check volumes is NOT in scope.
- Margin / profitability reporting. Turnover per currency IS included; margin
  needs net-vs-gross semantics reviewed before it can be trusted.
- Nightly PRE-warm beyond ‑30 days; deep history is reachable on demand but is
  archive.

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
