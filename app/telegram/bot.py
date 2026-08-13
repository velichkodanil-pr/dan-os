"""Telegram adapter: thin handlers that call the orchestrator. No business logic.

Round 1: text/voice note -> preview card (✅/✏️/❌) -> task -> Today -> reminder.
Non-owners get silence everywhere.
"""
import logging
import uuid

import httpx
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo,
)

from app import db as database
from app.config import settings
from app.core import briefs, google_client
from app.core.orchestrator import Orchestrator
from app.core.policy import PolicyDenied
from app.core.transcription import TranscriptionError, get_transcriber
from app.telegram.cards import proposal_card, task_created_card, today_card

logger = logging.getLogger(__name__)
router = Router()
orch = Orchestrator()

bot_instance = None  # set by app.main after Bot creation (used by scheduler rituals)


def _memory_kb(item_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Запам'ятати", callback_data=f"mo:{item_id}"),
        InlineKeyboardButton(text="❌ Відкинути", callback_data=f"mx:{item_id}"),
    ]])


async def send_brief(user_id: int) -> None:
    """Morning brief (used by /brief and the 07:30 ritual)."""
    async with database.session() as db:
        today_data = await orch.today(db, user_id=user_id)
        text = await briefs.morning_brief(db, user_id, today_data)
    await bot_instance.send_message(user_id, text)


async def send_checkin(user_id: int) -> None:
    """Evening check-in: summary + habits + memory-candidate review (21:30)."""
    from app.core import coach
    async with database.session() as db:
        summary = await briefs.evening_summary(db, user_id)
        candidates = await briefs.pending_candidates(db, user_id)
        habits = await coach.habits_overview(db, user_id)
    await bot_instance.send_message(user_id, summary)
    undone = [h for h in habits if not h["done_today"]]
    if undone:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"☑️ {h['title'][:30]}",
                                  callback_data=f"hb:{h['id']}")]
            for h in undone[:8]])
        await bot_instance.send_message(
            user_id, "🏃 <b>Звички сьогодні</b> — що з цього зроблено? "
            "Тисни, щоб позначити:", reply_markup=kb)
    if candidates:
        await bot_instance.send_message(
            user_id, f"🧠 Розберемо пам'ять — кандидатів: {len(candidates)}")
        for item in candidates:
            await bot_instance.send_message(
                user_id, f"🧠 {item.content}", reply_markup=_memory_kb(item.id))


async def send_digest(user_id: int) -> None:
    """Mail digest ritual (skips silently when the inbox is quiet)."""
    from app.core.digest import build_digest
    async with database.session() as db:
        text = await build_digest(db, user_id)
    if text:
        await bot_instance.send_message(user_id, text)


async def send_weekly(user_id: int) -> None:
    """Sunday coverage report."""
    from app.core.reports import weekly_coverage_report
    async with database.session() as db:
        text = await weekly_coverage_report(db, user_id)
    await bot_instance.send_message(user_id, text)


def _conflict_kb(new_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Нове замінює старе", callback_data=f"cf:{new_id}:n")],
        [InlineKeyboardButton(text="📌 Лишити старе", callback_data=f"cf:{new_id}:o"),
         InlineKeyboardButton(text="🤝 Обидва правильні", callback_data=f"cf:{new_id}:b")],
    ])


def _draft_kb(draft_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💾 Створити чернетку в Gmail", callback_data=f"dm:{draft_id}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"dx:{draft_id}"),
    ]])


def _is_owner(entity) -> bool:
    return bool(settings.owner_telegram_id and entity.from_user
                and entity.from_user.id == settings.owner_telegram_id)


def _proposal_kb(proposal_id, version: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"ap:{proposal_id}:{version}"),
        InlineKeyboardButton(text="✏️ Змінити", callback_data=f"ed:{proposal_id}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"rj:{proposal_id}"),
    ]])


def _task_kb(task_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="☑️ Виконано", callback_data=f"dn:{task_id}"),
        InlineKeyboardButton(text="🚫 Скасувати", callback_data=f"cn:{task_id}"),
    ]])


