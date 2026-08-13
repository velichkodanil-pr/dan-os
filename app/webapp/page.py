"""DAN.OS Mini App — one self-contained HTML page (Telegram WebApp).

All data flows through /webapp/api/* with initData auth; this page is a shell.
Styling follows Telegram theme variables, so it matches light/dark themes.
"""

PAGE_HTML = """<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>DAN.OS</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: var(--tg-theme-bg-color, #10151d);
    --fg: var(--tg-theme-text-color, #e8eef5);
    --hint: var(--tg-theme-hint-color, #8ba0b5);
    --link: var(--tg-theme-link-color, #6ab3f3);
    --card: var(--tg-theme-secondary-bg-color, #1a2230);
    --accent: var(--tg-theme-button-color, #2f81d6);
    --accent-fg: var(--tg-theme-button-text-color, #ffffff);
  }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--bg); color: var(--fg);
         font: 15px/1.45 -apple-system, system-ui, Roboto, sans-serif;
         padding: 12px 12px 76px; }
  h2 { font-size: 15px; margin: 18px 2px 8px; color: var(--hint);
       text-transform: uppercase; letter-spacing: .4px; font-weight: 600; }
  .card { background: var(--card); border-radius: 14px; padding: 12px 14px;
          margin-bottom: 8px; }
  .row { display: flex; align-items: center; gap: 10px; }
  .grow { flex: 1; min-width: 0; }
  .title { font-weight: 500; overflow-wrap: anywhere; }
  .sub { color: var(--hint); font-size: 13px; margin-top: 2px; }
  .due-over { color: #e5636c; }
  button { border: 0; border-radius: 10px; padding: 8px 12px; font-size: 14px;
           background: var(--accent); color: var(--accent-fg); cursor: pointer;
           flex-shrink: 0; }
  button.ghost { background: transparent; color: var(--hint);
                 border: 1px solid var(--hint); opacity: .85; }
  button.ok { background: #2e9e5b; color: #fff; }
  button:active { transform: scale(.96); }
  .empty { color: var(--hint); text-align: center; padding: 26px 8px; }
  .badge { display: inline-block; background: var(--accent); color: var(--accent-fg);
           border-radius: 9px; font-size: 12px; padding: 0 7px; margin-left: 6px;
           line-height: 18px; }
  .habit-done { text-decoration: line-through; opacity: .65; }
  .check { width: 26px; height: 26px; border-radius: 8px; border: 2px solid var(--hint);
           display: flex; align-items: center; justify-content: center; flex-shrink: 0;
           font-size: 15px; color: transparent; }
  .check.on { background: #2e9e5b; border-color: #2e9e5b; color: #fff; }
  nav { position: fixed; left: 0; right: 0; bottom: 0; display: flex;
        background: var(--card); border-top: 1px solid rgba(128,128,128,.2);
        padding-bottom: env(safe-area-inset-bottom); }
  nav button { flex: 1; background: none; color: var(--hint); border-radius: 0;
               padding: 10px 0 12px; font-size: 12px; }
  nav button.active { color: var(--link); }
  nav .ico { font-size: 20px; display: block; margin-bottom: 2px; }
  #loading { text-align: center; padding: 40px 0; color: var(--hint); }
  .addrow { display: flex; gap: 8px; margin: 6px 0 10px; }
  .addrow input { flex: 1; min-width: 0; background: var(--card); border: 1px solid
    rgba(128,128,128,.35); border-radius: 10px; padding: 9px 12px; color: var(--fg);
    font-size: 14px; outline: none; }
  .addrow input:focus { border-color: var(--accent); }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .stat { background: var(--card); border-radius: 14px; padding: 12px 14px; }
  .stat .num { font-size: 24px; font-weight: 700; }
  .stat .lbl { color: var(--hint); font-size: 12px; margin-top: 2px; }
  .debt-amt { color: #e5636c; font-weight: 600; white-space: nowrap; }
</style>
</head>
<body>
<div id="loading">Завантаження…</div>
<div id="view"></div>
<nav id="nav" style="display:none">
  <button data-tab="today" class="active"><span class="ico">☀️</span>Сьогодні</button>
  <button data-tab="review"><span class="ico">🗂</span>Розбір<span id="revBadge"></span></button>
  <button data-tab="memory"><span class="ico">🧠</span>Пам'ять</button>
  <button data-tab="biz" id="bizTab" style="display:none"><span class="ico">🧳</span>Бізнес</button>
</nav>
<script>
const tg = window.Telegram.WebApp;
tg.ready(); tg.expand();
const HDRS = { 'X-Telegram-Init-Data': tg.initData, 'Content-Type': 'application/json' };
let DATA = null, TAB = 'today';

const esc = s => (s || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function load() {
  try {
    const r = await fetch('/webapp/api/overview', { headers: HDRS });
    if (!r.ok) throw new Error(r.status);
    DATA = await r.json();
    document.getElementById('loading').style.display = 'none';
    document.getElementById('nav').style.display = 'flex';
    render();
  } catch (e) {
    document.getElementById('loading').textContent =
      'Немає доступу. Відкрий застосунок кнопкою в чаті DAN.OS.';
  }
}

async function act(action, id, version, text) {
  if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
  try {
    const r = await fetch('/webapp/api/act', { method: 'POST', headers: HDRS,
      body: JSON.stringify({ action, id, version, text }) });
    const res = await r.json();
    if (res.status === 'conflict')
      tg.showAlert('Конфлікт із наявним фактом — розв\\'яжи його в чаті з ботом ⚖️');
  } catch (e) {}
  load();
}

function addItem(kind, inpId) {
  const inp = document.getElementById(inpId);
  const v = (inp.value || '').trim();
  if (v.length < 2) { inp.focus(); return; }
  inp.value = '';
  act(kind, '', null, v);
}

let BIZ = null, BIZ_LOADING = false;
async function loadBiz() {
  if (BIZ_LOADING) return;
  BIZ_LOADING = true;
  render();
  try {
    const r = await fetch('/webapp/api/travelon', { headers: HDRS });
    BIZ = await r.json();
  } catch (e) { BIZ = { configured: true, data: null }; }
  BIZ_LOADING = false;
  render();
}

function taskCard(t, showDue) {
  const due = t.due ? `<div class="sub ${t.overdue ? 'due-over' : ''}">🕐 ${esc(t.due)}</div>` : '';
  return `<div class="card row"><div class="grow"><div class="title">${esc(t.title)}</div>
    ${showDue ? due : ''}</div>
    <button class="ok" onclick="act('task_done','${t.id}')">☑️</button>
    <button class="ghost" onclick="act('task_cancel','${t.id}')">✕</button></div>`;
}

function renderToday() {
  const d = DATA; let h = '';
  const over = d.today.overdue, today = d.today.today, nd = d.today.no_date;
  if (over.length) { h += '<h2>🔴 Прострочено</h2>' + over.map(t => taskCard(t, true)).join(''); }
  h += '<h2>Задачі на сьогодні</h2>';
  h += today.length ? today.map(t => taskCard(t, true)).join('')
                    : '<div class="card sub">Задач із дедлайном немає ✅</div>';
  if (nd.length) { h += '<h2>Без дати</h2>' + nd.map(t => taskCard(t, false)).join(''); }
  h += '<h2>🏃 Звички</h2>';
  h += d.habits.map(x => `
      <div class="card row" onclick="act('habit_toggle','${x.id}')">
        <div class="check ${x.done_today ? 'on' : ''}">✓</div>
        <div class="grow ${x.done_today ? 'habit-done' : ''}">${esc(x.title)}</div>
        <div class="sub">${x.week_count}/${x.week_days}</div>
      </div>`).join('');
  h += `<div class="addrow"><input id="habInp" placeholder="Нова звичка…"
        enterkeyhint="done">
        <button onclick="addItem('habit_add','habInp')">➕</button></div>`;
  h += '<h2>🎯 Цілі</h2>';
  h += d.goals.map(g => `
      <div class="card row"><div class="grow title">${esc(g.title)}</div>
      <button class="ok" onclick="act('goal_done','${g.id}')">🏁</button></div>`).join('');
  h += `<div class="addrow"><input id="goalInp" placeholder="Нова ціль…"
        enterkeyhint="done">
        <button onclick="addItem('goal_add','goalInp')">➕</button></div>`;
  return h;
}

function renderBiz() {
  if (BIZ_LOADING || !BIZ)
    return '<div class="empty">🧳 Збираю пульс TravelON…<br>перший раз ~20 секунд</div>';
  if (!BIZ.configured)
    return '<div class="empty">TravelON не підключено</div>';
  const p = BIZ.data;
  if (!p) return '<div class="empty">Звіт зараз недоступний — спробуй пізніше</div>';
  let h = `<h2>🧳 TravelON · ${p.date}</h2>
    <div class="grid">
      <div class="stat"><div class="num">${p.created_today}</div><div class="lbl">нових сьогодні</div></div>
      <div class="stat"><div class="num">${p.created_yesterday}</div><div class="lbl">нових учора</div></div>
      <div class="stat"><div class="num">${p.arrivals_today}</div><div class="lbl">заїздів сьогодні</div></div>
      <div class="stat"><div class="num">${p.arrivals_week}</div><div class="lbl">заїздів за 7 днів</div></div>
    </div>
    <div class="card" style="margin-top:8px"><div class="sub">💰 Сума нових за 2 дні</div>
      <div class="title">${esc(p.sum_2d)}</div></div>
    <div class="card"><div class="sub">👥 Туристів у заїздах 7 днів</div>
      <div class="title">${p.tourists}</div></div>`;
  if (p.debt_count) {
    h += `<h2>💸 Борг у найближчих заїздах · ${p.debt_count} · ${esc(p.debt_total)}</h2>`;
    h += p.debtors.map(x => `
      <div class="card row"><div class="grow"><div class="title">№${esc(x.order_no)} · ${esc(x.where)}</div>
      <div class="sub">заїзд ${esc(x.when)}</div></div>
      <div class="debt-amt">${esc(x.amount)}</div></div>`).join('');
  } else {
    h += '<div class="card sub">💸 Боргів у найближчих заїздах немає 👌</div>';
  }
  h += `<div class="empty" style="padding:12px">Станом на ${esc(p.generated_at)} ·
    <a href="#" style="color:var(--link)" onclick="BIZ=null;loadBiz();return false">оновити</a></div>`;
  return h;
}

function renderReview() {
  const d = DATA; let h = '';
  if (d.approvals.length) {
    h += '<h2>Пропозиції задач</h2>' + d.approvals.map(p => `
      <div class="card"><div class="title">${esc(p.title)}</div>
      ${p.due ? `<div class="sub">🕐 ${esc(p.due)}</div>` : ''}
      <div class="row" style="margin-top:8px">
        <button class="ok grow" onclick="act('approve','${p.id}',${p.version})">✅ Підтвердити</button>
        <button class="ghost grow" onclick="act('reject','${p.id}')">❌ Відхилити</button>
      </div></div>`).join('');
  }
  if (d.memory_candidates.length) {
    h += '<h2>Кандидати в пам\\'ять</h2>' + d.memory_candidates.map(m => `
      <div class="card"><div>🧠 ${esc(m.content)}</div>
      <div class="row" style="margin-top:8px">
        <button class="ok grow" onclick="act('mem_confirm','${m.id}')">✅ Запам'ятати</button>
        <button class="ghost grow" onclick="act('mem_reject','${m.id}')">🗑</button>
      </div></div>`).join('');
  }
  return h || '<div class="empty">Нічого не чекає на розбір 👌</div>';
}

function renderMemory() {
  const d = DATA;
  let h = `<div class="card sub">📚 База знань: ${d.kb_docs} документ(ів) ·
    підтверджених фактів: ${d.memory_confirmed.length}</div>`;
  if (d.memory_confirmed.length) {
    h += '<h2>Підтверджені факти</h2>' + d.memory_confirmed.map(m =>
      `<div class="card"><div>${esc(m.content)}</div>
       <div class="sub">${esc(m.date)}</div></div>`).join('');
  } else {
    h += '<div class="empty">Поки нічого не підтверджено.<br>Скажи боту «запам\\'ятай: …»</div>';
  }
  return h;
}

function render() {
  const view = document.getElementById('view');
  view.innerHTML = TAB === 'today' ? renderToday()
                 : TAB === 'review' ? renderReview()
                 : TAB === 'biz' ? renderBiz() : renderMemory();
  const n = DATA.approvals.length + DATA.memory_candidates.length;
  document.getElementById('revBadge').innerHTML =
    n ? `<span class="badge">${n}</span>` : '';
  if (DATA.travelon) document.getElementById('bizTab').style.display = '';
  document.querySelectorAll('nav button').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === TAB));
}

document.getElementById('nav').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  TAB = b.dataset.tab;
  if (TAB === 'biz' && !BIZ) loadBiz(); else render();
});
load();
</script>
</body>
</html>"""
