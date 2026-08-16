"""Telegram adapter: thin handlers that call the orchestrator. No business logic.

Round 1: text/voice note -> preview card (✅/✏️/❌) -> task -> Today -> reminder.
Non-owners get silence everywhere.
"""
import logging
import re
import uuid

import httpx
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo,
)

from app import db as database
from app.config import APP_RELEASE, settings
from app.core import briefs, google_client
from app.core.domains import (
    ALLOWED_DOMAINS, DESCRIPTIONS, Domain, get_active_domain,
    label as domain_label, parse_domain, set_active_domain)
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


async def _active_domain(user_id: int) -> Domain:
    """The server-side active domain for a Telegram request (its own session)."""
    async with database.session() as db:
        return await get_active_domain(db, user_id)


async def send_brief(user_id: int) -> None:
    """Morning brief (/brief and the 07:30 ritual). A cross-domain ritual built
    from SEPARATE per-domain sections with explicit headers (§14); empty
    domains are skipped."""
    sections = [briefs.brief_date_header()]
    for dom in Domain:
        async with database.session() as db:
            today_data = await orch.today(db, user_id=user_id, domain=dom)
            section = await briefs.morning_brief(db, user_id, dom, today_data)
        if section:
            sections.append(section)
    if len(sections) == 1:
        sections.append("\nСьогодні нічого термінового — усі домени спокійні ✅")
    await bot_instance.send_message(user_id, "\n".join(sections))


async def send_checkin(user_id: int) -> None:
    """Evening check-in: per-domain summary + habits + memory-candidate review."""
    from app.core import coach
    summaries, all_undone, all_candidates = [], [], []
    for dom in Domain:
        async with database.session() as db:
            summary = await briefs.evening_summary(db, user_id, dom)
            candidates = await briefs.pending_candidates(db, user_id, dom)
            habits = await coach.habits_overview(db, user_id, dom)
        if summary:
            summaries.append(summary)
        all_undone += [(dom, h) for h in habits if not h["done_today"]]
        all_candidates += [(dom, c) for c in candidates]
    head = "🌙 <b>Вечірній підсумок</b>"
    await bot_instance.send_message(
        user_id, head + ("\n" + "\n".join(summaries) if summaries
                         else "\nСьогодні порожньо."))
    if all_undone:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"☑️ {domain_label(dom)}: {h['title'][:22]}",
                callback_data=f"hb:{h['id']}")]
            for dom, h in all_undone[:8]])
        await bot_instance.send_message(
            user_id, "🏃 <b>Звички сьогодні</b> — що з цього зроблено? "
            "Тисни, щоб позначити:", reply_markup=kb)
    if all_candidates:
        await bot_instance.send_message(
            user_id, f"🧠 Розберемо пам'ять — кандидатів: {len(all_candidates)}")
        for dom, item in all_candidates:
            await bot_instance.send_message(
                user_id, f"🧠 [{domain_label(dom)}] {item.content}",
                reply_markup=_memory_kb(item.id))


async def send_digest(user_id: int) -> None:
    """Mail digest ritual — one labeled block per domain (§14), silent when
    every inbox is quiet."""
    from app.core.digest import build_digest
    blocks = []
    for dom in Domain:
        async with database.session() as db:
            text = await build_digest(db, user_id, dom)
        if text:
            blocks.append(text)
    if blocks:
        await bot_instance.send_message(user_id, "\n\n".join(blocks))


async def send_weekly(user_id: int) -> None:
    """Sunday coverage report — one labeled section per domain (§14)."""
    from app.core.reports import weekly_coverage_report
    sections = ["📊 <b>Тижневий звіт DAN.OS</b>"]
    for dom in Domain:
        async with database.session() as db:
            section = await weekly_coverage_report(db, user_id, dom)
        if section:
            sections.append(section)
    if len(sections) == 1:
        sections.append("\nЦього тижня активності по доменах не було.")
    await bot_instance.send_message(user_id, "\n".join(sections))


async def send_debt_alert(user_id: int) -> None:
    """Daily TravelON alert: tomorrow's check-ins with unpaid balance.
    Stays silent when there is nothing to flag."""
    from app.core import travelon
    text = await travelon.debt_alert_text()
    if text:
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
        f"Привіт, Данило! <b>DAN.OS</b> · {APP_RELEASE} 🟢\n\n"
        "🔒 Технічні секрети (API-ключі, токени, приватні ключі, seed-фрази) "
        "НЕ зберігаю — відсікаю до бази й до моделі. Логіни й паролі до "
        "кабінетів партнерів зберігаю і знаходжу · перевірка бази: "
        "/kb_security_scan · що ізольовано: /kb_quarantine\n"
        "🎙 Ключі й токени не диктуй голосом: аудіо йде на розпізнавання "
        "ДО того, як я можу його перевірити.\n\n"
        "🧭 <b>Домени</b> — три ізольовані простори (🏠 особисте / 🧳 TravelON / "
        "🛠 tech). Знання, задачі, пошта й пам'ять одного не видно в іншому. "
        "Перемкнути: /domain · перевірити цілісність: /domain_audit\n\n"
        "• текст/голосове «нагадай…» → задача з нагадуванням\n"
        "• «запам'ятай: …» → факт у пам'ять\n"
        "• «скасуй мою участь у зустрічі…» → відхилю подію в календарі (з підтвердженням)\n"
        "• «постав зустріч з Юрою завтра о 15» → подія в календарі (з підтвердженням)\n"
        "• 📄 документ (pdf/docx/txt/md) чи пересилка → база знань, потім просто питай\n"
        "• 🎙 транскрипт зустрічі (vtt/srt із Zoom) → підсумок, рішення і задачі\n"
        "• /app — міні-застосунок: сьогодні, підтвердження, пам'ять 📱\n"
        "• /goal і /habit — цілі та звички (тренер) · /goals · /habits\n"
        "• /travelon — пульс TravelON 🧳 · «заявка 59266» чи /order — картка заявки\n"
        "• 🚨 щодня о 10:00 попереджу, якщо завтра заїзд із боргом\n"
        "• /drive_all — проіндексувати ВЕСЬ Drive (усі акаунти) · /drive — одну папку\n"
        "• /wiki — база знань сторінками (партнери, процеси, умови) · /wiki_build\n"
        "• «напиши лист на adresa@…» → чернетка нового листа · /reply — відповідь\n"
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
        domain = await get_active_domain(db, message.from_user.id)
        data = await orch.today(db, user_id=message.from_user.id, domain=domain)
    await message.answer(f"{domain_label(domain)}\n" + today_card(data))


def _domain_kb(active: Domain) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=("● " if d == active else "○ ") + domain_label(d),
            callback_data=f"dom:{d.value}")]
        for d in Domain])


