"""R6.1C — order context: the insurance-letter case.

A forwarded letter quoted «поліс № 3490138» and «заявкою № 64772». The bot
grabbed the FIRST №-number (the policy), looked it up as an order, missed, and
answered «заявку не знайшов» — burying three real questions about coverage.
These tests pin the three defects that produced that answer, and the parsing
that lets the agent actually answer it.
"""
import json

import pytest

from app.core import chat_tools, travelon
from app.core.orchestrator import _order_lookup_no

OWNER = 111

LETTER = (
    "Шановні представники страхової компанії! Просимо надати роз'яснення щодо "
    "можливості отримання медичної допомоги в межах страхового полісу "
    "№ 3490138 для туриста Муцака Михайла, який подорожує до Туреччини за "
    "туристичною заявкою № 64772 від туроператора «Тревелон». Під час "
    "перебування у дитини виник гострий біль у вусі. Чи буде звернення до "
    "готельного лікаря покриватися умовами страхового полісу?"
)

ORDER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<orders><order>
  <id>64772</id><order>64772</order><status>Confirmed</status>
  <create-date>24.07.2026</create-date><valute>EUR</valute>
  <hotel><hotel-name>CESARS SIDE</hotel-name><country>Туреччина</country>
    <from-date>12.08.2026</from-date><to-date>18.08.2026</to-date>
    <nights>6</nights></hotel>
  <insurance><included>true</included><provider>ЄТС</provider>
    <from-date>12.08.2026</from-date><to-date>18.08.2026</to-date>
    <category>А</category><insurance-sum>30000.0</insurance-sum>
    <territory>Turkey, Egypt, Tunisia</territory>
    <policy-nr>KM 3490138</policy-nr></insurance>
  <customers>
    <customer><name>ROMAN</name><surname>MUTSAK</surname><dob>05.04.1987</dob></customer>
    <customer><name>MYKHAILO</name><surname>MUTSAK</surname><dob>21.11.2013</dob></customer>
  </customers>
  <documents>
    <insurance>https://travelon.to/book/print/insurance/64772?token=SECRET</insurance>
    <voucher>https://travelon.to/book/print/voucher/64772?token=SECRET</voucher>
    <attached-files><attached-file><title>Пам'ятка туристу</title>
      <url>https://travelon.to/book/file/download/abc.pdf?token=SECRET</url>
    </attached-file></attached-files>
  </documents>
  <costs><gross-cost>1200.0</gross-cost></costs>
</order></orders>"""


# ───────── 1. the trigger that misfired ─────────

# 1
def test_01_policy_number_is_not_an_order_number():
    """THE bug: «поліс № 3490138» must never be looked up as an order."""
    assert _order_lookup_no(LETTER) != "3490138"


# 2
def test_02_long_letter_is_never_shortcut():
    """A letter needs reading, not an order card — even with a valid number."""
    assert _order_lookup_no(LETTER) is None


# 3
def test_03_plain_lookups_still_work():
    """The convenience must survive the fix."""
    assert _order_lookup_no("заявка 59266") == "59266"
    assert _order_lookup_no("Що там по заявці №66422?") == "66422"
    assert _order_lookup_no("покажи №66784") == "66784"
    assert _order_lookup_no("order 12345") == "12345"


# 4
def test_04_labelled_beats_bare():
    """Both numbers present, short text: the one labelled «заявка» wins."""
    assert _order_lookup_no("поліс № 3490138, заявка № 64772") == "64772"


# 5
def test_05_two_bare_numbers_do_not_shortcut():
    """Two unlabelled №-numbers: we cannot tell which is the order — ask, don't guess."""
    assert _order_lookup_no("№ 3490138 і № 64772") is None
    # A labelled number still wins even when another number follows it: the
    # first «заявка N» is answered, the rest the owner can ask for separately.
    assert _order_lookup_no("заявки 111222 і 333444") == "111222"


# 6
def test_06_non_orders_ignored():
    assert _order_lookup_no("нагадай завтра о 10 подзвонити") is None
    assert _order_lookup_no("додай 5 задач") is None


# ───────── 2. what the agent now gets ─────────

# 7
def test_07_insurance_is_parsed():
    d = travelon.parse_order_detail(ORDER_XML)
    assert d is not None and d.insurance is not None
    i = d.insurance
    assert i.provider == "ЄТС" and i.policy_nr == "KM 3490138"
    assert i.category == "А" and i.sum_insured == 30000.0
    assert i.territory == "Turkey, Egypt, Tunisia"
    assert i.from_date.strftime("%d.%m.%Y") == "12.08.2026"


# 8
def test_08_tourists_are_parsed():
    d = travelon.parse_order_detail(ORDER_XML)
    assert [t.name for t in d.tourists] == ["MUTSAK ROMAN", "MUTSAK MYKHAILO"]
    assert d.tourists[1].dob.year == 2013


# 9
def test_09_documents_are_listed_and_resolvable():
    d = travelon.parse_order_detail(ORDER_XML)
    kinds = {x.kind for x in d.documents}
    assert {"insurance", "voucher", "attached"} <= kinds
    assert d.doc("insurance") is not None
    assert d.doc("Пам'ятка") is not None          # attached matched by title
    assert d.doc("nope") is None


