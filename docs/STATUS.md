# STATUS

_Last verified: 2026-08-15 (prod = 31db942 / r6.1a; R6.1A.1 hotfix is CODE COMPLETE locally and NOT deployed — scanner v2 has never run in production)_

## R6.1A.1 — Secret boundary hardening: CODE COMPLETE, NOT DEPLOYED

Independent audit of R6.1A found the gate real but its perimeter incomplete.
Fixed here; see DECISIONS.md for the reasoning. **Status discipline: "code
complete" is not "scan complete".** Scanner v2 has run only against the local
test database. The production scanner-v2 pass has NOT happened, so no claim is
made about the current state of the live knowledge base.

- Scanner v2 (`SCANNER_VERSION = 2`): Cyrillic/Russian password values, PINs
  and repeated-digit values, `$ % {`-prefixed values, comma/semicolon/tab/pipe
  credential tables, column values at ANY row depth, plain numeric recovery
  codes, JSON-quoted keys, at-most-one-newline assignments. Placeholders are
  matched exactly, not by first character. Table detection runs over the whole
  document, not per window.
- Recursive envelope scan: body + title + filename + source_ref + nested meta
  (bounded depth/nodes). Blocked titles are replaced by a generic safe title,
  blocked meta dropped, and no unsafe title is written to the audit log.
- Quarantine dedupe uses a keyed HMAC, never a raw SHA-256 of the blocked body.
- Provider-argument gates in core: `rag.retrieve`, `chat_tools.run_tool`,
  `/admin/search`, Gmail reply-draft search, `coach.create_goal/create_habit`
  (so Telegram and the Mini App share one gate).
- Model-egress gates: extractor output, chat reply, meeting digest, draft body
  — before persistence, Telegram, TTS and Gmail; blocked turns are marked
  `provider_eligible=False`; the read path re-scans stored turns.
- Security scan v2 covers Proposal payloads, PendingDraft, PendingCalCreate,
  Task/Goal/Habit, KnowledgeGap and RECURSIVE RawEvent payloads. A v1
  completion no longer satisfies the v2 gate.
- Voice: the STT exception is documented, not papered over — the transcript is
  scanned before echo/persistence/model, and `/start` warns against dictating
  keys. Local STT stays in NEXT.md.
- Passwords remain searchable (owner decision R6.1A.1); the audit's
  password-blocking cases are covered as detection under
  `QUARANTINE_PASSWORDS=true`.
- Tests: **266 passed** locally (75 new in `tests/test_r61a1.py`). No new
  migration — the hotfix is code-only; the existing chain was re-verified.

Gate to close before this can be called done: deploy, then run
`/kb_security_scan` (scanner v2) to completion in production, then record the
result here.

## R6.1A — Emergency knowledge safety: DELIVERED (6e12ffd; amended by R6.1A.1)

> **R6.1A.1 (2026-08-15, owner decision):** passwords are now searchable business data; only HARD technical secrets (API keys, OAuth/bearer tokens, private keys, cookies, recovery codes, seed phrases) are blocked. Toggle `QUARANTINE_PASSWORDS=true` to restore strict mode. `/kb_security_scan` reconciles both ways (releases password content, keeps tokens). Prod scan 2026-08-15: 37 files / 5 wiki pages / 2 chat lines held under strict R6.1A; re-running after the amendment releases the ~29 password files and keeps the 8 token files.

Hard secrets no longer reach persistence, embeddings, the wiki compiler, chat
context or model tool output. Existing content is contained by an owner-run
local scan, without any automatic deletion.

- **`app/core/secret_policy.py`** — deterministic classifier: password,
  api_key, oauth_token, bearer_token, private_key, session_cookie,
  recovery_code, seed_phrase. No LLM / embeddings / web / connectors. Patterns
  compiled once; NFKC + unicode-whitespace normalisation (fullwidth,
  zero-width and nbsp evasion); the WHOLE document is scanned in overlapping
  20k windows, never a prefix; credential TABLES (a «Пароль» column with values
  on other rows) are matched positionally. `scan_text()` returns
  `SecretScanResult(blocked, categories, finding_count)` — never a value, an
  excerpt, an encoding or a hash.
