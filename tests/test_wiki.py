"""Compiled knowledge layer (R6): slugs/aliases, compile+merge, lookup, archive."""
import json

import pytest

from app.core import wiki
from app.core.policy import evaluate

OWNER = 111


# ---------- slugs & aliases (the Toco lesson: spelling must not matter) ----------

def test_slugify_and_aliases():
    assert wiki.slugify("ТОКО Україна") == "toko-ukraina"
    assert wiki.slugify("Gepard Turizm  Ltd.") == "gepard-turizm-ltd"
    v = wiki.alias_variants("ТОКО")
    assert "toko" in v and "toco" in v
    assert wiki.norm_alias("toco-tour.com.ua") == "tocotourcomua"


@pytest.mark.asyncio
async def test_find_page_by_any_alias(db):
    await wiki.upsert_page(
        db, user_id=OWNER, domain="personal", kind="entity",
        title="Toco UA (ТОКО Україна)",
        summary="Партнер-оператор", content="- Контакт: i.k@travelon.to",
        aliases=["Toco UA", "ТОКО", "toco-tour.com.ua"], tags=["partner"],
        source={"title": "DMC", "ref": "d1", "date": "2026-08-14"})
    await db.commit()
    for name in ("ТОКО", "Toco UA", "toco-tour.com.ua", "токо україна"):
        page = await wiki.find_page(db, OWNER, "personal", name)
        assert page is not None, name
        assert "i.k@travelon.to" in page.content
    assert await wiki.find_page(db, OWNER, "personal", "Анекс") is None


@pytest.mark.asyncio
async def test_search_pages_cyrillic_to_latin(db):
    await wiki.upsert_page(
        db, user_id=OWNER, domain="personal", kind="entity", title="Toco UA",
        summary="оператор",
        content="- сайт toco-tour.com.ua", aliases=["Toco UA", "ТОКО"],
        tags=["partner"])
    await db.commit()
    found = await wiki.search_pages(db, OWNER, "personal", "ТОКО логін")
    assert found and found[0].title == "Toco UA"


# ---------- compile: create then MERGE (accumulation, not re-discovery) ----------

@pytest.mark.asyncio
async def test_compile_creates_then_merges(db, monkeypatch):
    calls = {"n": 0}

    async def fake_haiku(prompt, max_tokens=600):
        calls["n"] += 1
        if "Виділи до" in prompt:  # compile prompt
            if "DMC" in prompt:
                return json.dumps({"pages": [{
                    "kind": "entity", "title": "Toco UA (ТОКО Україна)",
                    "aliases": ["Toco UA", "ТОКО", "toco-tour.com.ua"],
                    "summary": "Партнер-оператор, є кабінет",
                    "facts": ["Сайт: toco-tour.com.ua",
                              "Менеджер: i.kornienko@travelon.to",
                              "Доступ до кабінету — у менеджері паролів"],
                    "tags": ["partner", "travelon"]}]}, ensure_ascii=False)
            return json.dumps({"pages": [{
                "kind": "entity", "title": "ТОКО Україна",
                "aliases": ["Toco UA"], "summary": "оператор",
                "facts": ["Контакт: Ірина", "Умови: депозит 30%"],
                "tags": ["partner"]}]}, ensure_ascii=False)
        # merge prompt
        return json.dumps({
            "summary": "Партнер-оператор: кабінет, контакт, умови",
            "content": ("- Сайт: toco-tour.com.ua\n"
                        "- Менеджер: i.kornienko@travelon.to\n"
                        "- Доступ до кабінету — у менеджері паролів\n"
                        "- Контакт: Ірина\n- Умови: депозит 30%"),
            "contradictions": ""}, ensure_ascii=False)
    monkeypatch.setattr("app.core.extraction.haiku_text", fake_haiku)

    first = await wiki.compile_source(
        db, user_id=OWNER, domain="personal",
        title="Travelon Project · аркуш «DMC»",
        text="Other | Toco UA | https://toco-tour.com.ua | i.kornienko@travelon.to",
        source_ref="doc-1")
    assert first.status == "succeeded"
    assert first.pages == [("toco-ua-toko-ukraina", "created")]

    # a DIFFERENT source about the same partner must MERGE into the same page
    second = await wiki.compile_source(
        db, user_id=OWNER, domain="personal", title="Умови роботи з операторами",
        text="ТОКО Україна: контакт Ірина, депозит 30%", source_ref="doc-2")
    assert second.pages and second.pages[0][1] == "updated"

    page = await wiki.find_page(db, OWNER, "personal", "ТОКО")
    assert "i.kornienko@travelon.to" in page.content  # old facts kept
    assert "депозит 30%" in page.content              # new facts added
    assert len(page.sources) == 2                     # provenance from both