@router.message(Command("domain"))
async def cmd_domain(message: Message) -> None:
    """Show or switch the ACTIVE domain — the isolation boundary for everything
    that follows. `/domain` alone shows the current one with buttons; `/domain
    personal|travelon|tech` switches directly. TravelON is entered ONLY here (or
    the button) — /travelon stays the business pulse, not a switch."""
    if not _is_owner(message):
        return
    user_id = message.from_user.id
    parts = (message.text or "").split()
    if len(parts) >= 2:
        try:
            target = parse_domain(parts[1])
        except Exception:
            await message.answer(
                "Домен має бути один із: " + ", ".join(ALLOWED_DOMAINS)
                + ". Приклад: <code>/domain travelon</code>")
            return
        await _switch_domain(message, user_id, target)
        return
    async with database.session() as db:
        active = await get_active_domain(db, user_id)
    desc = "\n".join(f"{'●' if d == active else '○'} {domain_label(d)} — "
                     f"{DESCRIPTIONS[d]}" for d in Domain)
    await message.answer(
        f"Активний домен: <b>{domain_label(active)}</b>\n\n{desc}\n\n"
        "Це ізольовані простори: знання, задачі, пошта й пам'ять одного "
        "домену не видно в іншому. Перемкнути — кнопкою нижче:",
        reply_markup=_domain_kb(active))


async def _switch_domain(message_or_cb, user_id: int, target: Domain) -> None:
    """Switch active domain, audited; clears any pending edit state (§3)."""
    from app.core.audit import audit
    async with database.session() as db:
        old = await get_active_domain(db, user_id)
        await set_active_domain(db, user_id, target)
        await audit(db, actor=f"user:{user_id}", action="domain.switched",
                    resource_type="user_state", resource_id=str(user_id),
                    policy_level="L1", old_domain=old.value, new_domain=target.value)
        await db.commit()
    text = (f"✅ Активний домен: <b>{domain_label(target)}</b>\n"
            f"{DESCRIPTIONS[target]}.\nДані інших доменів зараз недоступні.")
    answer = getattr(message_or_cb, "answer", None)
    if hasattr(message_or_cb, "message"):        # CallbackQuery
        await message_or_cb.message.answer(text)
        await message_or_cb.answer("Перемкнено ✅")
    else:
        await answer(text)


@router.message(Command("personal"))
async def cmd_personal(message: Message) -> None:
    if not _is_owner(message):
        return
    await _switch_domain(message, message.from_user.id, Domain.PERSONAL)


@router.message(Command("tech"))
async def cmd_tech(message: Message) -> None:
    if not _is_owner(message):
        return
    await _switch_domain(message, message.from_user.id, Domain.TECH)


