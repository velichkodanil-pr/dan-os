"""Agentic chat engine (R5): tool loop, executors under policy, per-sheet xlsx."""
import io
import json

import pytest
from openpyxl import Workbook

from app.config import settings
from app.core import chat, chat_tools
from app.core.ingest import ingest_xlsx_by_sheets

OWNER = 111


# ---------- thinking style per model ----------

def test_thinking_params_by_model():
    p = chat.thinking_params("claude-sonnet-5")
    assert p["thinking"]["type"] == "adaptive" and "output_config" in p
    p2 = chat.thinking_params("claude-opus-4-5")
    assert p2["thinking"] == {"type": "enabled", "budget_tokens": 1500}


# ---------- tool executors ----------

@pytest.mark.asyncio
async def test_tool_search_knowledge(db):
    from app.core.ingest import ingest_document
    await ingest_document(db, user_id=OWNER, title="Доступи",
                          text="Other | Toco UA | toco-tour.example | log1 | pass1"
                               + "\n\n" + "\n\n".join(f"рядок {i}" for i in range(30)),
                          source_type="drive", source_ref="x1")
    raw = await chat_tools.run_tool(db, OWNER, "search_knowledge",
                                    {"query": "логін Toco"})
    data = json.loads(raw)
    assert data["found"] >= 1
    assert any("log1" in c["text"] for c in data["chunks"])


@pytest.mark.asyncio
async def test_tool_recent_mail_no_google(db):
    raw = await chat_tools.run_tool(db, OWNER, "get_recent_mail", {})
    assert "connect_google" in json.loads(raw)["error"]


@pytest.mark.asyncio
async def test_tool_unknown_denied(db):
    raw = await chat_tools.run_tool(db, OWNER, "delete_everything", {})
    assert "не дозволено" in json.loads(raw)["error"]


@pytest.mark.asyncio
async def test_tool_travelon_unconfigured(db):
    raw = await chat_tools.run_tool(db, OWNER, "travelon_pulse", {})
    assert "error" in json.loads(raw)


# ---------- the agentic loop ----------

@pytest.mark.asyncio
async def test_chat_loop_calls_tool_then_answers(db, monkeypatch):
    monkeypatch.setattr(settings, "chat_model", "claude-sonnet-5")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    calls = {"n": 0}
    responses = [
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "get_tasks", "input": {}}]},
        {"stop_reason": "end_turn", "content": [
            {"type": "text", "text": "У тебе 0 відкритих задач ✅"}]},
    ]

    async def fake_api(payload):
        i = calls["n"]
        calls["n"] += 1
        if i == 1:  # tool_result went back in
            last = payload["messages"][-1]
            assert last["role"] == "user"
            assert last["content"][0]["type"] == "tool_result"
            assert last["content"][0]["tool_use_id"] == "tu_1"
        return responses[i]
    monkeypatch.setattr(chat, "_call_api", fake_api)

    reply = await chat.chat_reply("що по задачах?", db=db, user_id=OWNER,
                                  profile=[], history=[])
    assert reply == "У тебе 0 відкритих задач ✅"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_chat_loop_gives_up_after_rounds(db, monkeypatch):
    monkeypatch.setattr(settings, "chat_model", "claude-sonnet-5")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")

    async def always_tools(_payload):
        return {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "x", "name": "get_tasks", "input": {}}]}
    monkeypatch.setattr(chat, "_call_api", always_tools)
    reply = await chat.chat_reply("зациклись", db=db, user_id=OWNER,
                                  profile=[], history=[])
    assert reply is None  # falls back gracefully


# ---------- per-sheet xlsx ingestion ----------

def _workbook(sheets: dict) -> bytes:
    wb = Workbook()
    first = True
    for title, rows in sheets.items():
        ws = wb.active if first else wb.create_sheet()
        ws.title = title
        first = False
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_xlsx_each_sheet_is_own_document(db):
    data = _workbook({
        "Продукт": [[f"Ідея {i}", f"опис {i}"] for i in range(200)],
        "DMC": [["Паролі від інших операторів"],
                ["Other", "Toco UA", "https://toco-tour.example",
                 "i.k@travelon.to", "Secret1"]],
    })
    results = await ingest_xlsx_by_sheets(
        db, user_id=OWNER, filename="Travelon Project.xlsx", data=data,
        source_type="drive", source_ref="TP1")
    titles = [r.document.title for r in results if r.document]
    assert any("аркуш «Продукт»" in t for t in titles)
    assert any("аркуш «DMC»" in t for t in titles)
    # the credentials row from the LAST tab is searchable
    from app.core import rag
    chunks = await rag.retrieve(db, user_id=OWNER, query="логін Toco пароль")
    assert any("i.k@travelon.to" in c.text for c in chunks)