- **`app/core/security.py`** — the single core gate: fail-closed scan, append-
  only findings (idempotent per resource + scanner version, SAVEPOINT-safe),
  metadata-only audit, the Ukrainian safe replies, the scan-complete flag, and
  the deterministic «is this asking for a stored credential?» check.
- **Gate points** — `ingest_document` (before `chunk_text` and the embedder),
  `ingest_document_parts` (whole source before splitting, so a secret cannot
  ride a part boundary), per-sheet xlsx, `Orchestrator.handle_note` (before
  RawEvent / ChatLog / extractor / RAG / tools), transcripts, Drive,
  `/admin/ingest`, the wiki compiler (before the provider call AND on its
  output), `upsert_page`, `chat_tools.run_tool` (every tool result before it
  reaches the model), the Gmail digest, reply-draft composition, and the
  assembled chat context block (chunks + calendar + profile facts).
- **Containment** — `security_findings` (migration `f6a1b2c3d4e7`),
  `Document.status=quarantined`, `WikiPage.status=active|quarantined`,
  `MemoryItem.status=quarantined`, `ChatLog.provider_eligible=false`. Every
  retrieval, wiki lookup, index, lint and chat-history path filters them out.
  Raw events are flagged, never modified or deleted.
- **`/kb_quarantine`** (owner-only) — the rotation walk-list: which
  files/sheets and wiki pages are quarantined (titles, dates,
  categories; parts grouped; self-secret titles masked; no content).
- **`/kb_security_scan`** (owner-only) — bounded keyset batches over chunks
  (grouped by document), wiki pages, memory, chat log and raw-event payloads.
  Zero provider calls, idempotent re-runs, counts-only report with no titles
  or excerpts. The scan-complete flag is cleared at start and set only after a
  full successful pass.
- **Autonomy reduced** — `wiki_save_answer` removed from tool defs, policy map,
  executors, agent prompt, tests and docs (returns as a confirmed flow in
  R6.3). `AUTO_WIKI_COMPILE_ENABLED=false` by default; `/admin/ingest`
  `compile` defaults to false; `/wiki_build` and auto-compile refuse until the
  scan gate is complete.
- **Honest compilation status** — `pending | succeeded | empty_valid | failed |
  deferred_large | quarantined` + compiler_version, source_chars,
  processed_chars, pages, error_code, timestamp. `failed` and `deferred_large`
  stay in the pending queue; error metadata carries codes, never bodies.
- **Version metadata** — `APP_VERSION`/`APP_RELEASE` in `app/config.py`;
  `/health/live` and `/start` no longer claim «round 4».
- Tests: **191 passed** locally (76 in `tests/test_security.py`; the tests
  that asserted credential retention/retrieval were replaced, not left
  contradicting). Provider tripwires fail a test on any embedder, Anthropic or
  network call on a gated path. Migration verified three ways: fresh DB from
  zero, upgrade of a populated R6 database (rows and defaults intact), and a
  replay over an already-applied schema.

Not done here (deliberately, own rounds): domain isolation (R6.1B), wiki
revisions / durable queue / full section compiler (R6.2), confirmed
save-answer (R6.3), vault connector, and deletion of existing data.

Gate for R6.1A (scanner v1) CLOSED 2026-08-15: deployed, prod v1 scan completed (1014 docs / 64 pages
checked; 37 docs, 5 pages, 2 chat lines contained; 44 findings). Remaining
manual step for Danylo: rotate the credentials named by `/kb_quarantine`,
strip password columns from the source sheets, then `/drive_all` +
`/wiki_build`. Auto-compilation may be enabled once rotation is done.

