"""English coach (R7): a 12-minute daily habit with memory.

Why this is a system and not a prompt. One-shot «act as a language teacher»
prompts produce a good lesson and forget it. What actually moves a B1 speaker
is the opposite: a small session every day, built around the phrases HE keeps
losing and the mistakes HE actually made — decided by the system, so the
learner never has to choose. Hence: a curriculum position, spaced repetition,
and a mistake log that rebuilds next week.

Personal-development data: everything here is domain='personal' and the coach
is reachable only in that domain — symmetric with TravelON tools being
travelon-only. Learning never leaks into a business answer.
"""
from __future__ import annotations

import html as _html
import json
import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import security
from app.core.audit import audit
from app.models import EnglishItem, EnglishProfile, EnglishSession

logger = logging.getLogger(__name__)

DOMAIN = "personal"
TALK_CORRECT_EVERY = 3       # correction block after every N learner turns
TALK_IDLE_MINUTES = 90       # a forgotten conversation closes itself
DEFAULT_MINUTES = 12


# ───────────────────────── curriculum ─────────────────────────
# Twelve weeks, ordered by what a tour-operator owner actually hits: partners
# first (that is where money and friction live), then written work, then IT,
# then everyday. Each week is one theme, one grammar focus, and phrases he
# would really say — not textbook sentences.

@dataclass(frozen=True)
class Week:
    no: int
    title: str
    scenario: str            # negotiation | email | it | travel
    grammar: str
    why: str                 # what changes for him when this clicks
    phrases: tuple[tuple[str, str, str], ...]   # (english, ukrainian, trap/hook)
    task: str                # the speaking task for the week


