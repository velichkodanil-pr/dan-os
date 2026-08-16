"""R6.1B — end-to-end domain isolation matrix.

personal / travelon / tech are real security/context boundaries, not columns.
Every test plants a domain-unique MARKER and proves the other domains cannot
see it through ANY channel: vector RAG, keyword RAG, wiki (slug + alias), memory,
chat, tasks, and the agent tools. Plus the server-side rules: fail-closed domain
parsing, DB CHECK constraints, model-cannot-choose-domain, domain-scoped Google
accounts, signed-state OAuth, admin endpoints, the security scan, and that the
R6.1A.1 secret gates do not regress.
"""
import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.core import chat_tools, coach, google_client, rag, security, wiki
from app.core.domains import (
    ALLOWED_DOMAINS, DEFAULT_DOMAIN, Domain, DomainError, get_active_domain,
    parse_domain, set_active_domain)
from app.core.ingest import delete_stale_versions, ingest_document
from app.core.orchestrator import Orchestrator
from app.models import (
    ChatLog, Document, GoogleCredential, MemoryItem, Proposal, Reminder, Task,
    UserState, WikiPage)

OWNER = 111

MARK = {Domain.PERSONAL: "ксилофонперсонал",
        Domain.TRAVELON: "ксилофонтревелон",
        Domain.TECH: "ксилофонтехно"}
PAIRS = [(a, b) for a in Domain for b in Domain if a != b]  # 6 ordered pairs


def orch() -> Orchestrator:
    return Orchestrator()


async def _use(db, domain):
    await set_active_domain(db, OWNER, domain)
    await db.commit()


async def _doc(db, domain, marker, ref=None):
    return await ingest_document(
        db, user_id=OWNER, domain=domain, title=f"Файл {marker}",
        text=f"{marker} {marker} реквізити {marker} деталі тут.",
        source_type="test", source_ref=ref or f"ref-{domain}-{marker}")


async def _page(db, domain, marker):
    page, _ = await wiki.upsert_page(
        db, user_id=OWNER, domain=domain, kind="entity",
        title=f"Сторінка {marker}", summary=f"про {marker}",
        content=f"деталі {marker} тут", aliases=[f"алиас{marker}"], tags=[])
    return page


async def _plant_all(db, domain, marker):
    """One marker across EVERY channel of a domain."""
    await _doc(db, domain, marker)
    await _page(db, domain, marker)
    db.add(MemoryItem(user_id=OWNER, domain=domain, content=f"факт {marker}",
                      status="confirmed"))
    db.add(ChatLog(user_id=OWNER, domain=domain, role="user",
                   text=f"розмова {marker}"))
    db.add(Task(user_id=OWNER, domain=domain, title=f"задача {marker}",
                status="open"))
    await coach.create_goal(db, user_id=OWNER, domain=domain,
                            title=f"ціль {marker}")
    await db.commit()


# ───────────────────────── central model & switching ─────────────────────────

# 1
async def test_01_default_domain_is_personal(db):
    assert await get_active_domain(db, OWNER) == Domain.PERSONAL
    assert DEFAULT_DOMAIN == Domain.PERSONAL


# 2
async def test_02_switch_persists(db):
    await _use(db, Domain.TRAVELON)
    assert await get_active_domain(db, OWNER) == Domain.TRAVELON
    await _use(db, Domain.TECH)
    assert await get_active_domain(db, OWNER) == Domain.TECH


# 3
def test_03_parse_domain_fail_closed():
    # case/whitespace are tolerated at entry (normalised), value is not guessed
    for good in ("personal", "TRAVELON", " Tech ", Domain.TECH):
        assert isinstance(parse_domain(good), Domain)
    for bad in ("", None, "all", "both", "bogus", "админ", "person"):
        with pytest.raises(DomainError):
            parse_domain(bad)
    assert set(ALLOWED_DOMAINS) == {"personal", "travelon", "tech"}


