# DAN.OS — покрокова інструкція роботи в Claude

**Версія:** 1.0  
**Дата:** 12 серпня 2026  
**Мета:** перейти від концепції DAN.OS до першого безпечного наскрізного MVP у Telegram.

---

## 1. Правильна схема роботи

Використовуйте два окремі середовища:

1. **Claude Project** — продуктова база знань, архітектурні рішення, ризики, backlog і документація.
2. **Claude Code у локальному GitHub-репозиторії** — створення та перевірка коду.

Не будуйте весь продукт в одному звичайному чаті Claude. Чат не є джерелом правди. Усі затверджені рішення мають бути записані у файли проєкту та Git.

---

## 2. Підготуйте вихідні файли

Збережіть локально:

- `DAN_OS_Project_Spec_UA_v0.1.md` — головна продуктова специфікація;
- `DAN_OS_Claude_Implementation_Prompt_v0.1.md` — технічне завдання на перший vertical slice;
- `DAN_OS_Claude_Project_Instructions_v1.txt` — постійні інструкції Claude Project;
- `CLAUDE.md` — правила роботи Claude Code всередині репозиторію.

Не перейменовуйте перші два файли: подальші prompts посилаються на них за назвою.

---

## 3. Створіть Claude Project

У Claude:

1. Відкрийте **Projects**.
2. Натисніть **New Project**.
3. Назва: `DAN.OS — Product & Architecture`.
4. Видимість: **Private**.
5. У Project Knowledge завантажте:
   - `DAN_OS_Project_Spec_UA_v0.1.md`;
   - `DAN_OS_Claude_Implementation_Prompt_v0.1.md`.
6. Відкрийте **Set project instructions**.
7. Вставте весь текст із `DAN_OS_Claude_Project_Instructions_v1.txt`.
8. Збережіть інструкції.

### Важливе правило

Не покладайтеся на попередній чат. Кожне затверджене рішення потрібно оформити окремим `.md`-файлом і додати до Project Knowledge або Git-репозиторію.

---

## 4. Проведіть Architecture Gate до написання коду

Створіть у Claude Project новий чат і вставте:

```text
Прочитай повністю файли DAN_OS_Project_Spec_UA_v0.1.md та DAN_OS_Claude_Implementation_Prompt_v0.1.md.

Поки що НЕ пиши код і НЕ створюй репозиторій. Працюй як principal product architect, security architect і technical lead.

Завдання Architecture Gate:
1. Знайди суперечності, неоднозначності, пропущені рішення та надмірний scope.
2. Перевір, що personal, family, Travelon, health, finance і technical projects розділені за доступами та пам’яттю.
3. Перевір boundaries: Telegram є adapter; LLM не має direct DB access; policy decisions визначає детермінований код; external writes заборонені в MVP.
4. Зафіксуй найменший vertical slice, який реально можна завершити й протестувати.
5. Сформуй чотири окремі готові файли:
   - docs/project/DECISIONS.md
   - docs/project/DATA_MAP.md
   - docs/security/RISK_REGISTER.md
   - docs/project/IMPLEMENTATION_ROUNDS.md
6. Для кожного невизначеного питання прийми консервативне рішення, явно познач його як assumption і не розширюй MVP.
7. В IMPLEMENTATION_ROUNDS розбий роботу на R0–R7 з acceptance criteria, тестами та stop gate після кожного раунду.
8. Наприкінці дай verdict: GO, GO WITH CONDITIONS або NO-GO.

Не починай реалізацію. Результат має бути придатним для збереження у Git без додаткового переписування.
```

### Що перевірити у відповіді

Architecture Gate не пройдено, якщо Claude:

- пропонує одразу Gmail, Travelon CRM, Android і Telegram;
- хоче дати LLM прямий доступ до SQL, shell або секретів;
- автоматично перетворює всі повідомлення на підтверджену пам’ять;
- не має idempotency, audit і domain isolation;
- не визначив точний Definition of Done першого vertical slice.