CURRICULUM: tuple[Week, ...] = (
    Week(1, "Перше знайомство і small talk перед справою", "negotiation",
         "Present Simple vs Present Continuous",
         "Перші 60 секунд дзвінка перестають бути найважчими.",
         (("We work mainly with Turkey and Egypt.", "Ми працюємо переважно з Туреччиною та Єгиптом.",
           "Не «we are working» — це про постійну діяльність, не про зараз."),
          ("How long have you been with the company?", "Скільки ви вже в компанії?",
           "Класичний small-talk міст до справи."),
          ("Thanks for making the time.", "Дякую, що знайшли час.",
           "Природніше за «thanks for your time» на початку."),
          ("Let me give you some background.", "Дозвольте коротко ввести в курс.",
           "Так відкривають контекст, а не «I will tell you the situation»."),
          ("Before we start — can you hear me all right?", "Перед початком — мене добре чути?",
           "«All right» тут природніше за «good»."),
          ("We've been in the market since 2015.", "Ми на ринку з 2015 року.",
           "Present Perfect Continuous: почалось тоді, триває досі."),
          ("What's your role on the account?", "Яка ваша роль по цьому напрямку?",
           "«Account» = клієнт/напрямок, не «рахунок»."),
          ("I'll keep it short.", "Буду стисло.", "Ввічливий сигнал, що ти цінуєш їхній час.")),
         "Проведи 2-хвилинне знайомство: хто ти, чим займається компанія, чому дзвониш."),

    Week(2, "Ціни й умови: питати і давати", "negotiation",
         "Questions: word order and indirect questions",
         "Ти перестаєш звучати різко, коли питаєш про гроші.",
         (("Could you walk me through the pricing?", "Можете розібрати зі мною ціноутворення?",
           "«Walk me through» — м'якше за «explain»."),
          ("What does that include?", "Що туди входить?",
           "Не «what is included there» — коротше і природніше."),
          ("Is that per person or per room?", "Це за особу чи за номер?",
           "Питання, яке рятує від дорогих непорозумінь."),
          ("We'd be looking at around 30 rooms a week.", "Ми розраховуємо приблизно на 30 номерів на тиждень.",
           "«We'd be looking at» — необов'язкова оцінка, не зобов'язання."),
          ("Could you send that over in writing?", "Можете надіслати це письмово?",
           "«Send over» — стандарт для документів."),
          ("What's your best rate for that volume?", "Яка ваша найкраща ціна за такий обсяг?",
           "Прямо, але не грубо — саме так і питають."),
          ("I'd like to understand how you got to that number.",
           "Хочу зрозуміти, як ви отримали цю цифру.",
           "Indirect: ввічливий спосіб сказати «поясніть»."),
          ("Does that price hold until the end of the season?",
           "Ця ціна тримається до кінця сезону?",
           "«Hold» про ціну = лишається чинною.")),
         "Запитай партнера про ціну на 30 номерів і з'ясуй, що входить."),

    Week(3, "Торг: ввічливо тиснути й не поступатись", "negotiation",
         "Conditionals 1 and 2 (if we…, we would…)",
         "З'являється інструмент торгу замість «yes» або мовчання.",
         (("If we commit to the full season, could you improve the rate?",
           "Якщо ми законтрактуємо весь сезон, зможете покращити ціну?",
           "1st conditional — реальна пропозиція."),
          ("That's higher than we budgeted.", "Це вище, ніж ми закладали.",
           "Факт, не звинувачення. Дуже дієво."),
          ("I'm afraid that doesn't work for us.", "Боюся, це нам не підходить.",
           "«I'm afraid» пом'якшує тверде «ні»."),
          ("What would it take to get to 45?", "Що потрібно, щоб вийти на 45?",
           "Переводить розмову з «ні» на «як»."),
          ("We'd need something in return.", "Нам потрібно щось натомість.",
           "Коротко позначає, що поступка має ціну."),
          ("Let's park that and come back to it.", "Відкладімо це і повернемось пізніше.",
           "«Park» — професійний спосіб не сваритись зараз."),
          ("That's our final position on the deposit.", "Це наша остаточна позиція щодо депозиту.",
           "«Position» звучить твердо, але не агресивно."),
          ("If you could do 40, we'd sign this week.",
           "Якби ви дали 40, ми підписали б цього тижня.",
           "2nd conditional — гіпотетичний обмін.")),
         "Проведи торг: тобі дали 50, ціль — 42. Не погоджуйся одразу."),

    Week(4, "Рекламації та проблеми туристів", "negotiation",
         "Past Simple vs Present Perfect",
         "Ти можеш описати інцидент так, щоб його вирішили, а не сперечались.",
         (("We've had a complaint from a family in room 214.",
           "До нас надійшла скарга від родини з номера 214.",
           "Present Perfect — наслідок актуальний зараз."),
          ("The room wasn't ready when they arrived.", "Номер не був готовий, коли вони приїхали.",
           "Past Simple — конкретний момент у минулому."),
          ("This is the third time this month.", "Це вже третій раз цього місяця.",
           "Фактаж замість емоцій — найсильніший хід."),
          ("How do you plan to resolve this?", "Як ви плануєте це вирішити?",
           "Питання, що вимагає плану, а не вибачень."),
          ("We need this fixed today, not tomorrow.", "Нам треба вирішити це сьогодні, не завтра.",
           "«Need this fixed» — пасивна конструкція тиску."),
          ("Our client is asking for compensation.", "Наш клієнт вимагає компенсацію.",
           "«Asking for» м'якше за «demanding» — лишає простір."),
          ("I've attached the photos they sent us.", "Додаю фото, які вони нам надіслали.",
           "Стандарт для доказів."),
          ("Can you confirm this won't happen again?", "Можете підтвердити, що це не повториться?",
           "Просить зобов'язання, а не співчуття.")),
         "Опиши інцидент з готелем і домовся про компенсацію."),

    Week(5, "Листи: запити й нагадування", "email",
         "Modals for politeness (could / would / should)",
         "Твої листи починають отримувати відповіді швидше.",
         (("I'm following up on my email from Monday.", "Нагадую про свій лист від понеділка.",
           "«Follow up» — нейтральне нагадування без докору."),
          ("Could you confirm receipt?", "Можете підтвердити отримання?",
           "Коротко і професійно."),
          ("Please find the contract attached.", "Надсилаю договір у вкладенні.",
           "Класична формула. «In attachment» — помилка."),
          ("Just a gentle reminder about the deposit.", "Делікатно нагадую про депозит.",
           "«Gentle reminder» — стандарт для другого нагадування."),
          ("Would you be able to send it by Friday?", "Чи зможете надіслати до п'ятниці?",
           "«Would you be able» ввічливіше за «can you»."),
          ("Let me know if you need anything from our side.",
           "Дайте знати, якщо щось потрібно з нашого боку.",
           "«From our side» — калька, яка тут доречна."),
          ("I look forward to your reply.", "Чекаю на вашу відповідь.",
           "Після «look forward to» — ing або іменник, не інфінітив."),
          ("Apologies for the delay in getting back to you.",
           "Перепрошую за затримку з відповіддю.",
           "Коротке вибачення, без самобичування.")),
         "Напиши лист-нагадування про несплачений депозит."),

    Week(6, "Листи: погані новини і тверда позиція", "email",
         "Passive voice",
         "Ти можеш відмовити письмово, не зіпсувавши стосунки.",
         (("Unfortunately, we're not able to accommodate that.",
           "На жаль, ми не можемо на це піти.", "«Not able to» м'якше за «can't»."),
          ("The booking was cancelled by the client.", "Бронювання скасував клієнт.",
           "Пасив знімає звинувачення з конкретної людини."),
          ("This was agreed in our contract of 12 March.",
           "Це було узгоджено в договорі від 12 березня.",
           "Пасив + дата = документальна вага."),
          ("We're not in a position to cover that cost.",
           "Ми не в тому становищі, щоб покривати цей кошт.",
           "Формальна, але не ворожа відмова."),
          ("I'd like to escalate this internally.", "Хочу підняти це на вищий рівень усередині.",
           "Сигнал серйозності без погрози."),
          ("Our position remains unchanged.", "Наша позиція лишається незмінною.",
           "Використовуй, коли на тебе тиснуть повторно."),
          ("We'd appreciate a written confirmation.", "Будемо вдячні за письмове підтвердження.",
           "Ввічливо фіксує домовленість."),
          ("Please treat this as urgent.", "Прошу вважати це терміновим.",
           "Працює, коли не зловживати.")),
         "Відмов партнеру в компенсації, зберігши стосунки."),

    Week(7, "Контракти й умови", "negotiation",
         "Reported speech",
         "Ти впевнено переказуєш домовленості й ловиш розбіжності.",
         (("The deposit is due 30 days before arrival.", "Депозит сплачується за 30 днів до заїзду.",
           "«Due» = має бути сплачено."),
          ("Cancellation penalties apply from 14 days.", "Штрафи за ануляцію діють від 14 днів.",
           "«Apply» — стандарт для умов."),
          ("You said the rate would include breakfast.",
           "Ви казали, що ціна включатиме сніданок.",
           "Reported speech: will → would."),
          ("They confirmed they had received the payment.",
           "Вони підтвердили, що отримали оплату.",
           "Past Perfect у переказі — дія раніша за підтвердження."),
          ("That's not what we agreed.", "Це не те, про що ми домовлялись.",
           "Коротка фраза, яка зупиняє підміну умов."),
          ("Let's put that in the contract.", "Внесімо це в договір.",
           "Переводить обіцянку в зобов'язання."),
          ("Subject to availability.", "За наявності місць.",
           "Юридична обмовка, яку треба впізнавати."),
          ("The terms are valid until the end of April.", "Умови чинні до кінця квітня.",
           "«Valid until» — точна межа.")),
         "Перекажи телефонну домовленість і познач, що піде в договір."),

    Week(8, "Дзвінки та зустрічі: керувати розмовою", "negotiation",
         "Articles a / the / zero",
         "Ти перестаєш губитись, коли говорять швидко чи перебивають.",
         (("Sorry, could you repeat that?", "Вибачте, можете повторити?",
           "Не соромся — цю фразу кажуть носії."),
          ("Just to make sure I understood…", "Просто щоб переконатись, що я зрозумів…",
           "Найкорисніша фраза для B1 у швидкій розмові."),
          ("Can I jump in here?", "Можна я втручусь?",
           "Ввічливе перебивання — необхідна навичка."),
          ("So to summarise: you'll send the contract by Friday.",
           "Отже підсумую: ви надішлете договір до п'ятниці.",
           "Підсумок наприкінці рятує від непорозумінь."),
          ("Let me check and come back to you.", "Я перевірю і повернусь до вас.",
           "Замість того щоб вигадувати відповідь на місці."),
          ("We're on the same page.", "Ми розуміємо одне одного однаково.",
           "Ідіома, яку чуєш постійно."),
          ("Could you speak a bit more slowly, please?", "Можете говорити трохи повільніше?",
           "Не «more slow» — прислівник."),
          ("Who's taking this forward?", "Хто веде це далі?",
           "Питання про відповідального, без тиску.")),
         "Проведи дзвінок і підсумуй домовленості в кінці."),

    Week(9, "IT: описати проблему й задачу", "it",
         "Present Perfect for results and bugs",
         "Розробник розуміє тебе з першого разу.",
         (("The bot stopped responding after the deploy.",
           "Бот перестав відповідати після деплою.", "Причина через «after», не «because of deploy»."),
          ("It works on my side, but not in production.",
           "У мене працює, а в проді — ні.", "«On my side» / «in production» — жаргон, який усі знають."),
          ("I've already tried restarting it.", "Я вже пробував перезапустити.",
           "Present Perfect — результат актуальний."),
          ("Can you take a look when you get a chance?",
           "Можеш глянути, коли буде час?", "Ввічливий пріоритет без дедлайну."),
          ("It fails silently — no error in the logs.",
           "Падає тихо — жодної помилки в логах.", "«Fails silently» — точний технічний опис."),
          ("What's the expected behaviour here?", "Яка тут очікувана поведінка?",
           "Британське «behaviour» у доках частіше."),
          ("Could you walk me through the logic?", "Можеш пояснити логіку?",
           "Та сама конструкція, що і в цінах — перенось звички."),
          ("Let's ship it and iterate.", "Викотимо і доопрацюємо.",
           "«Ship» = випустити в прод.")),
         "Опиши баг: що зламалось, коли, що вже пробував."),

    Week(10, "IT: документація, API, інструменти", "it",
         "Prepositions in technical collocations",
         "Ти читаєш доки без словника і питаєш точно.",
         (("The endpoint returns a list of orders.", "Ендпоінт повертає список заявок.",
           "«Returns», не «gives back»."),
          ("It's rate-limited to 100 requests per minute.",
           "Обмеження — 100 запитів на хвилину.", "«Rate-limited to» — фіксована конструкція."),
          ("Authentication is handled via a bearer token.",
           "Автентифікація — через bearer-токен.", "«Via», не «by help of»."),
          ("This is deprecated — use v2 instead.", "Це застаріле — використовуй v2.",
           "«Deprecated» = ще працює, але не варто."),
          ("The response is cached for ten minutes.", "Відповідь кешується на десять хвилин.",
           "«Cached for» — тривалість."),
          ("Make sure you're on the latest version.", "Переконайся, що в тебе остання версія.",
           "«On» з версіями, не «in»."),
          ("It depends on how the data is structured.", "Залежить від того, як структуровані дані.",
           "«Depends on» — завжди on."),
          ("Roll it back if anything breaks.", "Відкоти, якщо щось зламається.",
           "«Roll back» — стандарт для відкату.")),
         "Постав розробнику точне питання по API з документації."),

    Week(11, "Подорожі й побут", "travel",
         "Countable / uncountable and quantifiers",
         "Ти вирішуєш побутові проблеми в поїздці без напруги.",
         (("I have a reservation under Velichko.", "У мене бронювання на Величко.",
           "«Under» + прізвище — стандарт на рецепції."),
          ("Is there any chance of a late check-out?", "Чи є шанс на пізній виїзд?",
           "«Any chance of» — ввічливе прохання."),
          ("My luggage didn't arrive.", "Мій багаж не прилетів.",
           "«Luggage» незлічуване — не «luggages»."),
          ("Could I get some more information about the transfer?",
           "Можна більше інформації про трансфер?",
           "«Information» незлічуване — «some», не «a few»."),
          ("There's been a mistake with the booking.", "З бронюванням сталася помилка.",
           "«There's been» — щойно виявлено."),
          ("How long does it take to get to the airport?",
           "Скільки часу добиратись до аеропорту?", "«Take» про час — фіксовано."),
          ("Do you have anything quieter?", "Є щось тихіше?",
           "Порівняльний прикметник після «anything»."),
          ("Could I have the bill, please?", "Можна рахунок?",
           "«Bill» у Британії, «check» у США.")),
         "Виріши проблему на рецепції: номер не той, що бронював."),

    Week(12, "Зведення: повні переговори", "negotiation",
         "Повторення всього: умовні, модальні, пасив, артиклі",
         "Ти проводиш реальні переговори від початку до підпису.",
         (("Let's find a solution that works for both of us.",
           "Знайдімо рішення, яке влаштує обох.", "Відкриває вихід із глухого кута."),
          ("Where do we stand on the contract?", "На чому ми зупинились по договору?",
           "«Where do we stand» — статус без тиску."),
          ("I think we're close.", "Думаю, ми близько.",
           "Створює відчуття прогресу."),
          ("Let's get this signed and move on.", "Підпишімо і рухаймось далі.",
           "«Get this signed» — каузативна конструкція."),
          ("That works for us.", "Це нам підходить.", "Коротка згода."),
          ("I'll send a summary of what we agreed.",
           "Надішлю підсумок домовленостей.", "Обов'язковий крок після переговорів."),
          ("Pleasure doing business with you.", "Приємно мати з вами справу.",
           "Закриває розмову тепло."),
          ("Let's schedule a follow-up for next month.",
           "Заплануймо наступну зустріч на місяць.", "«Follow-up» як іменник.")),
         "Проведи повні переговори: знайомство → ціна → торг → підсумок."),
)

