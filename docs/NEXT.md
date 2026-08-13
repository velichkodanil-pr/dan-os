# NEXT — the one authorized round

## Round 2 — Secretary (~1 week)

Scope (Plan v1.1 section 10):

1. Google OAuth (installed-app flow, minimal scopes: Calendar read, Gmail read).
   App in Production mode from day one (avoid 7-day refresh-token expiry).
2. Morning brief 07:30 Kyiv: calendar events, top emails overnight, today's tasks,
   deadlines. One compact message; quiet hours respected.
3. Evening check-in 21:30 (opt-in): day summary + memory-candidate review
   (confirm ✅ / reject ❌ per candidate).
4. Profile memory: confirmed facts injected into extraction/chat context.
5. Persona: DAN.OS system prompt (TRUTH/MEMORY/PRIVACY/TOOLS/PROACTIVITY/SAFETY
   structure from docs/product) + multi-turn chat with short conversation window.
6. /brief command for on-demand brief.

Out of scope: Gmail drafts/send, calendar writes, Drive, RAG, Mini App, Travelon.

Gate: brief arrives at 07:30 with real calendar+email data; candidate review works;
no task lost during the week; tests extended (OAuth token storage, brief builder,
candidate confirm/reject idempotency).
