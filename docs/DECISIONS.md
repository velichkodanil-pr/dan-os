# DECISIONS

Approved decisions on top of `docs/product/DAN_OS_Plan_v1.1.md`. Newest first.

## 2026-08-14 — R6: compiled knowledge layer («LLM Wiki»)

Adopted from Karpathy's LLM-Wiki idea and its Ukrainian implementation
(github.com/BogdanovychA/llm-wiki, dou.ua/forums/topic/60484), after our own
painful lesson: 6k+ raw chunks of flattened spreadsheets could not answer
«який логін до ТОКО Україна?» — pure RAG re-discovers, never accumulates.

What we took (and how it differs here):

- **Pages, not just chunks.** `wiki_pages`: entity | concept | archive. A page
  answers «what do we KNOW about X», merged from many sources over time.
  Raw chunks stay as the second layer for exact quotes.
- **Merge-vs-create.** A new source about a known partner MERGES into that
  page (LLM merge, facts preserved; on merge failure facts are APPENDED, never
  lost) and appends provenance instead of creating a disconnected duplicate.
- **Aliases as first-class.** Every page carries all spellings seen in sources
  (Toco UA / ТОКО / toco-tour.com.ua); lookup is translit- and case-insensitive
  (к↔k↔c). This is the structural fix for the Toco failure.
- **Contradictions section** on the page (llm-wiki's «Суперечності»), surfaced
  by lint and in the Sunday report — instead of two silently conflicting chunks.
- **Query archiving.** A synthesized answer can be saved as an archive page
  (`wiki_save_answer`), so the next identical question is instant and knowledge
  compounds (the «Гепард» answer becomes permanent).
- **Index first.** `wiki_index` is a compact map of everything known; the
  agentic chat is instructed to consult wiki BEFORE raw search.
- **Lint workflow.** Thin/orphan/no-source/duplicate/conflicting pages →
  /wiki_lint and a block in the Sunday report (llm-wiki's linter skill).

What we deliberately did NOT copy: files-in-git + Obsidian storage (we keep
Postgres — the bot needs server-side access, and provenance/audit already
exist), and per-file subagents (our ingest is already one-document-at-a-time).
Policy: wiki.read L0, wiki.write / wiki.archive L1 (internal, audited,
reversible) — no confirmation cards for internal knowledge.

## 2026-08-13 — Calendar RSVP (Danylo: «він мав скасувати мою участь»)

- First L3 calendar write, scoped to the SMALLEST useful action: change OWN
  attendance (decline/accept/tentative) on an existing event. Creating,
  editing and deleting events remain denied (calendar.write) — next candidate
  for R4b with its own preview flow.
- Same safety pattern as email drafts: model only PROPOSES (PendingCalAction),
  a preview card states the side effect («організатора буде повідомлено»),
  the button press is the L3 confirmation, everything audited, idempotent.
- sendUpdates=all on the PATCH — mirrors what pressing «Ні» in Google
  Calendar does; hiding the decline from the organizer would be dishonest.
- Scope calendar.events added next to calendar.readonly (calendarList needs
  readonly; events PATCH needs events) → one more re-consent with a new
  checkbox for every connected account.

## 2026-08-13 — Round 4 (Mini App, coach, TravelON)

- Mini App = one self-contained HTML served by the same FastAPI service
  (no separate frontend build/deploy); Telegram theme variables for native look;
  every API call re-validates initData (HMAC per Telegram spec, auth_date
  freshness 1h, owner-only). Actions go through the SAME orchestrator methods
  as chat buttons — no parallel business logic. Memory conflicts stay a chat
  flow (Mini App shows an alert and defers to the bot).
- Coach is deterministic (no LLM): goals lifecycle active→done|dropped;
  habits with one log row per local day, toggle deletes the row (reversible,
  hence L2 not append-only audit_log — the audit records both log/unlog).
  Habit prompts merged into the evening check-in; progress into Sunday report
  (counts only, no guilt-tripping per spec).
- TravelON: read-only gateway per travelon skill — token only in env, never in
  logs/DB; store-minimum parse (order no, status, dates, hotel, country,
  tourist COUNT, totals; no passports/names/payer); domain=travelon;
  travelon.read=L0; API errors degrade to a friendly message, brief line
  skips silently. Writes to Travelon: never (not even a policy entry).

## 2026-08-13 — Multi-account Google (Danylo's request)

- google_credentials keyed by (user, account_email) with uuid PK; account email
  resolved via Gmail profile at token exchange; label = mail local part.
- Aggregation policy: brief/digest merge all accounts (tagged when >1);
  /reply drafts in the account that owns the found email (credential_id on the
  pending draft); /drive operates on an explicitly selected account (app_state).
- Deploy process change: Danylo pushes himself in GitHub Desktop; Claude's
  pipeline ends at the prepared local commit (no computer-use pushes).

## 2026-08-13 — Round 3b

- Google scopes extended to gmail.compose + drive.readonly (one re-consent via
  /connect_google). email.draft flipped to allowed L3 (preview + explicit button);
  email SEND remains denied — drafts land in Gmail only.
- Drive import v1 is one-shot per folder (/drive → pick folder → index files +
  Google Docs export); refresh by re-running — hash dedupe makes it cheap.
- Conflict detection runs at CONFIRM time (not ingest), same-domain confirmed
  facts only, Haiku judge (mock: word-overlap); resolution is a user choice:
  supersede (history kept via superseded_by) / keep old / keep both.
- Weekly Sunday report (19:00) merges stats + knowledge gaps + up to 3 source
  suggestions; reported gaps are marked resolved so they never repeat.

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