@pytest.mark.asyncio
async def test_compile_merge_failure_keeps_facts(db, monkeypatch):
    """If the merge call fails, facts are appended — never silently lost."""
    state = {"phase": "compile"}

    async def flaky(prompt, max_tokens=600):
        if "Виділи до" in prompt:
            return json.dumps({"pages": [{
                "kind": "entity", "title": "Анекс", "aliases": ["Anex"],
                "summary": "оператор", "facts": ["Менеджер кабінету: anex_manager"],
                "tags": ["partner"]}]}, ensure_ascii=False)
        return None  # merge fails
    monkeypatch.setattr("app.core.extraction.haiku_text", flaky)

    await wiki.compile_source(db, user_id=OWNER, domain="personal", title="Джерело 1",
                              text="Анекс менеджер anex_manager", source_ref="d1")

    async def flaky2(prompt, max_tokens=600):
        if "Виділи до" in prompt:
            return json.dumps({"pages": [{
                "kind": "entity", "title": "Anex", "aliases": ["Анекс"],
                "summary": "оператор", "facts": ["Новий контакт: Марія"],
                "tags": ["partner"]}]}, ensure_ascii=False)
        return None
    monkeypatch.setattr("app.core.extraction.haiku_text", flaky2)
    await wiki.compile_source(db, user_id=OWNER, domain="personal", title="Джерело 2",
                              text="Anex новий контакт Марія", source_ref="d2")

    page = await wiki.find_page(db, OWNER, "personal", "Анекс")
    assert "anex_manager" in page.content and "Марія" in page.content


@pytest.mark.asyncio
async def test_compile_ignores_empty_result(db, monkeypatch):
    async def nothing(prompt, max_tokens=600):
        return json.dumps({"pages": []})
    monkeypatch.setattr("app.core.extraction.haiku_text", nothing)
    outcome = await wiki.compile_source(db, user_id=OWNER, domain="personal",
                                        title="Реєстр",
                                        text="1 | 2 | 3", source_ref="d9")
    assert outcome.pages == [] and outcome.status == "empty_valid"


# ---------- archive (query archiving) ----------

@pytest.mark.asyncio
async def test_save_archive_and_index(db):
    page = await wiki.save_archive(
        db, user_id=OWNER, domain="personal",
        title="Скільки перерахували Гепард",
        summary="50 662,86 EUR за 01.01.2026",
        body="Платіж GEPARD TURIZM: 50 662,86 EUR (2 526 556,83 грн, курс 49,87)",
        used=["Валютування · аркуш Дані1-Приватбанк"])
    assert page.kind == "archive" and "GEPARD" in page.content
    index = await wiki.render_index(db, OWNER, "personal")
    assert "Архів відповідей" in index and "Гепард" in index
    found = await wiki.find_page(db, OWNER, "personal", "Скільки перерахували Гепард")
    assert found is not None


# ---------- lint & policy ----------

@pytest.mark.asyncio
async def test_lint_flags_conflicts_and_dupes(db):
    await wiki.upsert_page(db, user_id=OWNER, domain="personal", kind="entity",
                           title="Партнер X",
                           summary="s", content="- факт", aliases=[], tags=[],
                           contradictions="Джерело А: 30%, Джерело Б: 50%",
                           source={"title": "s1", "ref": "r1", "date": "2026-08-14"})
    await wiki.upsert_page(db, user_id=OWNER, domain="personal", kind="entity",
                           title="Партнер-X",
                           summary="s", content="", aliases=[], tags=[],
                           slug="partner-x-duplicate")
    await db.commit()
    r = await wiki.lint(db, OWNER, "personal")
    assert r["total"] == 2 and r["conflicts"] == ["Партнер X"]
    assert r["thin"] and r["no_source"]
    assert wiki.lint_block(r) and "Вікі знань" in wiki.lint_block(r)


def test_policy_wiki_levels():
    assert evaluate("wiki.read").allowed and evaluate("wiki.read").level == "L0"
    assert evaluate("wiki.write").allowed and evaluate("wiki.write").level == "L1"
    assert evaluate("wiki.archive").level == "L1"
    assert not evaluate("wiki.delete_everything").allowed  # unknown -> denied


# ---------- agent tools ----------

@pytest.mark.asyncio
async def test_wiki_tools_for_agent(db):
    from app.core import chat_tools
    await wiki.upsert_page(
        db, user_id=OWNER, domain="personal", kind="entity", title="Toco UA",
        summary="оператор",
        content="- Менеджер: i.k@travelon.to", aliases=["ТОКО"], tags=["partner"])
    await db.commit()

    idx = json.loads(await chat_tools.run_tool(db, OWNER, "personal", "wiki_index", {}))
    assert "Toco UA" in idx["index"]

    page = json.loads(await chat_tools.run_tool(db, OWNER, "personal", "wiki_page",
                                                {"name": "ТОКО"}))
    assert page["found"] and "i.k@travelon.to" in page["page"]

    miss = json.loads(await chat_tools.run_tool(db, OWNER, "personal", "wiki_page",
                                                {"name": "Невідомий партнер"}))
    assert miss["found"] is False

    # R6.1A: the model can no longer write to long-term memory on its own
    denied = json.loads(await chat_tools.run_tool(db, OWNER, "personal", "wiki_save_answer", {
        "title": "Порівняння операторів", "summary": "коротко",
        "body": "Детальний аналіз з кількох джерел " * 3}))
    assert "не дозволено" in denied["error"]