# ---------- commands ----------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not settings.owner_telegram_id:
        user_id = message.from_user.id if message.from_user else 0
        await message.answer(
            f"Привіт! Я <b>DAN.OS</b>.\nТвій Telegram ID: <code>{user_id}</code>\n"
            "Додай його у змінну <code>OWNER_TELEGRAM_ID</code> на Railway."
        )
        return
    if not _is_owner(message):
        return
    await message.answer(
        "Привіт, Данило! <b>DAN.OS</b> · раунд 4 🟢\n\n"
        "• текст/голосове «нагадай…» → задача з нагадуванням\n"
        "• «запам'ятай: …» → факт у пам'ять\n"
        "• 📄 документ (pdf/docx/txt/md) чи пересилка → база знань, потім просто питай\n"
        "• /app — міні-застосунок: сьогодні, підтвердження, пам'ять 📱\n"
        "• /goal і /habit — цілі та звички (тренер) · /goals · /habits\n"
        "• /travelon — пульс заявок TravelON 🧳\n"
        "• /drive — індексувати папку Google Drive · /reply — чернетка відповіді на лист\n"
        "• /accounts — Google-акаунти (можна кілька: особистий + робочі)\n"
        "• /today · /brief · /checkin · /kb\n"
        f"• бриф {settings.brief_time} · чек-ін {settings.checkin_time} · "
        f"дайджест {settings.digest_times} · тижневий звіт нд {settings.weekly_time}"
    )


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    if not _is_owner(message):
        return
    async with database.session() as db:
        data = await orch.today(db, user_id=message.from_user.id)
    await message.answer(today_card(data))


@router.message(Command("brief"))
async def cmd_brief(message: Message) -> None:
    if not _is_owner(message):
        return
    await send_brief(message.from_user.id)


@router.message(Command("checkin"))
async def cmd_checkin(message: Message) -> None:
    if not _is_owner(message):
        return
    await send_checkin(message.from_user.id)


@router.message(Command("connect_google"))
async def cmd_connect_google(message: Message) -> None:
    if not _is_owner(message):
        return
    if not (settings.google_client_id and settings.google_client_secret):
        await message.answer(
            "Google ще не сконфігуровано на сервері — чекаю Client ID/Secret "
            "(файл danos-google-client.txt)")
        return
    url = google_client.auth_url(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔐 Підключити Google", url=url)]])
    await message.answer(
        "Тисни кнопку й обери акаунт (можна додати кілька — кожен прохід "
        "додає ще один). На екрані дозволів постав УСІ галочки: календар, "
        "Gmail (читання і чернетки), Drive — все лише читання, крім чернеток. "
        "Попередження «Google hasn't verified» — нормально для власного "
        "застосунку: Продовжити.", reply_markup=kb)


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    if not _is_owner(message):
        return
    await message.answer("pong ✅")


# ---------- Mini App (R4) ----------

@router.message(Command("app"))
async def cmd_app(message: Message) -> None:
    if not _is_owner(message):
        return
    if not settings.public_url:
        await message.answer("Публічний домен ще не готовий — спробуй пізніше.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📱 Відкрити DAN.OS",
                             web_app=WebAppInfo(url=f"{settings.public_url}/app"))]])
    await message.answer(
        "Міні-застосунок: задачі на сьогодні, підтвердження і пам'ять — "
        "усе кнопками з телефону:", reply_markup=kb)


# ---------- coach: goals & habits (R4) ----------

@router.message(Command("goal"))
async def cmd_goal(message: Message) -> None:
    if not _is_owner(message):
        return
    from app.core import coach
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Формат: <code>/goal текст цілі</code>\n"
            "Наприклад: <code>/goal Запустити продажі літа до 1 жовтня</code>\n"
            "Список: /goals")
        return
    async with database.session() as db:
        goal = await coach.create_goal(db, user_id=message.from_user.id,
                                       title=parts[1])
    import html as _html
    await message.answer(
        f"🎯 Ціль додано: <b>{_html.escape(goal.title)}</b>\n"
        "Прогрес питатиму в неділю у тижневому звіті. Список: /goals")


@router.message(Command("goals"))
async def cmd_goals(message: Message) -> None:
    if not _is_owner(message):
        return
    from app.core import coach
    async with database.session() as db:
        goals = await coach.list_goals(db, message.from_user.id)
    if not goals:
        await message.answer("Активних цілей немає. Додати: <code>/goal текст</code>")
        return
    import html as _html
    for g in goals:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏁 Досягнуто", callback_data=f"gd:{g.id}"),
            InlineKeyboardButton(text="🗑 Зняти", callback_data=f"gx:{g.id}"),
        ]])
        await message.answer(f"🎯 {_html.escape(g.title)}", reply_markup=kb)


