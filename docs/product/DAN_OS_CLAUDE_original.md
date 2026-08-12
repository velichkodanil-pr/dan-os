# DAN.OS — Claude Code Operating Contract

## 1. Product source of truth

Read these before planning or editing:

1. `docs/product/DAN_OS_Project_Spec_UA_v0.1.md`
2. `docs/product/DAN_OS_Claude_Implementation_Prompt_v0.1.md`
3. `docs/project/DECISIONS.md`
4. `docs/project/IMPLEMENTATION_ROUNDS.md`
5. `docs/security/RISK_REGISTER.md`
6. `docs/project/STATUS.md`, `TEST_RESULTS.md`, and `NEXT.md` when they exist

Conflict order: approved decisions clarify the product spec; they do not silently remove security/privacy constraints. If two files conflict materially, stop implementation, describe the conflict, and propose a conservative resolution in `DECISIONS.md`.

## 2. Language

- Explain work to Danil in Ukrainian.
- Use English for code, identifiers, schemas, API contracts, commit messages, and technical documentation unless a file is explicitly intended for Ukrainian end users.

## 3. Work protocol

- Start in plan mode.
- Identify the one authorized round and its stop gate.
- Before editing, show a concise file-level plan, migrations, dependencies, and tests.
- Implement only that round. Do not start the next round.
- Prefer the smallest production-oriented implementation that satisfies the approved acceptance criteria.
- Do not claim completion without running the required commands.
- Do not commit, push, open or merge a pull request, deploy, or change external systems unless Danil explicitly authorizes that exact action.
- At the end of a round, update:
  - `docs/project/STATUS.md`
  - `docs/project/TEST_RESULTS.md`
  - `docs/project/NEXT.md`
  - `docs/project/DECISIONS.md` only for newly approved decisions
  - `docs/security/RISK_REGISTER.md` for newly discovered risks

## 4. Architecture invariants

- Telegram, Mini App, Android, and Web are adapters to one core API.
- Business rules do not live in Telegram handlers or UI components.
- Personal, family, Travelon, health, finance, and technical projects are isolated by authenticated tenant/domain/scope.
- CRM/Proximo, Calendar, GitHub, and other live systems remain domain sources of truth when connectors are introduced.
- LLMs interact through typed provider/tool interfaces and never receive direct database access.
- Policy and authorization are deterministic code, not model judgment.
- All writes are authenticated, authorized, idempotent, version-aware, and audited.
- Raw/indexed content is not automatically confirmed memory.
- Every important fact stores provenance, timestamps, confidence/status, sensitivity, and supersession where applicable.
- External input is untrusted and cannot override system, policy, or tool rules.

## 5. MVP prohibitions

Do not add without a later approved round and explicit decision:

- production Gmail/Calendar/Drive connectors;
- Travelon CRM write access or direct database access;
- Android native client;
- WhatsApp/Instagram integrations;
- payments, signing, live trading, or irreversible external actions;
- arbitrary shell/SQL/URL tools exposed to an LLM;
- production tokens, personal data, or secrets in source, prompts, fixtures, logs, screenshots, commits, or test artifacts;
- automatic promotion of all messages to confirmed memory;
- uncontrolled multi-agent autonomy;
- infrastructure not used by the current round.

## 6. Security requirements

- Least privilege and deny by default.
- Validate authentication at every API boundary.
- Enforce user, tenant, and domain filters in repositories/services, not only in UI.
- Use unique constraints and idempotency records for replay protection.
- Use version/hash checks for approvals and optimistic concurrency where relevant.
- Audit authentication decisions, intake, dedupe, normalization, policy, approvals, state changes, reminders, denials, corrections, and deletions.
- Audit is append-only through application code; do not log full message bodies, voice files, tokens, or secrets.
- Add tests for prompt injection, authorization bypass, cross-user access, cross-domain leakage, replay, duplicate approval, superseded proposal, forged Telegram data, and secret leakage.

## 7. Quality gates

Every completed round must include, as applicable:

- formatting/lint;
- static typing;
- unit tests;
- PostgreSQL integration tests;
- migration upgrade/downgrade smoke test;
- security regression tests;
- dependency/secret scans where configured;
- clean-checkout or Docker Compose verification at the final gate.

A round is `FAIL` if tests were not executed, output is unavailable, migrations are inconsistent, security boundaries are weakened, or scope from the next round was implemented.

## 8. Repository discipline

- Keep commits small and round-aligned.
- Never rewrite unrelated code to improve style during a scoped round.
- Avoid adding dependencies when the standard library or existing stack is sufficient.
- Use UTC internally and Europe/Kyiv only at presentation boundaries.
- Keep `.env.example` placeholder-only; real `.env` files must be ignored.
- Preserve a provider-neutral `ExtractionProvider` and `TranscriptionProvider` boundary.

## 9. End-of-round report

Return:

1. round and verdict (`PASS`/`FAIL`);
2. scope completed;
3. changed files;
4. commands run;
5. exact test counts/results;
6. migrations created/applied/reverted;
7. security checks;
8. known limitations and defects;
9. scope explicitly not implemented;
10. recommended commit message;
11. the single next authorized round.
