"""DAN.OS core entrypoint: FastAPI app + aiogram webhook adapter + scheduler."""
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update
from fastapi import FastAPI, Request, Response
from sqlalchemy import text as sql_text

from app import db as database
from app.config import settings
from app.core import scheduler
from app.telegram.bot import router as telegram_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("dan_os")

WEBHOOK_PATH = "/telegram/webhook"

dp = Dispatcher()
dp.include_router(telegram_router)

bot: Bot | None = None
if settings.telegram_bot_token:
    bot = Bot(token=settings.telegram_bot_token,
              default=DefaultBotProperties(parse_mode="HTML"))
else:
    logger.warning("TELEGRAM_BOT_TOKEN is not set — starting without Telegram (health only).")


async def _send_reminder(user_id: int, html: str, task_id: str = "") -> None:
    if bot is None:
        raise RuntimeError("bot is not configured")
    kb = None
    if task_id:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="☑️ Виконано", callback_data=f"dn:{task_id}"),
        ]])
    await bot.send_message(user_id, html, reply_markup=kb)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.database_url:
        database.init_engine()
        scheduler.start(_send_reminder)
    else:
        logger.warning("DATABASE_URL is not set — running without persistence.")
    if bot and settings.public_url:
        url = settings.public_url + WEBHOOK_PATH
        await bot.set_webhook(url, secret_token=settings.webhook_secret or None,
                              drop_pending_updates=False,
                              allowed_updates=["message", "callback_query"])
        logger.info("Webhook set to %s", url)
    elif bot:
        logger.warning("RAILWAY_PUBLIC_DOMAIN is not set — webhook not registered.")
    yield
    scheduler.stop()
    if bot:
        await bot.session.close()


app = FastAPI(title="DAN.OS", lifespan=lifespan)


@app.get("/health/live")
async def health_live() -> dict:
    return {"status": "ok", "service": "dan-os", "round": 1}


@app.get("/health/ready")
async def health_ready() -> dict:
    db_ok = None
    if settings.database_url and database.SessionLocal is not None:
        try:
            async with database.session() as db:
                await db.execute(sql_text("SELECT 1"))
            db_ok = True
        except Exception:
            logger.exception("DB health check failed")
            db_ok = False
    return {
        "status": "ok" if db_ok in (True, None) else "degraded",
        "db": db_ok,
        "telegram_configured": bot is not None,
        "webhook_target": (settings.public_url + WEBHOOK_PATH) if settings.public_url else None,
    }


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Response:
    if bot is None:
        return Response(status_code=503)
    if settings.webhook_secret:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header != settings.webhook_secret:
            logger.warning("Webhook call with invalid secret token rejected.")
            return Response(status_code=403)
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response(status_code=200)
