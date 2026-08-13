# STATUS

_Last verified: 2026-08-13 (round 4 implementation session)_

## Round 4 — Expansion: DELIVERED (gate: live phone checks pending)

- **Mini App** (`/app` + menu button "DAN.OS"): one self-contained page,
  Telegram theme vars, 3 tabs — Сьогодні (tasks ☑️/✕, habits toggle, goals 🏁),
  Розбір (proposal ✅/❌ + memory candidates, badge counter), Пам'ять (confirmed
  facts + KB count). Server-side initData HMAC validation (fresh auth_date
  ≤ WEBAPP_MAX_AGE=3600s, owner-only → 401), API `/webapp/api/overview|act`;
  actions reuse orchestrator/coach → same policy+audit+idempotency as chat.
- **Coach**: goals (active|done|dropped) + daily habits with per-day log
  (unique habit+date, toggle = reversible L2). /goal /goals /habit /habits;
  evening check-in asks about unfinished habits (buttons); Sunday report shows
  goals + week counts. Policy: goal.*, habit.* = L2.
- **TravelON pulse** (read-only, domain travelon): gateway to
  travelon.to/book/report/xml with TRAVELON_TOKEN env (never logged/stored);
  store-minimum parse (no passports/names — counts and totals only).
  /travelon = 7-day card (нові заявки / заїзди / борги); morning brief gets a
  one-liner. travelon.read = L0; empty <orders/> = valid zero.
- Migration `b7c8d9e0f1a2` (goals, habits, habit_log). Tests: **46 passed**
  (initData forge/stale/foreign, goals lifecycle, habit week-count,
  travelon parser fixtures incl. nil/empty, policy).

Gate: Danylo opens /app from the phone (Today+Memory work), /travelon shows a
card once TRAVELON_TOKEN is set, habits appear in the evening check-in.

## Round 3b — Knowledge extensions: DELIVERED (gate: re-consent + live checks)

- /drive: list Drive folders → index pdf/docx/txt/md + Google Docs (read-only,
  20 files/run, hash dedupe). /reply <query>: find email → Haiku reply draft →
  preview → 💾 creates a Gmail DRAFT (L3; sending stays denied).
- Memory conflicts at confirm time: ⚖️ card with «нове замінює старе / лишити
  старе / обидва»; supersede keeps history (superseded_by).
- Sunday 19:00 weekly report: stats + unanswered questions + up to 3 source
  suggestions (coverage map v1); reported gaps marked resolved.
- Scopes now calendar.readonly + gmail.readonly + gmail.compose + drive.readonly
  → requires ONE /connect_google re-consent.
- **Multi-account Google**: every /connect_google adds another account
  (upsert by user+email); brief/digest aggregate all accounts (·label tags);
  /reply searches every account and drafts in the one that owns the email;
  /drive asks which account; /accounts lists/disconnects. Old single-account
  credentials table recreated (re-consent was required anyway).
- Tests: **30 passed**. Migrations `f75ba5fb32c6` + `a1b2c3d4e5f6`.

Gate: Danylo re-consents, /drive indexes a folder, /reply creates a Gmail draft,
Sunday report arrives.

## Round 0 — Foundation: DONE · Round 1 — Vertical slice: DONE · Round 2 — Secretary: DONE
(All gates closed 2026-08-13; Google connected, /brief works with real calendar+mail.)

## Round 3a — Knowledge core: DELIVERED (gate: live doc-question check pending)

- pgvector confirmed in Railway postgres-ssl:18 image; migration `5304547a0b52`
  creates extension + documents / knowledge_chunks (vector 1536, hnsw cosine
  index) / knowledge_gaps.
- Ingest: Telegram documents (pdf/docx/txt/md, ≤15MB) and forwarded messages →
  extract → chunk (800/120) → embed (text-embedding-3-small) → store with
  provenance. Dedupe by content hash (re-upload = no-op). /kb lists the base.
- RAG: every note runs retrieval (top-5, cosine ≤ 0.55); matched chunks are
  injected into the single Haiku prompt as DATA with source+date citation
  instruction. Unanswered questions land in knowledge_gaps (coverage map R3b).
- Gmail digest 2×/day (`DIGEST_TIMES`=13:00,18:30): recent inbox → Haiku
  importance ranking (⚡ marks) → one P2 message; silent when inbox is quiet.

Tests: **21 passed** (15 prior + chunking, ingest dedupe, retrieval provenance,
gap logged / not logged, question detector). Mock embedder is bag-of-words so
retrieval semantics are testable offline.

Gate check:

- [x] Tests green, migration applied locally
- [ ] Deploy green; Danylo: send a document → ask about its content → answer
  cites source; digest arrives at 13:00/18:30

## Round 3b — deferred scope

Drive folders read, email drafts (L3, scope upgrade + re-consent), memory
conflicts (supersede flow), weekly coverage-map report from knowledge_gaps.
