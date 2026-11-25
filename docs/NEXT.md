# NEXT — the one authorized round

## Round 9 — Token & cost meter (CURRENT, pushed)

1. After deploy: `/usage` — empty at first, fills with every model call.
2. After a day: `/usage 1` — which feature costs what; tune CHAT_MODEL /
   context window if the chat dominates.
3. USAGE_ALERT_USD_DAY (default 5) — one alert a day above it.

## Round 8.2 — Energy (pushed)

1. Check Railway logs for `deye shape` lines — every endpoint's real field
   names; fix the mapping if one differs from the swagger.
2. In Telegram: the pinned card now has 🔋 lines with «запас/вистачить».
3. `/energy` now, `/energy month`; the automatic report comes at 21:00.
4. `/precharge 1` on a day with grid — check the preview (slots, times), press
   ✅, confirm in the Deye app that grid charge is on, and that the table is
   restored after an hour.
5. Open the Mini App in the personal domain → ⚡ Дім.
6. Open question for the owner: outage queue (черга) and operator, so
   pre-charge can be PROPOSED automatically before scheduled outages.

## Round 7.1 — Rituals cannot be lost to markup (pushed)

Out-of-round hotfix asked for on 2026-11-23 after the 2026-11-22 brief was
lost; rebased onto R8.1 (`ba7f6e1`) before the push. No migration, no new env.

