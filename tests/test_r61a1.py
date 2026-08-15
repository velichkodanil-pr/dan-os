"""R6.1A.1 — secret boundary hardening (independent-audit hotfix).

Scope of this file: the gaps the audit found in R6.1A — a scanner that missed
Cyrillic passwords / PINs / CSV columns / late rows, an envelope (title,
source_ref, meta) nobody scanned, provider ARGUMENTS that were never checked,
and model OUTPUT that was trusted on the way back.

Two conventions matter here:

* Every fixture is a clearly synthetic, non-functional value, and every
  provider-shaped literal is assembled at run time (`_shape`) so no credential
  pattern ever appears verbatim in the source.
* Passwords do NOT block by default — that is Danylo's standing decision
  (R6.1A.1, DECISIONS.md), and this suite honours it. The audit's
  password-blocking cases are therefore asserted as DETECTION under
  `strict_passwords`, which is the exact behaviour `QUARANTINE_PASSWORDS=true`
  ships. Hard technical secrets block in both modes.
"""
import json

import httpx
import pytest
from sqlalchemy import func, select

from app.config import settings
from app.core import (
    chat_tools, coach, rag, secret_policy, security, security_scan,
)
from app.core.ingest import ingest_document
from app.core.orchestrator import Orchestrator
from app.core.secret_policy import SecretCategory as Cat
from app.core.secret_policy import scan_text
from app.models import (
    ChatLog, Document, Goal, Habit, MemoryItem, PendingDraft, Proposal,
    RawEvent, SecurityFinding,
)

OWNER = 111


def _shape(*parts: str) -> str:
    """Assemble a synthetic credential at run time — see tests/test_security.py."""
    return "".join(parts)


FAKE_API_KEY = _shape("sk-", "ant-", "EXAMPLEONLY0000notarealkey1111AAAA")
FAKE_SECRET_LINE = f"Сервіс api.example, ключ доступу {FAKE_API_KEY}"
UA_PASSWORD = "пароль: Секретний"
RU_PASSWORD = "пароль: Секретный"


# ---------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _default_policy():
    """Passwords allowed by default; restored after every test."""
    secret_policy.set_blocking_categories(secret_policy.HARD_SECRET_CATEGORIES)
    settings.quarantine_passwords = False
    yield
    secret_policy.set_blocking_categories(secret_policy.HARD_SECRET_CATEGORIES)
    settings.quarantine_passwords = False


@pytest.fixture
def strict_passwords():
    """`QUARANTINE_PASSWORDS=true` — the audit's assumed configuration."""
    secret_policy.set_blocking_categories(secret_policy.ALL_CATEGORIES)
    settings.quarantine_passwords = True
    yield


class _DeadEmbedder:
    async def embed(self, texts):
        raise AssertionError("embedder must not be called on a blocked path")


@pytest.fixture
def spies(monkeypatch):
    """Fail the test if a blocked path reaches ANY provider."""
    calls: list = []

    async def _boom(name):
        raise AssertionError(f"{name} must not be called on a blocked path")

    async def _no_anthropic(*a, **kw):
        await _boom("Anthropic")

    async def _no_gmail(*a, **kw):
        await _boom("Gmail")

    async def _no_calendar(*a, **kw):
        await _boom("Calendar")

    async def _no_tts(*a, **kw):
        await _boom("TTS")

    class _NoNetwork:
        def __init__(self, *a, **kw):
            raise AssertionError("no network call is allowed on a blocked path")

    monkeypatch.setattr("app.core.ingest.get_embedder", lambda: _DeadEmbedder())
    monkeypatch.setattr("app.core.rag.get_embedder", lambda: _DeadEmbedder())
    monkeypatch.setattr("app.core.extraction.haiku_text", _no_anthropic)
    monkeypatch.setattr("app.core.google_client.gmail_find_message", _no_gmail)
    monkeypatch.setattr("app.core.google_client.gmail_recent", _no_gmail)
    monkeypatch.setattr("app.core.google_client.calendar_range", _no_calendar)
    monkeypatch.setattr("app.core.tts.synthesize", _no_tts)
    monkeypatch.setattr(httpx, "AsyncClient", _NoNetwork)
    return calls