## R6 — Wiki-пам'ять (compiled knowledge): DELIVERED

- `wiki_pages` (migration `e5f60819aabb`): entity | concept | archive, with
  summary/content/contradictions/aliases/tags/sources/domain.
- `app/core/wiki.py`: slugify+translit aliases, find_page (spelling-insensitive,
  containment match), search_pages, render_index, upsert_page, compile_source
  (extract facts -> create or LLM-merge into existing page), save_archive,
  document compilation from indexed chunks, lint + Sunday-report block.
- Agent tools: `wiki_index`, `wiki_page` (prompt: wiki BEFORE raw search).
  `wiki_save_answer` shipped in R6 and was REMOVED in R6.1A — see above.
- Commands: `/wiki` (index), `/wiki <назва>` (page), `/wiki_build [N]`
  (background compilation with progress), `/wiki_lint`.
- Auto-compile on new Telegram documents and on `/admin/ingest` (Cowork
  channel) — DISABLED BY DEFAULT since R6.1A (`AUTO_WIKI_COMPILE_ENABLED`,
  plus the scan gate).
- Tests at the time of R6: **118 passed** (10 new: aliases, create->merge
  accumulation, merge-failure fact preservation, archive, lint, agent tools).

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

### Hotfix (same day): calendar honesty + chat engine API

- Prod logs showed calendar 403 on BOTH accounts (tokens granted without the
  calendar checkbox) while the bot confidently answered «порожньо». Now:
  CalendarAccessError is raised on 401/403 and surfaced — agenda block tells
  the chat to admit the calendar is unreachable; morning brief prints ⚠️ with
  the broken account; /accounts shows per-account scope status (📆✉️📝📁 ✅/❌);
  OAuth callback warns immediately when boxes were left unticked.
- calendar_range now reads ALL visible calendars (calendarList → merge,
  dedupe, sort), not just primary — meetings in secondary calendars appear.
- Short follow-ups («а сьогодні?», ≤40 chars) inherit the calendar trigger
  from the last two user turns.
- Chat engine: Sonnet 5 rejected `thinking.enabled` (400 on every message,
  silent fallback to Haiku one-liners). Switched to `thinking: adaptive` +
  `output_config.effort` (CHAT_EFFORT=high), verified live. Tests: **51**.
- Calendar 403 root cause found in logs: **Calendar API was disabled** in the
  Google Cloud project (551440869378) — scopes were fine. Danylo enabled it;
  calendar answers verified live (3 events listed).

### Calendar RSVP slice (same day, Danylo's request)

«Скасуй мою участь у зустрічі з маркетингом» must actually decline the event:

- New intent `calendar` (extraction): cal_action decline|accept|tentative,
  cal_query words, cal_date day hint; mock has deterministic triggers.
- Flow: find events (fuzzy word-match ≥0.5 across ALL visible calendars of all
  accounts, ≤3 matches) → PendingCalAction row + preview card («Організатора
  буде повідомлено») → ✅ button = L3 confirmation → PATCH own attendee
  responseStatus with sendUpdates=all. Idempotent (done→already); reject never
  touches Google; organizer-of-own-event → honest "not_attendee" alert.
- Policy: `calendar.respond` L3 allowed (RSVP only); `calendar.write`
  (create/delete) STAYS denied. Scope added: `calendar.events` → **both
  accounts need /connect_google re-consent with the NEW checkbox**.
- Migration `c3d4e5f60718` (pending_cal_actions). Tests: **59 passed**.
- LIVE VERIFIED: Danylo enabled the Calendar API, re-consented, calendar
  questions answer with real events; RSVP slice deployed (07941d4).

### Calendar event creation (R4b slice 1, «Розробляй по плану далі»)

«Постав зустріч з Юрою завтра о 15» → preview card → button = L3 confirm →
event lands in the chosen account's primary calendar:

