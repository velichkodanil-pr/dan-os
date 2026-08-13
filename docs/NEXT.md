# NEXT — the one authorized round

## Round 4 — Expansion (from month 2)

Scope (Plan v1.1 section 10):

1. Telegram Mini App: Today / Approvals / Memory screens (view + correct facts),
   server-side init-data validation, typed API client. Backend correctness first.
2. Travelon read-only pulse: bookings/statuses via existing travelon skills/API
   through a gateway module (separate `travelon` domain; no CRM writes ever).
3. Coach/OKR: goals + habits tables, weekly check-in merged into the Sunday
   report, honest progress framing (no guilt-tripping per spec).
4. Later in round: Zoom transcripts, wider L3 actions (calendar event creation
   with preview), TTS replies (optional).

Out of scope: Android app (after Mini App stabilizes), WhatsApp/Instagram,
Health, crypto, payments, email send.

Gate: Mini App shows Today+Memory from the phone; Travelon pulse card arrives;
first OKR check-in runs; tests extended (init-data validation, domain isolation
travelon vs personal, goals lifecycle).
