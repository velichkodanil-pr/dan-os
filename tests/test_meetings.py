"""Meeting transcripts (R4b): VTT parsing, digest parsing, proposal flow."""
import pytest
from sqlalchemy import func, select

from app.core import meetings
from app.core.ingest import extract_text, parse_subtitles
from app.core.orchestrator import Orchestrator
from app.models import Proposal

OWNER = 111

VTT = """WEBVTT

1
00:00:03.600 --> 00:00:06.240
Danil Velichko: Привіт, почнемо з продажів літа.

2
00:00:06.900 --> 00:00:09.000
Danil Velichko: Далі — чартери на вересень.

3
00:00:09.500 --> 00:00:14.100
Olena: Я підготую звіт по боргах до п'ятниці.

4
00:00:14.500 --> 00:00:16.000
Olena: І надішлю всім.
""".encode()

SRT = """1
00:00:01,000 --> 00:00:04,000
Danil Velichko: Тест SRT формату.

2
00:00:04,500 --> 00:00:08,000
Danil Velichko: Другий рядок без спікера злиється.
""".encode()


def test_parse_vtt_clean_dialogue():
    text = parse_subtitles(VTT)
    assert "-->" not in text and "WEBVTT" not in text
    lines = text.split("\n")
    assert len(lines) == 2  # consecutive same-speaker cues merged
    assert lines[0].startswith("Danil Velichko: Привіт")
    assert "чартери" in lines[0]
    assert lines[1].startswith("Olena: Я підготую звіт")


def test_extract_text_vtt_and_srt():
    assert "продажів літа" in extract_text("meeting.vtt", VTT)
    assert "Тест SRT" in extract_text("meeting.SRT", SRT)


def test_looks_like_transcript():
    assert meetings.looks_like_transcript("GMT2026 Recording.vtt")
    assert meetings.looks_like_transcript("call.SRT")
    assert not meetings.looks_like_transcript("report.txt")


def test_parse_digest_valid_and_garbage():
    raw = ('Ось JSON: {"summary":"Обговорили літо.","decisions":["Запуск акції"],'
           '"actions":[{"title":"Підготувати акцію","who":"me","who_name":null,'
           '"due":null},{"title":"Звіт по боргах","who":"other",'
           '"who_name":"Олена","due":"2026-08-15T18:00:00+03:00"}]}')
    d = meetings.parse_digest(raw)
    assert d["summary"] == "Обговорили літо."
    assert d["decisions"] == ["Запуск акції"]
    assert len(d["actions"]) == 2 and d["actions"][0]["who"] == "me"
    assert meetings.parse_digest("не json") is None
    assert meetings.parse_digest(None) is None
    assert meetings.parse_digest('{"no_summary": 1}') is None


@pytest.mark.asyncio
async def test_transcript_flow_proposals_once(db, monkeypatch):
    async def fake_digest(_text):
        return {"summary": "Планірка по літу.", "decisions": ["Акція з 20.08"],
                "actions": [
                    {"title": "Підготувати акцію по Туреччині", "who": "me",
                     "who_name": None, "due": "2026-08-20T10:00:00+03:00"},
                    {"title": "Звіт по боргах", "who": "other",
                     "who_name": "Олена", "due": None}]}
    monkeypatch.setattr(meetings, "meeting_digest", fake_digest)

    orch = Orchestrator()
    text = parse_subtitles(VTT)
    out = await orch.handle_transcript(db, user_id=OWNER, title="meeting.vtt",
                                       text=text)
    assert out["ingest"].status == "indexed"
    assert out["digest"]["summary"].startswith("Планірка")
    # only MY action becomes a proposal; Олена's stays informational
    assert len(out["proposals"]) == 1
    assert out["proposals"][0].payload["title"] == "Підготувати акцію по Туреччині"
    assert out["proposals"][0].payload["due_at"].startswith("2026-08-20")

    # re-upload of the same transcript: duplicate, NO new proposals
    out2 = await orch.handle_transcript(db, user_id=OWNER, title="meeting.vtt",
                                        text=text)
    assert out2["ingest"].status == "duplicate" and out2["proposals"] == []
    total = (await db.execute(
        select(func.count()).select_from(Proposal))).scalar_one()
    assert total == 1


@pytest.mark.asyncio
async def test_transcript_digest_failure_still_ingests(db, monkeypatch):
    async def no_digest(_text):
        return None
    monkeypatch.setattr(meetings, "meeting_digest", no_digest)
    orch = Orchestrator()
    out = await orch.handle_transcript(db, user_id=OWNER, title="m2.vtt",
                                       text="A: Розмова про справи компанії і плани.")
    assert out["ingest"].status == "indexed"
    assert out["digest"] is None and out["proposals"] == []