### Що зробити після відповіді

1. Завантажте створені Claude файли.
2. Додайте їх до Project Knowledge.
3. Не замінюйте ними основну специфікацію: вони доповнюють її.
4. Якщо є суттєва суперечність, спочатку оновіть `DECISIONS.md`, а вже потім починайте код.

---

## 5. Створіть приватний GitHub-репозиторій

У GitHub створіть:

- repository: `dan-os`;
- visibility: **Private**;
- default branch: `main`;
- без production secrets;
- на початку достатньо README або порожнього репозиторію.

На Windows у PowerShell:

```powershell
cd C:\Projects
git clone https://github.com/YOUR_GITHUB_USERNAME/dan-os.git
cd dan-os
New-Item -ItemType Directory -Force -Path docs/product | Out-Null
New-Item -ItemType Directory -Force -Path docs/project | Out-Null
New-Item -ItemType Directory -Force -Path docs/security | Out-Null
```

Скопіюйте у репозиторій:

```text
docs/product/DAN_OS_Project_Spec_UA_v0.1.md
docs/product/DAN_OS_Claude_Implementation_Prompt_v0.1.md
docs/project/DECISIONS.md
docs/project/DATA_MAP.md
docs/project/IMPLEMENTATION_ROUNDS.md
docs/security/RISK_REGISTER.md
CLAUDE.md
```

Перший commit документації:

```powershell
git add .
git commit -m "docs: initialize DAN.OS product and architecture"
git push -u origin main
```

Потім створіть робочу гілку:

```powershell
git switch -c feat/mvp-vertical-slice
```

Не розробляйте безпосередньо в `main`.

---

## 6. Підготуйте Claude Code на Windows

Мінімально потрібно:

- Git for Windows;
- Docker Desktop;
- VS Code;
- Claude Code CLI або офіційне розширення Claude Code для VS Code.

Встановлення Claude Code через WinGet:

```powershell
winget install Anthropic.ClaudeCode
claude --version
```

Альтернативна native-установка в PowerShell:

```powershell
irm https://claude.ai/install.ps1 | iex
claude --version
```

Відкрийте репозиторій:

```powershell
cd C:\Projects\dan-os
code .
```

Запустіть Claude Code:

```powershell
claude
```

На першому запуску виконайте авторизацію через браузер. У VS Code також можна встановити розширення **Claude Code** та працювати через його панель.

---

## 7. Ініціалізуйте правила Claude Code

У корені вже має бути підготовлений `CLAUDE.md`.

У Claude Code:

1. Перемкніться в **Plan mode**.
2. Виконайте `/init`.
3. Попросіть перевірити наявний `CLAUDE.md`, але не переписувати його мовчки.
4. Приймайте лише зміни, які не послаблюють security boundaries і stop gates.

Вставте:

```text
Read CLAUDE.md and all files under docs/product, docs/project, and docs/security.

Do not edit files yet. Work in plan mode.

Validate that the repository instructions are consistent with the product specification. Show any proposed CLAUDE.md changes as a diff and explain why each change is necessary. Do not remove security constraints, round stop gates, test requirements, or the rule that Claude must not push or merge automatically.
```

---

## 8. Отримайте повний план, але не дозволяйте реалізовувати все одразу

У Plan mode вставте:

```text
Read:
- CLAUDE.md
- docs/product/DAN_OS_Project_Spec_UA_v0.1.md
- docs/product/DAN_OS_Claude_Implementation_Prompt_v0.1.md
- docs/project/DECISIONS.md
- docs/project/IMPLEMENTATION_ROUNDS.md
- docs/security/RISK_REGISTER.md

Do not edit files and do not run destructive commands.

Produce the execution plan for the MVP vertical slice. For each round R0–R7 show:
1. exact scope;
2. files/modules expected;
3. dependencies introduced;
4. database migrations;
5. tests required;
6. acceptance criteria;
7. security checks;
8. stop gate;
9. likely risks.

Then show only the detailed implementation plan for R0. Do not start R1 and do not write code until I explicitly authorize R0.
```