WEEKS = len(CURRICULUM)


def week_for(no: int) -> Week:
    return CURRICULUM[max(0, min(no, WEEKS) - 1)]


# ───────────────────────── profile ─────────────────────────

DEFAULT_GOALS = ["negotiation", "email", "it", "travel"]


def today_local() -> date:
    return datetime.now(ZoneInfo(settings.tz_name)).date()


async def get_profile(db: AsyncSession, user_id: int) -> EnglishProfile:
    """The learner's position in the plan. Created on first touch with the
    settings he chose: B1, ~12 minutes, partner talks + email + IT + travel."""
    prof = await db.get(EnglishProfile, user_id)
    if prof is None:
        prof = EnglishProfile(user_id=user_id, domain=DOMAIN, level="B1",
                              minutes_per_day=DEFAULT_MINUTES,
                              goals=list(DEFAULT_GOALS), week=1, day_in_week=1,
                              talk_log=[], drill={})
        db.add(prof)
        await db.flush()
    return prof


# ───────────────────────── spaced repetition ─────────────────────────
# A small SM-2 variant. Three grades, because on a phone anything more is
# noise: «не згадав» / «важко» / «знаю». The only job of this code is to
# decide WHEN a phrase comes back, so the learner never decides what to study.

EASE_MIN, EASE_MAX = 1.3, 2.8
INTERVAL_MAX = 180
GRADES = ("again", "hard", "good")


