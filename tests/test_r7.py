"""R7 — English coach: a habit with memory, not a prompt.

What these tests pin, in the order it matters:
  1. the plan is real and internally consistent (no duplicate phrases, no
     empty week) — a curriculum with a hole is worse than no curriculum;
  2. spaced repetition actually spaces (and a forgotten phrase comes back
     tomorrow, not in three weeks);
  3. a session never comes out empty on day one and never runs past the
     minute budget on a normal day;
  4. the streak counts calendar days, not button presses;
  5. corrections from a live conversation become tomorrow's review cards —
     this is the loop that makes the thing a system;
  6. the whole capability is PERSONAL-domain only, the mirror image of the
     TravelON tools being travelon-only.
"""
from datetime import date, timedelta

import pytest

from app.core import english
from app.models import EnglishItem, EnglishProfile

OWNER = 111


# ---------- 1. the plan ----------

def test_curriculum_is_complete_and_unique():
    assert english.WEEKS == 12
    seen: set[str] = set()
    for wk in english.CURRICULUM:
        assert wk.title and wk.grammar and wk.why and wk.task
        assert wk.scenario in {"negotiation", "email", "it", "travel"}
        assert len(wk.phrases) >= 6, f"week {wk.no} is thin"
        for en, uk, hook in wk.phrases:
            assert en and uk and hook, f"week {wk.no}: empty phrase field"
            assert en not in seen, f"duplicate phrase: {en}"
            seen.add(en)
    assert len(seen) >= 90


def test_week_for_is_clamped_not_crashing():
    """Week 13 must not explode after the plan is finished."""
    assert english.week_for(0).no == 1
    assert english.week_for(1).no == 1
    assert english.week_for(99).no == english.WEEKS


# ---------- 2. spaced repetition ----------

def test_good_answers_stretch_the_interval():
    ease, interval, reps = 2.5, 0, 0
    seq = []
    for _ in range(5):
        ease, interval, reps, _ = english.schedule(ease, interval, reps, "good")
        seq.append(interval)
    assert seq[0] == 1 and seq[1] == 3
    assert seq == sorted(seq), f"intervals must grow: {seq}"
    assert seq[-1] <= english.INTERVAL_MAX


def test_forgetting_resets_to_tomorrow_and_costs_ease():
    ease, interval, reps, lapsed = english.schedule(2.5, 30, 6, "again")
    assert interval == 1 and reps == 0 and lapsed is True
    assert ease < 2.5


def test_ease_stays_inside_its_bounds():
    ease = 2.5
    for _ in range(30):
        ease, *_ = english.schedule(ease, 10, 3, "again")
    assert ease == pytest.approx(english.EASE_MIN)
    for _ in range(30):
        ease, *_ = english.schedule(ease, 10, 3, "good")
    assert ease == pytest.approx(english.EASE_MAX)


async def test_apply_grade_sets_a_real_due_date(db):
    item = await english.add_item(db, OWNER, term="Let's find a solution.",
                                  meaning="Знайдімо рішення.")
    on = date(2026, 8, 21)
    english.apply_grade(item, "good", on)
    assert item.due_on > on
    english.apply_grade(item, "again", on)
    assert item.due_on == on + timedelta(days=1) and item.lapses == 1


# ---------- 3. the session ----------

async def test_first_session_is_never_empty(db):
    """Day one has nothing to review. The session must fill itself with new
    material instead of congratulating him on an empty queue."""
    prof = await english.get_profile(db, OWNER)
    drill = await english.start_session(db, prof)
    assert len(drill["q"]) >= 6
    assert all(c["t"] == "new" for c in drill["q"])


async def test_session_respects_the_minute_budget(db):
    prof = await english.get_profile(db, OWNER)
    for i in range(40):                      # a large backlog of due phrases
        item = await english.add_item(db, OWNER, term=f"phrase {i}",
                                      meaning=f"фраза {i}")
        item.due_on = english.today_local() - timedelta(days=1)
    await db.flush()
    drill = await english.start_session(db, prof)
    assert len(drill["q"]) <= english.budget_cards(prof.minutes_per_day) \
        + english.MAX_NEW


async def test_a_new_phrase_becomes_a_memory_item_only_when_shown(db):
    """Building a queue must not create cards. An abandoned session leaves
    nothing behind — otherwise the backlog fills with phrases he never saw."""
    prof = await english.get_profile(db, OWNER)
    await english.start_session(db, prof)
    assert await english.stats(db, OWNER, english.today_local()) == {
        "total": 0, "due": 0, "solid": 0, "mistakes": 0}
    await english.advance(db, prof, "good")
    assert (await english.stats(db, OWNER, english.today_local()))["total"] == 1