### Перед дозволом на R0 перевірте

- немає Gmail, Calendar, Travelon CRM або Android;
- немає production deployment;
- немає реального Telegram token;
- є Docker Compose, PostgreSQL, API health endpoints, lint/type/tests і CI;
- є `.env.example`, `.gitignore`, ADR і документація;
- є чіткий stop після R0.

---

## 9. Реалізуйте MVP окремими раундами

### R0 — Repository Bootstrap

Результат:

- структура monorepo;
- Python/FastAPI skeleton;
- Docker Compose з PostgreSQL;
- health endpoints;
- lint, formatting, typing, pytest;
- Alembic initialization;
- CI skeleton;
- README, ADR, status files;
- жодної бізнес-логіки Telegram або пам’яті.

Prompt:

```text
Implement only Round R0 as defined in docs/project/IMPLEMENTATION_ROUNDS.md.

Rules:
- Before editing, show a concise file-level plan.
- Do not start R1.
- Do not add Gmail, Calendar, Travelon, Android, n8n, Redis, external AI providers, or production deployment.
- Do not use real secrets.
- Run all R0 tests and quality checks.
- Update docs/project/STATUS.md, docs/project/TEST_RESULTS.md, and docs/project/NEXT.md.
- Do not commit, push, open a PR, or merge.
- Stop after reporting changed files, commands run, test results, migrations, limitations, and recommended commit message.
```

### R1 — Identity, Domains, Raw Events, Idempotency, Audit

Результат:

- `User`, `Tenant`, `DomainScope`;
- `RawEvent`, `IdempotencyRecord`, `AuditRecord`;
- immutable intake;
- unique dedupe key;
- replay повертає попередній результат;
- cross-user і cross-domain tests.

Prompt:

```text
Implement only Round R1. Treat the approved R0 repository and docs/project/STATUS.md as the baseline.

Required focus: identity context, tenant/domain isolation, raw immutable events, idempotency, append-only audit, migrations, API contracts, and security tests.

Do not implement proposals, tasks, reminders, Telegram, Mini App, remote AI, or external integrations. Run the full test suite, update STATUS/TEST_RESULTS/NEXT, do not commit or push, and stop at the R1 gate.
```

### R2 — Normalization, Extraction Boundary, Policy, Proposals

Результат:

- `NormalizedEvent`;
- deterministic extractor;
- provider-neutral interfaces;
- `TaskProposal` і `MemoryCandidate`;
- deterministic Policy Engine;
- prompt-injection-shaped text лишається untrusted content.

Prompt:

```text
Implement only Round R2. Add normalization, the deterministic extraction boundary, typed task and memory proposals, and code-based policy evaluation.

The model may propose data but must never authorize actions. The system must work without an external AI provider. Add prompt-injection and policy-bypass tests. Do not implement approval execution, task creation, Telegram, or Mini App. Run all tests, update project status files, do not commit or push, and stop at the R2 gate.
```

### R3 — Approvals, Tasks, Today, Reminders

Результат:

- versioned approval state machine;
- double approval idempotency;
- superseded proposal conflict;
- task creation/cancellation;
- Today API;
- reminder schedule/cancel;
- full audit trail.

Prompt:

```text
Implement only Round R3. Add approval semantics, task lifecycle, Today API, reminder scheduling, cancellation, version/hash conflict checks, and complete audit coverage.

Test duplicate approval, superseded proposals, retry after interruption, reminder cancellation, correction/deletion visibility, and authorization boundaries. Do not implement Telegram, voice, Mini App, or external integrations. Run the full suite, update status files, do not commit or push, and stop at the R3 gate.
```

