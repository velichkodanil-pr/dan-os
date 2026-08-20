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
from app.config import APP_RELEASE, APP_VERSION, SCANNER_BUILD, settings
from app.core import google_client, scheduler
from app.core.audit import audit
from app.core.domains import ALLOWED_DOMAINS, label as domain_label
from app.telegram import bot as botmod
from app.telegram.bot import router as telegram_router
from app.webapp.routes import router as webapp_router

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
                        botmod.send_digest, botmod.send_weekly,
                        botmod.send_debt_alert, botmod.run_travelon_sync)
    else:
        logger.warning("DATABASE_URL is not set — running without persistence.")
    if settings.database_url and bot:
        try:  # a deploy/restart kills background indexing — say so, not silence
            from app.models import AppState
            async with database.session() as db:
                flag = await db.get(AppState, "drive_all_running")
                if flag is not None:
                    owner = int(flag.value or settings.owner_telegram_id or 0)
                    await db.delete(flag)
                    await db.commit()
                    if owner:
                        await bot.send_message(
                            owner, "⚠️ Індексацію Drive перервав перезапуск "
                            "бота (деплой). Усе оброблене збережено — запусти "
                            "/drive_all ще раз, він продовжить з того ж місця.")
        except Exception:
            logger.exception("drive_all interrupted-flag check failed")
    if bot and settings.public_url:
        url = settings.public_url + WEBHOOK_PATH
        await bot.set_webhook(url, secret_token=settings.webhook_secret or None,
                              drop_pending_updates=False,
                              allowed_updates=["message", "callback_query"])
        logger.info("Webhook set to %s", url)
        if settings.owner_telegram_id:
            try:  # Mini App on the menu button next to the input field
                from aiogram.types import MenuButtonWebApp, WebAppInfo
                await bot.set_chat_menu_button(
                    chat_id=settings.owner_telegram_id,
                    menu_button=MenuButtonWebApp(
                        text="DAN.OS",
                        web_app=WebAppInfo(url=settings.public_url + "/app")))
            except Exception:
                logger.exception("menu button setup failed (non-fatal)")
    elif bot:
        logger.warning("RAILWAY_PUBLIC_DOMAIN is not set — webhook not registered.")
    yield
    scheduler.stop()
    if bot:
        await bot.session.close()


app = FastAPI(title="DAN.OS", lifespan=lifespan)
app.include_router(webapp_router)