async def test_review_card_asks_before_it_reveals(db):
    prof = await english.get_profile(db, OWNER)
    item = await english.add_item(db, OWNER, term="That works for us.",
                                  meaning="Це нам підходить.", note="Коротка згода.")
    item.due_on = english.today_local()
    await db.flush()
    await english.start_session(db, prof)
    card = await english.current_card(db, prof)
    assert card["kind"] == "ask"
    assert "That works for us." not in card["text"]       # no free answer
    assert "Це нам підходить." in card["text"]
    await english.reveal(db, prof)
    card = await english.current_card(db, prof)
    assert card["kind"] == "answer" and "That works for us." in card["text"]


async def test_session_walks_to_the_end_and_closes(db):
    prof = await english.get_profile(db, OWNER)
    drill = await english.start_session(db, prof)
    n = len(drill["q"])
    for _ in range(n - 1):
        assert await english.advance(db, prof, "good") is True
    assert await english.advance(db, prof, "good") is False
    res = await english.finish_session(db, prof)
    assert res["cards"] == n and prof.drill == {}
    assert prof.sessions_done == 1 and prof.streak == 1
    assert english.session_summary(res)


# ---------- 4. progress is honest ----------

async def test_streak_counts_days_not_button_presses(db):
    prof = await english.get_profile(db, OWNER)
    await english.start_session(db, prof)
    await english.finish_session(db, prof)
    day, streak = prof.day_in_week, prof.streak
    await english.start_session(db, prof)          # second session, same day
    await english.finish_session(db, prof)
    assert prof.streak == streak, "two sessions in a day are not two days"
    assert prof.day_in_week == day, "the plan must not run ahead of him"
    assert prof.sessions_done == 1


async def test_a_missed_day_breaks_the_streak(db):
    prof = await english.get_profile(db, OWNER)
    prof.streak, prof.best_streak = 9, 9
    prof.last_session_on = english.today_local() - timedelta(days=3)
    await english.start_session(db, prof)
    await english.finish_session(db, prof)
    assert prof.streak == 1
    assert prof.best_streak == 9, "the record survives a broken streak"


async def test_the_week_turns_over_after_seven_days(db):
    prof = await english.get_profile(db, OWNER)
    prof.day_in_week = 7
    prof.last_session_on = english.today_local() - timedelta(days=1)
    await english.start_session(db, prof)
    await english.finish_session(db, prof)
    assert prof.week == 2 and prof.day_in_week == 1


# ---------- 5. the loop: mistakes become cards ----------

def test_fix_block_is_parsed_and_never_shown_raw():
    raw = ('Sure, we can do that. What volume are you thinking of?\n'
           '###FIX\n[{"wrong": "since 5 years", "right": "for 5 years", '
           '"uk": "протягом 5 років", "why": "тривалість — for"}]')
    reply, fixes = english.split_fixes(raw)
    assert "###FIX" not in reply and "{" not in reply
    assert fixes == [{"wrong": "since 5 years", "right": "for 5 years",
                      "uk": "протягом 5 років", "why": "тривалість — for"}]
    assert "for 5 years" in english.fixes_card(fixes)


def test_a_broken_fix_block_degrades_to_plain_reply():
    """A truncated JSON block must cost him the corrections, never the reply."""
    reply, fixes = english.split_fixes("Good point.\n###FIX\n[{\"wrong\": ")
    assert reply == "Good point." and fixes == []


def test_reply_without_a_fix_block_is_untouched():
    reply, fixes = english.split_fixes("  And what about the transfer?  ")
    assert reply == "And what about the transfer?" and fixes == []


async def test_a_correction_becomes_tomorrows_card(db):
    item = await english.add_item(db, OWNER, term="for 5 years",
                                  meaning="протягом 5 років",
                                  note="тривалість — for", source="mistake")
    assert item is not None
    assert item.due_on == english.today_local() + timedelta(days=1)
    st = await english.stats(db, OWNER, english.today_local())
    assert st["mistakes"] == 1


async def test_the_same_mistake_twice_is_one_card(db):
    await english.add_item(db, OWNER, term="for 5 years", meaning="a",
                           source="mistake")
    again = await english.add_item(db, OWNER, term="for 5 years", meaning="b",
                                   source="mistake")
    assert again is None
    rows = (await db.execute(
        __import__("sqlalchemy").select(EnglishItem)
        .where(EnglishItem.user_id == OWNER))).scalars().all()
    assert len(rows) == 1