@router.message(Command("domain_audit"))
async def cmd_domain_audit(message: Message) -> None:
    """Owner-only integrity report: COUNTS ONLY per domain — never any content
    (§15). Resources per domain, unassigned Google accounts, parent/child
    domain mismatches, open security findings per domain."""
    if not _is_owner(message):
        return
    from app.core.domain_audit import domain_audit_report
    async with database.session() as db:
        text = await domain_audit_report(db, message.from_user.id)
    await message.answer(text)


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
    domain = await _active_domain(message.from_user.id)
    url = google_client.auth_url(message.from_user.id, domain)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔐 Підключити Google", url=url)]])
    await message.answer(
        f"Новий акаунт буде прив'язаний до домену <b>{domain_label(domain)}</b> "
        "і використовуватиметься лише в ньому (змінити — у /accounts).\n\n"
        "Тисни кнопку й обери акаунт (можна додати кілька — кожен прохід "
        "додає ще один). На екрані дозволів постав УСІ галочки: перегляд "
        "календаря, події календаря (для відповідей на запрошення), "
        "Gmail (читання і чернетки), Drive (читання). "
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
        domain = await get_active_domain(db, message.from_user.id)
        goal = await coach.create_goal(db, user_id=message.from_user.id,
                                       domain=domain, title=parts[1])
    import html as _html
    await message.answer(
        f"🎯 Ціль додано ({domain_label(domain)}): <b>{_html.escape(goal.title)}</b>\n"
        "Прогрес питатиму в неділю у тижневому звіті. Список: /goals")


@router.message(Command("goals"))
async def cmd_goals(message: Message) -> None:
    if not _is_owner(message):
        return
    from app.core import coach
    async with database.session() as db:
        domain = await get_active_domain(db, message.from_user.id)
        goals = await coach.list_goals(db, message.from_user.id, domain)
    if not goals:
        await message.answer(f"Активних цілей у домені {domain_label(domain)} "
                             "немає. Додати: <code>/goal текст</code>")
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
        domain = await get_active_domain(db, message.from_user.id)
        habit = await coach.create_habit(db, user_id=message.from_user.id,
                                         domain=domain, title=parts[1])
    import html as _html
    await message.answer(
        f"🏃 Звичка додана ({domain_label(domain)}): <b>{_html.escape(habit.title)}</b>\n"
        "Відмічай у /habits або ввечері в чек-іні.")


@router.message(Command("habits"))
async def cmd_habits(message: Message) -> None:
    if not _is_owner(message):
        return
    from app.core import coach
    async with database.session() as db:
        domain = await get_active_domain(db, message.from_user.id)
        overview = await coach.habits_overview(db, message.from_user.id, domain)
    if not overview:
        await message.answer(f"Звичок у домені {domain_label(domain)} ще немає. "
                             "Додати: <code>/habit назва</code>")
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


# ---------- wiki: compiled knowledge (R6) ----------

async def _wiki_build_job(user_id: int, domain: Domain, limit: int) -> None:
    """Background: compile indexed documents into wiki pages, with progress.

    §7: the domain is an IMMUTABLE snapshot captured when the job was launched.
    A /domain switch mid-compile does NOT change what this job compiles — it
    only ever touches `domain`'s documents and pages."""
    from app.core import wiki
    try:
        async with database.session() as db:
            docs = await wiki.pending_documents(db, user_id, domain, limit=limit)
        if not docs:
            await bot_instance.send_message(
                user_id, "📚 Нових документів для вікі немає — усе скомпільовано ✅")
            return
        await bot_instance.send_message(
            user_id, f"📚 Компілюю знання з {len(docs)} документів "
            "(створюю/оновлюю сторінки про партнерів, процеси, домовленості)…")
        created = updated = failed = 0
        quarantined = deferred = 0
        for i, doc in enumerate(docs, 1):
            try:
                async with database.session() as db:
                    doc = await db.merge(doc)
                    outcome = await wiki.compile_document(
                        db, user_id=user_id, document=doc, domain=domain)
                created += sum(1 for _s, st in outcome.pages if st == "created")
                updated += sum(1 for _s, st in outcome.pages if st == "updated")
                if outcome.status == "quarantined":
                    quarantined += 1
                elif outcome.status == "deferred_large":
                    deferred += 1
                elif outcome.status == "failed":
                    failed += 1
            except Exception:
                logger.exception("wiki compile failed: %s", doc.title[:60])
                failed += 1
            if i % 10 == 0:
                await bot_instance.send_message(
                    user_id, f"📚 {i}/{len(docs)} · нових сторінок {created}, "
                    f"оновлено {updated}…")
        async with database.session() as db:
            report = await wiki.lint(db, user_id, domain)
        extra = ""
        if quarantined:
            extra += f"\n🔒 У карантині (є секрети, не компілюю): {quarantined}."
        if deferred:
            extra += (f"\n📏 Завеликі — оброблено лише початок: {deferred}. "
                      "Лишились у черзі на повну компіляцію.")
        await bot_instance.send_message(
            user_id,
            f"📚 <b>Вікі оновлено.</b> Нових сторінок: {created} · оновлено: "
            f"{updated}" + (f" · невдалих: {failed}" if failed else "") + "\n"
            f"Усього: {report['total']} (сутностей {report['entities']}, "
            f"концепцій {report['concepts']}, архів {report['archives']})."
            + extra + "\nДивитись: /wiki · сторінка: /wiki ТОКО")
    except Exception:
        logger.exception("wiki build job failed")
        try:
            await bot_instance.send_message(
                user_id, "📚 Компіляція перервалась — запусти /wiki_build ще раз "
                "(зроблене збережено).")
        except Exception:
            pass


@router.message(Command("wiki_build"))
async def cmd_wiki_build(message: Message) -> None:
    if not _is_owner(message):
        return
    import asyncio as _asyncio
    from app.core import security
    async with database.session() as db:
        if not await security.scan_complete(db):
            await message.answer(
                "🔒 Спершу локальна перевірка бази: <b>/kb_security_scan</b>.\n"
                "Компіляція відправляє збережені документи в модель, тому вона "
                "заблокована, доки не перевірено, що в базі немає паролів і "
                "токенів. Скан локальний — жодних зовнішніх викликів.")
            return
    parts = (message.text or "").split()
    try:
        limit = min(max(int(parts[1]), 1), 200) if len(parts) > 1 else 40
    except ValueError:
        limit = 40
    domain = await _active_domain(message.from_user.id)
    await message.answer(
        f"📚 Стартую компіляцію знань домену <b>{domain_label(domain)}</b> "
        f"(до {limit} документів). Це кілька хвилин — відпишу прогрес. "
        "Повторний запуск бере наступну порцію.")
    _asyncio.create_task(_wiki_build_job(message.from_user.id, domain, limit))


@router.message(Command("wiki_lint"))
async def cmd_wiki_lint(message: Message) -> None:
    if not _is_owner(message):
        return
    from app.core import wiki
    async with database.session() as db:
        domain = await get_active_domain(db, message.from_user.id)
        r = await wiki.lint(db, message.from_user.id, domain)
    lines = [f"🧹 <b>Стан вікі ({domain_label(domain)}):</b> {r['total']} сторінок "
             f"(сутності {r['entities']} · концепції {r['concepts']} · "
             f"архів {r['archives']})"]
    if r["conflicts"]:
        lines.append("\n⚖️ <b>Суперечності:</b>\n" +
                     "\n".join(f" • {t}" for t in r["conflicts"]))
    if r["dupes"]:
        lines.append("\n👯 <b>Схожі сторінки (варто злити):</b>\n" +
                     "\n".join(f" • {t}" for t in r["dupes"]))
    if r["thin"]:
        lines.append("\n🪶 <b>Замало змісту:</b>\n" +
                     "\n".join(f" • {t}" for t in r["thin"][:5]))
    if r["no_source"]:
        lines.append("\n❓ <b>Без джерела:</b>\n" +
                     "\n".join(f" • {t}" for t in r["no_source"][:5]))
    if r.get("quarantined"):
        lines.append(f"\n🔒 <b>У карантині:</b> {r['quarantined']} сторінок — "
                     "у них знайдено секрети, тому їх не видно ні в пошуку, "
                     "ні моделі. Назви не показую навмисно.")
    if len(lines) == 1:
        lines.append("Проблем не знайшов 👌")
    await message.answer("\n".join(lines))


@router.message(Command("wiki"))
async def cmd_wiki(message: Message) -> None:
    if not _is_owner(message):
        return
    import html as _html
    from app.core import wiki
    parts = (message.text or "").split(maxsplit=1)
    async with database.session() as db:
        domain = await get_active_domain(db, message.from_user.id)
        if len(parts) < 2:
            index = await wiki.render_index(db, message.from_user.id, domain, limit=40)
            await message.answer(
                f"📚 <b>Вікі знань ({domain_label(domain)})</b>\n<pre>"
                + _html.escape(index[:3500]) + "</pre>\n"
                "Сторінка: <code>/wiki ТОКО</code> · оновити: /wiki_build · "
                "перевірка: /wiki_lint")
            return
        query = parts[1].strip()
        page = await wiki.find_page(db, message.from_user.id, domain, query)
        if page is None:
            found = await wiki.search_pages(db, message.from_user.id, domain,
                                            query, limit=6)
            if not found:
                await message.answer(
                    f"Сторінки «{_html.escape(query)}» немає. Спробуй /wiki_build "
                    "або просто спитай — пошукаю в документах.")
                return
            if len(found) == 1:
                page = found[0]
            else:
                listing = "\n".join(f" • {p.title} — <code>/wiki {p.slug}</code>"
                                    for p in found)
                await message.answer(f"Знайшов кілька сторінок:\n{listing}")
                return
        text = wiki.page_text(page)
    for i in range(0, len(text), 3800):
        await message.answer("📄 <pre>" + _html.escape(text[i:i + 3800]) + "</pre>")


@router.message(Command("order"))
async def cmd_order(message: Message) -> None:
    if not _is_owner(message):
        return
    from app.core import travelon
    parts = (message.text or "").split(maxsplit=1)
    if not travelon.configured():
        await message.answer("🧳 TravelON не підключено.")
        return
    if len(parts) < 2 or not parts[1].strip().lstrip("№").strip().isdigit():
        await message.answer("Формат: <code>/order 59266</code> — покажу заявку. "
                             "Або просто напиши «заявка 59266».")
        return
    order_no = parts[1].strip().lstrip("№").strip()
    await message.answer("🔎 Шукаю заявку…")
    try:
        order = await travelon.fetch_order(order_no)
    except Exception:
        logger.exception("order cmd failed")
        order = None
    import html as _html
    await message.answer(_html.escape(travelon.order_card(order)) if order
                         else f"Заявку №{order_no} не знайшов — перевір номер.")


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
    async with database.session() as db:
        text = await travelon.pulse_text(db)
    await message.answer(text or "Не вдалося отримати звіт, спробуй пізніше.")


async def _set_drive_account(user_id: int, cred_id: str) -> None:
    from app.models import AppState
    async with database.session() as db:
        state = await db.get(AppState, f"drive_acc_{user_id}") or AppState(
            key=f"drive_acc_{user_id}")
        state.value = cred_id
        db.add(state)
        await db.commit()


async def _drive_account(db, user_id: int, domain):
    """Selected (or only) Google account for Drive operations, in this domain."""
    from app.models import AppState
    accounts = await google_client.get_accounts(db, user_id, domain)
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
        domain = await get_active_domain(db, user_id)
        cred = await _drive_account(db, user_id, domain)
        if cred is None:
            await message.answer(
                f"Для домену {domain_label(domain)} немає Google-акаунтів — "
                "признач у /accounts або підключи: /connect_google")
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
        domain = await get_active_domain(db, message.from_user.id)
        accounts = await google_client.get_accounts(db, message.from_user.id, domain)
    if not accounts:
        await message.answer(
            f"Для домену {domain_label(domain)} немає Google-акаунтів — "
            "признач у /accounts або підключи: /connect_google")
        return
    if len(accounts) == 1:
        await _set_drive_account(message.from_user.id, str(accounts[0].id))
        await _show_drive_folders(message, message.from_user.id)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📧 {c.account_email}", callback_data=f"da:{c.id}")]
        for c in accounts])
    await message.answer("З якого акаунта індексуємо Drive?\n"
                         "<i>(або /drive_all — одразу все з усіх акаунтів)</i>",
                         reply_markup=kb)


