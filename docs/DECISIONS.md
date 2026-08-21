# DECISIONS

Approved decisions on top of `docs/product/DAN_OS_Plan_v1.1.md`. Newest first.

## 2026-08-21 — R7: the English coach is a system, not a prompt

Seven prompt templates were on the table. They were not adopted as-is, with
the owner's blessing («можеш її не притримуватись, якщо можна краще»).

- **State beats prompting.** A one-shot «act as a language teacher» produces a
  good lesson and forgets it. Progress, spaced repetition and a mistake log are
  the whole difference between a lesson and a habit — so the plan position,
  the memory items and the session log live in Postgres, not in a prompt.
- **The system chooses, he answers.** The daily session is assembled from what
  is due plus what the week owes. Choosing what to study is the friction that
  ends daily habits.
- **Twelve minutes is a hard constraint, not a suggestion.** The queue is
  built to the minute budget; a thin day is filled with new material rather
  than ending early, and a large backlog is capped rather than dumped.
- **Correct in batches, never inside the sentence.** Fluency dies under
  constant repair. Corrections arrive every third turn and immediately become
  review cards — the fix he needed is the card he gets tomorrow.
- **The coach gets no tools.** It is a separate model call with no access to
  the knowledge base, mail or TravelON. Speaking practice has no reason to
  touch business data, and a coach that could would be a new egress path.
- **Personal domain only**, and practice is never written to `ChatLog`. This is
  the exact mirror of TravelON tools being travelon-only: learning data must
  not become context for a business answer, in either direction.
- **The streak counts days, not taps.** Two sessions in one day are one day.
  A progress number that can be farmed is worse than no progress number.
- **A dead coach closes the practice.** If the model cannot answer, the session
  ends and the message goes to ordinary chat — silently swallowing his English
  into the assistant would be the confusing failure.

## 2026-08-20 — R6.1D: aggregates come from our own mirrored data

«Скільки туристів з приймаючою Kalanit» had no tool, so the model answered
from QA chat logs — a sample presented next to an honest caveat, but still not
the number asked for.

- **Ask only our own system.** Owner decision: supplier cabinets are not
  queried to answer "how many did WE book". Our booking system is the source of
  truth for our own volumes; a cabinet answers a different question (what the
  supplier thinks we owe) and belongs to a separate round.
- **Mirror, because the source is too slow.** The period report times out on a
  six-week window, so an aggregate cannot be computed live. Orders are cached
  locally and warmed nightly; the cache is a derived index, never a second
  source of truth.
- **Coverage is data, not an assumption.** A day with zero orders and a day
  never fetched must be distinguishable, or a partial window gets reported as a
  confident total. This failed for real during the round: an August figure was
  quoted from a cache that held only the first 19 days. Hence a per-day,
  per-basis coverage table.
- **Gaps are filled, not merely reported.** A question about an uncovered
  period fetches it once (bounded to a quarter) and remembers it — flexibility
  the owner should not have to prepare for. A window beyond the bound is
  refused with a reason rather than silently truncated.
- **A cache must confess its coverage.** Every aggregate returns what it
  counted (дата заїзду), the filters applied, and the span the cache actually
  holds. An empty cache asks for a sync; a period outside the span is flagged.
  A confident number over partial data is the failure mode being designed out.
- **Store-minimum survives the cache.** It holds a tourist COUNT, never names,
  passports or document links. Aggregates need arithmetic, not passengers.
- **Default window is forward.** «Скільки заброньовано» is operational — who is
  still coming — not a historical total. Past dates are available on request.
- **Fetch on demand, then remember.** A question about an uncovered period
  fetches it (bounded to a quarter) instead of returning a caveat. Anything
  larger is refused with a reason: a silently truncated window reads as a
  complete answer, which is the worst possible failure for a number.
- **"No orders" ≠ "never looked".** Coverage is recorded per day and per basis.
  Without that distinction the bot either re-fetches forever or reports a
  confident zero for a period it never opened.
- **Two bases, never conflated.** Check-in answers "who travels then", create
  date answers "what we sold then". Same rows, different questions, separate
  coverage.

## 2026-08-20 — R6.1C: deterministic shortcuts must not answer questions

