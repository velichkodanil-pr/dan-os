# NEXT — the one authorized round

## Round 1 — Vertical slice (~1 week)

End-to-end scenario that exercises the whole architecture (Plan v1.1, section 10):

1. Text or voice note → immutable raw event with a dedupe key (unique constraint).
2. Extraction boundary (Haiku via ExtractionProvider; deterministic mock for tests) → task proposal + optional memory candidate.
3. Preview card with ✅ Підтвердити / ✏️ Змінити / ❌ Відхилити buttons.
4. Approve → task created; «Сьогодні» command shows it; reminder fires on time.
5. Every step lands in the append-only audit log; policy L0–L2 enforced in code.

Required tests before the gate:

- replaying the same Telegram update does not create duplicates;
- double-tap on Approve does not create a second task;
- forbidden action (external write) is denied by policy;
- non-owner user gets silence and no data.

Gate: scenario works from the phone; tests green.

Out of scope (do NOT start): Gmail/Calendar connectors, Mini App, RAG, Travelon, briefs.