- Extraction: cal_action=create + cal_title/cal_start/cal_duration_min
  (default 60, clamp 15..480); no time → honest ask-back, nothing staged.
- Past start → ask-back; multi-account → one button per account (≤3);
  no invitees by design (inviting people = communication, stays out).
- Policy `calendar.create` L3 allowed; `calendar.write` (edit/delete of
  existing events) still denied. Idempotent; reject never touches Google.
- Migration `d4e5f60819aa` (pending_cal_creates). Tests: **64 passed**.

### TravelON pulse v2 (token received — volume-aware rework)

Live token turned out to be the FULL operator flow (~100 orders/day, ~1MB XML
per day; 7-day range requests time out). Reworked before first prod use:

- fetch_days(): day-sized windows fetched concurrently (semaphore 4, UA
  header, timeout 90s; FROM==TO verified as a valid single-day window).
- Pulse is now AGGREGATES: created today/yesterday + sums by currency,
  arrivals today/tomorrow/7d + tourist count; per-order lines ONLY for the
  actionable bit — unpaid balances among nearest check-ins (top 5 + total).
- Flight-only orders (no hotel block — big share of the flow): check-in and
  direction fall back to transport (depart date, ✈️ charter name).
- Verified live: pulse 22.8s (26/82 orders, 513 arrivals/7d, 47 debtors),
  brief line 9.7s. TRAVELON_TOKEN set on Railway. Tests: **66 passed**.

### R4b slice 4: TravelON owner pack (Android postponed by Danylo)

- Order lookup in chat: «заявка 59266» / «що по заявці №…» / /order —
  deterministic trigger (заявк/заявц stem + bare №NNNNN), plain-text card
  (status, hotel/direction, check-in, tourists, cost, debt ⚠️/paid ✅).
  Read-only, L0, nothing stored, audited as travelon.order_viewed.
- Daily debt alert (10:00, DEBT_ALERT_TIME, empty disables): tomorrow's
  check-ins with unpaid balance -> 🚨 list (top 10 by debt) + total;
  SILENT when all paid. New scheduler ritual "debts".
- Sunday report gets a TravelON week block: new orders count + sums by
  currency + top-3 destinations. Tests: **82 passed**.

### R4b slice 3: voice replies + Mini App extensions (Zoom API dropped)

- Zoom API auto-pull DROPPED per Danylo. Remaining plan items built instead:
- TTS voice replies: voice message in -> text reply + voice note (OpenAI
  gpt-4o-mini-tts, opus). Text ALWAYS sent; voice only when reply ≤ 600
  chars. /voice toggles per user (default on). Chat-intent replies only.
- Mini App: add-goal and add-habit inputs right in the Сьогодні tab
  (habits/goals sections always visible now); new 🧳 Бізнес tab renders the
  TravelON pulse natively (stat grid + debtor cards + refresh link).
- TravelON pulse refactored to pulse_data() (JSON) + pulse_text() (chat
  render) with a 10-min app_state cache — Mini App tab and /travelon share
  it; first load ~20s, then instant. Tests: **76 passed**.

### Meeting transcripts v1 (R4b slice 2)

Send a Zoom transcript file (.vtt/.srt) to the bot → knowledge base +
summary + decisions + action items:

- parse_subtitles(): WEBVTT/SRT -> clean "Speaker: text" dialogue (headers,
  cue numbers, timestamps dropped; consecutive same-speaker cues merged).
- meetings.meeting_digest(): Haiku -> {summary, decisions[], actions[]}
  (transcript is DATA; injected instructions ignored; max 6 actions, no
  invented deadlines). Digest failure -> KB-only ingest, honest message.
- Actions with who=me become NORMAL task proposals (✅/✏️/❌ cards, same
  policy/audit); others' commitments listed informationally. Duplicate
  upload => no re-digest, no duplicate proposals (raw-event dedupe anchor
  on content hash). Zoom API auto-pull stays a candidate. Tests: **72**.

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