def schedule(ease: float, interval: int, reps: int, grade: str) -> tuple:
    """Pure: (ease, interval_days, reps, lapse?) after one answer."""
    if grade == "again":
        return max(EASE_MIN, ease - 0.20), 1, 0, True
    if grade == "hard":
        nxt = 1 if reps == 0 else max(1, round(interval * 1.2))
        return max(EASE_MIN, ease - 0.05), min(INTERVAL_MAX, nxt), reps + 1, False
    ease = min(EASE_MAX, ease + 0.05)
    if reps == 0:
        nxt = 1
    elif reps == 1:
        nxt = 3
    else:
        nxt = max(1, round(interval * ease))
    return ease, min(INTERVAL_MAX, nxt), reps + 1, False


def apply_grade(item: EnglishItem, grade: str, on: date) -> None:
    ease, interval, reps, lapsed = schedule(
        item.ease, item.interval_days, item.reps, grade)
    item.ease, item.interval_days, item.reps = ease, interval, reps
    if lapsed:
        item.lapses += 1
    item.due_on = on + timedelta(days=interval)


async def add_item(db: AsyncSession, user_id: int, *, term: str, meaning: str,
                   example: str = "", note: str = "", scenario: str = "",
                   source: str = "plan") -> EnglishItem | None:
    """Add one thing to remember. Returns None if it is already there — the
    unique key on (user_id, term) is what keeps a repeated mistake from
    becoming five identical cards."""
    term = (term or "").strip()
    if not term:
        return None
    existing = (await db.execute(
        select(EnglishItem).where(EnglishItem.user_id == user_id,
                                  EnglishItem.term == term))).scalar_one_or_none()
    if existing is not None:
        if source == "mistake" and existing.source == "plan":
            # he got a planned phrase wrong in real speech: pull it forward
            existing.due_on = today_local() + timedelta(days=1)
            existing.interval_days, existing.reps = 1, 0
        return None
    item = EnglishItem(user_id=user_id, domain=DOMAIN, term=term[:400],
                       meaning=(meaning or "")[:400], example=(example or "")[:400],
                       note=(note or "")[:400], scenario=scenario[:40],
                       source=source, due_on=today_local() + timedelta(days=1),
                       interval_days=1, reps=1)
    db.add(item)
    await db.flush()
    return item


async def due_items(db: AsyncSession, user_id: int, on: date,
                    limit: int) -> list[EnglishItem]:
    """Oldest-due first, so nothing rots at the bottom of the queue."""
    rows = await db.execute(
        select(EnglishItem)
        .where(EnglishItem.user_id == user_id, EnglishItem.due_on <= on)
        .order_by(EnglishItem.due_on.asc(), EnglishItem.id.asc())
        .limit(limit))
    return list(rows.scalars())


