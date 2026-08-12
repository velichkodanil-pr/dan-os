"""Telegram adapter: aiogram router only. No business logic here (see CLAUDE.md).

Round 0 scope: prove the pipeline works end to end. The bot answers its owner,
prints the Telegram ID on /start when OWNER_TELEGRAM_ID is not configured yet,
and stays silent for everyone else.
"""
import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.config import settings

logger = logging.getLogger(__name__)
router = Router()


def _is_owner(message: Message) -> bool:
    return bool(
        settings.owner_telegram_id
        and message.from_user
        and message.from_user.id == settings.owner_telegram_id
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not settings.owner_telegram_id:
        # Bootstrap mode: help the owner claim the bot, then stay silent for others.
        user_id = message.from_user.id if message.from_user else 0
        await message.answer(
            "Привіт! Я <b>DAN.OS</b> — раунд 0.\n"
            f"Твій Telegram ID: <code>{user_id}</code>\n"
            "Додай його у змінну <code>OWNER_TELEGRAM_ID</code> на Railway — "
            "і я працюватиму лише для тебе."
        )
        return
    if not _is_owner(message):
        return  # silence for strangers
    await message.answer(
        "Привіт, Данило! <b>DAN.OS</b> на зв'язку 🟢\n"
        "Раунд 0: ядро на Railway, вебхук живий, деплой-конвеєр працює.\n"
        "Наступний раунд — задачі з голосових і превʼю з кнопками."
    )


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    if not _is_owner(message):
        return
    await message.answer("pong ✅ (webhook → core → Telegram)")


@router.message()
async def any_message(message: Message) -> None:
    if not _is_owner(message):
        logger.info("Ignored message from non-owner user_id=%s",
                    message.from_user.id if message.from_user else "?")
        return
    await message.answer(
        "Чую ✅ Це раунд 0 — поки що я лише підтверджую зв'язок.\n"
        "Задачі, пам'ять і превʼю з кнопками приїдуть у раунді 1."
    )