@router.message(Command("habit"))
async def cmd_habit(message: Message) -> None:
    if not _is_owner(message):
        return
    from app.core import coach
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Формат: <code>/habit назва звички</code>\n"
            "Наприклад: <code>/habit Зарядка 15 хв</code>\n"
            "Відмічати і дивитись тиждень: /habits (і ввечері нагадаю сам)")
        return
    async with database.session() as db:
        habit = await coach.create_habit(db, user_id=message.from_user.id,
                                         title=parts[1])
    import html as _html
    await message.answer(
        f"🏃 Звичка додана: <b>{_html.escape(habit.title)}</b>\n"
        "Відмічай у /habits або ввечері в чек-іні.")


@router.message(Command("habits"))
async def cmd_habits(message: Message) -> None:
    if not _is_owner(message):
        return
    from app.core import coach
    async with database.session() as db:
        overview = await coach.habits_overview(db, message.from_user.id)
    if not overview:
        await message.answer("Звичок ще немає. Додати: <code>/habit назва</code>")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'✅' if h['done_today'] else '⬜️'} {h['title'][:28]} · "
                 f"{h['week_count']}/{h['week_days']}",
            callback_data=f"hb:{h['id']}")]
        for h in overview[:10]])
    await message.answer(
        "🏃 <b>Звички цього тижня</b> (тисни, щоб відмітити/зняти сьогодні):",
        reply_markup=kb)


@router.message(Command("travelon"))
async def cmd_travelon(message: Message) -> None:
    if not _is_owner(message):
        return
    from app.core import travelon
    if not travelon.configured():
        await message.answer(
            "🧳 TravelON ще не підключено — чекаю токен звітів "
            "(файл danos-travelon-token.txt, змінна TRAVELON_TOKEN).")
        return
    await message.answer("🧳 Збираю пульс TravelON…")
    text = await travelon.pulse_text()
    await message.answer(text or "Не вдалося отримати звіт, спробуй пізніше.")


async def _set_drive_account(user_id: int, cred_id: str) -> None:
    from app.models import AppState
    async with database.session() as db:
        state = await db.get(AppState, f"drive_acc_{user_id}") or AppState(
            key=f"drive_acc_{user_id}")
        state.value = cred_id
        db.add(state)
        await db.commit()


async def _drive_account(db, user_id: int):
    """Selected (or only) Google account for Drive operations."""
    from app.models import AppState
    accounts = await google_client.get_accounts(db, user_id)
    if not accounts:
        return None
    state = await db.get(AppState, f"drive_acc_{user_id}")
    if state:
        for c in accounts:
            if str(c.id) == state.value:
                return c
    return accounts[0]


async def _show_drive_folders(message: Message, user_id: int) -> None:
    async with database.session() as db:
        cred = await _drive_account(db, user_id)
        if cred is None:
            await message.answer("Спершу підключи Google: /connect_google")
            return
        access = await google_client.access_for(db, cred)
    if not access:
        await message.answer("Не зміг оновити доступ — спробуй /connect_google")
        return
    try:
        folders = await google_client.drive_list_folders(access)
    except Exception:
        await message.answer(
            "Не маю доступу до Drive — онови дозволи через /connect_google "
            "(додались права читання Drive і створення чернеток).")
        return
    if not folders:
        await message.answer(f"Папок у Drive ({cred.account_email}) не знайшов.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📁 {f['name'][:40]}", callback_data=f"dr:{f['id']}")]
        for f in folders[:15]])
    await message.answer(
        f"📧 Акаунт: <b>{cred.account_email}</b>\nОбери папку Drive — проіндексую "
        "pdf/docx/txt/md і Google Docs (лише читання):", reply_markup=kb)


@router.message(Command("drive"))
async def cmd_drive(message: Message) -> None:
    if not _is_owner(message):
        return
    async with database.session() as db:
        accounts = await google_client.get_accounts(db, message.from_user.id)
    if not accounts:
        await message.answer("Спершу підключи Google: /connect_google")
        return
    if len(accounts) == 1:
        await _set_drive_account(message.from_user.id, str(accounts[0].id))
        await _show_drive_folders(message, message.from_user.id)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📧 {c.account_email}", callback_data=f"da:{c.id}")]
        for c in accounts])
    await message.answer("З якого акаунта індексуємо Drive?", reply_markup=kb)