async def stats(db: AsyncSession, user_id: int, on: date) -> dict:
    total = (await db.execute(select(func.count()).select_from(EnglishItem)
                              .where(EnglishItem.user_id == user_id))).scalar() or 0
    due = (await db.execute(
        select(func.count()).select_from(EnglishItem)
        .where(EnglishItem.user_id == user_id,
               EnglishItem.due_on <= on))).scalar() or 0
    solid = (await db.execute(
        select(func.count()).select_from(EnglishItem)
        .where(EnglishItem.user_id == user_id,
               EnglishItem.interval_days >= 21))).scalar() or 0
    mistakes = (await db.execute(
        select(func.count()).select_from(EnglishItem)
        .where(EnglishItem.user_id == user_id,
               EnglishItem.source == "mistake"))).scalar() or 0
    return {"total": total, "due": due, "solid": solid, "mistakes": mistakes}


# ───────────────────────── the daily session ─────────────────────────
# Twelve minutes is the whole design constraint. What fits: the phrases that
# are due today, two new ones from this week, and one thing to say out loud.
# The system picks; he only answers.

MAX_NEW = 8
NEW_PER_DAY = 2
NEW_DAYS = 4          # days 1-4 of a week bring new material, 5-7 consolidate


def budget_cards(minutes: int) -> int:
    """Roughly one card per minute — a recall takes ~25 s, a new phrase ~45 s,
    and the closing task eats the rest."""
    return max(6, min(20, minutes))


async def _known_terms(db: AsyncSession, user_id: int) -> set[str]:
    rows = await db.execute(select(EnglishItem.term)
                            .where(EnglishItem.user_id == user_id))
    return {t for (t,) in rows}


def _unseen_phrases(week_no: int, known: set[str]) -> list[tuple[int, int]]:
    """(week, phrase index) for everything not yet in memory, current week
    first, then forward. Weeks already passed are not re-opened."""
    out: list[tuple[int, int]] = []
    for w in range(week_no, WEEKS + 1):
        for i, (en, _uk, _hook) in enumerate(week_for(w).phrases):
            if en not in known:
                out.append((w, i))
    return out


async def build_queue(db: AsyncSession, prof: EnglishProfile,
                      on: date) -> list[dict]:
    """The session, as a list of cards. Reviews first (they are the debt),
    then new material — and if there is not enough of either, the session
    pulls more new phrases forward rather than ending after 40 seconds."""
    cap = budget_cards(prof.minutes_per_day)
    queue: list[dict] = [{"t": "rev", "id": it.id}
                         for it in await due_items(db, prof.user_id, on, cap)]
    known = await _known_terms(db, prof.user_id)
    pool = _unseen_phrases(prof.week, known)
    want = NEW_PER_DAY if prof.day_in_week <= NEW_DAYS else 0
    want = max(want, cap - len(queue))          # a thin day fills with new material
    for w, i in pool[:min(MAX_NEW, want)]:
        queue.append({"t": "new", "w": w, "i": i})
    return queue


async def start_session(db: AsyncSession, prof: EnglishProfile) -> dict:
    """Open a session. Idempotent per day only in spirit: pressing ▶️ twice
    simply rebuilds the queue from what is still due."""
    on = today_local()
    queue = await build_queue(db, prof, on)
    prof.drill = {"q": queue, "pos": 0, "ok": 0, "seen": 0, "shown": False,
                  "started": datetime.now(timezone.utc).isoformat()}
    await audit(db, actor=f"user:{prof.user_id}", action="english.session_started",
                resource_type="english", policy_level="L1", domain=DOMAIN,
                week=prof.week, cards=len(queue))
    await db.flush()
    return prof.drill


def session_active(prof: EnglishProfile) -> bool:
    d = prof.drill or {}
    return bool(d.get("q")) and d.get("pos", 0) < len(d.get("q", []))


async def current_card(db: AsyncSession, prof: EnglishProfile) -> dict | None:
    """Render the card at the cursor. Returns {kind, text, revealed} or None
    when the queue is done. A review card is shown Ukrainian-first (recall),
    then reveals; a new phrase is shown whole."""
    d = prof.drill or {}
    q, pos = d.get("q", []), d.get("pos", 0)
    if pos >= len(q):
        return None
    entry, n, total = q[pos], pos + 1, len(q)
    head = f"<b>{n}/{total}</b>  "
    if entry["t"] == "new":
        wk = week_for(entry["w"])
        en, uk, hook = wk.phrases[entry["i"]]
        return {"kind": "new", "revealed": True, "term": en,
                "text": (f"{head}🆕 <i>{_html.escape(wk.title)}</i>\n\n"
                         f"<b>{_html.escape(en)}</b>\n{_html.escape(uk)}\n\n"
                         f"💡 {_html.escape(hook)}")}
    item = await db.get(EnglishItem, entry["id"])
    if item is None:                     # deleted between build and answer
        return {"kind": "gone", "revealed": True, "term": "", "text": ""}
    if not d.get("shown"):
        prompt = item.meaning or item.example or "?"
        return {"kind": "ask", "revealed": False, "term": item.term,
                "text": (f"{head}🔁 Як сказати англійською?\n\n"
                         f"<b>{_html.escape(prompt)}</b>")}
    extra = ""
    if item.note:
        extra += f"\n\n💡 {_html.escape(item.note)}"
    if item.example:
        extra += f"\n<i>{_html.escape(item.example)}</i>"
    return {"kind": "answer", "revealed": True, "term": item.term,
            "text": (f"{head}🔁 <b>{_html.escape(item.term)}</b>\n"
                     f"{_html.escape(item.meaning)}{extra}")}