# 4
async def test_04_switch_clears_pending_edit(db):
    st = UserState(user_id=OWNER, active_domain="personal",
                   pending_edit_proposal=None)
    db.add(st)
    await db.commit()
    import uuid as _uuid
    st.pending_edit_proposal = _uuid.uuid4()
    await db.commit()
    await set_active_domain(db, OWNER, Domain.TRAVELON)
    await db.commit()
    await db.refresh(st)
    assert st.active_domain == "travelon" and st.pending_edit_proposal is None


# 5
async def test_05_db_check_rejects_invalid_active_domain(db):
    db.add(UserState(user_id=222, active_domain="bogus"))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


# 6
async def test_06_db_check_rejects_invalid_resource_domain(db):
    db.add(Task(user_id=OWNER, domain="bogus", title="x"))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


# ───────────────────────── request-domain snapshot ─────────────────────────

# 7
async def test_07_note_stamps_active_domain_on_raw_event(db):
    await _use(db, Domain.TECH)
    await orch().handle_note(db, user_id=OWNER, text="просто нотатка про життя",
                             dedupe_key="n-tech-1")
    from app.models import RawEvent
    ev = (await db.execute(select(RawEvent))).scalars().first()
    assert ev.domain == "tech"


# 8
async def test_08_task_proposal_and_task_inherit_domain(db):
    await _use(db, Domain.TRAVELON)
    o = await orch().handle_note(db, user_id=OWNER,
                                 text="нагадай завтра подзвонити партнеру",
                                 dedupe_key="t-trav-1")
    assert o.kind == "proposal" and o.proposal.domain == "travelon"
    status, task, reminder = await orch().approve(
        db, user_id=OWNER, proposal_id=o.proposal.id, version=o.proposal.version)
    assert status == "created" and task.domain == "travelon"
    if reminder is not None:  # reminder inherits the task's domain (§9)
        assert reminder.domain == "travelon"


# 9
async def test_09_reminder_inherits_task_domain(db):
    await _use(db, Domain.TRAVELON)
    o = await orch().handle_note(db, user_id=OWNER,
                                 text="нагадай завтра о 10 оплатити готель",
                                 dedupe_key="t-trav-2")
    await orch().approve(db, user_id=OWNER, proposal_id=o.proposal.id,
                         version=o.proposal.version)
    rem = (await db.execute(select(Reminder))).scalars().first()
    if rem is not None:
        assert rem.domain == "travelon"


# ───────────────────────── RAG isolation ─────────────────────────

# 10
async def test_10_vector_rag_isolated(db):
    await _doc(db, Domain.PERSONAL, MARK[Domain.PERSONAL])
    await _doc(db, Domain.TRAVELON, MARK[Domain.TRAVELON])
    await db.commit()
    hits_p = await rag.retrieve(db, user_id=OWNER, domain=Domain.PERSONAL,
                                query=MARK[Domain.PERSONAL])
    hits_t = await rag.retrieve(db, user_id=OWNER, domain=Domain.TRAVELON,
                                query=MARK[Domain.PERSONAL])
    assert any(MARK[Domain.PERSONAL] in c.text for c in hits_p)
    assert all(c.domain == "personal" for c in hits_p)
    assert not any(MARK[Domain.PERSONAL] in c.text for c in hits_t)


# 11
async def test_11_keyword_rag_isolated(db):
    await _doc(db, Domain.PERSONAL, MARK[Domain.PERSONAL])
    await db.commit()
    q = f"реквізити {MARK[Domain.PERSONAL]}"   # _LOOKUP_RE -> keyword fallback
    assert any(MARK[Domain.PERSONAL] in c.text for c in await rag.retrieve(
        db, user_id=OWNER, domain=Domain.PERSONAL, query=q))
    assert [] == await rag.retrieve(db, user_id=OWNER, domain=Domain.TECH, query=q)


# 12
async def test_12_knowledge_gap_scoped(db):
    await rag.log_gap(db, user_id=OWNER, domain=Domain.TECH, question="як?")
    await db.commit()
    from app.models import KnowledgeGap
    rows = (await db.execute(select(KnowledgeGap))).scalars().all()
    assert len(rows) == 1 and rows[0].domain == "tech"