class _Tripwire:
    async def extract(self, text, context=None):
        raise AssertionError("extractor must not see this text")


async def _async_none():
    return None


# ============================================== 1-11. scanner v2 detection

def test_01_pure_ukrainian_password_blocked(strict_passwords):
    result = scan_text(UA_PASSWORD)
    assert result.blocked and Cat.PASSWORD in result.categories


def test_02_pure_russian_password_blocked(strict_passwords):
    result = scan_text(RU_PASSWORD)
    assert result.blocked and Cat.PASSWORD in result.categories


@pytest.mark.parametrize("text", ["password: 0000", "PIN: 1234",
                                  "пін-код: 4321", "password: 1234"])
def test_03_pin_and_short_numeric_blocked(strict_passwords, text):
    assert scan_text(text).blocked, text


@pytest.mark.parametrize("text", ["password: 111111", "password: aaaaaa",
                                  "пароль: 000000"])
def test_04_repeated_low_entropy_password_blocked(strict_passwords, text):
    """v1 treated any value with ≤2 distinct characters as masking."""
    assert scan_text(text).blocked, text


@pytest.mark.parametrize("text", ["password: $ExampleOnly123!",
                                  "password: %ExampleOnly123!",
                                  "password: {ExampleOnly123!}",
                                  "password: [ExampleOnly123!]"])
def test_05_leading_symbol_values_blocked(strict_passwords, text):
    """v1 rejected anything STARTING with < [ { $ % as a placeholder."""
    assert scan_text(text).blocked, text


@pytest.mark.parametrize("text", [
    "PASSWORD=<PASSWORD>", "API_KEY=${TOKEN}", "API_KEY=%TOKEN%",
    "password: [REDACTED]", "api_key: YOUR_API_KEY", "password: ***",
    "password: null", "password:", "token: {{token}}", "PASSWORD=$TOKEN",
])
def test_06_exact_placeholders_accepted(strict_passwords, text):
    assert not scan_text(text).blocked, text


def test_07_comma_csv_password_column_blocked(strict_passwords):
    csv = "Партнер,Логін,Пароль\nToco UA,ikorn,Qw3rty-Zx9\n"
    assert scan_text(csv).blocked


@pytest.mark.parametrize("sep", [";", "\t", "|"])
def test_08_semicolon_tab_pipe_tables_blocked(strict_passwords, sep):
    table = (f"Партнер{sep}Логін{sep}Пароль\n"
             f"Toco UA{sep}ikorn{sep}Qw3rty-Zx9\n")
    assert scan_text(table).blocked, sep


def test_09_sensitive_value_after_row_60_blocked(strict_passwords):
    """v1 stopped looking 60 rows after the header."""
    rows = "\n".join(f"Оператор {i},login{i}," for i in range(200))
    table = f"Партнер,Логін,Пароль\n{rows}\nToco UA,ikorn,Qw3rty-Zx9"
    assert scan_text(table).blocked


def test_10_plain_numeric_recovery_codes_blocked():
    """Hard category — blocks in the DEFAULT configuration too."""
    result = scan_text("Recovery codes: 12345678, 23456789, 34567890")
    assert result.blocked and Cat.RECOVERY_CODE in result.categories
    # …but a bare list of numbers without the marker is an invoice register
    assert not scan_text("Рахунки: 12345678, 23456789, 34567890").blocked