async def reveal(db: AsyncSession, prof: EnglishProfile) -> None:
    d = dict(prof.drill or {})
    d["shown"] = True
    prof.drill = d
    await db.flush()


async def advance(db: AsyncSession, prof: EnglishProfile,
                  grade: str = "good") -> bool:
    """Grade the current card and move on. Returns True while cards remain.

    A NEW phrase becomes a memory item here — at the moment it was actually
    shown, not when the queue was built, so an abandoned session leaves no
    phantom cards behind."""
    d = dict(prof.drill or {})
    q, pos = d.get("q", []), d.get("pos", 0)
    if pos >= len(q):
        return False
    entry, on = q[pos], today_local()
    if entry["t"] == "new":
        wk = week_for(entry["w"])
        en, uk, hook = wk.phrases[entry["i"]]
        await add_item(db, prof.user_id, term=en, meaning=uk, note=hook,
                       scenario=wk.scenario, source="plan")
    elif entry["t"] == "rev":
        item = await db.get(EnglishItem, entry["id"])
        if item is not None:
            apply_grade(item, grade if grade in GRADES else "good", on)
            d["seen"] = d.get("seen", 0) + 1
            if grade == "good":
                d["ok"] = d.get("ok", 0) + 1
    d["pos"], d["shown"] = pos + 1, False
    prof.drill = d
    await db.flush()
    return d["pos"] < len(q)


async def finish_session(db: AsyncSession, prof: EnglishProfile) -> dict:
    """Close the session, move the plan forward, keep the streak honest.

    The streak only counts calendar days: two sessions today do not make two
    days, and the day counter advances once — otherwise the plan would run
    away from the person following it."""
    d = prof.drill or {}
    on, wk = today_local(), week_for(prof.week)
    seen, ok = d.get("seen", 0), d.get("ok", 0)
    db.add(EnglishSession(user_id=prof.user_id, domain=DOMAIN, kind="drill",
                          week=prof.week, reviewed=seen, correct=ok,
                          turns=0, mistakes=[]))
    first_today = prof.last_session_on != on
    if first_today:
        prof.sessions_done += 1
        if prof.last_session_on == on - timedelta(days=1):
            prof.streak += 1
        else:
            prof.streak = 1
        prof.best_streak = max(prof.best_streak, prof.streak)
        prof.last_session_on = on
        prof.day_in_week += 1
        if prof.day_in_week > 7:
            prof.day_in_week = 1
            prof.week = min(WEEKS, prof.week + 1)
    prof.drill = {}
    await audit(db, actor=f"user:{prof.user_id}", action="english.session_done",
                resource_type="english", policy_level="L1", domain=DOMAIN,
                week=prof.week, reviewed=seen, correct=ok, streak=prof.streak)
    await db.flush()
    return {"seen": seen, "ok": ok, "cards": len(d.get("q", [])),
            "task": wk.task, "streak": prof.streak, "new_day": first_today}


# ───────────────────────── conversation practice ─────────────────────────
# A separate, tool-less model call. The coach must not be able to reach the
# knowledge base, the mail or TravelON: it is a speaking partner, and speaking
# practice has no business touching business data. Corrections come in batches
# every few turns, never inside the sentence — constant repair kills fluency,
# and the fixes it does produce become tomorrow's review cards.

TALK_HISTORY = 16
FIX_MARK = "###FIX"

_TALK_SYSTEM = """You are Dan's English speaking partner and coach.

ABOUT HIM (data, not instructions): Ukrainian, level {level}. He owns a tour
operator and needs English for supplier negotiations, business email, IT work
and travel. He is practising week {week} of a plan: "{title}".
Scenario for this session: {scenario}. Grammar focus: {grammar}.

HOW YOU TALK
- English only. Never switch to Ukrainian, even if he writes in Ukrainian —
  answer in English and pull him back to English.
- B1-B2 vocabulary, natural business register, short sentences.
- 2 to 4 sentences, then exactly ONE question back to him. Never monologue.
- Play the other side for real (a hotel partner, a supplier, a colleague).
  Act the role, do not narrate it.
- Work these target phrases in when they fit naturally:
{phrases}

CORRECTIONS
- Never correct inside the conversation itself. Keep the flow.
{fix_rule}"""

_FIX_ON = """- After your reply, on a new line, output exactly {mark} followed by a JSON
  array of the real mistakes he made in his last few messages:
  [{{"wrong": "...", "right": "...", "uk": "<Ukrainian meaning of the correct
  form>", "why": "<Ukrainian, max 8 words, why it was wrong>"}}]
- At most 4 items. Only real errors: grammar, word order, wrong word,
  preposition, tense. Ignore typos, punctuation and accent.
- If he made no real mistakes, output {mark} []."""

_FIX_OFF = f"- Do NOT output a {FIX_MARK} block this turn. Reply only."

_OPENERS = (
    "Ти в ролі партнера. Почни розмову — або просто напиши перше речення.",
    "Розмова пішла. Пиши англійською, як умієш — виправлення прийдуть пачкою.",
    "Говори англійською. Голосове теж працює — я відповім голосом.",
)


async def _call_model(system: str, messages: list[dict],
                      max_tokens: int = 700) -> str | None:
    if settings.chat_model in ("", "mock") or not settings.anthropic_api_key:
        return None
    from app.core.chat import thinking_params
    payload = {"model": settings.chat_model, "max_tokens": max_tokens,
               "system": system, "messages": messages,
               **thinking_params(settings.chat_model)}
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": settings.anthropic_api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=payload)
        if resp.status_code != 200:
            logger.error("english coach %s: %s", resp.status_code, resp.text[:200])
            return None
        blocks = resp.json().get("content", [])
    except Exception:
        logger.exception("english coach call failed")
        return None
    return "".join(b.get("text", "") for b in blocks
                   if b.get("type") == "text").strip() or None