@router.message(Command("accounts"))
async def cmd_accounts(message: Message) -> None:
    if not _is_owner(message):
        return
    async with database.session() as db:
        accounts = await google_client.get_accounts(db, message.from_user.id)
    if not accounts:
        await message.answer("Підключених Google-акаунтів немає. Додати: /connect_google")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ Відключити {c.account_email}",
                              callback_data=f"ga:{c.id}")]
        for c in accounts])
    listing = "\n".join(f" • {c.account_email}" for c in accounts)
    await message.answer(
        f"📧 <b>Google-акаунти ({len(accounts)}):</b>\n{listing}\n\n"
        "➕ Додати ще один: /connect_google", reply_markup=kb)


@router.message(Command("reply"))
async def cmd_reply(message: Message) -> None:
    if not _is_owner(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Формат: <code>/reply що шукати</code>\n"
            "Наприклад: <code>/reply від Olena тема оплата</code> — знайду лист, "
            "підготую чернетку відповіді. Нічого не надсилаю без тебе.")
        return
    await message.answer("✍️ Шукаю лист і готую чернетку…")
    try:
        async with database.session() as db:
            status, draft = await orch.propose_draft(
                db, user_id=message.from_user.id, query=parts[1])
    except Exception:
        logger.exception("propose_draft failed")
        await message.answer("Не вийшло — можливо, бракує прав. Спробуй /connect_google.")
        return
    if status == "no_google":
        await message.answer("Спершу /connect_google")
    elif status == "not_found":
        await message.answer("Лист за цим запитом не знайшов. Уточни відправника чи тему.")
    elif status != "proposed" or draft is None:
        await message.answer("Не зміг скласти чернетку, спробуй ще раз.")
    else:
        await message.answer(
            f"📧 <b>Чернетка відповіді</b>\n<b>Кому:</b> {draft.to_addr}\n"
            f"<b>Тема:</b> {draft.subject}\n\n{draft.body[:2500]}",
            reply_markup=_draft_kb(draft.id))


@router.message(Command("kb"))
async def cmd_kb(message: Message) -> None:
    if not _is_owner(message):
        return
    from sqlalchemy import func, select
    from app.models import Document
    async with database.session() as db:
        docs = (await db.execute(
            select(Document).where(Document.user_id == message.from_user.id)
            .order_by(Document.created_at.desc()).limit(5))).scalars().all()
        total = (await db.execute(
            select(func.count()).select_from(Document)
            .where(Document.user_id == message.from_user.id))).scalar_one()
    if not total:
        await message.answer(
            "📚 База знань порожня. Перешли мені документ (pdf, docx, txt, md) "
            "або будь-яке повідомлення — і я запам'ятаю.")
        return
    lines = [f"📚 <b>База знань:</b> {total} документ(ів). Останні:"]
    lines += [f" • {d.title} ({d.chunk_count} фр., {d.created_at.strftime('%d.%m')})"
              for d in docs]
    await message.answer("\n".join(lines))


# ---------- knowledge intake: files & forwards ----------

ALLOWED_DOC_EXT = (".pdf", ".docx", ".txt", ".md")


@router.message(F.document)
async def on_document(message: Message) -> None:
    if not _is_owner(message):
        return
    from app.core.ingest import IngestError, extract_text, ingest_document
    doc = message.document
    name = (doc.file_name or "file").strip()
    if not name.lower().endswith(ALLOWED_DOC_EXT):
        await message.answer("Підтримую pdf, docx, txt, md — цей формат поки ні.")
        return
    if doc.file_size and doc.file_size > 15 * 1024 * 1024:
        await message.answer("Файл завеликий (ліміт 15 МБ)")
        return
    try:
        file = await message.bot.get_file(doc.file_id)
        url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file.file_path}"
        async with httpx.AsyncClient(timeout=120) as client:
            data = (await client.get(url)).content
        text = extract_text(name, data)
        async with database.session() as db:
            result = await ingest_document(
                db, user_id=message.from_user.id, title=name, text=text,
                source_type="telegram_file", source_ref=name)
    except IngestError as e:
        await message.answer(f"📄 {e}")
        return
    except Exception:
        logger.exception("document ingest failed")
        await message.answer("📄 Не вдалося обробити файл, спробуй ще раз.")
        return
    if result.status == "duplicate":
        await message.answer(f"📚 «{name}» вже є в базі знань ✅")
    else:
        await message.answer(
            f"📚 Додав у базу знань: <b>{name}</b> ({result.chunks} фрагментів).\n"
            "Тепер можеш просто спитати мене про його зміст.")


