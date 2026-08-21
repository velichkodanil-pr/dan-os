# DAN.OS

Personal AI operating system for Danylo. Telegram-first interface, one core API,
managed memory with provenance, deterministic action policy, proactive routines.

**Plan and architecture:** `docs/product/DAN_OS_Plan_v1.1.md` (approved 2026-08-12).
**Current state:** `docs/STATUS.md` · **Next authorized round:** `docs/NEXT.md`.

## Stack

Python 3.12 · aiogram 3 (webhook) · FastAPI · PostgreSQL + pgvector (from round 1) ·
Railway (deploy = push to `main`).

## Local run

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                            # fill TELEGRAM_BOT_TOKEN etc.
uvicorn app.main:app --reload --port 8000
```

Health: `GET /health/live` (returns the build `version`/`release` from
`app/config.py`), `GET /health/ready`. Telegram webhook: `POST /telegram/webhook`
(guarded by `X-Telegram-Bot-Api-Secret-Token`). Without `RAILWAY_PUBLIC_DOMAIN` the app
starts fine and simply skips webhook registration.

## Secrets are not knowledge (R6.1A)

DAN.OS is not a password manager. Passwords, API keys, OAuth/bearer tokens,
private keys, session cookies, recovery codes and seed phrases are detected by
deterministic local code (`app/core/secret_policy.py` — no LLM, no embeddings,
no network) **before** anything is persisted, embedded, compiled or sent to a
model. Affected content is quarantined: contained and excluded from retrieval,
never deleted by code.

Usernames, e-mail addresses, URLs, IBAN/ЄДРПОУ/ІПН, invoice and order numbers
and ordinary requisites stay fully searchable — the gate targets credentials,
not business data.

## Domains: three isolated spaces (R6.1B)

DAN.OS keeps three separate worlds — `🏠 personal`, `🧳 travelon`, `🛠 tech`.
Knowledge, tasks, mail, calendar and memory of one are invisible to the others;
a personal note can never surface inside a business answer. The active domain is
chosen server-side and the model cannot change it.

- `/domain` — show the active domain and switch it with buttons;
  `/domain travelon` switches directly (`/personal` and `/tech` are aliases).
  `/travelon` stays the business pulse, it is NOT a switch.
- Everything you do — uploads, forwards, notes, questions, goals, habits,
  drafts, calendar actions — happens in whatever domain is active at that moment.
- `/accounts` — connect Google accounts and **assign each one to a domain**.
  After the R6.1B migration every existing account is unassigned; until you
  assign it, Gmail/Calendar/Drive for that domain will honestly say there is no
  account. A new `/connect_google` binds the account to the domain you are in;
  reconnecting never moves an account between domains.
- `/domain_audit` — owner-only integrity report: how many resources sit in each
  domain, unassigned accounts, parent/child mismatches. Counts only, no content.
- **Legacy note:** the migration never guesses a domain from content. Anything
  that historically landed in the wrong domain is not moved automatically — to
  re-file it, re-upload the material while that domain is active.

- `/kb_security_scan` (owner-only) — one local, bounded, idempotent pass over
  the existing base. Zero provider calls; counts-only report; nothing deleted.
- `AUTO_WIKI_COMPILE_ENABLED` (default `false`) — automatic wiki compilation
  also requires that scan to have completed successfully.
- Real tokens live only in Railway variables or a local gitignored `.env`.

## English coach (R7)

`/english` — a daily ~12-minute session with memory. Not a prompt: a 12-week
curriculum (96 working phrases), SM-2 spaced repetition, and a mistake log that
rebuilds what tomorrow shows.

- ▶️ **Сесія** — reviews that are due, then this week's new phrases, then the
  week's speaking task. The system chooses; you only answer «не згадав /
  важко / знаю».
- 💬 **Розмова** — a speaking partner that stays in English and plays the other
  side of the scenario. Corrections arrive in a batch every third turn, never
  inside the sentence, and become review cards. Voice in → voice out.
- 📈 **Прогрес** / 📚 **План** — streak (counted in days, not taps), accuracy,
  the phrases that keep breaking, and all 12 weeks.
- **Personal domain only.** Practice never lands in `ChatLog`, so it can never
  become context for a business answer — the mirror of TravelON tools being
  travelon-only. The coach itself gets no tools at all.
- `ENGLISH_TIME` (default `20:00`) — evening nudge; silent when the day is
  already done. Empty disables it.

## Layout

```
app/
  main.py          FastAPI app, webhook endpoint, health, lifespan (sets webhook)
  config.py        pydantic-settings (env)
  telegram/bot.py  aiogram router — adapter only, no business logic
  core/            module boundaries: orchestrator, memory, policy, audit, scheduler (R1+)
docs/              STATUS / NEXT / DECISIONS + product docs
```