def split_fixes(raw: str) -> tuple[str, list[dict]]:
    """Separate the reply from the correction block. A malformed block is
    dropped, never shown: the learner sees English or nothing, not JSON."""
    if FIX_MARK not in raw:
        return raw.strip(), []
    body, _, tail = raw.partition(FIX_MARK)
    tail = tail.strip().strip("`")
    if tail.startswith("json"):
        tail = tail[4:].strip()
    try:
        data = json.loads(tail)
    except Exception:
        logger.warning("english coach: unparsable fix block")
        return body.strip(), []
    if not isinstance(data, list):
        return body.strip(), []
    out = []
    for f in data[:4]:
        if isinstance(f, dict) and str(f.get("right", "")).strip():
            out.append({k: str(f.get(k, ""))[:300] for k in
                        ("wrong", "right", "uk", "why")})
    return body.strip(), out


def fixes_card(fixes: list[dict]) -> str:
    lines = ["\n\n✏️ <b>Робота над помилками</b>"]
    for f in fixes:
        wrong, right = _html.escape(f["wrong"]), _html.escape(f["right"])
        lines.append(f"• <s>{wrong}</s> → <b>{right}</b>"
                     + (f"\n  <i>{_html.escape(f['why'])}</i>" if f.get("why") else ""))
    return "\n".join(lines)


async def start_talk(db: AsyncSession, prof: EnglishProfile) -> str:
    wk = week_for(prof.week)
    prof.talk_started_at = datetime.now(timezone.utc)
    prof.talk_turns, prof.talk_topic = 0, wk.title
    prof.talk_log, prof.talk_mistakes = [], []
    await db.flush()
    return (f"💬 <b>Розмовна практика</b> — {_html.escape(wk.title)}\n"
            f"Завдання тижня: {_html.escape(wk.task)}\n\n"
            f"{random.choice(_OPENERS)}\n"
            f"Завершити: /english_stop")


def talk_expired(prof: EnglishProfile) -> bool:
    if prof.talk_started_at is None:
        return False
    last = prof.talk_started_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) > timedelta(minutes=TALK_IDLE_MINUTES)


async def talk_active(db: AsyncSession, user_id: int) -> EnglishProfile | None:
    """The profile iff a live conversation is running. An abandoned one closes
    itself here rather than swallowing a business question hours later."""
    prof = await db.get(EnglishProfile, user_id)
    if prof is None or prof.talk_started_at is None:
        return None
    if talk_expired(prof):
        await end_talk(db, prof)
        return None
    return prof


async def talk_reply(db: AsyncSession, prof: EnglishProfile,
                     text: str) -> tuple[str, str] | None:
    """One conversational turn.

    Returns (what the card SHOWS, what a voice SAYS), or None to let the caller
    fall back to ordinary chat. The two differ on purpose: the spoken half is
    the English answer only — reading a Ukrainian correction list aloud in the
    middle of English practice would undo the practice."""
    # Defence in depth (§R6.1A): this is a provider egress point of its own.
    # The orchestrator already gated the text; the coach re-checks so a future
    # caller cannot bypass the boundary by reaching this function directly.
    if security.scan(text).blocked:
        return security.SAFE_REFUSAL, security.SAFE_REFUSAL
    wk = week_for(prof.week)
    prof.talk_turns += 1
    correcting = prof.talk_turns % TALK_CORRECT_EVERY == 0
    phrases = "\n".join(f"  - {en}" for en, _uk, _h in wk.phrases[:6])
    system = _TALK_SYSTEM.format(
        level=prof.level, week=prof.week, title=wk.title, scenario=wk.scenario,
        grammar=wk.grammar, phrases=phrases,
        fix_rule=(_FIX_ON.format(mark=FIX_MARK) if correcting else _FIX_OFF))
    log = list(prof.talk_log or [])
    messages = [{"role": ("user" if r == "user" else "assistant"),
                 "content": m[:1500]} for r, m in log[-TALK_HISTORY:]]
    messages.append({"role": "user", "content": text[:2000]})

    raw = await _call_model(system, messages)
    if raw is None:
        prof.talk_turns -= 1
        return None
    reply, fixes = split_fixes(raw)
    if not reply:
        prof.talk_turns -= 1
        return None

    log.append(["user", text[:1500]])
    log.append(["coach", reply[:1500]])
    prof.talk_log = log[-TALK_HISTORY * 2:]
    prof.talk_started_at = datetime.now(timezone.utc)   # idle clock resets

    saved = 0
    for f in fixes:
        item = await add_item(db, prof.user_id, term=f["right"],
                              meaning=f.get("uk", ""), note=f.get("why", ""),
                              example=(f"Було: {f['wrong']}" if f.get("wrong") else ""),
                              scenario=wk.scenario, source="mistake")
        saved += item is not None
    if fixes:
        prof.talk_mistakes = (list(prof.talk_mistakes or []) + fixes)[-40:]
    await db.flush()

    out = _html.escape(reply)
    if fixes:
        out += fixes_card(fixes)
        if saved:
            out += f"\n\n<i>+{saved} у повторення на завтра</i>"
    return out, reply