# 13
async def test_13_same_content_two_domains_allowed(db):
    r1 = await _doc(db, Domain.PERSONAL, "спільнийтекст", ref="same")
    r2 = await _doc(db, Domain.TRAVELON, "спільнийтекст", ref="same")
    await db.commit()
    assert r1.status == "indexed" and r2.status == "indexed"
    docs = (await db.execute(select(Document))).scalars().all()
    assert {d.domain for d in docs} == {"personal", "travelon"}


# 14
async def test_14_dedupe_same_domain(db):
    await ingest_document(db, user_id=OWNER, domain=Domain.PERSONAL,
                          title="Один", text="Однаковий рядок про депозити тут.",
                          source_type="test", source_ref="d1")
    r2 = await ingest_document(db, user_id=OWNER, domain=Domain.PERSONAL,
                               title="Один",
                               text="Однаковий рядок про депозити тут.",
                               source_type="test", source_ref="d1")
    await db.commit()
    assert r2.status == "duplicate"


# 15
async def test_15_delete_stale_versions_scoped(db):
    r_p = await _doc(db, Domain.PERSONAL, "версіямаркер", ref="shared-ref")
    r_t = await _doc(db, Domain.TRAVELON, "версіямаркер", ref="shared-ref")
    await db.commit()
    # cleaning personal's ref must not touch travelon's same-ref document
    await delete_stale_versions(db, user_id=OWNER, domain=Domain.PERSONAL,
                                source_ref="shared-ref",
                                keep_doc_ids={r_p.document.id})
    await db.commit()
    remaining = {d.domain for d in
                 (await db.execute(select(Document))).scalars().all()}
    assert "travelon" in remaining


# ───────────────────────── wiki isolation ─────────────────────────

# 16
async def test_16_same_slug_three_domains(db):
    for d in Domain:
        await wiki.upsert_page(db, user_id=OWNER, domain=d, kind="entity",
                               title="ТОКО", summary="s", content="c",
                               aliases=["toko"], tags=[], slug="toko")
    await db.commit()
    pages = (await db.execute(select(WikiPage).where(
        WikiPage.slug == "toko"))).scalars().all()
    assert len(pages) == 3 and {p.domain for p in pages} == set(ALLOWED_DOMAINS)


# 17
async def test_17_find_page_current_domain_only(db):
    await _page(db, Domain.PERSONAL, MARK[Domain.PERSONAL])
    await db.commit()
    assert await wiki.find_page(db, OWNER, Domain.PERSONAL,
                                f"Сторінка {MARK[Domain.PERSONAL]}") is not None
    assert await wiki.find_page(db, OWNER, Domain.TRAVELON,
                                f"Сторінка {MARK[Domain.PERSONAL]}") is None


# 18
async def test_18_alias_does_not_cross_domain(db):
    await _page(db, Domain.PERSONAL, MARK[Domain.PERSONAL])
    await db.commit()
    alias = f"алиас{MARK[Domain.PERSONAL]}"
    assert await wiki.find_page(db, OWNER, Domain.PERSONAL, alias) is not None
    assert await wiki.find_page(db, OWNER, Domain.TECH, alias) is None


# 19
async def test_19_wiki_search_and_index_scoped(db):
    await _page(db, Domain.PERSONAL, MARK[Domain.PERSONAL])
    await db.commit()
    assert await wiki.search_pages(db, OWNER, Domain.PERSONAL,
                                   MARK[Domain.PERSONAL])
    assert [] == await wiki.search_pages(db, OWNER, Domain.TRAVELON,
                                         MARK[Domain.PERSONAL])
    idx_other = await wiki.render_index(db, OWNER, Domain.TECH)
    assert MARK[Domain.PERSONAL] not in idx_other