# 10
def test_10_insurance_card_never_leaks_document_urls():
    """Document URLs carry the full-access token — they must not be rendered."""
    d = travelon.parse_order_detail(ORDER_XML)
    card = travelon.insurance_card(d)
    assert "SECRET" not in card and "travelon.to" not in card
    assert "KM 3490138" in card and "ЄТС" in card
    assert "MUTSAK MYKHAILO" in card


# 11
def test_11_empty_orders_is_not_an_error():
    assert travelon.parse_order_detail('<?xml version="1.0"?><orders/>') is None


# ───────── 3. agent tools ─────────

# 12
def test_12_document_tool_is_offered_only_in_travelon():
    travelon_tools = {t["name"] for t in chat_tools.tools_for_domain("travelon")}
    personal = {t["name"] for t in chat_tools.tools_for_domain("personal")}
    assert "travelon_document" in travelon_tools
    assert "travelon_document" not in personal


# 13
def test_13_document_tool_schema_has_no_domain():
    """The model never chooses the domain — R6.1B invariant, re-checked here."""
    for t in chat_tools.TOOL_DEFS:
        assert "domain" not in (t["input_schema"].get("properties") or {})


# 14
@pytest.mark.asyncio
async def test_14_document_tool_fails_closed_outside_travelon(db, monkeypatch):
    called = []

    async def _boom(*a, **k):
        called.append(1)
        return ("ok", "secret terms")
    monkeypatch.setattr(travelon, "fetch_document_text", _boom)
    for dom in ("personal", "tech"):
        res = json.loads(await chat_tools.run_tool(
            db, OWNER, dom, "travelon_document",
            {"order_no": "64772", "kind": "insurance"}))
        assert res.get("error") == "wrong_domain"
    assert called == []          # zero TravelON traffic outside the domain


# 15
@pytest.mark.asyncio
async def test_15_order_tool_exposes_insurance_not_urls(db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "travelon_token", "T")
    monkeypatch.setattr(travelon, "fetch_order_detail",
                        lambda _id: _detail())

    async def _detail():
        return travelon.parse_order_detail(ORDER_XML)
    raw = await chat_tools.run_tool(db, OWNER, "travelon", "travelon_order",
                                    {"order_no": "64772"})
    assert "SECRET" not in raw                      # no token to the model
    data = json.loads(raw)
    assert data["found"] is True
    assert "KM 3490138" in data["insurance"]
    assert "MUTSAK MYKHAILO" in data["tourists"]
    assert "insurance" in data["documents"]


# 16
@pytest.mark.asyncio
async def test_16_document_tool_returns_text(db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "travelon_token", "T")

    async def _text(order_no, kind):
        assert (order_no, kind) == ("64772", "insurance")
        return "ok", "Умови: франшиза відсутня. Понад 1000 EUR — узгодити."
    monkeypatch.setattr(travelon, "fetch_document_text", _text)
    data = json.loads(await chat_tools.run_tool(
        db, OWNER, "travelon", "travelon_document",
        {"order_no": "64772", "kind": "insurance"}))
    assert data["found"] is True and "франшиза" in data["text"]


# 17
@pytest.mark.asyncio
async def test_17_missing_document_is_honest(db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "travelon_token", "T")

    async def _missing(order_no, kind):
        return "no_document", ""
    monkeypatch.setattr(travelon, "fetch_document_text", _missing)
    data = json.loads(await chat_tools.run_tool(
        db, OWNER, "travelon", "travelon_document",
        {"order_no": "64772", "kind": "insurance"}))
    assert data["found"] is False and data["reason"] == "no_document"


# ───────── 4. model ─────────

# 18
def test_18_chat_model_is_opus_5_with_fifth_gen_thinking():
    """conftest forces CHAT_MODEL=mock, so assert the shipped DEFAULT."""
    from app.config import Settings, settings
    from app.core.chat import thinking_params
    assert Settings.model_fields["chat_model"].default == "claude-opus-5"
    p = thinking_params("claude-opus-5")
    assert p["thinking"]["type"] == "adaptive"      # 5-gen style, not budget
    assert p["output_config"]["effort"] == settings.chat_effort


# ───────── 5. tool payload must stay valid JSON ─────────

# 19
def test_19_oversized_tool_output_is_still_valid_json():
    """Latent bug found while wiring documents: the old code sliced the
    SERIALISED json (`[:8000]`), so any oversized result reached the model as
    an unterminated string. Truncation now happens on a FIELD, and the
    envelope always parses."""
    from app.core.chat_tools import _PAYLOAD_DEFAULT, _payload
    huge = {"found": True, "chars": 50000, "text": "я" * 50000}
    out = _payload(huge, _PAYLOAD_DEFAULT)
    data = json.loads(out)                       # must not raise
    assert data["truncated"] is True
    assert len(out) <= _PAYLOAD_DEFAULT
    assert len(data["text"]) < 50000


# 20
def test_20_small_results_are_untouched():
    from app.core.chat_tools import _PAYLOAD_DEFAULT, _payload
    assert json.loads(_payload({"a": 1, "b": "ok"}, _PAYLOAD_DEFAULT)) == {"a": 1, "b": "ok"}


# 21
def test_21_documents_get_a_bigger_budget_than_ordinary_tools():
    """A policy's terms do not fit an 8k task-list budget."""
    from app.core.chat_tools import _PAYLOAD_DEFAULT, _PAYLOAD_LIMITS
    assert _PAYLOAD_LIMITS["travelon_document"] > _PAYLOAD_DEFAULT
    assert travelon.MAX_DOC_CHARS < _PAYLOAD_LIMITS["travelon_document"]