A forwarded insurance letter was answered with «заявку №3490138 не знайшов».
The number was a POLICY number; the real order number sat later in the same
text; and the three questions the letter actually asked never reached the model.

- **A deterministic shortcut is a convenience, not a parser.** The order-card
  shortcut now fires only for a SHORT message that is essentially just a
  lookup. Any longer text goes to the agent — answering a letter with an order
  card is a wrong answer even when the number is right.
- **Labelled beats bare, ambiguity never guesses.** «заявка №N» wins over a
  bare «№N» (policies, invoices and contracts are written with № too). Two
  bare candidates produce no shortcut at all.
- **A failed lookup is not an answer.** A miss falls through to the agent
  instead of terminating the turn, so a wrong guess can no longer bury the
  question.
- **Document URLs are secrets.** Order documents carry the full-access
  TravelON token in the URL. The token stays inside `travelon.py`: tools
  return document KINDS and extracted TEXT, never a URL, and the text passes
  the same output scan as every other tool result.
- **Detail is parsed only for an order the owner named.** The bulk/period path
  stays store-minimum (no names, no documents) — a period fetch covers
  hundreds of orders and has no business touching passports.
- **Never truncate serialised JSON.** Slicing `json.dumps(...)[:N]` produced an
  unterminated string for any oversized tool result. Truncate a field, mark
  `truncated`, and keep the envelope parseable.
- **Opus 5 for conversation.** The code default had drifted to
  `claude-sonnet-5` while production ran `claude-opus-4-5`; both now say
  `claude-opus-5`. Owner decision: quality over token cost for the chat path.

## 2026-08-16 — R6.1B: domains as real isolation boundaries

`domain` existed on most tables since earlier rounds but was inert: retrieval,
wiki, chat context and Google accounts all mixed domains freely. R6.1B makes
`personal` / `travelon` / `tech` genuine security/context boundaries.

- **Three fixed domains, fail-closed parsing.** Only `personal`, `travelon`,
  `tech`. `parse_domain` rejects empty, unknown or model-generated values with a
  `DomainError` — it never falls back to "personal". Case and surrounding
  whitespace are tolerated at entry (normalised), but the *value* is never
  guessed. The only place a missing value becomes personal is bootstrap
  (`get_active_domain` for a user who has never switched) and the migration
  backfill — both documented, neither is "parsing".

- **Active domain is a server-side request snapshot.** It is read once, at the
  start of a request (from `UserState.active_domain`), and passed down as an
  immutable value. It is NEVER taken from model output, tool arguments, callback
  data or client JSON. A `/domain` switch that lands mid-request does not change
  the request in flight, and a background job (wiki compile, Drive index) carries
  the domain it was launched with.