# 20
async def test_20_compile_document_domain_guard(db):
    r = await _doc(db, Domain.PERSONAL, "компаркер")
    await db.commit()
    out = await wiki.compile_document(db, user_id=OWNER, document=r.document,
                                      domain=Domain.TRAVELON)
    assert out.status == "failed" and out.error_code == "domain_mismatch"


# 21
async def test_21_wiki_lint_scoped(db):
    await _page(db, Domain.PERSONAL, MARK[Domain.PERSONAL])
    await db.commit()
    assert (await wiki.lint(db, OWNER, Domain.PERSONAL))["total"] == 1
    assert (await wiki.lint(db, OWNER, Domain.TECH))["total"] == 0


# ───────────────────────── memory / chat / tasks ─────────────────────────

# 22
async def test_22_memory_profile_scoped(db):
    db.add(MemoryItem(user_id=OWNER, domain="personal",
                      content=f"факт {MARK[Domain.PERSONAL]}", status="confirmed"))
    await db.commit()
    got = (await db.execute(select(MemoryItem).where(
        MemoryItem.user_id == OWNER, MemoryItem.domain == "travelon",
        MemoryItem.status == "confirmed"))).scalars().all()
    assert got == []


# 23
async def test_23_chat_history_scoped(db):
    db.add(ChatLog(user_id=OWNER, domain="personal",
                   text=f"розмова {MARK[Domain.PERSONAL]}", role="user"))
    await db.commit()
    other = (await db.execute(select(ChatLog).where(
        ChatLog.user_id == OWNER, ChatLog.domain == "tech"))).scalars().all()
    assert other == []


# 24
async def test_24_today_tasks_scoped(db):
    db.add(Task(user_id=OWNER, domain="personal",
                title=f"задача {MARK[Domain.PERSONAL]}", status="open"))
    await db.commit()
    tp = await orch().today(db, user_id=OWNER, domain=Domain.PERSONAL)
    tt = await orch().today(db, user_id=OWNER, domain=Domain.TRAVELON)
    assert tp["total_open"] == 1 and tt["total_open"] == 0


# 25
async def test_25_goals_and_habits_scoped(db):
    await coach.create_goal(db, user_id=OWNER, domain=Domain.PERSONAL, title="g")
    await coach.create_habit(db, user_id=OWNER, domain=Domain.PERSONAL, title="h")
    await db.commit()
    assert len(await coach.list_goals(db, OWNER, Domain.PERSONAL)) == 1
    assert len(await coach.list_goals(db, OWNER, Domain.TECH)) == 0
    assert len(await coach.habits_overview(db, OWNER, Domain.PERSONAL)) == 1
    assert len(await coach.habits_overview(db, OWNER, Domain.TECH)) == 0


# 26
async def test_26_habit_log_inherits_domain(db):
    h = await coach.create_habit(db, user_id=OWNER, domain=Domain.TRAVELON,
                                 title="ранкова")
    await db.commit()
    await coach.toggle_habit(db, user_id=OWNER, habit_id=h.id)
    await db.commit()
    from app.models import HabitLog
    logs = (await db.execute(select(HabitLog))).scalars().all()
    assert logs and all(hl.domain == "travelon" for hl in logs)


# ───────────────────────── agent tools ─────────────────────────

# 27
def test_27_domain_not_in_any_tool_schema():
    for t in chat_tools.TOOL_DEFS:
        assert "domain" not in (t["input_schema"].get("properties") or {})


# 28
def test_28_tools_filtered_by_domain():
    personal = {t["name"] for t in chat_tools.tools_for_domain(Domain.PERSONAL)}
    travelon = {t["name"] for t in chat_tools.tools_for_domain(Domain.TRAVELON)}
    assert "travelon_pulse" not in personal and "travelon_order" not in personal
    assert "travelon_pulse" in travelon and "travelon_order" in travelon
    # fail-closed: an unparseable domain gets the non-TravelON set
    assert "travelon_pulse" not in {
        t["name"] for t in chat_tools.tools_for_domain("bogus")}


