"""DAN.OS core entrypoint: FastAPI app + aiogram webhook adapter + scheduler."""
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import text as sql_text

from app import db as database
from app.config import settings
from app.core import google_client, scheduler
from app.core.audit import audit
from app.telegram import bot as botmod
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
    botmod.bot_instance = bot
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
        scheduler.start(_send_reminder, botmod.send_brief, botmod.send_checkin,
                        botmod.send_digest, botmod.send_weekly)
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


_OAUTH_OK_HTML = """<!doctype html><html lang="uk"><meta charset="utf-8">
<title>DAN.OS</title><body style="font-family:system-ui;display:flex;align-items:center;
justify-content:center;height:90vh;background:#0f1b2d;color:#fff;text-align:center">
<div><h1>{title}</h1><p style="color:#9fb3c8">{sub}</p></div></body></html>"""


@app.get("/google/oauth/callback")
async def google_oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse(_OAUTH_OK_HTML.format(
            title="Скасовано", sub="Доступ не надано. Спробуй /connect_google ще раз."))
    user_id = google_client.verify_state(state)
    if user_id is None or user_id != settings.owner_telegram_id:
        logger.warning("OAuth callback with invalid state rejected.")
        return HTMLResponse(status_code=403, content=_OAUTH_OK_HTML.format(
            title="Відхилено", sub="Недійсний або протермінований запит."))
    try:
        tokens = await google_client.exchange_code(code)
        async with database.session() as db:
            await google_client.store_tokens(db, user_id, tokens)
            await audit(db, actor=f"user:{user_id}", action="google.connected",
                        resource_type="connector", resource_id="google",
                        policy_level="L2", scopes=tokens.get("scope", ""))
            await db.commit()
    except Exception:
        logger.exception("OAuth exchange failed")
        return HTMLResponse(status_code=500, content=_OAUTH_OK_HTML.format(
            title="Помилка", sub="Не вдалося обміняти код. Спробуй ще раз."))
    if bot:
        try:
            await bot.send_message(
                user_id, "🔐 Google підключено ✅ Календар і пошта тепер у брифі — "
                         "спробуй /brief")
        except Exception:
            logger.exception("notify failed")
    return HTMLResponse(_OAUTH_OK_HTML.format(
        title="Google підключено ✅", sub="Повертайся в Telegram — DAN.OS уже все бачить."))


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
