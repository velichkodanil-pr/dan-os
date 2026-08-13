# NEXT — the one authorized round

## Round 3 — Knowledge (~2-3 weeks)

Scope (Plan v1.1 section 10):

1. Verify/enable pgvector on the Railway Postgres; embeddings via
   text-embedding-3-small behind a thin EmbeddingProvider.
2. Ingest into the knowledge base (raw → indexed): forwarded messages, documents
   sent to the chat (pdf/docx/txt), selected Drive folders (read-only).
3. RAG answers with source + date attribution ("звідки ти це знаєш").
4. Gmail digest 2×/day (P2 bundle) + email drafts as L3 (preview + confirm,
   draft only — no send).
5. Memory conflicts: detect contradicting confirmed facts → show both versions →
   supersede with history.
6. Coverage map v1: weekly analysis (unanswered questions, repeated manual
   uploads) → 1-3 source suggestions with value/permissions/risk.

Out of scope: Mini App, Travelon gateway, Zoom, Android, email send, calendar writes.

Gate: bot answers a question from an ingested document with source; at least one
useful source suggestion produced; digest arrives twice a day; tests extended
(ingest dedupe, RAG provenance, conflict flow, digest builder).