@app.get("/health/live")
async def health_live() -> dict:
    """Build metadata only — deliberately says nothing about whether the
    production security scan has run. That claim belongs to /health/ready,
    which can actually check it."""
    return {"status": "ok", "service": "dan-os", "version": APP_VERSION,
            "release": APP_RELEASE, "scanner_version": SCANNER_BUILD}


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
    scan_done = None
    if db_ok:
        try:
            from app.core import security
            async with database.session() as db:
                scan_done = await security.scan_complete(db)
        except Exception:
            logger.exception("scan-gate check failed")
    return {
        "status": "ok" if db_ok in (True, None) else "degraded",
        "db": db_ok,
        "scanner_version": SCANNER_BUILD,
        # truthful: False until a FULL scanner-v2 pass finishes in production
        "security_scan_complete": scan_done,
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
    verified = google_client.verify_state(state)
    if verified is None or verified[0] != settings.owner_telegram_id:
        logger.warning("OAuth callback with invalid state rejected.")
        return HTMLResponse(status_code=403, content=_OAUTH_OK_HTML.format(
            title="Відхилено", sub="Недійсний або протермінований запит."))
    user_id, oauth_domain = verified   # domain came from the signed state (§11)
    try:
        tokens = await google_client.exchange_code(code)
        async with database.session() as db:
            email = await google_client.store_tokens(db, user_id, tokens, oauth_domain)
            total = len(await google_client.get_all_accounts(db, user_id))
            await audit(db, actor=f"user:{user_id}", action="google.connected",
                        resource_type="connector", resource_id="google",
                        policy_level="L2", email=email, domain=oauth_domain.value,
                        scopes=tokens.get("scope", ""))
            await db.commit()
    except Exception:
        logger.exception("OAuth exchange failed")
        return HTMLResponse(status_code=500, content=_OAUTH_OK_HTML.format(
            title="Помилка", sub="Не вдалося обміняти код. Спробуй ще раз."))
    if bot:
        granted = tokens.get("scope", "")
        missing = [label for key, label in
                   (("calendar.readonly", "Календар"),
                    ("gmail.readonly", "Gmail (читання)"),
                    ("gmail.compose", "Gmail (чернетки)"),
                    ("drive.readonly", "Drive"))
                   if key not in granted]
        msg = (f"🔐 Google-акаунт <b>{email}</b> підключено ✅ (усього: {total})\n"
               f"Прив'язано до домену: {domain_label(oauth_domain)}. Цей акаунт "
               "використовується лише в цьому домені. Керування: /accounts")
        if missing:
            msg += (f"\n\n⚠️ <b>Без дозволів:</b> {', '.join(missing)}.\n"
                    "Ці функції для акаунта працювати НЕ будуть. Запусти "
                    "/connect_google ще раз для цього ж акаунта й постав "
                    "УСІ галочки на екрані дозволів.")
        try:
            await bot.send_message(user_id, msg)
        except Exception:
            logger.exception("notify failed")
    return HTMLResponse(_OAUTH_OK_HTML.format(
        title="Google підключено ✅", sub="Повертайся в Telegram — DAN.OS уже все бачить."))


from pydantic import BaseModel  # noqa: E402


class AdminIngestRequest(BaseModel):
    title: str
    text: str
    # §13: domain is REQUIRED and explicit — but validated INSIDE the handler
    # (400), after the token gate, so an unauthenticated caller still gets 403
    # first. Empty/unknown -> 400. There is NO silent 'personal'.
    domain: str = ""
    source_ref: str = ""
    # R6.1A: compilation is a provider call over stored content — opt-in only,
    # and only when the local security scan of the base has finished.
    compile: bool = False


@app.post("/admin/ingest")
async def admin_ingest(req: AdminIngestRequest, request: Request):
    """Cowork knowledge channel: Danylo drops materials in the dev chat,
    Claude pushes them here -> the bot's knowledge base (same ingest pipeline,
    same dedupe/provenance). Off unless ADMIN_TOKEN is set; constant-time check."""
    import hmac as _hmac
    token = request.headers.get("X-Admin-Token", "")
    if (not settings.admin_token or not settings.owner_telegram_id
            or not _hmac.compare_digest(token, settings.admin_token)):
        return Response(status_code=403)
    text = req.text.strip()
    if len(text) < 20:
        return Response(status_code=400, content="text too short")
    if req.domain not in ALLOWED_DOMAINS:   # §13: explicit valid domain only
        return Response(status_code=400, content="invalid or missing domain")
    domain = req.domain
    from app.core import security
    from app.core.ingest import ingest_document
    pages: list = []
    compile_status = "skipped"
    async with database.session() as db:
        result = await ingest_document(
            db, user_id=settings.owner_telegram_id,
            title=req.title.strip()[:200] or "Матеріал",
            text=text, source_type="cowork_upload",
            source_ref=req.source_ref.strip()[:200], domain=domain)
        if result.status == "indexed" and result.document is not None and req.compile:
            if not await security.scan_complete(db):
                compile_status = "blocked_scan_incomplete"
            else:
                try:  # compile into wiki pages (best effort)
                    from app.core import wiki
                    outcome = await wiki.compile_document(
                        db, user_id=settings.owner_telegram_id,
                        document=result.document)
                    pages, compile_status = outcome.pages, outcome.status
                except Exception:
                    logger.exception("admin ingest wiki compile failed")
                    compile_status = "failed"
    return {"status": result.status, "chunks": result.chunks,
            "document_id": str(result.document.id) if result.document else None,
            # categories only — the endpoint never echoes what it rejected
            "security": {"categories": [str(c) for c in result.categories]}
            if result.status == "quarantined" else None,
            "compile_status": compile_status,
            "wiki_pages": [{"slug": s, "status": st} for s, st in pages]}


class AdminSearchRequest(BaseModel):
    query: str
    domain: str = ""   # §13: required, validated in-handler (400) after token
    k: int = 8


@app.post("/admin/search")
async def admin_search(req: AdminSearchRequest, request: Request):
    """KB diagnostics for the Cowork channel: what would retrieval see?
    Same token gate as /admin/ingest; read-only, chunks trimmed."""
    import hmac as _hmac
    token = request.headers.get("X-Admin-Token", "")
    if (not settings.admin_token or not settings.owner_telegram_id
            or not _hmac.compare_digest(token, settings.admin_token)):
        return Response(status_code=403)
    from app.core import rag, security
    if req.domain not in ALLOWED_DOMAINS:   # §13: explicit valid domain only
        return Response(status_code=400, content="invalid or missing domain")
    if security.scan(req.query).blocked:   # zero embedding / provider calls
        return {"hits": [], "refused": "secret_in_query"}
    async with database.session() as db:
        chunks = await rag.retrieve(db, user_id=settings.owner_telegram_id,
                                    domain=req.domain, query=req.query,
                                    k=min(req.k, 15))
    return {"hits": [{"title": c.title, "distance": round(c.distance, 3),
                      "text": c.text[:400]} for c in chunks]}


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
