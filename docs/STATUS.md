# STATUS

_Last verified: 2026-08-13 (round 2 implementation session)_

## Round 0 — Foundation: DONE (gate closed 2026-08-13)
## Round 1 — Vertical slice: DONE (gate closed 2026-08-13, verified from phone)

Voice/text → raw event (dedupe) → Haiku extraction → preview ✅✏️❌ → task →
/today → reminder → audit. 9 tests green. Live on Railway.

## Round 2 — Secretary: DELIVERED (gate: waiting for Google OAuth client + live brief)

- Google OAuth through the bot's own domain (web flow): /connect_google →
  signed-state URL → /google/oauth/callback → refresh token stored Fernet-encrypted.
  Read-only scopes (calendar.readonly, gmail.readonly).
- Morning brief (07:30 Kyiv, `BRIEF_TIME`): calendar today + overnight inbox top +
  tasks/overdue + candidates count. /brief on demand. Works without Google
  (tasks-only + connect hint).
- Evening check-in (21:30, `CHECKIN_TIME`): day summary + memory-candidate review
  with ✅/❌ per item. /checkin on demand.
- Rituals run from the same 30s DB-poll loop; once-per-day claim in app_state
  (run-then-claim), restart-safe.
- Persona + context: extraction/chat prompt now carries confirmed profile facts
  (≤12) and an 8-message conversation window (chat_log).
- Memory review: confirm/reject (L2, idempotent, audited).

Tests: **15 passed** (9 R1 + 6 R2: confirm/reject idempotency, owner-only review,
state sign/verify/tamper/expiry, ritual once-per-day, brief w/o Google, chat log).
Migrations: `7bcd20078579` + `3bb2753afe37`.

Gate check:

- [x] Tests green, deploy green
- [ ] Google client configured (Danylo) → /connect_google → /brief with real data
- [ ] Brief arrives at 07:30, check-in reviews candidates

## Known limitations

- Email "top" is latest-inbox heuristic, no importance ranking yet (R3).
- No Gmail drafts/calendar writes (R3+, L3 with preview).
- pgvector/RAG not enabled yet (R3).