### R4 — Telegram Text Adapter

Результат:

- aiogram adapter;
- text update intake;
- webhook secret validation;
- development polling mode;
- proposal card;
- Approve/Edit/Reject callbacks;
- Telegram залишається adapter, а не бізнес-ядром.

Prompt:

```text
Implement only Round R4: Telegram text adapter and inline approval UX.

Use mocks/fixtures by default. No production bot token in code or tests. Validate webhook secrets and preserve end-to-end idempotency. Telegram handlers must call core application services rather than contain business logic. Do not implement voice or Mini App. Run all tests, update status files, do not commit or push, and stop at the R4 gate.
```

### R5 — Voice Provider Boundary

Результат:

- voice file intake contract;
- `TranscriptionProvider` interface;
- mock/local test provider;
- size/time/error limits;
- без прив’язки до одного AI-провайдера.

Prompt:

```text
Implement only Round R5: voice-note ingestion through a provider-neutral TranscriptionProvider interface with mock/local testing.

Add input-size, timeout, unsupported-format, retry, dedupe, and audit tests. Do not add paid provider credentials or production transcription integration. Do not start Mini App. Run all tests, update status files, do not commit or push, and stop at the R5 gate.
```

### R6 — Minimal Telegram Mini App

Результат:

- authenticated shell;
- server-side Telegram init-data validation;
- Today screen;
- Approvals screen;
- typed API client;
- forged init-data test.

Prompt:

```text
Implement only Round R6: the smallest authenticated Telegram Mini App shell with Today and Approvals screens.

Backend correctness has priority over UI polish. Validate Telegram init data server-side. Do not add memory administration, Google connectors, Travelon, Android, or production deployment. Run frontend and backend checks, update status files, do not commit or push, and stop at the R6 gate.
```

### R7 — Security Closure and Clean-Checkout Demo

Результат:

- повна regression/security suite;
- secret scan;
- migration smoke test;
- clean Docker Compose startup;
- runbooks;
- end-to-end demo;
- фінальний звіт.

Prompt:

```text
Implement only Round R7: security closure, regression suite, documentation, clean-checkout verification, and end-to-end MVP demo.

Do not add new product scope. Fix only defects required to satisfy the existing Completion Criteria. Run every test and scan from a clean checkout. Update STATUS, TEST_RESULTS, NEXT, README, and runbooks. Do not push or merge. Stop with the exact commands, test counts, remaining limitations, changed files, migration status, and recommended release/commit information.
```

---

## 10. Обов’язкова перевірка після кожного раунду

У Claude Code виконайте або попросіть виконати:

```text
Review the complete current branch diff against CLAUDE.md, the product specification, the approved round scope, and the security risk register.

Find:
- scope creep;
- missing tests;
- authorization bypasses;
- idempotency failures;
- cross-domain leakage;
- secrets or sensitive data in logs;
- migrations that cannot safely upgrade/downgrade;
- business logic accidentally placed in adapters;
- claims that are not supported by executed tests.

Do not modify files during this review. Return findings by severity with file and line references, then give a PASS or FAIL verdict for the round gate.
```

Також можна запустити `/code-review`, але рішення про приймання раунду має базуватися на повному diff і виконаних тестах, а не лише на описі Claude.

### Ваш мінімальний ручний контроль

У PowerShell:

```powershell
git status
git diff --stat
git diff
```

Перевірте:

- Claude не змінив зайві файли;
- у diff немає токенів, паролів, приватних URL або персональних даних;
- тести реально запускались, а не лише були описані;
- `STATUS.md` відповідає фактичному стану;
- stop gate виконано.

---

## 11. Commit і push після приймання раунду

Claude може запропонувати commit message, але push краще виконувати після вашого приймання.

Приклад для R0:

```powershell
git add .
git commit -m "chore: bootstrap DAN.OS MVP repository"
git push -u origin feat/mvp-vertical-slice
```