async def test_breaking_a_planned_phrase_pulls_it_forward(db):
    """He learned it from the plan, then got it wrong in real speech: the card
    must jump back to tomorrow instead of waiting three weeks."""
    item = await english.add_item(db, OWNER, term="I'll keep it short.",
                                  meaning="Буду стисло.", source="plan")
    item.due_on = english.today_local() + timedelta(days=30)
    item.interval_days, item.reps = 30, 6
    await db.flush()
    assert await english.add_item(db, OWNER, term="I'll keep it short.",
                                  meaning="Буду стисло.", source="mistake") is None
    assert item.due_on == english.today_local() + timedelta(days=1)
    assert item.reps == 0


# ---------- 6. the boundary ----------

def test_the_coach_lives_in_the_personal_domain():
    assert english.DOMAIN == "personal"


async def test_every_row_it_writes_is_personal(db):
    prof = await english.get_profile(db, OWNER)
    await english.start_session(db, prof)
    await english.advance(db, prof, "good")
    res = await english.finish_session(db, prof)
    assert res["cards"] >= 1
    assert prof.domain == "personal"
    rows = (await db.execute(
        __import__("sqlalchemy").select(EnglishItem)
        .where(EnglishItem.user_id == OWNER))).scalars().all()
    assert rows and all(r.domain == "personal" for r in rows)


async def test_talk_text_is_still_scanned_for_secrets(db):
    """The coach is its own egress point to the model. Defence in depth: even
    reached directly, a pasted token must not leave the machine."""
    prof = await english.get_profile(db, OWNER)
    await english.start_talk(db, prof)
    reply, spoken = await english.talk_reply(
        db, prof, "my key is sk-ant-api03-" + "A" * 95)
    from app.core import security
    assert reply == spoken == security.SAFE_REFUSAL
    assert prof.talk_turns == 0, "a blocked message is not a practice turn"


async def test_an_abandoned_conversation_closes_itself(db):
    """Otherwise a forgotten session swallows a business question hours later."""
    from datetime import datetime, timezone
    prof = await english.get_profile(db, OWNER)
    await english.start_talk(db, prof)
    await db.commit()
    prof.talk_started_at = datetime.now(timezone.utc) - timedelta(
        minutes=english.TALK_IDLE_MINUTES + 1)
    await db.flush()
    assert english.talk_expired(prof) is True
    assert await english.talk_active(db, OWNER) is None
    assert prof.talk_started_at is None


async def test_a_live_conversation_is_found(db):
    prof = await english.get_profile(db, OWNER)
    await english.start_talk(db, prof)
    await db.commit()
    assert await english.talk_active(db, OWNER) is not None


# ---------- cards render ----------

async def test_cards_render_without_leaking_html(db):
    prof = await english.get_profile(db, OWNER)
    await english.add_item(db, OWNER, term="<b>hack</b>", meaning="<i>x</i>")
    st = await english.stats(db, OWNER, english.today_local())
    assert "Тиждень" in english.hub_card(prof, st)
    assert "12." in english.plan_card(prof)
    prog = await english.progress_card(db, prof)
    assert "Прогрес" in prog
    assert "<b>hack</b>" not in prog, "user text must be escaped"


# ---------- 7. the button flow, end to end ----------
# The Telegram layer is where a state machine actually breaks: a stale button,
# a domain switched between two taps, a queue that never ends. These drive the
# real handlers with the smallest possible fakes.

class _Msg:
    def __init__(self):
        self.sent: list[str] = []
        self.edited: list[str] = []
        self.kbs: list = []

    async def answer(self, text, reply_markup=None, **kw):
        self.sent.append(text)
        self.kbs.append(reply_markup)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edited.append(text)
        self.kbs.append(reply_markup)


class _User:
    def __init__(self, uid=OWNER):
        self.id = uid
        self.is_bot = False


class _Cb:
    def __init__(self, data, uid=OWNER):
        self.data = data
        self.from_user = _User(uid)
        self.message = _Msg()
        self.answers: list = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)


def _buttons(kb) -> set[str]:
    return {b.callback_data for row in (kb.inline_keyboard if kb else [])
            for b in row}



async def _press(data, uid=OWNER):
    from app.telegram import bot as botmod
    cb = _Cb(data, uid)
    await botmod._en_callback(cb, uid, data.split(":"))
    return cb


async def _set_domain(db, uid, value):
    from app.core.domains import Domain, set_active_domain
    await set_active_domain(db, uid, Domain(value))
    await db.commit()


async def test_hub_offers_the_four_things_he_can_do(db):
    await _set_domain(db, OWNER, "personal")
    cb = await _press("en:hub")
    assert "Англійська" in cb.message.sent[0]
    assert _buttons(cb.message.kbs[0]) == {"en:go", "en:talk", "en:prog", "en:plan"}