def _forward_title(message: Message) -> str:
    origin = message.forward_origin
    try:
        t = origin.type
        if t == "user":
            return f"Пересилка від {origin.sender_user.full_name}"
        if t == "channel":
            return f"Пересилка з «{origin.chat.title}»"
        if t == "hidden_user":
            return f"Пересилка від {origin.sender_user_name}"
        if t == "chat":
            return f"Пересилка з «{origin.sender_chat.title}»"
    except AttributeError:
        pass
    return "Переслане повідомлення"


@router.message(F.forward_origin, F.text | F.caption)
async def on_forward(message: Message) -> None:
    if not _is_owner(message):
        return
    from app.core.ingest import ingest_document
    content = message.text or message.caption or ""
    if len(content.strip()) < 25:
        await message.answer("У пересилці замало тексту, щоб її зберегти.")
        return
    title = _forward_title(message)
    try:
        async with database.session() as db:
            result = await ingest_document(
                db, user_id=message.from_user.id,
                title=f"{title} ({message.date.strftime('%d.%m.%Y')})",
                text=content, source_type="telegram_forward",
                source_ref=str(message.message_id))
    except Exception:
        logger.exception("forward ingest failed")
        await message.answer("Не вдалося зберегти пересилку.")
        return
    if result.status == "duplicate":
        await message.answer("📚 Це вже є в базі знань ✅")
    elif result.status == "indexed":
        await message.answer(f"📚 Зберіг у базу знань ({title.lower()})")
    else:
        await message.answer("Не вийшло проіндексувати цей текст.")


# ---------- notes: text & voice ----------

async def _process_note(message: Message, text: str, prefix: str = "") -> None:
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass
    dedupe = f"tg:{message.chat.id}:{message.message_id}"
    async with database.session() as db:
        outcome = await orch.handle_note(
            db, user_id=message.from_user.id, text=text, dedupe_key=dedupe)
    if outcome.kind == "duplicate":
        return  # replayed update — already processed, stay silent
    if outcome.kind == "proposal" and outcome.proposal:
        await message.answer(prefix + proposal_card(outcome.proposal),
                             reply_markup=_proposal_kb(outcome.proposal.id,
                                                       outcome.proposal.version))
        return
    import html as _html
    if outcome.kind == "note":
        await message.answer(
            f"{prefix}🧠 Запам'ятав (кандидат): {_html.escape(outcome.reply or '')}")
        return
    safe = _html.escape(outcome.reply or "Записав ✅")
    for i in range(0, len(safe), 3900):  # Telegram message limit
        await message.answer((prefix if i == 0 else "") + safe[i:i + 3900])


@router.message(F.voice)
async def on_voice(message: Message) -> None:
    if not _is_owner(message):
        return
    if message.voice.file_size and message.voice.file_size > 20 * 1024 * 1024:
        await message.answer("Голосове завелике (ліміт 20 МБ)")
        return
    try:
        file = await message.bot.get_file(message.voice.file_id)
        url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file.file_path}"
        async with httpx.AsyncClient(timeout=60) as client:
            audio = (await client.get(url)).content
        text = await get_transcriber().transcribe(audio)
    except TranscriptionError as e:
        await message.answer(f"🎙 {e}")
        return
    except Exception:
        logger.exception("voice processing failed")
        await message.answer("🎙 Не вдалося обробити голосове, спробуй ще раз.")
        return
    import html as _html
    await _process_note(message, text,
                        prefix=f"🎙 <i>Розчув:</i> {_html.escape(text)}\n\n")


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    if not _is_owner(message):
        logger.info("Ignored message from non-owner user_id=%s",
                    message.from_user.id if message.from_user else "?")
        return
    await _process_note(message, message.text)


@router.message(F.caption)
async def on_media_caption(message: Message) -> None:
    """Photo/video sent directly with a caption — process the caption as a note."""
    if not _is_owner(message):
        return
    await _process_note(message, message.caption)


@router.message()
async def on_other(message: Message) -> None:
    """Unsupported content — say so instead of silence."""
    if not _is_owner(message):
        return
    await message.answer(
        "Це медіа я поки не вмію читати 🙈\n"
        "Розумію: текст, голосові, документи (pdf/docx/txt/md) і пересилки "
        "з текстом чи підписом.")