async def end_talk(db: AsyncSession, prof: EnglishProfile) -> str:
    turns = prof.talk_turns
    mistakes = list(prof.talk_mistakes or [])
    if turns:
        db.add(EnglishSession(user_id=prof.user_id, domain=DOMAIN, kind="talk",
                              week=prof.week, reviewed=0, correct=0,
                              turns=turns, mistakes=mistakes))
    prof.talk_started_at, prof.talk_turns, prof.talk_topic = None, 0, ""
    prof.talk_log, prof.talk_mistakes = [], []
    await audit(db, actor=f"user:{prof.user_id}", action="english.talk_ended",
                resource_type="english", policy_level="L1", domain=DOMAIN,
                turns=turns, fixes=len(mistakes))
    await db.flush()
    if not turns:
        return "Розмову закрито."
    tail = ("Виправлення вже в черзі повторення — побачиш їх у наступній сесії."
            if mistakes else "Помилок не назбиралось — тримай темп.")
    return (f"💬 Розмову завершено: <b>{turns}</b> реплік, "
            f"виправлень: <b>{len(mistakes)}</b>.\n{tail}")


# ───────────────────────── cards ─────────────────────────

def hub_card(prof: EnglishProfile, st: dict) -> str:
    wk = week_for(prof.week)
    done = prof.last_session_on == today_local()
    streak = (f"🔥 серія {prof.streak} дн." if prof.streak else "серія: —")
    lines = [
        f"🇬🇧 <b>Англійська</b> · {prof.level} · ~{prof.minutes_per_day} хв/день",
        f"Тиждень <b>{prof.week}/{WEEKS}</b>, день {prof.day_in_week}/7 · "
        f"{streak} · сесій: {prof.sessions_done}",
        f"У пам'яті: <b>{st['total']}</b> фраз · на сьогодні: <b>{st['due']}</b>"
        f" · закріплено: {st['solid']}",
        "",
        f"<b>{_html.escape(wk.title)}</b>",
        f"Граматика: {_html.escape(wk.grammar)}",
        f"<i>{_html.escape(wk.why)}</i>",
        "",
        f"🎤 Завдання тижня: {_html.escape(wk.task)}",
    ]
    if done:
        lines.append("\n✅ Сьогодні вже займався. Ще один підхід теж рахується.")
    return "\n".join(lines)


def plan_card(prof: EnglishProfile) -> str:
    lines = ["📚 <b>План на 12 тижнів</b>",
             "<i>Партнери → листування → IT → побут → зведення.</i>", ""]
    for wk in CURRICULUM:
        mark = "▶️" if wk.no == prof.week else ("✅" if wk.no < prof.week else "▫️")
        lines.append(f"{mark} <b>{wk.no}.</b> {_html.escape(wk.title)}\n"
                     f"     <i>{_html.escape(wk.grammar)}</i>")
    lines.append("\nКожен тиждень: 8 робочих фраз, одна граматична тема, "
                 "одне усне завдання. Повторення підбирає система.")
    return "\n".join(lines)


async def progress_card(db: AsyncSession, prof: EnglishProfile) -> str:
    """What actually happened, not what was planned. Honest counters only:
    sessions, streak, memory size, and the phrases that keep breaking."""
    on = today_local()
    st = await stats(db, prof.user_id, on)
    week_ago = on - timedelta(days=7)
    recent = await db.execute(
        select(func.count(), func.coalesce(func.sum(EnglishSession.reviewed), 0),
               func.coalesce(func.sum(EnglishSession.correct), 0),
               func.coalesce(func.sum(EnglishSession.turns), 0))
        .where(EnglishSession.user_id == prof.user_id,
               EnglishSession.created_at >= datetime.combine(
                   week_ago, datetime.min.time(), tzinfo=timezone.utc)))
    sessions, reviewed, correct, turns = recent.one()
    acc = f"{round(100 * correct / reviewed)}%" if reviewed else "—"
    hard = await db.execute(
        select(EnglishItem)
        .where(EnglishItem.user_id == prof.user_id, EnglishItem.lapses > 0)
        .order_by(EnglishItem.lapses.desc(), EnglishItem.id.asc()).limit(5))
    lines = [
        "📈 <b>Прогрес</b>",
        f"Тиждень {prof.week}/{WEEKS} · серія {prof.streak} дн. "
        f"(рекорд {prof.best_streak}) · усього сесій {prof.sessions_done}",
        "",
        f"<b>За 7 днів:</b> сесій {sessions} · повторень {reviewed} · "
        f"влучність {acc} · реплік у розмові {turns}",
        f"<b>Пам'ять:</b> {st['total']} фраз, з них закріплено {st['solid']}, "
        f"з власних помилок {st['mistakes']}",
    ]
    stuck = list(hard.scalars())
    if stuck:
        lines.append("\n<b>Найчастіше губиться:</b>")
        for it in stuck:
            lines.append(f"• {_html.escape(it.term)} — <i>"
                         f"{_html.escape(it.meaning or '')}</i> ({it.lapses}×)")
    else:
        lines.append("\nЖодна фраза поки не випадала — або ще рано, "
                     "або справді тримається.")
    return "\n".join(lines)


def session_summary(res: dict) -> str:
    lines = ["✅ <b>Сесію завершено</b>",
             f"Карток: {res['cards']} · повторень: {res['seen']}"
             + (f" · без запинки: {res['ok']}" if res['seen'] else "")]
    if res.get("new_day"):
        lines.append(f"🔥 Серія: {res['streak']} дн.")
    lines.append(f"\n🎤 <b>Завдання:</b> {_html.escape(res['task'])}")
    lines.append("Зроби це вголос у розмові — там і виправлю.")
    return "\n".join(lines)