- **The model cannot choose the domain.** `domain` is not a property in any
  tool's input schema, so the model physically cannot write `{"domain":
  "travelon"}`. `run_tool` injects the server-side domain positionally. TravelON
  tools are hidden from the model outside the travelon domain AND refused at
  dispatch with zero network calls — two independent layers, matching the
  defence-in-depth style of the secret gates.

- **No `domain="all"`, no cross-domain model context.** No code path builds an
  LLM prompt from more than one domain's raw RAG/wiki/memory/chat. The only
  cross-domain operations are deterministic, owner-only and documented: the
  security scan (global by design, findings grouped by domain), `/accounts`,
  `/domain_audit`, and the scheduled rituals — and those rituals are assembled
  from SEPARATE per-domain queries under explicit domain headers, never mixed.

- **Google accounts are domain-scoped and never guessed.** An account belongs to
  exactly one domain, or is unassigned (NULL). Domain-scoped tools use only the
  active domain's accounts, with no "all accounts" fallback; if none is assigned
  they say so honestly. OAuth carries the active domain inside HMAC-signed state;
  a new account binds to that signed domain; a reconnect refreshes tokens but
  never moves an account between domains. The owner assigns/changes domains
  explicitly in `/accounts`.

- **No automatic semantic legacy classification.** The migration backfills only
  from a TRUSTED PARENT (chunk←document, reminder←task, habit_log←habit);
  anything without a trusted parent goes to personal; Google credentials go to
  NULL. It never reads content, titles, tags, email addresses or model output to
  decide a domain. A legacy row in the wrong domain is fixed by a conscious
  re-upload, not by a guess — surfaced (counts only) by `/domain_audit`.

- **Unique constraints are per-domain.** The same document hash, wiki slug or
  dedupe key may exist independently in each domain (they are isolated, so
  collisions across domains are not conflicts). Global uniques were replaced by
  `(user_id, domain, …)` scoped ones in the same migration.

## 2026-08-15 — R6.1A.1: secret boundary hardening (independent audit)

An independent audit of R6.1A found that the gate was real but its perimeter
was not. Four classes of gap, all fixed here; passwords stay searchable per
the owner decision above — this round is about WHERE the scan runs and HOW
well it sees, not about re-litigating that.

- **Scanner v2.** v1 could not see a Cyrillic password («пароль: Секретний»
  was rejected as prose because it rejected ALL-Cyrillic values), a PIN or any
  repeated-digit value (`0000` looked like masking), a value starting with
  `$ % {` (it treated the first character as a placeholder marker), a
  comma-separated credential table, a column value more than 60 rows below its
  header, or a plain numeric recovery-code list. Placeholders are now matched
  EXACTLY (`<PASSWORD>`, `${TOKEN}`, `%TOKEN%`, `[REDACTED]`, `YOUR_API_KEY`,
  `***`) instead of by first character, and table detection runs over the whole
  document rather than per 20k window. `SCANNER_VERSION` 1 → 2, so the previous
  scan-complete marker no longer satisfies the gate.
- **Recursive envelope scan.** A resource is more than its body: a filename, a
  `source_ref` URL with a token in the query string, and a free-form `meta`
  dict all get persisted, logged and shown. `scan_envelope()` walks strings,
  dicts and lists (bounded depth and node count); a blocked title is replaced
  with a generic safe title, blocked meta keys are dropped whole rather than
  partially redacted, and no unsafe title reaches the audit log.
- **No raw fingerprint of a blocked body.** `content_hash` was a plain SHA-256
  of the document text — for a short credential that is reversible in practice,
  which re-created the leak inside the row meant to contain it. Quarantined
  rows now use a keyed HMAC (`quarantine_fingerprint`) under a key that never
  leaves the deployment: same dedupe behaviour, useless to a reader of the table.
- **Provider ARGUMENTS are input too.** The gate only checked what came back.
  A RAG query goes to OpenAI verbatim; tool arguments go to Gmail and Calendar;
  a goal title is written from two different adapters. Scans now sit in
  `rag.retrieve`, `chat_tools.run_tool` (before the executor), `/admin/search`,
  the Gmail reply-draft search, and `coach.create_goal/create_habit` — in core,
  so Telegram and the Mini App share one gate instead of each having their own.
- **Model OUTPUT is scanned before it becomes anything.** Extractor output,
  the final chat reply, the meeting digest and the composed draft body are all
  checked before persistence, before the Telegram reply, before TTS and before
  a Gmail draft. A blocked reply is dropped whole and its turn is marked
  `provider_eligible=False` so it can never replay into a later prompt. The
  read path re-scans stored turns for the same reason.
- **Voice STT exception, stated honestly.** DAN.OS cannot scan speech before
  transcribing it: the audio reaches the STT provider first, by construction.
  This round does not pretend otherwise — it scans the transcript before the
  Telegram echo, RawEvent, ChatLog and the chat model, and warns in `/start`
  not to dictate keys. A local STT model is the only real fix; it is recorded
  in NEXT.md, not claimed here.

## 2026-08-15 — R6.1A.1: passwords allowed, hard tokens still blocked (Danylo)

After R6.1A deployed and `/kb_security_scan` ran, the quarantine list showed
what was actually being held: 29 of 37 documents were partner-portal
password tables (operator cabinets, agent mailboxes) — the exact data the bot
is meant to retrieve («який логін/пароль до ТОКО»). Danylo's call: don't
rotate, just keep the technical tokens quarantined and unblock the passwords.

- Only HARD technical secrets block the knowledge base now: API keys,
  OAuth/bearer tokens, private keys, session cookies, recovery codes, seed
  phrases. A password (including a whole «Пароль» spreadsheet column) is
  searchable business data.
- This is a deliberate, owner-scoped relaxation of the R6.1A contract for a
  single-owner bot. The trade-off is explicit and accepted: partner passwords
  are now indexed, returned in answers (i.e. sent to the model provider) and
  compiled into wiki pages. Hard tokens never are — those genuinely have no
  place in a knowledge base and are re-issued at their source, not stored.
- Implemented as one setting, `QUARANTINE_PASSWORDS` (default false).
  `secret_policy` blocks only `HARD_SECRET_CATEGORIES`; set the flag true to
  restore the strict R6.1A behaviour without a code change.
- `/kb_security_scan` now reconciles both ways: it RELEASES quarantined
  content that no longer trips (password docs regain their chunks and become
  searchable; findings resolve) and keeps token content contained. A document
  quarantined at ingest (no stored chunks) is not auto-released — it needs
  re-ingesting.
- A mixed document (password + token) still quarantines, on the token; the
  password inside it is simply not what caused the block.

## 2026-08-14 — R6.1A: DAN.OS is not a password manager

The R3–R6 pursuit of «який логін до ТОКО Україна?» was treated as a retrieval
defect and fixed five times (truncation, translit, ranking, xlsx dimensions,
per-sheet ingest). It was not a retrieval defect. Storing and returning a
credential violates the plan's own contract — «жодного SQL/shell/URL у LLM»,
«журнал дій без тіл повідомлень і секретів», «the LLM never gets … secrets».
The right fix was to stop trying to answer the question.

- **Hard secrets never reach persistence, embeddings, the compiler, chat
  context or tool output.** Passwords, API keys, OAuth/bearer tokens, private
  keys, session cookies, recovery-code lists and seed phrases are classified
  by deterministic local code (`app/core/secret_policy.py`) — no LLM, no
  embeddings, no network. The scanner sits in `app/core`, ahead of every
  provider call, because a check that lives in a Telegram handler protects one
  door out of six.
- **The scanner's API is value-free.** It returns categories and counts and
  nothing else — no excerpt, no reversible encoding, no hash (a hash of a short
  credential is brute-forceable, i.e. reversible). Same rule for findings,
  audit rows, log lines, exception text, Telegram replies and scan reports.
- **Identity is not a secret.** Usernames, e-mail addresses, URLs, phone
  numbers, IBAN/ЄДРПОУ/ІПН, invoice and order numbers and ordinary bank
  requisites stay indexable and searchable. A false positive costs Danylo a
  real business answer, so every value rule is guarded by a concreteness check
  and placeholders (`<PASSWORD>`, `${TOKEN}`, `***`, `[REDACTED]`) never trip.
  «Яка політика паролів?» must keep working; «який пароль до X?» must not.
- **Quarantine is containment, not deletion.** Affected rows are marked
  (`Document.status`, `WikiPage.status`, `MemoryItem.status`,
  `ChatLog.provider_eligible`) and disappear from retrieval, compilation and
  model context. Nothing is deleted by code in this round — Danylo decides
  that, after seeing the counts. Raw events stay immutable: a finding is
  recorded against them, their payload is untouched.
- **Autonomous writes to long-term memory are gone.** `wiki_save_answer` let
  the model decide what to remember forever; that is how a credential
  spreadsheet became five permanent pages. It returns in R6.3 as a confirmed
  action with a preview card. Automatic compilation is off by default
  (`AUTO_WIKI_COMPILE_ENABLED=false`) and additionally gated on a completed
  local scan.
- **Honest compilation status.** `pending | succeeded | empty_valid | failed |
  deferred_large | quarantined` with compiler version, source/processed chars
  and an error CODE (never a body). An oversized source that only had its
  first 12k characters read reports `deferred_large` and stays in the queue —
  «done» must not mean «we read the beginning».
- The password-manager connector stays out. DAN.OS points at the vault; it
  does not become one.

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
- **Query archiving.** A synthesized answer can be saved as an archive page, so
  the next identical question is instant and knowledge compounds (the «Гепард»
  answer becomes permanent). _Superseded by R6.1A:_ the autonomous tool
  (`wiki_save_answer`) was removed; `wiki.save_archive()` stays in core and
  returns as a user-confirmed action in R6.3.
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