async def _index_drive_folder(cb: CallbackQuery, user_id: int, folder_id: str) -> None:
    from app.core.ingest import IngestError, extract_text, ingest_document
    if not folder_id:
        await cb.answer("Невідома папка")
        return
    await cb.answer("Індексую папку…")
    try:
        async with database.session() as db:
            cred = await _drive_account(db, user_id)
            access = await google_client.access_for(db, cred) if cred else None
        if not access:
            await cb.message.answer("Спершу /connect_google")
            return
        files = await google_client.drive_list_files(access, folder_id)
    except Exception:
        logger.exception("drive listing failed")
        await cb.message.answer("Не зміг прочитати папку — перевір доступ: /connect_google")
        return
    if not files:
        await cb.message.answer("У папці немає підтримуваних файлів (pdf/docx/txt/md, Google Docs).")
        return
    added = dups = failed = 0
    for f in files[:20]:
        try:
            name, data = await google_client.drive_download_text_source(access, f)
            text = extract_text(name, data)
            async with database.session() as db:
                result = await ingest_document(
                    db, user_id=user_id, title=name, text=text,
                    source_type="drive", source_ref=f["id"])
            if result.status == "indexed":
                added += 1
            elif result.status == "duplicate":
                dups += 1
            else:
                failed += 1
        except (IngestError, Exception):
            failed += 1
    await cb.message.answer(
        f"📁 Готово: додав <b>{added}</b>, вже було {dups}, не вдалося {failed} "
        f"(з {min(len(files), 20)} файлів). Тепер можеш питати про їх зміст.")


# ---------- callbacks ----------

