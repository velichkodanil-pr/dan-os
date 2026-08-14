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

- `/kb_security_scan` (owner-only) — one local, bounded, idempotent pass over
  the existing base. Zero provider calls; counts-only report; nothing deleted.
- `AUTO_WIKI_COMPILE_ENABLED` (default `false`) — automatic wiki compilation
  also requires that scan to have completed successfully.
- Real tokens live only in Railway variables or a local gitignored `.env`.

## Layout

```
app/
  main.py          FastAPI app, webhook endpoint, health, lifespan (sets webhook)
  config.py        pydantic-settings (env)
  telegram/bot.py  aiogram router — adapter only, no business logic
  core/            module boundaries: orchestrator, memory, policy, audit, scheduler (R1+)
docs/              STATUS / NEXT / DECISIONS + product docs
```