_SCOPE_LABELS = (("calendar.readonly", "📆 календар"),
                 ("gmail.readonly", "✉️ пошта"),
                 ("gmail.compose", "📝 чернетки"),
                 ("drive.readonly", "📁 Drive"))


def _scope_status(scopes: str) -> tuple[str, bool]:
    """Human line of granted scopes + whether something is missing."""
    parts, missing = [], False
    for key, label in _SCOPE_LABELS:
        ok = key in (scopes or "")
        missing = missing or not ok
        parts.append(f"{label} {'✅' if ok else '❌'}")
    return "   " + " · ".join(parts), missing


async def _set_flag(key: str, value: str | None) -> None:
    from app.models import AppState
    async with database.session() as db:
        state = await db.get(AppState, key)
        if value is None:
            if state is not None:
                await db.delete(state)
        else:
            state = state or AppState(key=key)
            state.value = value
            db.add(state)
        await db.commit()


async def _index_all_drive(user_id: int, domain: Domain) -> None:
    """Background job: index every indexable file across this DOMAIN's Google
    accounts. Hash-dedupe makes re-runs cheap; progress lands in the owner chat.
    A running-flag survives restarts: if a deploy kills the job, the fresh
    container tells the owner to re-run instead of silent 'застиг'.

    §11/§13: `domain` is an immutable snapshot — the job only ever reaches this
    domain's accounts, and every ingested doc is stamped with it."""
    from app.core.ingest import IngestError, extract_text, ingest_document
    await _set_flag("drive_all_running", str(user_id))
    try:
        async with database.session() as db:
            accounts = await google_client.get_accounts(db, user_id, domain)
        grand = {"added": 0, "dups": 0, "failed": 0, "blocked": 0}
        for cred in accounts:
            async with database.session() as db:
                cred = await db.merge(cred)
                access = await google_client.access_for(db, cred)
            if not access:
                await bot_instance.send_message(
                    user_id, f"⚠️ {cred.account_email}: не зміг оновити доступ — "
                    "перепідключи /connect_google")
                continue
            try:
                files = await google_client.drive_list_all(
                    access, settings.drive_index_max)
            except google_client.DriveAccessError as e:
                if e.api_disabled:
                    await bot_instance.send_message(
                        user_id,
                        f"⚠️ {cred.account_email}: <b>Drive API вимкнений</b> у "
                        "Cloud-проєкті. Увімкни (один клік) і запусти /drive_all "
                        "знову:\nconsole.cloud.google.com/apis/library/"
                        "drive.googleapis.com")
                else:
                    await bot_instance.send_message(
                        user_id,
                        f"⚠️ {cred.account_email}: немає дозволу на Drive — "
                        "перепідключи /connect_google і постав галочку Drive.")
                continue
            except Exception:
                logger.exception("drive_list_all failed")
                await bot_instance.send_message(
                    user_id, f"⚠️ {cred.account_email}: не зміг прочитати Drive")
                continue
            await bot_instance.send_message(
                user_id, f"📧 {cred.account_email}: знайшов {len(files)} файлів, "
                "індексую…")
            added = dups = failed = blocked = 0
            for i, f in enumerate(files, 1):
                try:
                    # fast path: same file id + same modifiedTime -> no download
                    from sqlalchemy import select as _select
                    from app.models import Document as _Doc
                    async with database.session() as db:
                        prev = (await db.execute(_select(_Doc).where(
                            _Doc.user_id == user_id,
                            _Doc.source_type == "drive",
                            _Doc.source_ref == f["id"])
                            .limit(1))).scalar_one_or_none()
                    from app.core.ingest import INGEST_VERSION
                    if (prev is not None and f.get("modifiedTime")
                            and (prev.meta or {}).get("modifiedTime")
                            == f["modifiedTime"]
                            and (prev.meta or {}).get("v") == INGEST_VERSION):
                        dups += 1
                        continue
                    name, data = await google_client.drive_download_text_source(
                        access, f)
                    from app.core.ingest import (delete_stale_versions,
                                                 ingest_document_parts,
                                                 ingest_xlsx_by_sheets)
                    meta = {"modifiedTime": f.get("modifiedTime", ""),
                            "v": INGEST_VERSION}
                    async with database.session() as db:
                        if name.lower().endswith(".xlsx"):
                            results = await ingest_xlsx_by_sheets(
                                db, user_id=user_id, domain=domain, filename=name,
                                data=data, source_type="drive",
                                source_ref=f["id"], meta=meta)
                        else:
                            text = extract_text(name, data)
                            results = await ingest_document_parts(
                                db, user_id=user_id, domain=domain, title=name,
                                text=text, source_type="drive",
                                source_ref=f["id"], meta=meta)
                        keep = {r.document.id for r in results
                                if r.document is not None}
                        if keep:  # drop truncated/first-tab/stale leftovers
                            await delete_stale_versions(
                                db, user_id=user_id, domain=domain,
                                source_ref=f["id"], keep_doc_ids=keep)
                    statuses = [r.status for r in results]
                    if "indexed" in statuses:
                        added += 1
                    elif "quarantined" in statuses:
                        blocked += 1
                    elif "duplicate" in statuses:
                        dups += 1
                    else:
                        failed += 1
                except IngestError as e:
                    failed += 1
                    logger.warning("drive file skipped: %s — %s",
                                   f.get("name", "?"), e)
                except Exception:
                    failed += 1
                    logger.exception("drive file failed: %s", f.get("name", "?"))
                if i % 25 == 0:
                    try:
                        await bot_instance.send_message(
                            user_id, f"🗂 {cred.account_email}: {i}/{len(files)} "
                            f"(нових {added})…")
                    except Exception:
                        logger.exception("progress send failed")
            grand["added"] += added
            grand["dups"] += dups
            grand["failed"] += failed
            grand["blocked"] += blocked
            await bot_instance.send_message(
                user_id, f"📧 <b>{cred.account_email}</b>: файлів {len(files)} · "
                f"додано {added} · вже було {dups} · пропущено {failed}"
                + (f" · 🔒 в карантині {blocked}" if blocked else ""))
        await _set_flag("drive_all_running", None)
        await bot_instance.send_message(
            user_id,
            f"🗂 <b>Індексація Drive завершена.</b> Нових документів: "
            f"{grand['added']} (дублікатів {grand['dups']})."
            + (f"\n🔒 У карантині: {grand['blocked']} — там паролі/токени, "
               "їх не збережено і не проіндексовано."
               if grand["blocked"] else "")
            + "\nТепер просто питай: «де договір з …», «які умови в …», "
              "«що в файлі …» — знайду і процитую джерело. Паролів і токенів "
              "у мене немає за архітектурою — тримай їх у менеджері паролів.")
    except Exception:
        logger.exception("drive_all failed")
        try:
            await _set_flag("drive_all_running", None)
            await bot_instance.send_message(
                user_id, "🗂 Індексація перервалась помилкою — запусти "
                "/drive_all ще раз (все, що встиг, уже збережено).")
        except Exception:
            pass