@router.callback_query()
async def on_callback(cb: CallbackQuery) -> None:
    if not _is_owner(cb):
        await cb.answer()
        return
    parts = (cb.data or "").split(":")
    action = parts[0] if parts else ""
    user_id = cb.from_user.id

    if action == "dr":  # Drive folder id is not a UUID
        await _index_drive_folder(cb, user_id, parts[1] if len(parts) > 1 else "")
        return
    try:
        ref = uuid.UUID(parts[1])
    except (IndexError, ValueError):
        await cb.answer("Невідома дія")
        return

    if action == "da":  # pick Drive account, then show folders
        await _set_drive_account(user_id, str(ref))
        await cb.answer()
        await _show_drive_folders(cb.message, user_id)
        return
    if action == "ga":  # disconnect a Google account
        from app.models import GoogleCredential
        async with database.session() as db:
            cred = await db.get(GoogleCredential, ref)
            if cred and cred.user_id == user_id:
                from app.core.audit import audit
                await audit(db, actor=f"user:{user_id}", action="google.disconnected",
                            resource_type="connector", resource_id=cred.id,
                            policy_level="L2", email=cred.account_email)
                await db.delete(cred)
                await db.commit()
                await cb.answer("Відключено")
                await cb.message.edit_text(f"❌ {cred.account_email} відключено")
            else:
                await cb.answer("Не знайдено")
        return

    try:
        async with database.session() as db:
            if action == "ap":
                version = int(parts[2]) if len(parts) > 2 else 1
                status, task, reminder = await orch.approve(
                    db, user_id=user_id, proposal_id=ref, version=version)
                if status == "created" and task:
                    await cb.message.edit_text(
                        task_created_card(task, reminder.fire_at if reminder else None),
                        reply_markup=_task_kb(task.id))
                    await cb.answer("Створено ✅")
                elif status == "already" and task:
                    await cb.answer("Уже створено раніше ✅")
                elif status == "superseded":
                    await cb.answer("Це стара версія — дивись нову картку", show_alert=True)
                else:
                    await cb.answer("Пропозиція вже неактуальна")
            elif action == "rj":
                await orch.reject(db, user_id=user_id, proposal_id=ref)
                await cb.message.edit_text("❌ Відхилено")
                await cb.answer()
            elif action == "ed":
                ok = await orch.start_edit(db, user_id=user_id, proposal_id=ref)
                await cb.answer()
                if ok:
                    await cb.message.answer(
                        "✏️ Надішли виправлений текст задачі одним повідомленням\n"
                        "<i>(наприклад: «в пʼятницю о 9 подзвонити Юрі про трансфери»)</i>")
                else:
                    await cb.answer("Цю пропозицію вже не можна змінити", show_alert=True)
            elif action == "dn":
                status = await orch.complete_task(db, user_id=user_id, task_id=ref)
                await cb.message.edit_reply_markup(reply_markup=None)
                await cb.answer("Виконано ☑️" if status == "completed" else status)
            elif action == "cn":
                status = await orch.cancel_task(db, user_id=user_id, task_id=ref)
                await cb.message.edit_reply_markup(reply_markup=None)
                await cb.answer("Скасовано 🚫" if status == "cancelled" else status)
            elif action == "mo":
                status = await orch.confirm_memory(db, user_id=user_id, item_id=ref)
                if isinstance(status, tuple) and status[0] == "conflict":
                    old = status[1]
                    await cb.message.edit_text(
                        "⚖️ <b>Конфлікт пам'яті</b>\n"
                        f"🆕 Нове: {cb.message.text[2:].strip()}\n"
                        f"📌 Старе ({old.created_at.strftime('%d.%m.%Y')}): {old.content[:300]}",
                        reply_markup=_conflict_kb(ref))
                    await cb.answer("Потрібне рішення ⚖️")
                elif status == "confirmed":
                    await cb.message.edit_text(f"🧠✅ {cb.message.text[2:].strip()}")
                    await cb.answer("У пам'яті ✅")
                else:
                    await cb.answer(str(status))
            elif action == "cf":
                choice = parts[2] if len(parts) > 2 else "b"
                status = await orch.resolve_conflict(
                    db, user_id=user_id, new_id=ref, choice=choice)
                labels = {"n": "🆕 Нове замінило старе ✅",
                          "o": "📌 Лишив старе, нове відкинув",
                          "b": "🤝 Зберіг обидва ✅"}
                await cb.message.edit_reply_markup(reply_markup=None)
                await cb.answer(labels.get(choice, status) if status == "resolved" else str(status))
            elif action == "dm":
                status = await orch.approve_draft(db, user_id=user_id, draft_id=ref)
                if status in ("created", "already"):
                    await cb.message.edit_reply_markup(reply_markup=None)
                    await cb.answer("Чернетка в Gmail ✅ (нічого не надіслано)", show_alert=True)
                elif status == "no_google":
                    await cb.answer("Онови доступ: /connect_google", show_alert=True)
                else:
                    await cb.answer(str(status))
            elif action == "dx":
                await orch.reject_draft(db, user_id=user_id, draft_id=ref)
                await cb.message.edit_reply_markup(reply_markup=None)
                await cb.answer("Відхилено")
            elif action == "mx":
                status = await orch.reject_memory(db, user_id=user_id, item_id=ref)
                await cb.message.edit_text("🗑 Відкинуто")
                await cb.answer()
            elif action == "gd":
                from app.core import coach
                status = await coach.set_goal_status(
                    db, user_id=user_id, goal_id=ref, status="done")
                if status == "done":
                    await cb.message.edit_text(
                        f"🏁 {cb.message.text[2:].strip()} — досягнуто! 🎉")
                await cb.answer("Вітаю! 🎉" if status == "done" else str(status))
            elif action == "gx":
                from app.core import coach
                status = await coach.set_goal_status(
                    db, user_id=user_id, goal_id=ref, status="dropped")
                if status == "dropped":
                    await cb.message.edit_text(f"🗑 {cb.message.text[2:].strip()} — знято")
                await cb.answer()
            elif action == "hb":
                from app.core import coach
                status = await coach.toggle_habit(db, user_id=user_id, habit_id=ref)
                if status in ("done", "undone"):
                    overview = await coach.habits_overview(db, user_id)
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text=f"{'✅' if h['done_today'] else '⬜️'} {h['title'][:28]} · "
                                 f"{h['week_count']}/{h['week_days']}",
                            callback_data=f"hb:{h['id']}")]
                        for h in overview[:10]])
                    try:
                        await cb.message.edit_reply_markup(reply_markup=kb)
                    except Exception:
                        pass  # markup unchanged or message too old
                    await cb.answer("Відмічено ✅" if status == "done" else "Знято")
                else:
                    await cb.answer(str(status))
            else:
                await cb.answer("Невідома дія")
    except PolicyDenied as e:
        await cb.answer(f"Заборонено політикою: {e.decision.reason}", show_alert=True)
    except Exception:
        logger.exception("callback failed")
        await cb.answer("Помилка, спробуй ще раз")