Наступні commits:

```text
feat(core): add identity domains raw events and audit
feat(policy): add deterministic proposals and policy engine
feat(tasks): add approvals today view and reminders
feat(telegram): add text intake and approval callbacks
feat(voice): add provider-neutral voice transcription boundary
feat(miniapp): add authenticated today and approvals screens
test(security): close MVP security and replay regressions
```

Після першого commit у feature branch відкрийте **Draft Pull Request**. Не merge до проходження R7.

---

## 12. Як вести наступні сесії Claude Code

Не покладайтеся на пам’ять довгого чату. Наприкінці кожного раунду Claude має оновити:

- `docs/project/STATUS.md` — що реально готово;
- `docs/project/TEST_RESULTS.md` — точні команди й результати;
- `docs/project/NEXT.md` — тільки наступний дозволений раунд;
- `docs/project/DECISIONS.md` — лише нові затверджені рішення;
- `docs/security/RISK_REGISTER.md` — нові ризики та mitigation.

Нову сесію починайте так:

```text
Read CLAUDE.md, docs/project/STATUS.md, docs/project/NEXT.md, docs/project/DECISIONS.md, docs/project/TEST_RESULTS.md, and docs/security/RISK_REGISTER.md.

Inspect git status and the latest commits. State the current verified baseline, unresolved defects, and the one next authorized round. Do not edit files until you have shown the baseline and I authorize implementation.
```

Так код залишається керованим навіть після зміни чату або моделі.

---

## 13. Коли створювати реального Telegram-бота

Не створюйте реальний bot token до завершення R3.

Після приймання R3:

1. Через BotFather створіть окремого тестового бота, наприклад `DAN OS Dev`.
2. Token збережіть тільки в локальному `.env` або secret manager.
3. Перевірте, що `.env` є в `.gitignore`.
4. У development використовуйте polling.
5. Webhook залиште production contract, але не розгортайте production до R7.
6. Не надсилайте токен у Claude Project, звичайний чат, GitHub issue або screenshot.

---

## 14. Фінальний приймальний сценарій MVP

MVP не готовий, доки на чистому checkout не проходить такий сценарій:

1. Запускається Docker Compose.
2. API readiness успішний.
3. Telegram-style text event зберігається як один `RawEvent`.
4. Створюється одна proposal.
5. Повтор того самого update не створює дубль.
6. Після Approve створюється одна task.
7. Повторний Approve не створює другу task.
8. Task з’являється в Today.
9. Reminder створюється і скасовується разом із task.
10. Prompt-injection-shaped text не змінює policy.
11. User A не бачить user B.
12. Personal domain не читає Travelon domain.
13. Audit містить усі переходи стану без body/secrets.
14. Memory candidate можна reject, correct і delete згідно з контрактом.
15. Усі тести й security scans зелені.

---

## 15. Що робити після MVP

Порядок наступних vertical slices:

1. Gmail read/search + drafts із preview.
2. Calendar read + confirmed create/update.
3. Drive selected folders.
4. Travelon Gateway read-only.
5. People/follow-up memory.
6. Morning/Evening Brief.
7. Android private client.

Не додавайте WhatsApp, Instagram, Health Connect, payments, live crypto або write-доступ до CRM раніше, ніж стабілізовані identity, policy, memory, audit та approvals.

---

## 16. Що зробити сьогодні

1. Створити приватний Claude Project.
2. Завантажити дві основні специфікації.
3. Вставити Project Instructions.
4. Запустити Architecture Gate.
5. Зберегти чотири результуючі файли в Project Knowledge.
6. Створити приватний `dan-os` у GitHub.
7. Додати документацію та `CLAUDE.md`.
8. Встановити/відкрити Claude Code.
9. Запустити Plan mode.
10. Дозволити тільки R0.
11. Прийняти R0 лише після diff, тестів і review.
12. Зробити commit і Draft PR.

