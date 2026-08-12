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

Health: `GET /health/live`, `GET /health/ready`. Telegram webhook: `POST /telegram/webhook`
(guarded by `X-Telegram-Bot-Api-Secret-Token`). Without `RAILWAY_PUBLIC_DOMAIN` the app
starts fine and simply skips webhook registration.

## Layout

```
app/
  main.py          FastAPI app, webhook endpoint, health, lifespan (sets webhook)
  config.py        pydantic-settings (env)
  telegram/bot.py  aiogram router — adapter only, no business logic
  core/            module boundaries: orchestrator, memory, policy, audit, scheduler (R1+)
docs/              STATUS / NEXT / DECISIONS + product docs
```