@router.message(Command("drive_all"))
async def cmd_drive_all(message: Message) -> None:
    if not _is_owner(message):
        return
    import asyncio as _asyncio
    async with database.session() as db:
        domain = await get_active_domain(db, message.from_user.id)
        accounts = await google_client.get_accounts(db, message.from_user.id, domain)
    if not accounts:
        await message.answer(
            f"Для домену {domain_label(domain)} немає Google-акаунтів — "
            "признач у /accounts або підключи: /connect_google")
        return
    await message.answer(
        f"🗂 Стартую повну індексацію Drive домену <b>{domain_label(domain)}</b> "
        f"для {len(accounts)} акаунт(ів): Google Docs, Google Sheets (УСІ "
        "вкладки), pdf/docx/xlsx/txt/md/csv, "
        f"до {settings.drive_index_max} файлів на акаунт (найновіші перші). "
        "Це кілька хвилин — відпишу прогрес. Повторний запуск безпечний: "
        "дублікати пропускаються.")
    _asyncio.create_task(_index_all_drive(message.from_user.id, domain))


@router.message(Command("accounts"))
async def cmd_accounts(message: Message) -> None:
    if not _is_owner(message):
        return
    # account MANAGEMENT surface — ALL accounts, including unassigned (§11)
    async with database.session() as db:
        accounts = await google_client.get_all_accounts(db, message.from_user.id)
    if not accounts:
        await message.answer("Підключених Google-акаунтів немає. Додати: /connect_google")
        return
    any_missing = False
    rows_text, kb_rows = [], []
    for c in accounts:
        status_line, missing = _scope_status(c.scopes)
        any_missing = any_missing or missing
        dom_txt = domain_label(c.domain) if c.domain else "⚪️ не призначено"
        rows_text.append(f" • <b>{c.account_email}</b> — {dom_txt}\n{status_line}")
        kb_rows.append([InlineKeyboardButton(
            text=("● " if c.domain == d.value else "○ ") + d.value,
            callback_data=f"gm:{c.id}:{d.value}") for d in Domain])
        kb_rows.append([InlineKeyboardButton(
            text=f"❌ Відключити {c.account_email[:24]}",
            callback_data=f"ga:{c.id}")])
    listing = "\n".join(rows_text)
    hint = ("\n\n⚠️ Де ❌ — дозволу немає, ця функція для акаунта не працює. "
            "Виправити: /connect_google, обери ЦЕЙ акаунт і постав УСІ галочки."
            if any_missing else "")
    await message.answer(
        f"📧 <b>Google-акаунти ({len(accounts)}):</b>\n{listing}\n\n"
        "Кнопки: признач домен акаунту (● активний) або відключи. Акаунт без "
        "домену не використовує жоден інструмент, доки не призначиш."
        f"{hint}\n\n➕ Додати ще один: /connect_google",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


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
    elif status == "blocked_secret":
        await message.answer(
            "🔒 У цьому листі є технічний секрет (API-ключ / токен), тому я не "
            "відправляв його в модель і чернетку не складав. Відповідай на "
            "нього вручну.")
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
        quarantined = (await db.execute(
            select(func.count()).select_from(Document)
            .where(Document.user_id == message.from_user.id,
                   Document.status == "quarantined"))).scalar_one()
    if not total:
        await message.answer(
            "📚 База знань порожня. Перешли мені документ (pdf, docx, txt, md) "
            "або будь-яке повідомлення — і я запам'ятаю.")
        return
    lines = [f"📚 <b>База знань:</b> {total} документ(ів). Останні:"]
    lines += [(" • 🔒 " if d.status == "quarantined" else " • ")
              + f"{d.title} ({d.chunk_count} фр., {d.created_at.strftime('%d.%m')})"
              for d in docs]
    if quarantined:
        lines.append(f"\n🔒 У карантині: {quarantined} — там паролі/токени, "
                     "у пошук вони не потрапляють.")
    await message.answer("\n".join(lines))


# ---------- local security scan of the existing base (R6.1A) ----------

async def _security_scan_job(user_id: int) -> None:
    """Owner-run, local, bounded. Never calls OpenAI/Anthropic/web/connectors."""
    from app.core import security_scan
    try:
        async with database.session() as db:
            report = await security_scan.run_scan(db, user_id=user_id)
        await bot_instance.send_message(user_id,
                                        security_scan.report_text(report))
        if report.completed and not report.affected:
            await bot_instance.send_message(
                user_id, "Автокомпіляцію вікі тепер можна вмикати: постав "
                "<code>AUTO_WIKI_COMPILE_ENABLED=true</code> у змінних "
                "Railway. /wiki_build уже доступний.")
    except Exception:
        logger.exception("kb security scan failed")
        try:
            await bot_instance.send_message(
                user_id, "🔒 Скан перервався помилкою. Позначку «перевірено» "
                "НЕ поставлено — запусти /kb_security_scan ще раз (він "
                "ідемпотентний, повторний запуск безпечний).")
        except Exception:
            pass


@router.message(Command("kb_quarantine"))
async def cmd_kb_quarantine(message: Message) -> None:
    """Owner-only: WHICH sources are quarantined (titles/dates/categories,
    never content) — the walk-list for rotating credentials."""
    if not _is_owner(message):
        return
    from app.core import security_scan
    async with database.session() as db:
        listing = await security_scan.quarantine_listing(
            db, message.from_user.id)
    for chunk in security_scan.quarantine_text(listing):
        await message.answer(chunk)


@router.message(Command("kb_security_scan"))
async def cmd_kb_security_scan(message: Message) -> None:
    if not _is_owner(message):
        return
    import asyncio as _asyncio
    await message.answer(
        "🔒 Стартую локальну перевірку бази знань на паролі/токени/ключі.\n"
        "Це детермінований код на цій же машині — жодного виклику до "
        "OpenAI, Anthropic, вебу чи конекторів. Нічого не видаляю: знайдене "
        "переводжу в карантин (можна повернути). Відпишу лише цифрами.")
    _asyncio.create_task(_security_scan_job(message.from_user.id))


# ---------- knowledge intake: files & forwards ----------

ALLOWED_DOC_EXT = (".pdf", ".docx", ".txt", ".md", ".vtt", ".srt", ".csv", ".xlsx")

# Says what happened and what to do; never quotes what triggered it.
_QUARANTINE_MSG = (
    "🔒 <b>{name}</b> — у карантині: у файлі є технічний секрет "
    "(API-ключ / токен / приватний ключ).\n"
    "Текст НЕ збережено, НЕ проіндексовано і нікуди не відправлено.\n\n"
    "Логіни й паролі до кабінетів партнерів я зберігаю нормально — блокуються "
    "лише технічні ключі. Прибери такий ключ із файлу й надішли ще раз, або "
    "тримай його там, де він виданий.")


async def _compile_one(message: Message, document, domain) -> None:
    """Compile a freshly ingested document into wiki pages (best effort).

    Three conditions, all required (R6.1A §9): the feature flag is on, the
    local security scan of the base has completed, and this document is not
    quarantined. Autonomous compilation was how one credential spreadsheet
    became five permanent pages — it does not restart by default.
    """
    if document is None:
        return
    if not settings.auto_wiki_compile_enabled:
        return
    from app.core import security, wiki
    if getattr(document, "status", "") == "quarantined":
        return
    try:
        async with database.session() as db:
            if not await security.scan_complete(db):
                return
            document = await db.merge(document)
            if document.status == "quarantined":
                return
            outcome = await wiki.compile_document(
                db, user_id=message.from_user.id, document=document, domain=domain)
    except Exception:
        logger.exception("auto wiki compile failed")
        return
    if outcome.pages:
        created = [s for s, st in outcome.pages if st == "created"]
        updated = [s for s, st in outcome.pages if st == "updated"]
        bits = []
        if created:
            bits.append("нові сторінки: " + ", ".join(created[:4]))
        if updated:
            bits.append("оновлено: " + ", ".join(updated[:4]))
        await message.answer("📚 Вікі: " + " · ".join(bits) + " (/wiki)")


async def _handle_transcript_file(message: Message, name: str, text: str) -> None:
    import html as _html
    await message.answer("📝 Читаю транскрипт і готую підсумок…")
    async with database.session() as db:
        out = await orch.handle_transcript(
            db, user_id=message.from_user.id, title=name, text=text,
            source_ref=name)
    if out["ingest"].status == "duplicate":
        await message.answer("📚 Цей транскрипт уже розібраний раніше ✅")
        return
    if out["ingest"].status != "indexed":
        await message.answer("📄 Не вдалося обробити транскрипт, спробуй ще раз.")
        return
    digest = out["digest"]
    if not digest:
        await message.answer(
            f"📚 Додав транскрипт у базу знань ({out['ingest'].chunks} фр.), "
            "але підсумок скласти не вдалося — можеш спитати мене про зміст.")
        return
    lines = [f"📝 <b>Підсумок зустрічі</b> · {_html.escape(name)}",
             _html.escape(digest["summary"])]
    if digest["decisions"]:
        lines.append("\n✅ <b>Рішення:</b>")
        lines += [f" • {_html.escape(d)}" for d in digest["decisions"]]
    others = [a for a in digest["actions"] if a["who"] != "me"]
    if others:
        lines.append("\n👥 <b>Домовленості інших:</b>")
        lines += [f" • {_html.escape(a['who_name'] or 'Хтось')}: "
                  f"{_html.escape(a['title'])}" for a in others]
    lines.append(f"\n📚 У базі знань ({out['ingest'].chunks} фр.) — можеш питати про зміст.")
    await message.answer("\n".join(lines))
    for p in out["proposals"]:
        await message.answer(proposal_card(p), reply_markup=_proposal_kb(p.id, p.version))


@router.message(F.document)
async def on_document(message: Message) -> None:
    if not _is_owner(message):
        return
    from app.core import meetings
    from app.core.ingest import IngestError, extract_text, ingest_document
    doc = message.document
    name = (doc.file_name or "file").strip()
    if not name.lower().endswith(ALLOWED_DOC_EXT):
        await message.answer("Підтримую pdf, docx, xlsx, csv, txt, md і "
                             "транскрипти vtt/srt — цей формат поки ні.")
        return
    if doc.file_size and doc.file_size > 15 * 1024 * 1024:
        await message.answer("Файл завеликий (ліміт 15 МБ)")
        return
    domain = await _active_domain(message.from_user.id)   # request-start snapshot
    try:
        file = await message.bot.get_file(doc.file_id)
        url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file.file_path}"
        async with httpx.AsyncClient(timeout=120) as client:
            data = (await client.get(url)).content
        if name.lower().endswith(".xlsx"):
            from app.core.ingest import ingest_xlsx_by_sheets
            async with database.session() as db:
                results = await ingest_xlsx_by_sheets(
                    db, user_id=message.from_user.id, domain=domain, filename=name,
                    data=data, source_type="telegram_file", source_ref=name)
            indexed = sum(1 for r in results if r.status == "indexed")
            dups = sum(1 for r in results if r.status == "duplicate")
            blocked = sum(1 for r in results if r.status == "quarantined")
            await message.answer(
                f"📚 Таблиця <b>{name}</b>: проіндексував {indexed} аркуш(ів)"
                + (f", вже було {dups}" if dups else "")
                + ". Тепер можеш питати про її зміст."
                + (f"\n\n🔒 Аркушів у карантині: {blocked} — там паролі/токени. "
                   "Їх не збережено і не проіндексовано. Тримай доступи в "
                   "менеджері паролів." if blocked else ""))
            return
        text = extract_text(name, data)
        if meetings.looks_like_transcript(name):
            await _handle_transcript_file(message, name, text)
            return
        async with database.session() as db:
            result = await ingest_document(
                db, user_id=message.from_user.id, domain=domain, title=name,
                text=text, source_type="telegram_file", source_ref=name)
    except IngestError as e:
        await message.answer(f"📄 {e}")
        return
    except Exception:
        logger.exception("document ingest failed")
        await message.answer("📄 Не вдалося обробити файл, спробуй ще раз.")
        return
    if result.status == "quarantined":
        await message.answer(_QUARANTINE_MSG.format(name=name))
    elif result.status == "duplicate":
        await message.answer(f"📚 «{name}» вже є в базі знань ✅")
    else:
        await message.answer(
            f"📚 Додав у базу знань ({domain_label(domain)}): <b>{name}</b> "
            f"({result.chunks} фрагментів).\n"
            "Тепер можеш просто спитати мене про його зміст.")
        await _compile_one(message, result.document, domain)


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
    domain = await _active_domain(message.from_user.id)   # request-start snapshot
    try:
        async with database.session() as db:
            result = await ingest_document(
                db, user_id=message.from_user.id, domain=domain,
                title=f"{title} ({message.date.strftime('%d.%m.%Y')})",
                text=content, source_type="telegram_forward",
                source_ref=str(message.message_id))
    except Exception:
        logger.exception("forward ingest failed")
        await message.answer("Не вдалося зберегти пересилку.")
        return
    if result.status == "quarantined":
        await message.answer(_QUARANTINE_MSG.format(name=title))
    elif result.status == "duplicate":
        await message.answer("📚 Це вже є в базі знань ✅")
    elif result.status == "indexed":
        await message.answer(f"📚 Зберіг у базу знань ({title.lower()})")
    else:
        await message.answer("Не вийшло проіндексувати цей текст.")