async def test_full_drill_from_button_to_summary(db):
    """▶️ → cards → ✅ … → summary. The loop must terminate."""
    await _set_domain(db, OWNER, "personal")
    cb = await _press("en:go")
    assert cb.message.sent, "the first card must appear"
    assert _buttons(cb.message.kbs[0]) == {"en:g:good:0"}   # a new phrase
    pos = 0
    for _ in range(40):
        cb = await _press(f"en:g:good:{pos}")
        pos += 1
        if "Сесію завершено" in (cb.message.sent[-1] if cb.message.sent else ""):
            break
    else:
        pytest.fail("the drill never ended")
    assert "Завдання" in cb.message.sent[-1]
    db.expunge_all()
    prof = await db.get(EnglishProfile, OWNER)
    assert prof.sessions_done == 1 and prof.drill == {}


async def test_review_flow_shows_then_grades(db):
    await _set_domain(db, OWNER, "personal")
    item = await english.add_item(db, OWNER, term="Where do we stand?",
                                  meaning="На чому ми зупинились?")
    item.due_on = english.today_local()
    await db.commit()
    item_id = item.id
    cb = await _press("en:go")
    assert _buttons(cb.message.kbs[0]) == {"en:show:0"}
    assert "Where do we stand?" not in cb.message.sent[0]
    cb = await _press("en:show:0")
    assert cb.message.edited and "Where do we stand?" in cb.message.edited[0]
    assert _buttons(cb.message.kbs[-1]) == {"en:g:again:0", "en:g:hard:0",
                                            "en:g:good:0"}
    cb = await _press("en:g:hard:0")
    db.expunge_all()         # the handler wrote in its own session
    fresh = await db.get(EnglishItem, item_id)
    assert fresh.due_on > english.today_local()


async def test_buttons_refuse_outside_the_personal_domain(db):
    """A button pressed after /domain travelon must not reach personal data."""
    await _set_domain(db, OWNER, "travelon")
    cb = await _press("en:prog")
    assert cb.answers == ["Спершу 🏠 Особисте"]
    assert "Особисте" in cb.message.sent[0]
    assert _buttons(cb.message.kbs[0]) == {"dom:personal"}
    assert await db.get(EnglishProfile, OWNER) is None, "no profile was created"


async def test_command_refuses_outside_the_personal_domain(db):
    from app.telegram import bot as botmod
    await _set_domain(db, OWNER, "tech")
    msg = _Msg()
    msg.from_user = _User()
    await botmod.cmd_english(msg)
    assert "Особисте" in msg.sent[0]


# ---------- 8. conversation routing ----------

async def test_a_live_conversation_takes_plain_messages(db, monkeypatch):
    """While practising, a plain message must reach the coach — and must NOT
    land in ChatLog, or English practice becomes context for a business answer."""
    from app.core import english as eng
    from app.core.orchestrator import Orchestrator
    from app.models import ChatLog
    import sqlalchemy as sa

    await _set_domain(db, OWNER, "personal")
    prof = await eng.get_profile(db, OWNER)
    await eng.start_talk(db, prof)
    await db.commit()

    async def fake_model(system, messages, max_tokens=700):
        assert "English only" in system
        return ('Sounds good. What dates are you looking at?\n###FIX\n'
                '[{"wrong": "I want book", "right": "I want to book", '
                '"uk": "я хочу забронювати", "why": "після want — to"}]')
    monkeypatch.setattr(eng, "_call_model", fake_model)

    out = await Orchestrator().handle_note(
        db, user_id=OWNER, text="I want book 30 rooms", dedupe_key="en-1")
    assert out.kind == "chat"
    assert "What dates" in out.reply
    assert "Робота над помилками" in out.reply and "I want to book" in out.reply

    logged = (await db.execute(sa.select(sa.func.count())
                               .select_from(ChatLog))).scalar()
    assert logged == 0, "practice must stay out of the assistant's history"
    saved = (await db.execute(sa.select(EnglishItem)
                              .where(EnglishItem.source == "mistake"))).scalars().all()
    assert [i.term for i in saved] == ["I want to book"]


async def test_corrections_arrive_in_batches_not_every_sentence(db, monkeypatch):
    """Constant repair kills fluency: the coach is told to correct only every
    Nth turn, and the rule in the prompt must actually flip."""
    from app.core import english as eng
    await _set_domain(db, OWNER, "personal")
    prof = await eng.get_profile(db, OWNER)
    await eng.start_talk(db, prof)
    seen: list[bool] = []

    async def fake_model(system, messages, max_tokens=700):
        seen.append(eng.FIX_MARK in system and "Do NOT output" not in system)
        return "Right. And the transfer?"
    monkeypatch.setattr(eng, "_call_model", fake_model)

    for _ in range(eng.TALK_CORRECT_EVERY):
        await eng.talk_reply(db, prof, "we go there")
    assert seen == [False] * (eng.TALK_CORRECT_EVERY - 1) + [True]


