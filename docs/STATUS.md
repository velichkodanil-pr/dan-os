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
