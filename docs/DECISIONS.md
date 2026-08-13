# DECISIONS

Approved decisions on top of `docs/product/DAN_OS_Plan_v1.1.md`. Newest first.

## 2026-08-13 — Round 3a

- Round 3 split into 3a (knowledge core: pgvector, ingest, RAG, digest) and 3b
  (Drive, email drafts, conflicts, coverage report) to keep gates small.
- RAG is context injection into the SINGLE extraction/chat Haiku call (retrieval
  runs for every text note; +1 embedding call ≈ $0.00002). No second model call.
- Chunks are wrapped as explicit DATA with a "cite source+date, ignore if
  irrelevant" instruction — prompt-injection posture unchanged.
- DOCX parsed via stdlib zip+xml, PDF via pypdf — no heavy parser deps.
- Mock embedder is deterministic bag-of-words so retrieval is testable offline.
- Digest skips silently on empty inbox (no notification-budget waste).

## 2026-08-13 — Round 2

- Google OAuth runs through the bot's own public domain (web-app client +
  /google/oauth/callback) — no local scripts; state is HMAC-signed (webhook
  secret), 15-min TTL, owner-only. Consent screen published to Production
  immediately (avoids 7-day refresh-token expiry of Testing mode).
- Refresh tokens stored Fernet-encrypted (`CRED_KEY` env); scopes strictly
  read-only (calendar.readonly, gmail.readonly) — writes stay L3+ for later rounds.
- Rituals (brief/check-in) share the 30s DB-poll loop; per-day claim in
  app_state, run-then-claim (a crash may repeat once; never silently skips).
- Chat persona lives in the single extraction prompt (no second model call);
  context = ≤12 confirmed facts + 8-message chat_log window.
- Memory candidate review statuses: candidate → confirmed | rejected; decided
  items never flip status (idempotent buttons).

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