@pytest.mark.parametrize("text", [
    "Password policy: minimum 12 characters, rotate quarterly",
    "Пароль — не менше 12 символів, змінюємо щокварталу",
    "яка політика паролів у компанії?",
    "пароль: змінено",
    "Мені потрібні логін, пароль, сайт\nДякую, Данило",
    "Логін: i.k@example.invalid, сайт toco-tour.example",
    "IBAN UA213223130000026007233566001, ЄДРПОУ 46140224",
    "Рахунок №INV-2026-0041 на 2 346 EUR, заявка 59266",
])
def test_11_policy_and_business_text_accepted(strict_passwords, text):
    assert not scan_text(text).blocked, text


# ================================================ 12-15. resource envelope

@pytest.mark.asyncio
async def test_12_document_title_secret_is_sanitised(db, spies):
    result = await ingest_document(
        db, user_id=OWNER, title=f"Ключі {FAKE_API_KEY}.txt",
        text="Звичайний бізнес-текст про умови роботи з операторами.",
        source_type="drive", source_ref="t1")
    assert result.status == "quarantined"
    doc = (await db.execute(select(Document))).scalar_one()
    assert "EXAMPLEONLY" not in doc.title
    assert doc.title == security.SAFE_TITLE


@pytest.mark.asyncio
async def test_13_source_ref_secret_is_sanitised(db, spies):
    result = await ingest_document(
        db, user_id=OWNER, title="Звіт за серпень",
        text="Звичайний бізнес-текст про умови роботи з операторами.",
        source_type="drive", source_ref=f"https://x.example/?api_key={FAKE_API_KEY}")
    assert result.status == "quarantined"
    doc = (await db.execute(select(Document))).scalar_one()
    assert "EXAMPLEONLY" not in (doc.source_ref or "")


@pytest.mark.asyncio
async def test_14_nested_meta_secret_is_sanitised(db, spies):
    meta = {"modifiedTime": "2026-08-15T00:00:00Z", "v": 2,
            "sheets": [{"name": "DMC", "note": f"ключ {FAKE_API_KEY}"}]}
    result = await ingest_document(
        db, user_id=OWNER, title="Таблиця", text="Звичайний бізнес-текст.",
        source_type="drive", source_ref="m1", meta=meta)
    assert result.status == "quarantined"
    doc = (await db.execute(select(Document))).scalar_one()
    blob = json.dumps(doc.meta, ensure_ascii=False)
    assert "EXAMPLEONLY" not in blob
    assert doc.meta["modifiedTime"] == "2026-08-15T00:00:00Z"   # clean keys kept
    assert doc.meta["security"]["categories"] == ["api_key"]


@pytest.mark.asyncio
async def test_15_no_raw_secret_fingerprint_is_persisted(db, spies):
    """A plain SHA-256 of a short secret is reversible — so it is not stored."""
    import hashlib
    body = f"Доступи\n{FAKE_SECRET_LINE}"
    await ingest_document(db, user_id=OWNER, title="Доступи", text=body,
                          source_type="drive", source_ref="f1")
    doc = (await db.execute(select(Document))).scalar_one()
    raw_sha = hashlib.sha256(body.strip().lower().encode()).hexdigest()
    assert doc.content_hash != raw_sha
    assert doc.content_hash.startswith("q$")
    # …and it is still a STABLE id: re-ingesting dedupes instead of duplicating
    again = await ingest_document(db, user_id=OWNER, title="Доступи", text=body,
                                  source_type="drive", source_ref="f1")
    assert again.status == "quarantined"
    assert (await db.execute(
        select(func.count()).select_from(Document))).scalar_one() == 1


# ============================================== 16-19. provider boundaries

@pytest.mark.asyncio
async def test_16_blocked_rag_query_makes_zero_embedding_calls(db, spies):
    assert await rag.retrieve(db, user_id=OWNER,
                              query=f"знайди {FAKE_API_KEY}") == []


@pytest.mark.asyncio
async def test_17_blocked_tool_arguments_make_zero_connector_calls(db, spies):
    raw = await chat_tools.run_tool(db, OWNER, "search_mail",
                                    {"query": f"лист із {FAKE_API_KEY}"})
    payload = json.loads(raw)
    assert payload["refused"] is True and payload["reason"] == "secret_in_arguments"
    assert "EXAMPLEONLY" not in raw
    finding = (await db.execute(select(SecurityFinding))).scalar_one()
    assert finding.resource_type == "tool_args"


