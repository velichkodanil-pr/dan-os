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
from app.core.orchestrator import Orchestrator
from app.core.policy import PolicyDenied
from app.core.transcription import TranscriptionError, get_transcriber
from app.telegram.cards import proposal_card, task_created_card, today_card

logger = logging.getLogger(__name__)
router = Router()
orch = Orchestrator()


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
        "Привіт, Данило! <b>DAN.OS</b> · раунд 1 🟢\n\n"
        "Кидай мені текст або голосове:\n"
        "• «нагадай завтра о 10 подзвонити в банк» → задача з нагадуванням\n"
        "• «запам'ятай: …» → факт у пам'ять\n"
        "• /today — план на сьогодні"
    )


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    if not _is_owner(message):
        return
    async with database.session() as db:
        data = await orch.today(db, user_id=message.from_user.id)
    await message.answer(today_card(data))


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    if not _is_owner(message):
        return
    await message.answer("pong ✅")


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
            else:
                await cb.answer("Невідома дія")
    except PolicyDenied as e:
        await cb.answer(f"Заборонено політикою: {e.decision.reason}", show_alert=True)
    except Exception:
        logger.exception("callback failed")
        await cb.answer("Помилка, спробуй ще раз")
