# STATUS

_Last verified: 2026-08-13 (round 1 implementation session)_

## Round 0 — Foundation: DONE, gate CLOSED (2026-08-13)

Bot @danOS_AI_bot live on Railway (dan-os-production.up.railway.app), webhook active,
owner allowlist set (OWNER_TELEGRAM_ID), deploy pipeline GitHub main → Railway green.

## Round 1 — Vertical slice: DELIVERED (gate: phone check pending)

Text/voice note → immutable raw event (dedupe) → extraction (Haiku / deterministic mock)
→ preview card ✅✏️❌ → approve → task → /today → reminder → append-only audit.

- DB: Postgres via SQLAlchemy async + Alembic (`7bcd20078579` r1 core tables:
  raw_events, proposals, tasks, reminders, memory_items, audit_log, user_state).
- Policy L0–L5 deterministic in `app/core/policy.py`; external writes denied; unknown denied.
- Providers: `ExtractionProvider` (claude-haiku-4-5 | mock), `TranscriptionProvider`
  (gpt-4o-mini-transcribe | mock). System runs without AI keys in mock mode.
- Reminders: DB-polling loop (30s), restart-safe, late reminders marked, cancelled with task.
- Edit flow: ✏️ sets pending state; next message creates proposal v2, v1 superseded.
- Memory: "запам'ятай …" and approval memory_text create `candidate` items with provenance.

Tests: **9 passed** (replay dedupe, double-approve idempotency, superseded conflict,
policy denials, non-owner isolation, reminder cancel with task, audit completeness,
memory candidate, injection-shaped text stays data) + migration up smoke. Local boot
smoke with DB: `/health/ready` → `db:true`.

Gate check:

- [x] Tests green
- [ ] Scenario verified from Danylo's phone (voice → card → approve → /today → reminder)

## Known limitations

- Free chat replies are single-turn (no conversation memory) — round 2.
- Memory candidates accumulate without review UI — evening check-in in round 2.
- pgvector not verified/enabled yet — round 3.