@pytest.mark.asyncio
async def test_18_blocked_goal_and_habit_write_nothing(db, spies):
    with pytest.raises(security.SecretBlocked):
        await coach.create_goal(db, user_id=OWNER,
                                title=f"оновити {FAKE_API_KEY}")
    with pytest.raises(security.SecretBlocked):
        await coach.create_habit(db, user_id=OWNER,
                                 title=f"перевіряти {FAKE_API_KEY}")
    assert (await db.execute(
        select(func.count()).select_from(Goal))).scalar_one() == 0
    assert (await db.execute(
        select(func.count()).select_from(Habit))).scalar_one() == 0
    findings = (await db.execute(select(SecurityFinding))).scalars().all()
    assert {f.resource_type for f in findings} == {"goal", "habit"}


@pytest.mark.asyncio
async def test_19_blocked_admin_search_makes_zero_provider_calls(spies, monkeypatch):
    from app.main import AdminSearchRequest, admin_search
    monkeypatch.setattr(settings, "admin_token", "test-admin-token")
    monkeypatch.setattr(settings, "owner_telegram_id", OWNER)

    class _Req:
        headers = {"X-Admin-Token": "test-admin-token"}

    out = await admin_search(
        AdminSearchRequest(query=f"знайди {FAKE_API_KEY}"), _Req())
    assert out == {"hits": [], "refused": "secret_in_query"}


# ================================================= 20-22. model egress gate

@pytest.mark.asyncio
async def test_20_blocked_model_reply_is_never_shown_or_stored(db, monkeypatch):
    """No Telegram echo, no TTS, no ChatLog bot row."""
    from app.core.extraction import ExtractResult

    class _Chat:
        async def extract(self, text, context=None):
            return ExtractResult(intent="chat", reply="ок")

    async def _leaky_reply(*a, **kw):
        return f"Ось ключ: {FAKE_API_KEY}"

    async def _no_tts(*a, **kw):
        raise AssertionError("TTS must not speak a blocked reply")
    monkeypatch.setattr("app.core.chat.chat_reply", _leaky_reply)
    monkeypatch.setattr("app.core.tts.synthesize", _no_tts)

    outcome = await Orchestrator(extractor=_Chat()).handle_note(
        db, user_id=OWNER, text="що там по ключах?", dedupe_key="tg:9:1")
    assert outcome.kind == "blocked"
    assert outcome.reply == security.SAFE_OUTPUT
    assert "EXAMPLEONLY" not in outcome.reply
    bot_turns = (await db.execute(select(ChatLog).where(
        ChatLog.role == "bot"))).scalars().all()
    assert bot_turns == []
    user_turns = (await db.execute(select(ChatLog).where(
        ChatLog.role == "user"))).scalars().all()
    assert all(t.provider_eligible is False for t in user_turns)


@pytest.mark.asyncio
async def test_21_blocked_extractor_output_writes_no_memory_or_proposal(db, monkeypatch):
    from app.core.extraction import ExtractResult

    class _Leaky:
        async def extract(self, text, context=None):
            return ExtractResult(intent="note",
                                 memory_text=f"ключ доступу {FAKE_API_KEY}")
    monkeypatch.setattr("app.core.chat.chat_reply", lambda *a, **kw: _async_none())

    outcome = await Orchestrator(extractor=_Leaky()).handle_note(
        db, user_id=OWNER, text="запамʼятай це", dedupe_key="tg:9:2")
    assert outcome.kind == "blocked"
    assert (await db.execute(
        select(func.count()).select_from(MemoryItem))).scalar_one() == 0
    assert (await db.execute(
        select(func.count()).select_from(Proposal))).scalar_one() == 0
    finding = (await db.execute(select(SecurityFinding).where(
        SecurityFinding.resource_type == "model_output"))).scalar_one()
    assert finding.categories == ["api_key"]