async def test_a_dead_coach_closes_practice_instead_of_swallowing_him(db):
    """CHAT_MODEL is mock in tests, so the coach cannot answer. His message
    must fall through to ordinary chat with the practice closed — not vanish."""
    from app.core import english as eng
    from app.core.orchestrator import Orchestrator
    await _set_domain(db, OWNER, "personal")
    prof = await eng.get_profile(db, OWNER)
    await eng.start_talk(db, prof)
    await db.commit()
    out = await Orchestrator().handle_note(
        db, user_id=OWNER, text="hello there", dedupe_key="en-2")
    assert out.kind != "duplicate"
    db.expunge_all()
    fresh = await db.get(EnglishProfile, OWNER)
    assert fresh.talk_started_at is None, "a dead coach must not stay open"


async def test_business_domain_never_enters_practice(db, monkeypatch):
    """A live conversation plus /domain travelon: the business question wins."""
    from app.core import english as eng
    from app.core.orchestrator import Orchestrator
    await _set_domain(db, OWNER, "personal")
    prof = await eng.get_profile(db, OWNER)
    await eng.start_talk(db, prof)
    await db.commit()
    await _set_domain(db, OWNER, "travelon")

    called = False

    async def fake_model(system, messages, max_tokens=700):
        nonlocal called
        called = True
        return "no"
    monkeypatch.setattr(eng, "_call_model", fake_model)

    await Orchestrator().handle_note(
        db, user_id=OWNER, text="скільки туристів у Туреччину",
        dedupe_key="en-3")
    assert called is False, "the coach must not see a travelon message"


async def test_a_stale_card_button_is_inert(db):
    """A card that scrolled up the chat keeps its buttons. Tapping it must not
    grade whatever is current — that would score a phrase he never saw."""
    await _set_domain(db, OWNER, "personal")
    await _press("en:go")
    await _press("en:g:good:0")            # now at position 1
    stale = await _press("en:g:good:0")    # the same old button again
    assert stale.answers == ["Ця картка вже пройдена"]
    assert stale.message.sent == []
    db.expunge_all()
    prof = await db.get(EnglishProfile, OWNER)
    assert prof.drill["pos"] == 1, "a stale tap must not advance the queue"


async def test_a_double_tap_grades_once(db):
    await _set_domain(db, OWNER, "personal")
    item = await english.add_item(db, OWNER, term="I think we're close.",
                                  meaning="Думаю, ми близько.")
    item.due_on = english.today_local()
    item_id = item.id
    await db.commit()
    await _press("en:go")
    await _press("en:show:0")
    await _press("en:g:again:0")
    await _press("en:g:again:0")           # the impatient second tap
    db.expunge_all()
    fresh = await db.get(EnglishItem, item_id)
    assert fresh.lapses == 1, "one answer, one lapse"


# ---------- 9. voice ----------

def test_markup_never_reaches_the_voice():
    from app.core import tts
    spoken = tts.speakable("<b>Sure.</b> What&#x27;s your <i>volume</i>?")
    assert spoken == "Sure. What's your volume?"
    assert "<" not in spoken and "&" not in spoken


async def test_the_voice_says_the_english_not_the_corrections(db, monkeypatch):
    """He speaks, it answers in his ear. Reading a Ukrainian correction list
    aloud mid-practice would undo the practice — so the card shows both and
    the voice says only the English."""
    from app.core import english as eng
    from app.core.orchestrator import Orchestrator
    await _set_domain(db, OWNER, "personal")
    prof = await eng.get_profile(db, OWNER)
    await eng.start_talk(db, prof)
    await db.commit()

    async def fake_model(system, messages, max_tokens=700):
        return ('That works for us. When can you confirm?\n###FIX\n'
                '[{"wrong": "we can done", "right": "we can do it", '
                '"uk": "ми можемо це зробити", "why": "після can — інфінітив"}]')
    monkeypatch.setattr(eng, "_call_model", fake_model)

    out = await Orchestrator().handle_note(
        db, user_id=OWNER, text="we can done that", dedupe_key="en-voice")
    assert "Робота над помилками" in out.reply
    assert out.speech == "That works for us. When can you confirm?"
    assert "помилками" not in out.speech and "<" not in out.speech