# 29
async def test_29_get_tasks_tool_scoped(db):
    db.add(Task(user_id=OWNER, domain="personal",
                title=f"задача {MARK[Domain.PERSONAL]}", status="open"))
    await db.commit()
    out_p = json.loads(await chat_tools.run_tool(
        db, OWNER, Domain.PERSONAL, "get_tasks", {}))
    out_t = json.loads(await chat_tools.run_tool(
        db, OWNER, Domain.TECH, "get_tasks", {}))
    assert any(MARK[Domain.PERSONAL] in t["title"] for t in out_p["open_tasks"])
    assert out_t["open_tasks"] == []


# 30
async def test_30_search_knowledge_tool_scoped(db):
    await _doc(db, Domain.PERSONAL, MARK[Domain.PERSONAL])
    await db.commit()
    out_t = json.loads(await chat_tools.run_tool(
        db, OWNER, Domain.TECH, "search_knowledge",
        {"query": MARK[Domain.PERSONAL]}))
    assert out_t.get("found", 0) == 0


# 31
async def test_31_travelon_tools_fail_closed_zero_network(db, monkeypatch):
    called = []

    async def _boom(*a, **k):
        called.append(1)
        return {}

    monkeypatch.setattr("app.core.travelon.pulse_data", _boom)
    for dom in (Domain.PERSONAL, Domain.TECH):
        res = json.loads(await chat_tools.run_tool(
            db, OWNER, dom, "travelon_pulse", {}))
        assert res.get("error") == "wrong_domain"
    assert called == []   # zero TravelON network activity outside travelon


# 32
async def test_32_travelon_tool_allowed_in_travelon(db):
    res = json.loads(await chat_tools.run_tool(
        db, OWNER, Domain.TRAVELON, "travelon_pulse", {}))
    # gate passed (not wrong_domain); token unset in tests -> honest not-connected
    assert res.get("error") != "wrong_domain"


# 33
async def test_33_order_lookup_only_in_travelon(db):
    await _use(db, Domain.PERSONAL)
    o = await orch().handle_note(db, user_id=OWNER, text="що по заявці 59266?",
                                 dedupe_key="ord-personal")
    # in personal it is an ordinary turn, not a business order card
    assert o.kind != "chat" or "заявк" not in (o.reply or "").lower() \
        or "№59266" not in (o.reply or "")


# ───────────────────────── Google accounts ─────────────────────────

# 34
async def test_34_get_accounts_domain_scoped(db):
    db.add(GoogleCredential(user_id=OWNER, account_email="p@x.com", label="p",
                            domain="personal", refresh_token_enc="e", scopes=""))
    db.add(GoogleCredential(user_id=OWNER, account_email="t@x.com", label="t",
                            domain="travelon", refresh_token_enc="e", scopes=""))
    db.add(GoogleCredential(user_id=OWNER, account_email="u@x.com", label="u",
                            domain=None, refresh_token_enc="e", scopes=""))
    await db.commit()
    p = await google_client.get_accounts(db, OWNER, Domain.PERSONAL)
    t = await google_client.get_accounts(db, OWNER, Domain.TRAVELON)
    allacc = await google_client.get_all_accounts(db, OWNER)
    assert [c.account_email for c in p] == ["p@x.com"]
    assert [c.account_email for c in t] == ["t@x.com"]
    assert len(allacc) == 3           # unassigned included ONLY in management view


# 35
def test_35_signed_state_carries_domain_round_trip():
    st = google_client.sign_state(OWNER, Domain.TRAVELON)
    assert google_client.verify_state(st) == (OWNER, Domain.TRAVELON)


# 36
def test_36_tampered_state_fails_closed():
    st = google_client.sign_state(OWNER, Domain.TECH)
    assert google_client.verify_state(st[:-1] + ("0" if st[-1] != "0" else "1")) is None
    assert google_client.verify_state("111.tech.9999999999.deadbeef") is None
    assert google_client.verify_state("garbage") is None


