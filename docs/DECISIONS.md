# DECISIONS

Approved decisions on top of `docs/product/DAN_OS_Plan_v1.1.md`. Newest first.

## 2026-08-13 — Round 1

- Reminders use a DB-polling loop (30s tick, `FOR UPDATE SKIP LOCKED`) instead of
  in-process APScheduler jobs: survives restarts/redeploys with zero extra state;
  missed reminders fire late and are marked as late. One dependency fewer.
- Extraction model: `claude-haiku-4-5` (single call returns intent/title/dates/
  memory_text/reply). Model id configurable via `MODEL_EXTRACT` env.
- STT: `gpt-4o-mini-transcribe` ($0.003/min), configurable via `STT_MODEL`.
- Edit flow (✏️): pending-edit state in `user_state`; the next text message creates
  proposal v2 and supersedes v1 (approve of v1 then fails with a version conflict).
- A proposal can become at most one task — enforced by DB unique constraint
  (`tasks.proposal_id`), not only by status checks.
- Memory candidates are created from explicit "запам'ятай …" notes and from
  `memory_text` on task approval; review UI deferred to round 2 evening check-in.
- Non-owner isolation is enforced twice: adapter filter AND orchestrator guard.

## 2026-08-12 — Round 0 bootstrap

- Project name: **DAN.OS** (merges "AI Companion" v1.0 plan with the parallel DAN.OS spec; see Plan v1.1 section 13 for what was adopted/deferred/rejected).
- One Python service on Railway with logical modules in `app/core/` — no microservice monorepo at this stage.
- Telegram webhook (secret-token guarded) is the production contract; the app boots without a bot token in health-only mode so deploys never block on secrets.
- Owner allowlist via `OWNER_TELEGRAM_ID`; before it is set, `/start` prints the caller's ID to let the owner claim the bot; everyone else gets silence.
- Lightweight rounds with stop gates instead of the R0–R7 ceremony; status files (`STATUS.md`, `NEXT.md`, `DECISIONS.md`) are the session-to-session baseline.
- Models: Claude Haiku 4.5 for routine, Claude Sonnet 5 for complex reasoning, behind provider-neutral interfaces (round 1).
- Channels order: Telegram (R1) → Calendar + Gmail read (R2) → Drive/RAG (R3) → Mini App + Travelon read-only (R4). Android and Zoom later, same core API.
