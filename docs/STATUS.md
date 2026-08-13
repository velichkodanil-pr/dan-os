# STATUS

_Last verified: 2026-08-13 (round 3b implementation session)_

## Round 3b — Knowledge extensions: DELIVERED (gate: re-consent + live checks)

- /drive: list Drive folders → index pdf/docx/txt/md + Google Docs (read-only,
  20 files/run, hash dedupe). /reply <query>: find email → Haiku reply draft →
  preview → 💾 creates a Gmail DRAFT (L3; sending stays denied).
- Memory conflicts at confirm time: ⚖️ card with «нове замінює старе / лишити
  старе / обидва»; supersede keeps history (superseded_by).
- Sunday 19:00 weekly report: stats + unanswered questions + up to 3 source
  suggestions (coverage map v1); reported gaps marked resolved.
- Scopes now calendar.readonly + gmail.readonly + gmail.compose + drive.readonly
  → requires ONE /connect_google re-consent.
- **Multi-account Google**: every /connect_google adds another account
  (upsert by user+email); brief/digest aggregate all accounts (·label tags);
  /reply searches every account and drafts in the one that owns the email;
  /drive asks which account; /accounts lists/disconnects. Old single-account
  credentials table recreated (re-consent was required anyway).
- Tests: **30 passed**. Migrations `f75ba5fb32c6` + `a1b2c3d4e5f6`.

Gate: Danylo re-consents, /drive indexes a folder, /reply creates a Gmail draft,
Sunday report arrives.

## Round 0 — Foundation: DONE · Round 1 — Vertical slice: DONE · Round 2 — Secretary: DONE
(All gates closed 2026-08-13; Google connected, /brief works with real calendar+mail.)

## Round 3a — Knowledge core: DELIVERED (gate: live doc-question check pending)

- pgvector confirmed in Railway postgres-ssl:18 image; migration `5304547a0b52`
  creates extension + documents / knowledge_chunks (vector 1536, hnsw cosine
  index) / knowledge_gaps.
- Ingest: Telegram documents (pdf/docx/txt/md, ≤15MB) and forwarded messages →
  extract → chunk (800/120) → embed (text-embedding-3-small) → store with
  provenance. Dedupe by content hash (re-upload = no-op). /kb lists the base.
- RAG: every note runs retrieval (top-5, cosine ≤ 0.55); matched chunks are
  injected into the single Haiku prompt as DATA with source+date citation
  instruction. Unanswered questions land in knowledge_gaps (coverage map R3b).
- Gmail digest 2×/day (`DIGEST_TIMES`=13:00,18:30): recent inbox → Haiku
  importance ranking (⚡ marks) → one P2 message; silent when inbox is quiet.

Tests: **21 passed** (15 prior + chunking, ingest dedupe, retrieval provenance,
gap logged / not logged, question detector). Mock embedder is bag-of-words so
retrieval semantics are testable offline.

Gate check:

- [x] Tests green, migration applied locally
- [ ] Deploy green; Danylo: send a document → ask about its content → answer
  cites source; digest arrives at 13:00/18:30

## Round 3b — deferred scope

Drive folders read, email drafts (L3, scope upgrade + re-consent), memory
conflicts (supersede flow), weekly coverage-map report from knowledge_gaps.