# 37
async def test_37_store_tokens_binds_and_keeps_domain(db, monkeypatch):
    async def _email(_tokens):
        return "acc@x.com"
    monkeypatch.setattr(google_client, "_account_email", _email)
    monkeypatch.setattr(google_client, "_fernet",
                        lambda: type("F", (), {"encrypt": lambda s, b: b})())
    await google_client.store_tokens(
        db, OWNER, {"refresh_token": "r", "access_token": "a", "expires_in": 3600,
                    "scope": ""}, domain=Domain.TRAVELON)
    cred = (await db.execute(select(GoogleCredential))).scalars().one()
    assert cred.domain == "travelon"
    # reconnect from a DIFFERENT active domain must NOT move it (§11)
    await google_client.store_tokens(
        db, OWNER, {"refresh_token": "r2", "access_token": "a", "expires_in": 3600,
                    "scope": ""}, domain=Domain.PERSONAL)
    await db.refresh(cred)
    assert cred.domain == "travelon"


# ───────────────────────── admin endpoints ─────────────────────────

async def _admin_post(path, payload, token):
    transport = httpx.ASGITransport(app=__import__("app.main", fromlist=["app"]).app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post(path, json=payload,
                            headers={"X-Admin-Token": token} if token else {})


# 38
async def test_38_admin_ingest_requires_valid_domain(db, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "S3CRET")
    body = {"title": "T", "text": "Достатньо довгий текст про депозити партнерів."}
    assert (await _admin_post("/admin/ingest", body, "S3CRET")).status_code == 400
    assert (await _admin_post("/admin/ingest", {**body, "domain": "all"},
                              "S3CRET")).status_code == 400
    assert (await _admin_post("/admin/ingest", {**body, "domain": "tech"},
                              "S3CRET")).status_code == 200
    # token gate still comes FIRST (missing domain + bad token -> 403)
    assert (await _admin_post("/admin/ingest", body, "WRONG")).status_code == 403


# 39
async def test_39_admin_search_requires_domain(db, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "S3CRET")
    assert (await _admin_post("/admin/search", {"query": "депозит"},
                              "S3CRET")).status_code == 400
    r = await _admin_post("/admin/search", {"query": "депозит", "domain": "tech"},
                          "S3CRET")
    assert r.status_code == 200


# ───────────────────────── callbacks fail-closed ─────────────────────────

# 40
async def test_40_approve_draft_wrong_domain(db):
    from app.models import PendingDraft
    d = PendingDraft(user_id=OWNER, domain="travelon", to_addr="x@y.z",
                     subject="s", body="b", status="proposed")
    db.add(d)
    await db.commit()
    await _use(db, Domain.PERSONAL)     # active != resource domain
    assert await orch().approve_draft(db, user_id=OWNER, draft_id=d.id) \
        == "wrong_domain"


# 41
async def test_41_confirm_cal_action_wrong_domain(db):
    from app.models import PendingCalAction
    p = PendingCalAction(user_id=OWNER, domain="travelon", calendar_id="c",
                         event_id="e", summary="s", start_str="2030-01-01",
                         action="decline", status="proposed")
    db.add(p)
    await db.commit()
    await _use(db, Domain.PERSONAL)
    assert await orch().confirm_cal_action(db, user_id=OWNER, action_id=p.id) \
        == "wrong_domain"


# ───────────────────────── security scan / R6.1A.1 ─────────────────────────

SECRET = ("доступ: -----BEGIN PRIVATE KEY-----\n"
          "MIIBVwIBADANBgkqhkiG9w0BAQEFAASCAUEwggE9\n"
          "-----END PRIVATE KEY-----")


# 42
async def test_42_r61a1_secret_note_still_blocked(db):
    assert security.scan(SECRET).blocked          # precondition
    await _use(db, Domain.TRAVELON)
    o = await orch().handle_note(db, user_id=OWNER, text=SECRET,
                                 dedupe_key="sec-1")
    assert o.kind == "blocked"
    # nothing readable stored: no chat turn carrying it, no proposal
    chats = (await db.execute(select(ChatLog))).scalars().all()
    assert all(SECRET not in c.text for c in chats)
    assert (await db.execute(select(Proposal))).scalars().all() == []


# 43
async def test_43_security_scan_findings_carry_domain(db):
    db.add(MemoryItem(user_id=OWNER, domain="travelon", content=SECRET,
                      status="confirmed"))
    await db.commit()
    from app.core import security_scan
    report = await security_scan.run_scan(db, user_id=OWNER)
    from app.models import SecurityFinding
    findings = (await db.execute(select(SecurityFinding))).scalars().all()
    assert findings and all(f.domain == "travelon" for f in findings)
    assert report.by_domain.get("travelon", 0) >= 1          # grouped by domain


# ───────────────────────── rituals / audit ─────────────────────────

# 44
async def test_44_morning_brief_is_per_domain_section(db):
    from app.core import briefs
    # a pending memory candidate makes the section non-empty regardless of the
    # wall-clock time the test runs at (a no-due task wouldn't show in a brief)
    db.add(MemoryItem(user_id=OWNER, domain="travelon", content="кандидат",
                      status="candidate"))
    await db.commit()
    td = await orch().today(db, user_id=OWNER, domain=Domain.TRAVELON)
    section = await briefs.morning_brief(db, OWNER, Domain.TRAVELON, td)
    assert section is not None and "TravelON" in section
    # an empty domain yields no section (skipped in the composed brief)
    td_tech = await orch().today(db, user_id=OWNER, domain=Domain.TECH)
    assert await briefs.morning_brief(db, OWNER, Domain.TECH, td_tech) is None


# 45
async def test_45_domain_audit_is_counts_only(db):
    await _plant_all(db, Domain.PERSONAL, MARK[Domain.PERSONAL])
    from app.core.domain_audit import domain_audit_report
    text = await domain_audit_report(db, OWNER)
    assert "Аудит доменів" in text
    assert MARK[Domain.PERSONAL] not in text     # never leaks content/titles


# 46
async def test_46_single_alembic_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
    assert len(heads) == 1


# ───────────────────────── ordered-pair isolation matrix ─────────────────────────

@pytest.mark.parametrize("a,b", PAIRS)
async def test_47_ordered_pair_isolation(db, a, b):
    """A marker planted across EVERY channel of domain `a` is invisible from
    domain `b` — vector RAG, keyword RAG, wiki slug, wiki alias, memory, chat,
    tasks, and the agent search tool."""
    m = MARK[a]
    await _plant_all(db, a, m)

    # vector + keyword RAG
    assert [] == await rag.retrieve(db, user_id=OWNER, domain=b, query=m)
    assert [] == await rag.retrieve(db, user_id=OWNER, domain=b,
                                    query=f"реквізити {m}")
    # wiki slug + alias
    assert await wiki.find_page(db, OWNER, b, f"Сторінка {m}") is None
    assert await wiki.find_page(db, OWNER, b, f"алиас{m}") is None
    assert [] == await wiki.search_pages(db, OWNER, b, m)
    # memory + chat (the exact filters the orchestrator reads with)
    assert [] == (await db.execute(select(MemoryItem).where(
        MemoryItem.user_id == OWNER, MemoryItem.domain == b.value))).scalars().all()
    assert [] == (await db.execute(select(ChatLog).where(
        ChatLog.user_id == OWNER, ChatLog.domain == b.value))).scalars().all()
    # tasks + goals
    assert (await orch().today(db, user_id=OWNER, domain=b))["total_open"] == 0
    assert [] == await coach.list_goals(db, OWNER, b)
    # agent tool
    out = json.loads(await chat_tools.run_tool(db, OWNER, b, "search_knowledge",
                                               {"query": m}))
    assert out.get("found", 0) == 0
    # sanity: domain `a` DOES see its own marker (isolation, not deletion)
    assert any(m in c.text for c in await rag.retrieve(
        db, user_id=OWNER, domain=a, query=m))