@pytest.mark.asyncio
async def test_22_blocked_draft_output_creates_no_pending_draft(db, monkeypatch):
    from app.core import google_client
    from app.models import GoogleCredential
    db.add(GoogleCredential(user_id=OWNER, account_email="me@example.invalid",
                            label="me", refresh_token_enc="enc"))
    await db.commit()

    async def _access(_db, _cred):
        return "token"

    async def _find(_access, _query):
        return {"from": "a@example.invalid", "subject": "Питання",
                "body": "Звичайний лист про умови.", "thread_id": "t",
                "message_id": "m", "references": ""}

    async def _leaky_compose(*a, **kw):
        return f"Вітаю! Ось ключ: {FAKE_API_KEY}"
    monkeypatch.setattr(google_client, "access_for", _access)
    monkeypatch.setattr(google_client, "gmail_find_message", _find)
    monkeypatch.setattr("app.core.extraction.haiku_text", _leaky_compose)

    status, draft = await Orchestrator(extractor=_Tripwire()).propose_draft(
        db, user_id=OWNER, query="питання")
    assert status == "blocked_secret" and draft is None
    assert (await db.execute(
        select(func.count()).select_from(PendingDraft))).scalar_one() == 0


# ==================================================== 23-26. security scan v2

@pytest.mark.asyncio
async def test_23_nested_raw_event_secret_found_by_v2(db):
    db.add(RawEvent(event_type="meeting.transcript", dedupe_key="nested-1",
                    user_id=OWNER,
                    payload={"title": "Зустріч",
                             "blocks": [{"speaker": "Ірина",
                                         "text": f"ключ {FAKE_API_KEY}"}]}))
    await db.commit()
    report = await security_scan.run_scan(db, user_id=OWNER)
    assert report.raw_events_flagged == 1
    finding = (await db.execute(select(SecurityFinding).where(
        SecurityFinding.resource_type == "raw_event"))).scalar_one()
    assert finding.scanner_version == 2
    # immutable: the payload is untouched
    event = (await db.execute(select(RawEvent))).scalar_one()
    assert FAKE_API_KEY in json.dumps(event.payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_24_v1_completion_does_not_satisfy_the_v2_gate(db):
    from app.models import AppState
    db.add(AppState(key=security.SCAN_COMPLETE_KEY, value="1"))   # v1 marker
    await db.commit()
    assert security.SCANNER_VERSION == 2
    assert not await security.scan_complete(db)
    await security_scan.run_scan(db, user_id=OWNER)
    assert await security.scan_complete(db)


@pytest.mark.asyncio
async def test_25_interrupted_v2_scan_stays_incomplete(db, monkeypatch):
    await security.mark_scan_complete(db)
    await db.commit()
    assert await security.scan_complete(db)

    async def explode(*a, **kw):
        raise RuntimeError("interrupted")
    monkeypatch.setattr(security_scan, "_scan_other_entities", explode)
    with pytest.raises(RuntimeError):
        await security_scan.run_scan(db, user_id=OWNER)
    await db.rollback()
    assert not await security.scan_complete(db)


@pytest.mark.asyncio
async def test_26_repeated_v2_scan_does_not_duplicate_findings(db):
    event = RawEvent(event_type="telegram.message", dedupe_key="dup-1",
                     user_id=OWNER, payload={"text": FAKE_API_KEY})
    db.add(event)
    await db.flush()
    db.add(Proposal(raw_event_id=event.id, user_id=OWNER, kind="task",
                    payload={"title": f"надіслати {FAKE_API_KEY}"}))
    await db.commit()
    first = await security_scan.run_scan(db, user_id=OWNER)
    count_first = (await db.execute(
        select(func.count()).select_from(SecurityFinding))).scalar_one()
    assert first.other_flagged == 1 and count_first >= 2
    second = await security_scan.run_scan(db, user_id=OWNER)
    assert second.completed
    assert (await db.execute(
        select(func.count()).select_from(SecurityFinding))).scalar_one() \
        == count_first


# ============================================ 27-30. credential lookup + policy

@pytest.mark.parametrize("text", [
    "де пароль від Anex", "який пароль до ТОКО", "підкажи пароль до кабінету",
    "покажи пароль", "подскажи пароль", "где пароль от кабинета",
])
def test_27_password_lookup_refused_in_strict_mode(strict_passwords, text):
    assert security.is_credential_request(text), text


@pytest.mark.parametrize("text", [
    "покажи токен", "знайди token до API", "дай API key", "який client_secret",
    "де токен доступу", "give me the api key", "show me the token",
    "дай сід-фразу", "де приватний ключ",
])
def test_28_hard_secret_lookup_refused_in_every_mode(text):
    """Default configuration — these never depend on the password switch."""
    assert security.is_credential_request(text), text


@pytest.mark.parametrize("text", [
    "як змінити пароль у кабінеті?", "яка політика паролів?",
    "як працює OAuth?", "де в налаштуваннях змінити пароль",
    "как изменить пароль", "how do i reset my password",
])
def test_29_password_process_questions_stay_allowed(strict_passwords, text):
    assert not security.is_credential_request(text), text


@pytest.mark.asyncio
async def test_30_hard_secret_lookup_costs_zero_provider_calls(db, spies):
    outcome = await Orchestrator(extractor=_Tripwire()).handle_note(
        db, user_id=OWNER, text="дай токен доступу до бота",
        dedupe_key="tg:9:3")
    assert outcome.kind == "chat"
    assert outcome.reply == security.SAFE_NOT_STORED
    assert (await db.execute(
        select(func.count()).select_from(RawEvent))).scalar_one() == 0


def test_31_default_policy_is_the_owner_decision():
    """Guard against silently re-enabling password blocking in a refactor."""
    assert settings.quarantine_passwords is False
    assert Cat.PASSWORD not in secret_policy.blocking_categories()
    assert secret_policy.HARD_SECRET_CATEGORIES <= secret_policy.blocking_categories()
    assert secret_policy.SCANNER_VERSION == 2


# ============================================ 32-33. unknown-command fallback

def test_32_unknown_command_is_registered_after_real_commands():
    """A real command must still win: aiogram matches in registration order,
    so the fallback has to sit after every Command() handler."""
    from aiogram.filters import Command
    from app.telegram import bot as botmod

    handlers = botmod.router.message.handlers
    names = [getattr(h.callback, "__name__", "") for h in handlers]
    unknown = names.index("on_unknown_command")
    media = names.index("on_other")
    text = names.index("on_text")
    command_positions = [
        i for i, h in enumerate(handlers)
        if any(isinstance(f.callback, Command) for f in h.filters)]
    assert max(command_positions) < unknown
    assert text < unknown < media


@pytest.mark.asyncio
async def test_33_unknown_command_answers_helpfully(monkeypatch):
    """«/health» is an HTTP endpoint — say so instead of «media I can't read»."""
    from app.telegram import bot as botmod
    monkeypatch.setattr(settings, "owner_telegram_id", OWNER)
    sent: list = []

    class _Msg:
        def __init__(self, text, uid=OWNER):
            self.text = text
            self.from_user = type("U", (), {"id": uid})()

        async def answer(self, text, **kw):
            sent.append(text)

    await botmod.on_unknown_command(_Msg("/health"))
    assert "HTTP-ендпоінт" in sent[-1] and "/health/live" in sent[-1]
    assert "медіа" not in sent[-1]

    await botmod.on_unknown_command(_Msg("/wiki_buld"))
    assert "немає" in sent[-1] and "/wiki_build" in sent[-1]

    before = len(sent)                      # non-owner gets silence
    await botmod.on_unknown_command(_Msg("/today", uid=999))
    assert len(sent) == before
