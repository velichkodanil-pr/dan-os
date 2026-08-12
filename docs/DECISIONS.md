# DECISIONS

Approved decisions on top of `docs/product/DAN_OS_Plan_v1.1.md`. Newest first.

## 2026-08-12 — Round 0 bootstrap

- Project name: **DAN.OS** (merges "AI Companion" v1.0 plan with the parallel DAN.OS spec; see Plan v1.1 section 13 for what was adopted/deferred/rejected).
- One Python service on Railway with logical modules in `app/core/` — no microservice monorepo at this stage.
- Telegram webhook (secret-token guarded) is the production contract; the app boots without a bot token in health-only mode so deploys never block on secrets.
- Owner allowlist via `OWNER_TELEGRAM_ID`; before it is set, `/start` prints the caller's ID to let the owner claim the bot; everyone else gets silence.
- Lightweight rounds with stop gates instead of the R0–R7 ceremony; status files (`STATUS.md`, `NEXT.md`, `DECISIONS.md`) are the session-to-session baseline.
- Models: Claude Haiku 4.5 for routine, Claude Sonnet 5 for complex reasoning, behind provider-neutral interfaces (round 1).
- Channels order: Telegram (R1) → Calendar + Gmail read (R2) → Drive/RAG (R3) → Mini App + Travelon read-only (R4). Android and Zoom later, same core API.
