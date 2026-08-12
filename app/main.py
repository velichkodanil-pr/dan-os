"""DAN.OS core entrypoint: FastAPI app + aiogram webhook adapter."""
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Update
from fastapi import FastAPI, Request, Response

from app.config import settings
from app.telegram.bot import router as telegram_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("dan_os")

WEBHOOK_PATH = "/telegram/webhook"

dp = Dispatcher()
dp.include_router(telegram_router)

bot: Bot | None = None
if settings.telegram_bot_token:
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
else:
    logger.warning("TELEGRAM_BOT_TOKEN is not set — starting without Telegram (health only).")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if bot and settings.public_url:
        url = settings.public_url + WEBHOOK_PATH
        await bot.set_webhook(
            url,
            secret_token=settings.webhook_secret or None,
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        logger.info("Webhook set to %s", url)
    elif bot:
        logger.warning("RAILWAY_PUBLIC_DOMAIN is not set — webhook not registered.")
    yield
    if bot:
        await bot.session.close()


app = FastAPI(title="DAN.OS", lifespan=lifespan)


@app.get("/health/live")
async def health_live() -> dict:
    return {"status": "ok", "service": "dan-os", "round": 0}


@app.get("/health/ready")
async def health_ready() -> dict:
    # Round 1 will add a database ping here.
    return {
        "status": "ok",
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