# ---------- notes: text & voice ----------

_CAL_ACTION_LABEL = {"decline": ("Скасувати участь", "🙅"),
                     "accept": ("Підтвердити участь", "🙋"),
                     "tentative": ("Позначити «можливо»", "🤔")}


def _cal_when(start_str: str) -> str:
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _zi
    try:
        return _dt.fromisoformat(start_str).astimezone(
            _zi(settings.tz_name)).strftime("%d.%m %H:%M")
    except ValueError:
        return start_str[:10]


async def _send_cal_action_cards(message: Message, actions: list) -> None:
    import html as _html
    for p in actions:
        label, emoji = _CAL_ACTION_LABEL.get(p.action, ("Змінити участь", "📅"))
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"✅ {label}", callback_data=f"cr:{p.id}"),
            InlineKeyboardButton(text="❌ Ні", callback_data=f"cx:{p.id}"),
        ]])
        await message.answer(
            f"{emoji} <b>{label}?</b>\n"
            f"📅 {_html.escape(p.summary)}\n🕐 {_cal_when(p.start_str)}\n\n"
            "<i>Організатора буде повідомлено — як при відповіді в Google "
            "Calendar. Нічого не зміню без підтвердження.</i>",
            reply_markup=kb)


async def _send_cal_create_card(message: Message, pending, accounts: list) -> None:
    import html as _html
    from zoneinfo import ZoneInfo as _zi
    tz = _zi(settings.tz_name)
    start = pending.start_at.astimezone(tz)
    end = pending.end_at.astimezone(tz)
    when = f"{start.strftime('%a %d.%m %H:%M')}–{end.strftime('%H:%M')}"
    if len(accounts) == 1:
        rows = [[InlineKeyboardButton(text="✅ Створити подію",
                                      callback_data=f"ce:{pending.id}:0")]]
    else:
        rows = [[InlineKeyboardButton(text=f"📅 У {email}",
                                      callback_data=f"ce:{pending.id}:{i}")]
                for i, email in accounts[:3]]
    rows.append([InlineKeyboardButton(text="❌ Не треба",
                                      callback_data=f"cq:{pending.id}")])
    await message.answer(
        f"➕ <b>Нова подія в календарі?</b>\n"
        f"📅 {_html.escape(pending.title)}\n🕐 {when}\n\n"
        "<i>Без запрошених — лише твій календар. Нічого не створю без "
        "підтвердження.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _voice_enabled(user_id: int) -> bool:
    from app.models import AppState
    async with database.session() as db:
        state = await db.get(AppState, f"voice_{user_id}")
    return state.value != "off" if state else True  # default on


@router.message(Command("voice"))
async def cmd_voice(message: Message) -> None:
    if not _is_owner(message):
        return
    from app.models import AppState
    user_id = message.from_user.id
    async with database.session() as db:
        state = await db.get(AppState, f"voice_{user_id}") or AppState(
            key=f"voice_{user_id}", value="on")
        state.value = "off" if state.value != "off" else "on"
        db.add(state)
        await db.commit()
        now_on = state.value == "on"
    await message.answer(
        "🔊 Голосові відповіді УВІМКНЕНО — на голосове відповім і текстом, "
        "і голосом (короткі відповіді)." if now_on else
        "🔇 Голосові відповіді вимкнено — відповідатиму лише текстом. "
        "Увімкнути знову: /voice")


async def _process_note(message: Message, text: str, prefix: str = "",
                        want_voice: bool = False) -> None:
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
    if outcome.kind == "blocked":
        # nothing was stored, nothing was sent to a model, nothing is echoed
        await message.answer(outcome.reply or "")
        return
    if outcome.kind == "proposal" and outcome.proposal:
        await message.answer(prefix + proposal_card(outcome.proposal),
                             reply_markup=_proposal_kb(outcome.proposal.id,
                                                       outcome.proposal.version))
        return
    if outcome.kind == "cal_actions" and outcome.cal_actions:
        await _send_cal_action_cards(message, outcome.cal_actions)
        return
    if outcome.kind == "cal_create" and outcome.cal_create:
        await _send_cal_create_card(message, outcome.cal_create,
                                    outcome.cal_accounts or [])
        return
    if outcome.kind == "new_draft" and outcome.draft:
        import html as _html
        d = outcome.draft
        accounts = outcome.cal_accounts or []
        if len(accounts) <= 1:
            rows = [[InlineKeyboardButton(text="💾 Створити чернетку в Gmail",
                                          callback_data=f"dm:{d.id}")]]
        else:
            rows = [[InlineKeyboardButton(text=f"💾 З {email}",
                                          callback_data=f"de:{d.id}:{i}")]
                    for i, email in accounts[:3]]
        rows.append([InlineKeyboardButton(text="❌ Відхилити",
                                          callback_data=f"dx:{d.id}")])
        await message.answer(
            f"📧 <b>Новий лист (чернетка)</b>\n<b>Кому:</b> {_html.escape(d.to_addr)}\n"
            f"<b>Тема:</b> {_html.escape(d.subject)}\n\n{_html.escape(d.body[:2500])}\n\n"
            "<i>Нічого не надсилаю — створю лише чернетку в Gmail, "
            "надішлеш сам.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return
    import html as _html
    if outcome.kind == "note":
        await message.answer(
            f"{prefix}🧠 Запам'ятав (кандидат): {_html.escape(outcome.reply or '')}")
        return
    safe = _html.escape(outcome.reply or "Записав ✅")
    for i in range(0, len(safe), 3900):  # Telegram message limit
        await message.answer((prefix if i == 0 else "") + safe[i:i + 3900])
    if want_voice and outcome.kind == "chat":
        from app.core import tts
        if tts.should_speak(outcome.reply or "", True):
            audio = await tts.synthesize(outcome.reply)
            if audio:
                from aiogram.types import BufferedInputFile
                try:
                    await message.answer_voice(
                        BufferedInputFile(audio, filename="dan_os.ogg"))
                except Exception:
                    logger.exception("voice reply send failed")


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
    from app.core import security
    # HONEST LIMIT (R6.1A.1): the audio ALREADY went to the STT provider before
    # this line — DAN.OS cannot scan speech it has not transcribed yet. What the
    # gate can still do is stop the TEXT from being echoed, stored, embedded or
    # sent to the chat model. See docs/DECISIONS.md «voice STT exception».
    if security.scan(text).blocked:
        await message.answer(
            "🎙 Розшифрував, але у сказаному є технічний секрет "
            "(ключ/токен), тому я не показую текст і нічого не зберігаю.\n\n"
            "⚠️ Врахуй: аудіо вже пішло в сервіс розпізнавання — секрети "
            "краще не диктувати взагалі.")
        return
    await _process_note(message, text,
                        prefix=f"🎙 <i>Розчув:</i> {_html.escape(text)}\n\n",
                        want_voice=await _voice_enabled(message.from_user.id))


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


# Registered AFTER every Command() handler and BEFORE the media catch-all:
# aiogram matches in registration order, so a real command still wins here.
_UNKNOWN_CMD_RE = re.compile(r"^/([A-Za-z0-9_]+)")

_COMMAND_HELP = (
    "<b>Домени:</b> /domain (перемкнути особисте / travelon / tech) · "
    "/domain_audit\n"
    "<b>Задачі й день:</b> /today · /brief · /checkin · /goal · /goals · "
    "/habit · /habits\n"
    "<b>Знання:</b> /kb · /wiki · /wiki_build · /wiki_lint · /drive · "
    "/drive_all · /kb_security_scan · /kb_quarantine\n"
    "<b>Пошта й календар:</b> /reply · /accounts · /connect_google\n"
    "<b>Бізнес:</b> /travelon · /order\n"
    "<b>Інше:</b> /app · /voice · /start"
)
# Names people reach for that are HTTP endpoints of the service, not commands
_HTTP_ENDPOINT_NAMES = ("health", "healthz", "ready", "live", "metrics",
                        "status", "ping")


@router.message(F.text.startswith("/"))
async def on_unknown_command(message: Message) -> None:
    """An unknown command is not «media I cannot read».

    The media catch-all used to swallow these, so an obvious typo — or /health,
    which is a URL — produced an answer about photos. Say what happened and
    show what actually exists.
    """
    if not _is_owner(message):
        return
    import html as _html
    match = _UNKNOWN_CMD_RE.match(message.text or "")
    name = match.group(1) if match else ""
    if name.lower() in _HTTP_ENDPOINT_NAMES:
        base = settings.public_url or "https://<домен>"
        await message.answer(
            f"ℹ️ <code>/{_html.escape(name)}</code> — це HTTP-ендпоінт сервісу, "
            "а не команда бота.\n"
            f"Відкрий у браузері: <code>{base}/health/live</code> (версія "
            f"збірки) або <code>{base}/health/ready</code> (база, вебхук, стан "
            "security-скану).\n\n<b>Команди бота:</b>\n" + _COMMAND_HELP)
        return
    await message.answer(
        f"🤔 Команди <code>/{_html.escape(name)}</code> у мене немає.\n\n"
        "<b>Ось що є:</b>\n" + _COMMAND_HELP)


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
    domain = await _active_domain(user_id)   # request-start snapshot
    try:
        async with database.session() as db:
            cred = await _drive_account(db, user_id, domain)
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
                    db, user_id=user_id, domain=domain, title=name, text=text,
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
    if action == "dom":  # domain-switch button — value is not a UUID
        try:
            target = parse_domain(parts[1]) if len(parts) > 1 else None
        except Exception:
            target = None
        if target is None:
            await cb.answer("Невідомий домен")
            return
        await _switch_domain(cb, user_id, target)
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
    if action == "gm":  # assign a Google account to a domain (owner-only, §11)
        try:
            target = parse_domain(parts[2]) if len(parts) > 2 else None
        except Exception:
            target = None
        if target is None:
            await cb.answer("Невідомий домен")
            return
        from app.models import GoogleCredential
        async with database.session() as db:
            cred = await db.get(GoogleCredential, ref)
            if cred and cred.user_id == user_id:
                old = cred.domain
                cred.domain = target.value
                from app.core.audit import audit
                await audit(db, actor=f"user:{user_id}",
                            action="google.domain_assigned",
                            resource_type="connector", resource_id=cred.id,
                            policy_level="L2", email=cred.account_email,
                            old_domain=old or "", new_domain=target.value)
                await db.commit()
                await cb.answer(f"{cred.account_email} → {target.value} ✅",
                                show_alert=True)
                try:
                    await cb.message.edit_text(
                        f"✅ {cred.account_email} → домен {domain_label(target)}.\n"
                        "Керувати рештою: /accounts")
                except Exception:
                    pass
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
                elif status == "wrong_domain":
                    await cb.answer("Ця чернетка в іншому домені. Перемкни "
                                    "/domain і підтверди там.", show_alert=True)
                else:
                    await cb.answer(str(status))
            elif action == "de":  # compose-new draft: pick account, then create
                idx = int(parts[2]) if len(parts) > 2 else 0
                status = await orch.set_draft_account(
                    db, user_id=user_id, draft_id=ref, account_index=idx)
                if status == "ok":
                    status = await orch.approve_draft(db, user_id=user_id,
                                                      draft_id=ref)
                if status in ("created", "already"):
                    await cb.message.edit_reply_markup(reply_markup=None)
                    await cb.answer("Чернетка в Gmail ✅ (нічого не надіслано)",
                                    show_alert=True)
                elif status == "no_google":
                    await cb.answer("Онови доступ: /connect_google", show_alert=True)
                elif status == "wrong_domain":
                    await cb.answer("Ця чернетка в іншому домені. Перемкни "
                                    "/domain і підтверди там.", show_alert=True)
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
            elif action == "cr":
                status = await orch.confirm_cal_action(db, user_id=user_id,
                                                       action_id=ref)
                if status in ("done", "already"):
                    first = (cb.message.text or "").split("\n")[1] if "\n" in (
                        cb.message.text or "") else ""
                    await cb.message.edit_text(f"✅ Готово: {first}\n"
                                               "Участь оновлено, організатор отримає "
                                               "сповіщення.")
                    await cb.answer("Зроблено ✅")
                elif status == "not_attendee":
                    await cb.message.edit_reply_markup(reply_markup=None)
                    await cb.answer("Ти не в списку учасників цієї події (можливо, "
                                    "ти організатор) — змінити RSVP не можу.",
                                    show_alert=True)
                elif status in ("no_google", "no_scope"):
                    await cb.answer("Бракує прав на події календаря — перепідключи: "
                                    "/connect_google (постав усі галочки)",
                                    show_alert=True)
                elif status == "wrong_domain":
                    await cb.answer("Ця подія в іншому домені. Перемкни активний "
                                    "домен (/domain) і підтверди там.",
                                    show_alert=True)
                else:
                    await cb.answer(str(status))
            elif action == "cx":
                await orch.reject_cal_action(db, user_id=user_id, action_id=ref)
                await cb.message.edit_reply_markup(reply_markup=None)
                await cb.answer("Скасовано, нічого не міняв")
            elif action == "ce":
                idx = int(parts[2]) if len(parts) > 2 else 0
                status, email = await orch.confirm_cal_create(
                    db, user_id=user_id, create_id=ref, account_index=idx)
                if status == "done":
                    lines = (cb.message.text or "").split("\n")
                    what = lines[1] if len(lines) > 1 else "подію"
                    when = lines[2] if len(lines) > 2 else ""
                    await cb.message.edit_text(
                        f"✅ Створено: {what}\n{when}\n📧 {email}")
                    await cb.answer("Подія в календарі ✅")
                elif status == "already":
                    await cb.message.edit_reply_markup(reply_markup=None)
                    await cb.answer("Уже створено раніше ✅")
                elif status in ("no_google", "no_scope"):
                    await cb.answer("Бракує прав на події — /connect_google "
                                    "(постав усі галочки)", show_alert=True)
                elif status == "wrong_domain":
                    await cb.answer(f"Ця подія в домені «{email}». Перемкни: "
                                    f"/domain {email} — і підтверди там.",
                                    show_alert=True)
                else:
                    await cb.answer(str(status))
            elif action == "cq":
                await orch.reject_cal_create(db, user_id=user_id, create_id=ref)
                await cb.message.edit_reply_markup(reply_markup=None)
                await cb.answer("Добре, не створюю")
            elif action == "hb":
                from app.core import coach
                from app.models import Habit
                status = await coach.toggle_habit(db, user_id=user_id, habit_id=ref)
                if status in ("done", "undone"):
                    hb = await db.get(Habit, ref)
                    hdomain = parse_domain(hb.domain) if hb else Domain.PERSONAL
                    overview = await coach.habits_overview(db, user_id, hdomain)
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
