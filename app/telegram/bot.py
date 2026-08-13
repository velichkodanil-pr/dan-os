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
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
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
    """Evening check-in: summary + memory-candidate review (21:30 ritual)."""
    async with database.session() as db:
        summary = await briefs.evening_summary(db, user_id)
        candidates = await briefs.pending_candidates(db, user_id)
    await bot_instance.send_message(user_id, summary)
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
        "Привіт, Данило! <b>DAN.OS</b> · раунд 2 🟢\n\n"
        "• текст/голосове «нагадай…» → задача з нагадуванням\n"
        "• «запам'ятай: …» → факт у пам'ять\n"
        "• 📄 документ (pdf/docx/txt/md) чи пересилка → база знань, потім просто питай\n"
        "• /today — план · /brief — бриф · /checkin — розбір · /kb — база знань\n"
        f"• бриф о {settings.brief_time}, чек-ін о {settings.checkin_time}, "
        f"пошт. дайджест о {settings.digest_times}"
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
        "Тисни кнопку, обери свій акаунт і дозволь доступ (лише читання "
        "календаря і пошти). Екран «Google hasn't verified this app» — "
        "це нормально: Advanced → Go to DAN.OS.", reply_markup=kb)


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    if not _is_owner(message):
        return
    await message.answer("pong ✅")


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


@router.message(F.forward_origin, F.text)
async def on_forward(message: Message) -> None:
    if not _is_owner(message):
        return
    from app.core.ingest import ingest_document
    title = _forward_title(message)
    try:
        async with database.session() as db:
            result = await ingest_document(
                db, user_id=message.from_user.id,
                title=f"{title} ({message.date.strftime('%d.%m.%Y')})",
                text=message.text, source_type="telegram_forward",
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
    if outcome.kind == "note":
        await message.answer(f"{prefix}🧠 Запам'ятав (кандидат): {outcome.reply}")
        return
    await message.answer(prefix + (outcome.reply or "Записав ✅"))


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
    await _process_note(message, text, prefix=f"🎙 <i>Розчув:</i> {text}\n\n")


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    if not _is_owner(message):
        logger.info("Ignored message from non-owner user_id=%s",
                    message.from_user.id if message.from_user else "?")
        return
    await _process_note(message, message.text)


# ---------- callbacks ----------

@router.callback_query()
async def on_callback(cb: CallbackQuery) -> None:
    if not _is_owner(cb):
        await cb.answer()
        return
    try:
        parts = (cb.data or "").split(":")
        action, ref = parts[0], uuid.UUID(parts[1])
    except (IndexError, ValueError):
        await cb.answer("Невідома дія")
        return
    user_id = cb.from_user.id

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
                if status in ("confirmed", "already"):
                    await cb.message.edit_text(f"🧠✅ {cb.message.text[2:].strip()}")
                await cb.answer("У пам'яті ✅" if status == "confirmed" else status)
            elif action == "mx":
                status = await orch.reject_memory(db, user_id=user_id, item_id=ref)
                await cb.message.edit_text("🗑 Відкинуто")
                await cb.answer()
            else:
                await cb.answer("Невідома дія")
    except PolicyDenied as e:
        await cb.answer(f"Заборонено політикою: {e.decision.reason}", show_alert=True)
    except Exception:
        logger.exception("callback failed")
        await cb.answer("Помилка, спробуй ще раз")
