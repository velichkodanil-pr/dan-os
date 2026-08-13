"""Cowork knowledge channel: /admin/ingest auth + pipeline reuse."""
import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import Document

TEXT = ("Правила бронювання TravelON: депозит 30% при підтвердженні, "
        "повна оплата за 14 днів до заїзду. Штрафи за ануляцію залежать від "
        "готелю і сезону.")


async def _post(payload: dict, token: str | None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    headers = {"X-Admin-Token": token} if token is not None else {}
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post("/admin/ingest", json=payload, headers=headers)


@pytest.mark.asyncio
async def test_admin_ingest_disabled_by_default(db):
    assert settings.admin_token == ""
    r = await _post({"title": "X", "text": TEXT}, "anything")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_ingest_wrong_token(db, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "S3CRET")
    assert (await _post({"title": "X", "text": TEXT}, "WRONG")).status_code == 403
    assert (await _post({"title": "X", "text": TEXT}, None)).status_code == 403


@pytest.mark.asyncio
async def test_admin_ingest_happy_path_and_dedupe(db, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "S3CRET")
    r = await _post({"title": "Правила бронювання", "text": TEXT,
                     "domain": "travelon", "source_ref": "cowork"}, "S3CRET")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "indexed" and body["chunks"] >= 1

    doc = (await db.execute(select(Document))).scalars().one()
    assert doc.domain == "travelon" and doc.source_type == "cowork_upload"
    assert doc.user_id == settings.owner_telegram_id

    r2 = await _post({"title": "Правила бронювання", "text": TEXT,
                      "domain": "travelon"}, "S3CRET")
    assert r2.json()["status"] == "duplicate"


@pytest.mark.asyncio
async def test_admin_ingest_validation(db, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "S3CRET")
    assert (await _post({"title": "X", "text": "мало"}, "S3CRET")).status_code == 400
    r = await _post({"title": "X", "text": TEXT, "domain": "hack"}, "S3CRET")
    assert r.status_code == 200  # unknown domain falls back to personal
    docs = (await db.execute(select(Document))).scalars().all()
    assert all(d.domain == "personal" for d in docs)