1. (done — reviewed and pushed on the owner's «Так, пуш у main»)
2. `/health/live` must report `r8.1.1`.
3. In Telegram: `/brief`. It must arrive even when the inbox holds a
   «Name <address>» sender (that is the 22.11 case); the address shows as text.
4. Next morning the 07:30 brief arrives by itself. In the Railway log a failing
   ritual now reads «failed (attempt N of 5); next try after …», never a
   wall of identical tracebacks.

### Known limits (deliberate)

- A message that needed the plain-text fallback loses its bold headers — it
  arrives readable, not pretty. The source escaping makes this rare.
- Command replies and chat answers are unchanged: they already escaped their
  content; only rituals, reminders and owner alerts use `send_html()`. The R8
  power card keeps its own send/edit path.
- `test_r61d` still has its two date-dependent failures (see STATUS).
- `requirements.txt` has no upper pins: production built `aiogram 3.31.0` on
  2026-11-04, while today's `aiogram 3.34` does not import on Python 3.12.0.
  Railway's Python is newer, so builds are expected to be fine, but pinning is
  worth a decision.

## Round 8.1 — TravelON booking digest + alerts (DEPLOYED 2026-11-23; owner check open)

Owner request 23.11.2026. No migration, no new env required.

1. (done — deployed; production reported `r8.1` before R7.1 landed)
2. (done 23.11 16:33 — `/bookings` matched the CRM list exactly: 43 created,
   40 sales + 2 tests + 1 rebooking, 97 tourists, 72 036 EUR, +74% vs 16.11)
3. r8.1.2: digests arrive at 12:00, 15:00, 18:00, 21:00 and 00:00 Kyiv
   (`BOOKINGS_DIGEST_TIMES`), each «since the previous slot» + the day so far.
   A slot found more than 50 min late (deploy, outage) is skipped, not replayed.
   First live check: the 18:00 digest on 23.11.
4. Alerts: every 10 min from 08:00 to 22:00 — only big amounts, big groups,
   last-minute departures, new agencies, big cancellations. The «new agency»
   rule switches on by itself a few minutes after the first poll (history scan).
5. Tune after a week: `BOOKINGS_ALERT_BIG_EUR` (5000), `BOOKINGS_ALERT_GROUP`
   (8), `BOOKINGS_ALERT_LASTMINUTE_DAYS` (3), `BOOKINGS_ALERT_CANCEL_EUR`
   (3000), `BOOKINGS_ALERT_HOURS` (08:00-22:00), `BOOKINGS_ALERT_INTERVAL_MIN`
   (10; 0 disables).

## Round 8 — Home power status (DEPLOYED 2026-11-23; owner check open)

1. (done — deployed; no migration, state is one `app_state` row)
2. Within ~2 min a card appears in the DAN.OS chat and is pinned. It needs a
   Google account assigned to 🏠 Особисте that receives Deye mail
   (`/accounts`).
3. `/power` — the same card + 7-day total.
4. Optional stage 2: create an app at developer.deyecloud.com, then set Railway
   variables DEYE_APP_ID, DEYE_APP_SECRET, DEYE_EMAIL, DEYE_PASSWORD_SHA256
   (sha256 hex of the Deye Cloud password). Battery / PV / load lines appear
   on the next refresh. Field names were checked against the official
   swagger (eu1-developer.deyecloud.com/v2/api-docs, 23.11.2026):
   station/list → id, name, batterySOC, generationPower, connectionStatus
   (NORMAL / ALL_OFFLINE / PARTIAL_OFFLINE / NO_DEVICE), lastUpdateTime;
   station/latest → batterySOC, generationPower, consumptionPower,
   wirePower, gridPower, batteryPower, lastUpdateTime (date-time).
5. Tunables: POWER_POLL_SECONDS (120), POWER_DEBOUNCE_MINUTES (3),
   POWER_PUSH (true), POWER_MAIL_FROM (empty disables).

## Round 7 — English coach (DEPLOYED; owner check still open)

In production since at least 2026-09-06. The owner walk-through below has not
happened yet:

1. (done — deployed)
2. (done — migration `c9d3e4f5a6b7` ran in production)
3. Optional env: `ENGLISH_TIME` (default `20:00`) — the evening nudge. Empty
   disables it. Nothing else to configure; the coach uses `CHAT_MODEL`.
4. In Telegram: `/domain personal`, then `/english`.
5. Press ▶️ Сесія. First session is all new phrases (nothing is due yet) —
   ~8 cards. Then 💬 Розмова: write English, the coach answers in English and
   corrects in a batch every third turn. Voice works too and comes back as
   voice. Close with /english_stop.
6. Come back tomorrow: the queue now holds yesterday's phrases plus anything
   the conversation corrected. That is the whole point of the round.
7. 📈 Прогрес shows streak, accuracy and the phrases that keep breaking;
   📚 План shows all 12 weeks.

### Known limits (deliberate, not bugs)

- The plan is 12 weeks; after week 12 the position stops advancing and sessions
  become review + conversation. Extending the curriculum is a later round.
- No listening-comprehension content beyond voice replies, and no pronunciation
  scoring — the model hears a transcript, not audio.
- Level is fixed at B1 and minutes at 12 until he asks otherwise; both are
  columns, so changing them is an UPDATE, not a migration.

## Round 6.1D — Order aggregates (DEPLOYED; owner check still open)

Deployed; the nightly sync runs every day. Remaining owner steps:

1. (done — production reports `r7`)
2. (done — the commit is on `main`)
3. Optionally run `/travelon_sync` to pre-warm; not required — the bot fills
   any window it needs on demand the first time it is asked.
4. Ask in Telegram: «скільки туристів у Туреччину з приймаючою Gepard за
   серпень» — expect 627 заявок / 1 610 туристів, basis «дата заїзду». The
   first such question takes ~40 s while it fetches; the next is instant.
5. Try «а за датою створення» and «на яку суму» — both are supported.
6. From then on the cache also refreshes itself nightly at 04:30.

### Deferred out of this round (deliberately — do NOT start)

- **Supplier cabinets.** Owner decision: we ask only our own system. Logging
  into Kalanit / TOCO / E.Line to cross-check volumes is NOT in scope.
- Margin / profitability reporting. Turnover per currency IS included; margin
  needs net-vs-gross semantics reviewed before it can be trusted.
- Nightly PRE-warm beyond ‑30 days; deep history is reachable on demand but is
  archive.

## Round 6.2 — Wiki revisions & durable compile queue (later, separate round)

- immutable page revisions (history + rollback) instead of in-place overwrite;
- normalized fact/source tables so provenance is per-fact, not per-page;
- a durable compile queue with retries, replacing best-effort inline calls;
- a full section compiler for oversized sources — this is what turns R6.1A's
  honest `deferred_large` status into an actually-complete compilation.

## Round 6.3 — Confirmed save-answer flow (later, separate round)

R6.1A removed `wiki_save_answer`: the model can no longer write to long-term
memory on its own initiative. The capability itself is still worth having, but
as a user-confirmed action — a preview card («зберегти цю відповідь як сторінку
архіву?») with an explicit button, the same pattern as calendar and drafts, and
the secret gate on the way in. `wiki.save_archive()` already exists in core and
already passes the gate; only the confirmation flow is missing.

## Immediately after deploying R6.1A (owner actions, not code)

1. Deploy the reviewed commit.
2. Run `/kb_security_scan` and keep the counts-only result.
3. Rotate any credentials that could have been in indexed sources.
4. Verify: a password question returns the safe «не зберігаю» answer; an
   ordinary business document still indexes and is findable.
5. Leave `AUTO_WIKI_COMPILE_ENABLED=false` until the scan result is reviewed.

## Idea backlog (NOT authorized)

Monthly TravelON analytics, task auto-carryover proposals, /reply v2 (thread
context), memory browser in the Mini App, a vault/password-manager connector
(explicitly out of scope for R6.1A).

2026-11-23 architecture proposal «for the whole life» (owner page:
https://claude.ai/artifact/JfMw3byL9A2VEiTB4Cd9T8), awaiting the owner's four
decisions (finance/health domains, what MCP may write, owner-only vs family,
order). Proposed order (round numbers to be assigned — R8 / R8.1 are now
power status and the booking digest): attention budget (P0–P3, daily cap,
👍/👎) → DAN.OS as an MCP server for Claude → module contract → deadlines +
tech-ops → life graph → commitments + meeting prep → journal, reviews,
decision log → travel → money (own domain). None of it is authorized until
he picks it.

## Out of scope

WhatsApp/Instagram, Health, crypto, payments, email send; Zoom API integration
(dropped by Danylo); Android (postponed); editing/deleting existing calendar
events.
