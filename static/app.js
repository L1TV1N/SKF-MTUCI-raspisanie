'use strict';
/* Расписание СКФ МТУСИ — клиентская часть (без зависимостей) */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const KIND = {lec: 'Лекция', prac: 'Практика', lab: 'Лаб. работа', sem: 'Семинар'};
const WT = {all: 'Каждую неделю', odd: 'Числитель', even: 'Знаменатель'};
const WD = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота'];
const WD_SHORT = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'];
const MONTHS = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];
const MONTHS_SHORT = ['янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек'];
const TARGET = {
  group:   {label: 'Группа', icon: '👥', ph: 'Например, ИКТ-21'},
  teacher: {label: 'Преподаватель', icon: '🎓', ph: 'Фамилия преподавателя'},
  room:    {label: 'Аудитория', icon: '🚪', ph: 'Номер аудитории'},
};

const S = {
  meta: null, mode: 'group',
  target: null, monday: null, day: 0, data: null,
  adminTab: 'lessons', adminGroup: null, changesFrom: null,
};

/* ─────────── утилиты ─────────── */
const store = {
  get(k, d) {
    try {
      const v = JSON.parse(localStorage.getItem(k));
      return v === null || (Array.isArray(d) && !Array.isArray(v)) ? d : v;
    } catch { return d; }
  },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* приватный режим */ } },
};

async function api(path, opts = {}) {
  const init = {method: opts.method || 'GET', headers: {}, credentials: 'same-origin'};
  if (opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `Ошибка ${res.status}`);
    err.status = res.status; err.data = data;
    throw err;
  }
  return data;
}

const pad = n => String(n).padStart(2, '0');
const iso = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parseISO = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
const mondayOf = d => addDays(d, -((d.getDay() + 6) % 7));
const toMin = hhmm => { const [h, m] = hhmm.split(':').map(Number); return h * 60 + m; };
// «Сейчас» по Москве (СКФ МТУСИ — Ростов-на-Дону), независимо от часового пояса устройства
const MSK_OFFSET = 3 * 60;
const nowMsk = () => { const d = new Date(); return new Date(d.getTime() + (d.getTimezoneOffset() + MSK_OFFSET) * 60000); };
const nowMin = () => { const d = nowMsk(); return d.getHours() * 60 + d.getMinutes(); };
// Неделя, которую показывать «сегодня»: в воскресенье — следующая
const currentWeek = () => {
  const t = nowMsk(), wd = (t.getDay() + 6) % 7;
  return wd === 6 ? {monday: iso(addDays(mondayOf(t), 7)), day: 0} : {monday: iso(mondayOf(t)), day: wd};
};
const plural = (n, a, b, c) => { const m = n % 10, h = n % 100; return m === 1 && h !== 11 ? a : m >= 2 && m <= 4 && (h < 10 || h >= 20) ? b : c; };
const dur = min => min < 60 ? `${min} ${plural(min, 'минуту', 'минуты', 'минут')}` : `${Math.floor(min / 60)} ч ${min % 60} мин`;

function toast(msg, type = '') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'show ' + type;
  clearTimeout(toast.tm);
  toast.tm = setTimeout(() => { t.className = type; }, 2600);
}

const bell = n => S.meta.bells.find(b => b.pair_no === n);
const byId = (list, id) => S.meta[list].find(x => x.id === id);
const groupByName = name => S.meta.groups.find(g => g.name === name);

const roomLbl = r => /^\d/.test(r) ? 'ауд. ' + r : r;

function shortName(full) {
  const p = (full || '').split(/\s+/);
  return p.length >= 3 ? `${p[0]} ${p[1][0]}.${p[2][0]}.` : full;
}

/* ─────────── экраны ─────────── */
let curScreen = 'sh';
function go(id) {
  if (id === curScreen) return;
  const from = $('#' + curScreen), to = $('#' + id);
  from.classList.remove('on'); from.classList.add('out');
  setTimeout(() => from.classList.remove('out'), 320);
  to.classList.add('on');
  curScreen = id;
  particles.toggle(id === 'sh');
}

function route() {
  if (!S.meta) return;
  closeSheet();
  const m = location.hash.match(/^#\/(group|teacher|room)\/(\d+)$/);
  if (m) return openSchedule(m[1], +m[2]);
  if (location.hash === '#/admin') { go('sa'); return renderAdmin(); }
  go('sh');
  renderHome();
}

/* ─────────── главная ─────────── */
function renderHome() {
  const M = S.meta;
  $('#st-groups').textContent = M.groups.length;
  $('#st-teachers').textContent = M.teachers.length;
  $('#st-rooms').textContent = M.rooms.length;
  const w = M.week, t = nowMsk();
  $('#h-week').textContent = `Сегодня ${t.getDate()} ${MONTHS[t.getMonth()]} · ${w.show_parity ? w.parity_label + ', ' : ''}${w.week_no}-я неделя`;
  $('#h-sem').textContent = M.settings.semester_title || '';

  const valid = x => x && TARGET[x.kind] && Number.isInteger(x.id) && typeof x.title === 'string';
  const favs = store.get('rs_fav', []).filter(valid), recent = store.get('rs_recent', []).filter(valid);
  const seen = new Set();
  const chips = [...favs.map(f => ({...f, fav: true})), ...recent].filter(x => {
    const k = x.kind + x.id; if (seen.has(k)) return false; seen.add(k); return true;
  }).slice(0, 8);
  $('#h-recent').innerHTML = chips.map(c =>
    `<a class="chip" href="#/${c.kind}/${c.id}">${c.fav ? '★' : TARGET[c.kind].icon} ${esc(c.title)}</a>`).join('');
  renderSearch();
}

function searchItems(mode, q) {
  const norm = s => String(s).toLowerCase().replace(/[\s\-–.]/g, '').replace(/ё/g, 'е');
  const nq = norm(q);
  const M = S.meta;
  let items;
  if (mode === 'group') items = M.groups.map(g => ({id: g.id, t: g.name, d: `${g.course} курс · ${g.direction}`, s: g.name + g.direction}));
  else if (mode === 'teacher') items = M.teachers.map(x => ({id: x.id, t: x.full_name, d: [x.position, x.department].filter(Boolean).join(' · '), s: x.full_name + x.department}));
  else items = M.rooms.map(r => ({id: r.id, t: /^\d/.test(r.name) ? `Аудитория ${r.name}` : r.kind || r.name, d: [r.kind, r.building, r.capacity ? r.capacity + ' мест' : ''].filter(Boolean).join(' · '), s: r.name + r.kind}));
  return nq ? items.filter(i => norm(i.s).includes(nq)) : items;
}

function renderSearch() {
  const q = $('#h-q').value.trim();
  const items = searchItems(S.mode, q);
  $('#h-res').innerHTML = items.length ? items.map(i => `
    <a class="h-ri" href="#/${S.mode}/${i.id}" style="text-decoration:none">
      <div class="h-ri-ic">${TARGET[S.mode].icon}</div>
      <div><div class="h-ri-t">${esc(i.t)}</div><div class="h-ri-d">${esc(i.d)}</div></div>
      <div class="h-ri-go">→</div>
    </a>`).join('') : `<div class="h-empty">Ничего не найдено по запросу «${esc(q)}»</div>`;
}

/* ─────────── расписание ─────────── */
async function openSchedule(kind, id) {
  const same = S.target && S.target.kind === kind && S.target.id === id;
  S.target = {kind, id};
  if (!same || !S.monday) {
    ({monday: S.monday, day: S.day} = currentWeek());
    S.data = null;
  }
  go('ss');
  if (!same || !S.data) {
    $('#s-title').textContent = '…';
    $('#s-sub').textContent = '';
    $('#s-body').innerHTML = '<div class="day-list">' + '<div class="skel"></div>'.repeat(3) + '</div>';
  }
  await loadWeek();
}

let loadSeq = 0;
async function loadWeek() {
  const {kind, id} = S.target, seq = ++loadSeq;
  try {
    const data = await api(`/api/schedule?${kind}=${id}&date=${S.monday}`);
    if (seq !== loadSeq) return;  // пока грузили, пользователь ушёл на другую неделю или цель
    S.data = data;
    const recent = store.get('rs_recent', []).filter(r => !(r.kind === kind && r.id === id));
    recent.unshift({kind, id, title: kind === 'room' ? data.target.title.replace('Аудитория ', 'Ауд. ') : kind === 'teacher' ? shortName(data.target.title) : data.target.title});
    store.set('rs_recent', recent.slice(0, 6));
    renderSchedule();
  } catch (e) {
    if (seq !== loadSeq) return;
    $('#s-body').innerHTML = `<div class="empty"><div class="e-ic">⚠️</div><b>Не удалось загрузить</b><span>${esc(e.message)}</span></div>`;
    if (e.status === 404) {
      $('#s-title').textContent = 'Не найдено';
      $('#s-sub').textContent = '';
      $('#dpills').innerHTML = '';
      $('#wk-parity').textContent = '—';
      $('#wk-range').textContent = '';
    }
  }
}

function renderSchedule() {
  const d = S.data, {kind, id} = S.target;
  $('#s-emo').textContent = TARGET[kind].icon;
  $('#s-kind').textContent = TARGET[kind].label;
  $('#s-title').textContent = d.target.title;
  $('#s-sub').textContent = d.target.subtitle || '';
  document.title = `${d.target.title} — Расписание СКФ МТУСИ`;
  const isFav = store.get('rs_fav', []).some(f => f.kind === kind && f.id === id);
  $('#s-fav').textContent = isFav ? '★' : '☆';
  $('#s-fav').classList.toggle('on', isFav);
  $('#s-ics').href = `/api/export.ics?${kind}=${id}`;

  const mon = parseISO(d.monday), sat = addDays(mon, 5);
  const pb = $('#wk-parity');
  pb.textContent = d.show_parity ? `${d.parity_label} · ${d.week_no} нед.` : `${d.week_no}-я неделя`;
  pb.className = 'pts-badge ' + (d.show_parity ? d.parity : '');
  $('#wk-range').textContent = mon.getMonth() === sat.getMonth()
    ? `${mon.getDate()}–${sat.getDate()} ${MONTHS_SHORT[sat.getMonth()]}`
    : `${mon.getDate()} ${MONTHS_SHORT[mon.getMonth()]} – ${sat.getDate()} ${MONTHS_SHORT[sat.getMonth()]}`;
  const todayIso = iso(nowMsk());
  $('#wk-today').classList.toggle('hide', d.monday === currentWeek().monday);

  $('#dpills').innerHTML = d.days.map((day, i) => {
    const dt = parseISO(day.date);
    return `<button class="dp${i === S.day ? ' on' : ''}${day.date === todayIso ? ' today' : ''}" data-day="${i}">
      <b>${WD_SHORT[i]}</b><span>${dt.getDate()}</span><i class="${day.items.length ? '' : 'none'}"></i></button>`;
  }).join('');

  const note = S.meta.settings.announcement;
  const noteHidden = store.get('rs_note_hidden', '') === note;
  const notice = note && !noteHidden
    ? `<div class="notice"><span>ℹ️</span><div style="flex:1">${esc(note)}</div><button class="mini" id="note-x" aria-label="Скрыть">✕</button></div>` : '';

  const blank = d.days.every(x => !x.items.length)
    ? '<div class="empty" style="margin-bottom:16px"><div class="e-ic">🗓</div><b>На эту неделю занятий нет</b><span>Возможно, расписание на эти даты ещё не опубликовано. Полистайте недели стрелками ‹ ›</span></div>' : '';
  $('#s-body').innerHTML = notice + blank + '<div class="days">' + d.days.map((day, i) => renderDay(day, i, todayIso)).join('') + '</div>';
}

function renderDay(day, i, todayIso) {
  const dt = parseISO(day.date);
  const isToday = day.date === todayIso;
  const cnt = new Set(day.items.filter(x => x.status !== 'cancelled').map(x => x.pair_no)).size;
  let html = `<section class="day${i === S.day ? ' on' : ''}${isToday ? ' today' : ''}" data-i="${i}">
    <div class="day-h"><div class="day-t">${WD[i]}<small>${dt.getDate()} ${MONTHS[dt.getMonth()]}</small></div>
    <div class="day-n">${cnt ? `${cnt} ${plural(cnt, 'пара', 'пары', 'пар')}` : ''}</div></div>`;
  if (!day.items.length) {
    return html + `<div class="empty"><div class="e-ic">${i === 5 ? '🛋' : '🎉'}</div><b>Пар нет</b><span>${i === 5 ? 'Выходной — можно отдохнуть' : 'Свободный день'}</span></div></section>`;
  }
  if (isToday) html += nowBanner(day.items);
  html += '<div class="day-list">';
  let prev = null;
  for (const it of day.items) {
    if (prev !== null && it.pair_no - prev > 1) {
      const n = it.pair_no - prev - 1;
      html += `<div class="gap">Окно · ${n} ${plural(n, 'пара', 'пары', 'пар')}</div>`;
    }
    html += lessonCard(it, isToday);
    prev = it.pair_no;
  }
  return html + '</div></section>';
}

function nowBanner(items) {
  const t = nowMin();
  const live = items.filter(x => x.status !== 'cancelled' && bell(x.pair_no));
  const cur = live.find(x => { const b = bell(x.pair_no); return t >= toMin(b.starts_at) && t < toMin(b.ends_at); });
  if (cur) {
    const b = bell(cur.pair_no);
    return `<div class="banner"><div class="bi">⏱</div><div><b>Идёт ${cur.pair_no}-я пара · ${esc(cur.subject_short || cur.subject)}</b><span>До конца ${dur(toMin(b.ends_at) - t)} (в ${b.ends_at})</span></div></div>`;
  }
  const next = live.find(x => toMin(bell(x.pair_no).starts_at) > t);
  if (next) {
    const b = bell(next.pair_no);
    return `<div class="banner"><div class="bi">🔔</div><div><b>Далее: ${esc(next.subject)}</b><span>${b.starts_at} · через ${dur(toMin(b.starts_at) - t)}${next.room ? ' · ' + esc(roomLbl(next.room)) : ''}</span></div></div>`;
  }
  return live.length ? '<div class="banner"><div class="bi">✅</div><div><b>Пары на сегодня закончились</b><span>Хорошего вечера!</span></div></div>' : '';
}

function lessonCard(it, isToday) {
  const b = bell(it.pair_no);
  const kind = S.target.kind;
  let cls = 'les ' + it.status, prog = '';
  if (isToday && b && it.status !== 'cancelled') {
    const t = nowMin(), s = toMin(b.starts_at), e = toMin(b.ends_at);
    if (t >= s && t < e) { cls += ' now'; prog = `<div class="les-prog" style="width:${Math.round((t - s) / (e - s) * 100)}%"></div>`; }
    else if (t >= e) cls += ' past';
  }
  const tags = [`<span class="tg ${it.kind}">${KIND[it.kind] || it.kind}</span>`];
  if (it.subgroup) tags.push(`<span class="tg">Подгруппа ${it.subgroup}</span>`);
  if (kind === 'teacher' && it.assistant_id === S.target.id) tags.push('<span class="tg wk">Ассистент</span>');
  if (it.week_type !== 'all') tags.push(`<span class="tg wk">${WT[it.week_type]}</span>`);
  if (it.status === 'cancelled') tags.push('<span class="tg cnl">Отменена</span>');
  if (it.status === 'replaced') tags.push('<span class="tg chg">Замена</span>');
  if (it.status === 'added') tags.push('<span class="tg add">Доп. пара</span>');

  const meta = [];
  const person = (id, name) => kind === 'teacher' && id === S.target.id ? esc(shortName(name)) : `<a href="#/teacher/${id}">${esc(shortName(name))}</a>`;
  if (it.teacher) meta.push(`<span>🎓 ${person(it.teacher_id, it.teacher)}</span>`);
  if (it.assistant) meta.push(`<span>🤝 асс. ${person(it.assistant_id, it.assistant)}</span>`);
  if (it.room) meta.push(kind === 'room' ? `<span>🚪 ${esc(it.room)}</span>`
    : `<span>🚪 <a href="#/room/${it.room_id}">${esc(roomLbl(it.room))}</a></span>`);
  if (kind !== 'group' || it.groups.length > 1) {
    const links = it.groups.map(n => { const g = groupByName(n); return g && !(kind === 'group' && g.id === S.target.id) ? `<a href="#/group/${g.id}">${esc(n)}</a>` : esc(n); });
    meta.push(`<span>👥 ${it.groups.length > 1 ? 'поток: ' : ''}${links.join(', ')}</span>`);
  }

  let extra = '';
  if (it.orig) {
    const was = [];
    if (it.orig.subject !== it.subject) was.push(esc(it.orig.subject));
    if (it.orig.teacher !== it.teacher && it.orig.teacher) was.push(esc(shortName(it.orig.teacher)));
    if (it.orig.room !== it.room && it.orig.room) was.push(esc(roomLbl(it.orig.room)));
    if (was.length) extra += `<div class="les-o">Было: <s>${was.join(' · ')}</s></div>`;
  }
  if (it.change_note) extra += `<div class="les-o">💬 ${esc(it.change_note)}</div>`;
  else if (it.note) extra += `<div class="les-o">${esc(it.note)}</div>`;

  return `<article class="${cls}">
    <div class="les-tm"><div class="les-no">${it.pair_no}<small>пара</small></div>
      ${b ? `<div class="les-t1">${b.starts_at}</div><div class="les-t2">${b.ends_at}</div>` : ''}</div>
    <div class="les-b"><div class="les-tags">${tags.join('')}</div>
      <div class="les-s">${esc(it.subject)}</div>
      <div class="les-m">${meta.join('')}</div>${extra}</div>${prog}
  </article>`;
}

function setDay(i) {
  S.day = Math.max(0, Math.min(5, i));
  $$('.dp').forEach(b => b.classList.toggle('on', +b.dataset.day === S.day));
  $$('.day').forEach(s => s.classList.toggle('on', +s.dataset.i === S.day));
}

function shiftWeek(n) {
  S.monday = iso(addDays(parseISO(S.monday), 7 * n));
  loadWeek();
}

function toggleFav() {
  const {kind, id} = S.target;
  let favs = store.get('rs_fav', []);
  const had = favs.some(f => f.kind === kind && f.id === id);
  if (had) favs = favs.filter(f => !(f.kind === kind && f.id === id));
  else favs.unshift({kind, id, title: kind === 'teacher' ? shortName(S.data.target.title) : S.data.target.title.replace('Аудитория ', 'Ауд. ')});
  store.set('rs_fav', favs);
  renderSchedule();
  toast(had ? 'Убрано из избранного' : 'Добавлено в избранное ★');
}

function printSchedule() {
  const d = S.data;
  const days = d.days.map(day => {
    const dt = parseISO(day.date);
    const rows = day.items.map(it => {
      const b = bell(it.pair_no);
      const who = [it.teacher && S.target.kind !== 'teacher' ? shortName(it.teacher) : '', it.assistant ? 'асс. ' + shortName(it.assistant) : '', it.room && S.target.kind !== 'room' ? roomLbl(it.room) : '',
        S.target.kind !== 'group' ? it.groups.join(', ') : '', it.subgroup ? 'подгр. ' + it.subgroup : ''].filter(Boolean).join(' · ');
      return `<div class="pr-l"><div class="n">${it.pair_no} · ${b ? b.starts_at : ''}</div><div class="${it.status === 'cancelled' ? 'x' : ''}"><b>${esc(it.subject)}</b> (${(KIND[it.kind] || '').toLowerCase()})${it.status === 'replaced' ? ' — замена' : ''}<br>${esc(who)}</div></div>`;
    }).join('') || '<div class="pr-l">Пар нет</div>';
    return `<div class="pr-day"><h4>${WD[day.weekday - 1]}, ${dt.getDate()} ${MONTHS[dt.getMonth()]}</h4>${rows}</div>`;
  }).join('');
  $('#print-doc').innerHTML = `<div class="pr-head">${logoSvg}<div class="pr-t"><b>${esc(d.target.title)}</b><span>${[d.target.subtitle, (d.show_parity ? d.parity_label + ', ' : '') + d.week_no + '-я неделя', $('#wk-range').textContent].filter(Boolean).map(esc).join(' · ')}</span></div></div>
    <div class="pr-grid">${days}</div>
    <div class="pr-foot"><span>СКФ МТУСИ · ${esc(S.meta.settings.semester_title || '')}</span><span>Сформировано ${new Date().toLocaleString('ru-RU')}</span></div>`;
  window.print();
}

/* ─────────── нижняя панель (формы, подтверждения) ─────────── */
function openSheet(html) {
  $('#sheet-in').innerHTML = html;
  $('#sheet').classList.add('show');
  $('#sheet-bg').classList.add('show');
  $('#sheet').scrollTop = 0;
  const f = $('#sheet input, #sheet select');
  if (f && matchMedia('(min-width:1024px)').matches) setTimeout(() => f.focus(), 250);
}
function closeSheet() {
  $('#sheet').classList.remove('show');
  $('#sheet-bg').classList.remove('show');
}
function confirmSheet(title, text, okLabel = 'Удалить') {
  return new Promise(resolve => {
    openSheet(`<div class="sh-t">${esc(title)}</div><div class="sh-d">${esc(text)}</div>
      <div class="sh-acts row"><button class="btn btn-o" id="cf-no">Отмена</button><button class="btn btn-e" id="cf-yes">${esc(okLabel)}</button></div>`);
    $('#cf-no').onclick = () => { closeSheet(); resolve(false); };
    $('#cf-yes').onclick = () => { closeSheet(); resolve(true); };
  });
}

/* ─────────── формы ─────────── */
const opt = (list, val, lbl) => S.meta[list].map(x => [x.id, lbl(x)]);
const F = {
  subject: () => ({k: 'subject_id', l: 'Дисциплина', t: 'select', num: true, opts: opt('subjects', 'id', x => x.name)}),
  teacher: () => ({k: 'teacher_id', l: 'Преподаватель', t: 'select', num: true, empty: '— не указан —', opts: opt('teachers', 'id', x => x.full_name)}),
  room: () => ({k: 'room_id', l: 'Аудитория', t: 'select', num: true, empty: '— не указана —', opts: opt('rooms', 'id', x => `${x.name}${x.kind ? ' · ' + x.kind : ''}`)}),
  group: () => ({k: 'group_id', l: 'Группа', t: 'select', num: true, opts: opt('groups', 'id', x => x.name)}),
  pair: () => ({k: 'pair_no', l: 'Пара', t: 'select', num: true, opts: S.meta.bells.map(b => [b.pair_no, `${b.pair_no} · ${b.starts_at}–${b.ends_at}`])}),
  kind: (empty) => ({k: 'kind', l: 'Вид занятия', t: 'select', empty, opts: Object.entries(KIND)}),
  subgroup: () => ({k: 'subgroup', l: 'Подгруппа', t: 'select', num: true, opts: [[0, 'Вся группа'], [1, 'Подгруппа 1'], [2, 'Подгруппа 2']]}),
};

function fieldHtml(f, v) {
  const id = 'f_' + f.k;
  const val = v ?? '';
  let input;
  if (f.t === 'select') {
    const opts = (f.empty !== undefined ? [['', f.empty]] : []).concat(f.opts);
    input = `<select class="inp" id="${id}">${opts.map(([ov, ol]) => `<option value="${esc(ov)}"${String(ov) === String(val) ? ' selected' : ''}>${esc(ol)}</option>`).join('')}</select>`;
  } else if (f.t === 'textarea') {
    input = `<textarea class="inp" id="${id}" placeholder="${esc(f.ph || '')}">${esc(val)}</textarea>`;
  } else {
    input = `<input class="inp" id="${id}" type="${f.t || 'text'}" value="${esc(val)}" placeholder="${esc(f.ph || '')}"${f.t === 'number' ? ' inputmode="numeric"' : ''}>`;
  }
  return `<div class="fld"><label class="lbl" for="${id}">${esc(f.l)}${f.req ? ' *' : ''}</label>${input}</div>`;
}

function readForm(fields) {
  const out = {};
  for (const f of fields) {
    if (f.row) { Object.assign(out, readForm(f.row)); continue; }
    const el = $('#f_' + f.k);
    if (!el) continue;
    let v = el.value.trim();
    if (f.num || f.t === 'number') v = v === '' ? null : Number(v);
    else if (f.t === 'select' && v === '') v = null;
    out[f.k] = v;
  }
  return out;
}

function formHtml(fields, data) {
  return fields.map(f => f.row
    ? `<div class="frow">${f.row.map(x => fieldHtml(x, data[x.k])).join('')}</div>`
    : f.hint ? `<div class="hint">${esc(f.hint)}</div>` : fieldHtml(f, data[f.k])).join('');
}

/* opts: {title, desc, fields, data, save(payload, force) → Promise} */
function formSheet({title, desc = '', fields, data = {}, save, onDone}) {
  const draw = (conflicts) => {
    openSheet(`<div class="sh-t">${esc(title)}</div>${desc ? `<div class="sh-d">${esc(desc)}</div>` : '<div style="height:14px"></div>'}
      ${conflicts ? `<div class="cf-list"><b>⚠ Пересечения в расписании</b><ul>${conflicts.map(c => `<li>${esc(c)}</li>`).join('')}</ul></div>` : ''}
      <form id="sh-form">${formHtml(fields, data)}
      <div class="sh-acts row"><button type="button" class="btn btn-o" id="sh-cancel">Отмена</button>
      <button type="submit" class="btn ${conflicts ? 'btn-e' : 'btn-p'}" id="sh-save">${conflicts ? 'Сохранить всё равно' : 'Сохранить'}</button></div></form>`);
    $('#sh-cancel').onclick = closeSheet;
    $('#sh-form').onsubmit = async ev => {
      ev.preventDefault();
      const payload = readForm(fields);
      Object.assign(data, payload);
      $('#sh-save').disabled = true;
      try {
        await save(payload, !!conflicts);
        closeSheet();
        toast('Сохранено', 'ok');
        await refreshMeta();
        onDone && onDone();
      } catch (e) {
        if (e.status === 409 && e.data && e.data.conflicts) return draw(e.data.conflicts);
        toast(e.message, 'err');
        $('#sh-save').disabled = false;
      }
    };
  };
  draw(null);
}

/* ─────────── администрирование ─────────── */
const ADMIN_TABS = [
  ['lessons', '📚 Занятия'], ['changes', '🔁 Замены'], ['groups', '👥 Группы'], ['teachers', '🎓 Преподаватели'],
  ['rooms', '🚪 Аудитории'], ['subjects', '📖 Дисциплины'], ['bells', '🔔 Звонки'], ['settings', '⚙ Семестр'],
];

const REF = {
  groups: {
    list: 'groups', noun: 'группу', title: g => g.name, sub: g => `${g.course} курс · ${g.level} · ${g.direction}`,
    fields: () => [{k: 'name', l: 'Название', req: true, ph: 'ИКТ-21'},
      {row: [{k: 'course', l: 'Курс', t: 'number', req: true}, {k: 'level', l: 'Уровень', t: 'select', opts: [['бакалавриат', 'Бакалавриат'], ['специалитет', 'Специалитет'], ['магистратура', 'Магистратура'], ['СПО', 'СПО (колледж)']]}]},
      {k: 'direction', l: 'Направление подготовки', ph: '11.03.02 Инфокоммуникационные технологии и системы связи'}],
    defaults: {course: 1, level: 'бакалавриат'},
  },
  teachers: {
    list: 'teachers', noun: 'преподавателя', title: t => t.full_name, sub: t => [t.position, t.department].filter(Boolean).join(' · '),
    fields: () => [{k: 'full_name', l: 'ФИО', req: true, ph: 'Иванов Иван Иванович'}, {k: 'position', l: 'Должность', ph: 'доцент'}, {k: 'department', l: 'Кафедра'}],
  },
  rooms: {
    list: 'rooms', noun: 'аудиторию', title: r => `Аудитория ${r.name}`, sub: r => [r.kind, r.building, r.capacity ? r.capacity + ' мест' : ''].filter(Boolean).join(' · '),
    fields: () => [{row: [{k: 'name', l: 'Номер', req: true, ph: '305'}, {k: 'capacity', l: 'Мест', t: 'number'}]}, {k: 'kind', l: 'Тип', ph: 'Компьютерный класс'}, {k: 'building', l: 'Корпус'},
      {k: 'shared', l: 'Параллельные занятия', t: 'select', num: true, opts: [[0, 'Нет — одна группа за раз'], [1, 'Да — спортзал, дистанционно и т.п.']]}],
    defaults: {shared: 0},
  },
  subjects: {
    list: 'subjects', noun: 'дисциплину', title: s => s.name, sub: s => s.short_name ? 'Кратко: ' + s.short_name : '',
    fields: () => [{k: 'name', l: 'Название', req: true}, {k: 'short_name', l: 'Краткое название', ph: 'Для уведомлений и баннера «сейчас идёт»'}],
  },
};

async function refreshMeta() {
  S.meta = await api('/api/meta');
  S.data = null;
}

function renderAdmin() {
  const body = $('#a-body');
  $('#a-logout').classList.toggle('hidden', !S.meta.admin);
  $('#a-tabs').classList.toggle('hidden', !S.meta.admin);
  if (!S.meta.admin) {
    $('#a-subtitle').textContent = 'Вход для диспетчера учебного отдела';
    body.innerHTML = `<div class="card login"><h3>🔐 Вход</h3><p class="d">Редактирование расписания, замен и справочников доступно только администратору.</p>
      <form id="login-f"><label class="lbl" for="pw">Пароль</label><input class="inp" id="pw" type="password" autocomplete="current-password" required>
      <button class="btn btn-p" type="submit">Войти →</button></form></div>`;
    $('#login-f').onsubmit = async ev => {
      ev.preventDefault();
      try {
        await api('/api/login', {method: 'POST', body: {password: $('#pw').value}});
        await refreshMeta();
        toast('Добро пожаловать!', 'ok');
        renderAdmin();
      } catch (e) { toast(e.message, 'err'); $('#pw').select(); }
    };
    setTimeout(() => $('#pw') && $('#pw').focus(), 350);
    return;
  }
  $('#a-subtitle').textContent = S.meta.settings.semester_title || 'Управление расписанием';
  $('#a-tabs').innerHTML = ADMIN_TABS.map(([k, l]) => `<button class="a-tab${k === S.adminTab ? ' on' : ''}" data-tab="${k}">${l}</button>`).join('');
  const tab = S.adminTab;
  if (tab === 'lessons') return adminLessons();
  if (tab === 'changes') return adminChanges();
  if (tab === 'bells') return adminBells();
  if (tab === 'settings') return adminSettings();
  adminRef(tab);
}

// Выполняет запрос администратора; при ошибке показывает её и возвращает null
async function adminCall(fn) {
  try { return await fn(); }
  catch (e) {
    toast(e.message, 'err');
    if (e.status === 401) { S.meta.admin = false; renderAdmin(); }
    return null;
  }
}

/* — Занятия — */
function lessonFields(dated) {
  const when = dated
    ? [{row: [{k: 'date', l: 'Дата', t: 'date', req: true}, F.pair()]}]
    : [{row: [{k: 'weekday', l: 'День', t: 'select', num: true, opts: WD.map((d, i) => [i + 1, d])}, F.pair()]},
       {k: 'week_type', l: 'Периодичность', t: 'select', opts: Object.entries(WT)}];
  return [
    ...when,
    F.subject(),
    {row: [F.kind(), F.subgroup()]},
    F.teacher(),
    {...F.teacher(), k: 'assistant_id', l: 'Ассистент', empty: '— нет —'},
    F.room(),
    {k: 'note', l: 'Примечание', ph: 'Необязательно'},
  ];
}

async function adminLessons() {
  const M = S.meta;
  if (!M.groups.length) { $('#a-body').innerHTML = '<div class="card"><h3>Нет групп</h3><p class="d">Сначала добавьте группу во вкладке «Группы».</p></div>'; return; }
  if (!S.adminGroup || !byId('groups', S.adminGroup)) S.adminGroup = M.groups[0].id;
  const gid = S.adminGroup;
  $('#a-body').innerHTML = `<div class="bar"><select class="inp" id="a-group" aria-label="Группа">${M.groups.map(g => `<option value="${g.id}"${g.id === gid ? ' selected' : ''}>${esc(g.name)} · ${g.course} курс</option>`).join('')}</select>
    <a class="btn btn-o btn-s" href="#/group/${gid}" style="text-decoration:none">Просмотр →</a></div><div id="a-les"><div class="skel"></div></div>`;
  $('#a-group').onchange = e => { S.adminGroup = +e.target.value; adminLessons(); };
  const rows = await adminCall(() => api(`/api/admin/lessons?group=${gid}`));
  if (!rows || S.adminTab !== 'lessons' || S.adminGroup !== gid) return;
  const row = r => {
    const b = bell(r.pair_no);
    const tags = [`<span class="tg ${r.kind}">${KIND[r.kind]}</span>`, r.subgroup ? `<span class="tg">Подгр. ${r.subgroup}</span>` : '', !r.date && r.week_type !== 'all' ? `<span class="tg wk">${WT[r.week_type]}</span>` : ''].join('');
    const who = [r.teacher ? shortName(r.teacher) : 'преподаватель не указан', r.assistant ? 'асс. ' + shortName(r.assistant) : '', r.room ? roomLbl(r.room) : 'без аудитории'].filter(Boolean).map(esc).join(' · ');
    return `<div class="rbi"><div class="rbi-no">${r.pair_no}</div><div class="rbi-info"><div class="rbi-n">${esc(r.subject)}</div>
      <div class="rbi-d">${tags}${b ? b.starts_at + '–' + b.ends_at + ' · ' : ''}${who}</div></div>
      <div class="rbi-a"><button class="mini" data-edit-lesson="${r.id}" aria-label="Изменить">✎</button><button class="mini del" data-del-lesson="${r.id}" aria-label="Удалить">🗑</button></div></div>`;
  };
  const regular = rows.filter(r => !r.date), dated = rows.filter(r => r.date);
  const today = iso(mondayOf(nowMsk()));
  const past = dated.filter(r => r.date < today);
  const shown = S.showPast ? dated : dated.filter(r => r.date >= today);
  const dates = [...new Set(shown.map(r => r.date))];
  let html = `<div class="hint" style="margin:0 0 6px">Пересечения по группе, преподавателю, ассистенту и аудитории проверяются при сохранении; потоки (одна дисциплина, вид, преподаватель и аудитория у нескольких групп) пересечением не считаются.</div>
    <div class="stit"><span>Занятия по датам · ${dated.length}</span><button class="add-l" data-add-dated>+ Добавить</button></div>`;
  if (past.length) html += `<button class="btn btn-o btn-s" data-toggle-past style="margin-bottom:10px">${S.showPast ? 'Скрыть прошедшие' : `Показать прошедшие недели (${past.length})`}</button>`;
  html += dates.length ? dates.map(d => {
    const dt = parseISO(d);
    return `<div class="stit"><span>${WD[(dt.getDay() + 6) % 7]}, ${dt.getDate()} ${MONTHS[dt.getMonth()]}</span></div><div class="rb-card">${shown.filter(r => r.date === d).map(row).join('')}</div>`;
  }).join('') : '<div class="rb-card"><div class="none">Нет занятий по датам с текущей недели</div></div>';
  html += `<div class="stit" style="margin-top:28px"><span>Еженедельные занятия · ${regular.length}</span></div>` + WD.map((d, i) => {
    const list = regular.filter(r => r.weekday === i + 1);
    return `<div class="stit"><span>${d}</span><button class="add-l" data-add-lesson="${i + 1}">+ Добавить</button></div>
      <div class="rb-card">${list.length ? list.map(row).join('') : '<div class="none">Нет занятий</div>'}</div>`;
  }).join('');
  $('#a-les').innerHTML = html;
  S.adminLessons = rows;
}

function editLesson(row, weekday, dated) {
  const gid = S.adminGroup;
  dated = row ? !!row.date : dated;
  const data = row ? {...row} : {weekday, date: iso(nowMsk()), pair_no: S.meta.bells[0]?.pair_no, kind: 'lec', subgroup: 0, week_type: 'all'};
  formSheet({
    title: row ? 'Изменить занятие' : dated ? 'Занятие на дату' : 'Еженедельное занятие',
    desc: `Группа ${byId('groups', gid)?.name || ''}`,
    fields: lessonFields(dated), data,
    save: (p, force) => api(row ? `/api/admin/lessons/${row.id}` : '/api/admin/lessons', {method: row ? 'PUT' : 'POST', body: {...p, date: dated ? p.date : null, group_id: gid, force}}),
    onDone: adminLessons,
  });
}

/* — Замены — */
function changeFields() {
  return [
    {row: [{k: 'date', l: 'Дата', t: 'date', req: true}, F.pair()]},
    {row: [F.group(), F.subgroup()]},
    {k: 'action', l: 'Что происходит', t: 'select', opts: [['replace', 'Замена / перенос / доп. пара'], ['cancel', 'Пара отменена']]},
    {hint: 'Для замены заполните только то, что меняется (например, только аудиторию). Если в этом слоте пары нет — будет добавлена дополнительная пара (нужна дисциплина).'},
    F.subject(), F.kind('— как в расписании —'), F.teacher(), F.room(),
    {k: 'note', l: 'Комментарий для студентов', ph: 'Например: преподаватель на больничном'},
  ].map(f => f.k === 'subject_id' ? {...f, empty: '— как в расписании —'} : f);
}

async function adminChanges() {
  const from = S.changesFrom || iso(nowMsk());
  $('#a-body').innerHTML = `<div class="bar"><input class="inp" type="date" id="ch-from" value="${from}" aria-label="Показать начиная с даты">
    <button class="btn btn-p btn-s" id="ch-add">+ Замена</button></div><div id="ch-list"><div class="skel"></div></div>`;
  $('#ch-from').onchange = e => { S.changesFrom = e.target.value; adminChanges(); };
  $('#ch-add').onclick = () => editChange(null);
  const rows = await adminCall(() => api(`/api/admin/changes?from=${from}`));
  if (!rows || S.adminTab !== 'changes') return;
  S.adminChanges = rows;
  if (!rows.length) { $('#ch-list').innerHTML = '<div class="rb-card"><div class="none">Замен с этой даты нет</div></div>'; return; }
  const dates = [...new Set(rows.map(r => r.date))];
  $('#ch-list').innerHTML = dates.map(d => {
    const dt = parseISO(d);
    return `<div class="stit"><span>${WD[(dt.getDay() + 6) % 7] || 'Воскресенье'}, ${dt.getDate()} ${MONTHS[dt.getMonth()]}</span></div><div class="rb-card">` +
      rows.filter(r => r.date === d).map(r => {
        const what = r.action === 'cancel' ? '<span class="tg cnl">Отмена</span>' : '<span class="tg chg">Замена</span>';
        const parts = [r.subject, r.teacher && shortName(r.teacher), r.room && roomLbl(r.room)].filter(Boolean).map(esc).join(' · ');
        return `<div class="rbi"><div class="rbi-no">${r.pair_no}</div><div class="rbi-info"><div class="rbi-n">${esc(r.group_name)}${r.subgroup ? ', подгр. ' + r.subgroup : ''}</div>
          <div class="rbi-d">${what}${parts}${r.note ? ' — ' + esc(r.note) : ''}</div></div>
          <div class="rbi-a"><button class="mini" data-edit-change="${r.id}" aria-label="Изменить">✎</button><button class="mini del" data-del-change="${r.id}" aria-label="Удалить">🗑</button></div></div>`;
      }).join('') + '</div>';
  }).join('');
}

function editChange(row) {
  const data = row ? {...row} : {date: iso(nowMsk()), pair_no: S.meta.bells[0]?.pair_no, group_id: S.adminGroup || S.meta.groups[0]?.id, subgroup: 0, action: 'replace'};
  formSheet({
    title: row ? 'Изменить замену' : 'Новая замена',
    fields: changeFields(), data,
    save: p => api(row ? `/api/admin/changes/${row.id}` : '/api/admin/changes', {method: row ? 'PUT' : 'POST', body: p}),
    onDone: adminChanges,
  });
}

/* — Справочники — */
function adminRef(tab) {
  const R = REF[tab], list = S.meta[R.list];
  $('#a-body').innerHTML = `<div class="stit"><span>Всего: ${list.length}</span><button class="add-l" data-ref-add>+ Добавить ${R.noun}</button></div>
    <div class="rb-card">${list.length ? list.map(x => `<div class="rbi"><div class="rbi-info"><div class="rbi-n">${esc(R.title(x))}</div><div class="rbi-d">${esc(R.sub(x))}</div></div>
      <div class="rbi-a">${tab !== 'subjects' ? `<a class="mini" style="display:flex;align-items:center;justify-content:center;text-decoration:none" href="#/${tab.slice(0, -1)}/${x.id}" aria-label="Расписание">📅</a>` : ''}
      <button class="mini" data-ref-edit="${x.id}" aria-label="Изменить">✎</button><button class="mini del" data-ref-del="${x.id}" aria-label="Удалить">🗑</button></div></div>`).join('')
      : '<div class="none">Пока пусто</div>'}</div>`;
}

function editRef(tab, row) {
  const R = REF[tab];
  formSheet({
    title: row ? `Изменить ${R.noun}` : `Добавить ${R.noun}`,
    fields: R.fields(), data: row ? {...row} : {...(R.defaults || {})},
    save: p => api(row ? `/api/admin/${tab}/${row.id}` : `/api/admin/${tab}`, {method: row ? 'PUT' : 'POST', body: p}),
    onDone: renderAdmin,
  });
}

/* — Звонки — */
function adminBells() {
  const rows = S.meta.bells.map(b => ({...b}));
  const draw = () => {
    $('#a-body').innerHTML = `<div class="card"><h3>🔔 Расписание звонков</h3><p class="d">Время начала и конца каждой пары. Удалить можно только последнюю пару и только если на неё не стоят занятия.</p>
      <div id="bells">${rows.map((b, i) => `<div class="bell-row"><div class="rbi-no">${b.pair_no}</div>
        <input class="inp" type="time" data-bi="${i}" data-bk="starts_at" value="${b.starts_at}" aria-label="Начало ${b.pair_no} пары">
        <input class="inp" type="time" data-bi="${i}" data-bk="ends_at" value="${b.ends_at}" aria-label="Конец ${b.pair_no} пары">
        ${i === rows.length - 1 && rows.length > 1 ? `<button class="mini del" data-bdel="${i}" aria-label="Удалить пару">✕</button>` : '<span></span>'}</div>`).join('')}</div>
      <div class="sh-acts row" style="margin-top:14px"><button class="btn btn-o" id="b-add"${rows.length >= 8 ? ' disabled' : ''}>+ Пара</button><button class="btn btn-p" id="b-save">Сохранить</button></div></div>`;
    $$('[data-bi]').forEach(inp => inp.oninput = () => { rows[+inp.dataset.bi][inp.dataset.bk] = inp.value; });
    // Удаляем только последнюю пару: перенумерация сдвинула бы время у уже стоящих занятий
    $$('[data-bdel]').forEach(btn => btn.onclick = () => { rows.pop(); draw(); });
    $('#b-add').onclick = () => {
      const last = rows[rows.length - 1];
      const s = last ? toMin(last.ends_at) + 10 : 480;
      const f = m => `${pad(Math.floor(m / 60) % 24)}:${pad(m % 60)}`;
      rows.push({pair_no: rows.length + 1, starts_at: f(s), ends_at: f(s + 90)});
      draw();
    };
    $('#b-save').onclick = async () => {
      if (!await adminCall(() => api('/api/admin/bells', {method: 'PUT', body: rows}))) return;
      await refreshMeta(); toast('Звонки сохранены', 'ok'); adminBells();
    };
  };
  draw();
}

/* — Семестр — */
function adminSettings() {
  const s = S.meta.settings;
  const fields = [
    {k: 'semester_title', l: 'Название семестра', ph: 'Осенний семестр 2026/27'},
    {row: [{k: 'semester_start', l: 'Начало', t: 'date'}, {k: 'semester_end', l: 'Конец', t: 'date'}]},
    {row: [{k: 'first_week', l: 'Первая неделя', t: 'select', opts: [['odd', 'Числитель'], ['even', 'Знаменатель']]},
      {k: 'show_parity', l: 'Числитель/знаменатель', t: 'select', opts: [['1', 'Показывать'], ['0', 'Не показывать']]}]},
    {k: 'announcement', l: 'Объявление для студентов', t: 'textarea', ph: 'Показывается над расписанием; пусто — не показывать'},
  ];
  $('#a-body').innerHTML = `<div class="card"><h3>⚙ Семестр</h3><p class="d">Дата начала определяет нумерацию недель и чётность (числитель/знаменатель). Сейчас ${S.meta.week.week_no}-я неделя (${esc(S.meta.week.parity_label.toLowerCase())}).</p>
    <form id="set-f">${formHtml(fields, s)}<button class="btn btn-p" type="submit">Сохранить</button></form></div>`;
  $('#set-f').onsubmit = async ev => {
    ev.preventDefault();
    if (!await adminCall(() => api('/api/admin/settings', {method: 'PUT', body: readForm(fields)}))) return;
    await refreshMeta(); toast('Настройки сохранены', 'ok'); adminSettings();
  };
}

/* ─────────── частицы на главной (как в ПрофКвест) ─────────── */
const particles = (() => {
  const cv = $('#particles'), ctx = cv.getContext('2d');
  let pts = [], raf = 0, on = false;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  function size() {
    const r = cv.getBoundingClientRect(), dpr = Math.min(devicePixelRatio || 1, 2);
    cv.width = r.width * dpr; cv.height = r.height * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const n = Math.round(r.width * r.height / 16000);
    pts = Array.from({length: Math.min(n, 90)}, () => ({x: Math.random() * r.width, y: Math.random() * r.height, r: Math.random() * 2 + .6, vx: (Math.random() - .5) * .25, vy: -Math.random() * .3 - .05, a: Math.random() * .35 + .1}));
  }
  function frame() {
    const w = cv.clientWidth, h = cv.clientHeight;
    ctx.clearRect(0, 0, w, h);
    for (const p of pts) {
      p.x += p.vx; p.y += p.vy;
      if (p.y < -5) { p.y = h + 5; p.x = Math.random() * w; }
      if (p.x < -5) p.x = w + 5; else if (p.x > w + 5) p.x = -5;
      ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 6.283); ctx.fillStyle = `rgba(255,255,255,${p.a})`; ctx.fill();
    }
    raf = on ? requestAnimationFrame(frame) : 0;
  }
  addEventListener('resize', () => { size(); if (!on) frame(); });
  return {
    toggle(v) { on = v && !reduce; if (on && !raf) { size(); frame(); } if (reduce && v) { size(); frame(); } },
  };
})();

/* ─────────── события ─────────── */
let logoSvg = '';
function bind() {
  $('#h-mode').onclick = e => {
    const b = e.target.closest('.seg-b'); if (!b) return;
    S.mode = b.dataset.mode;
    $$('.seg-b').forEach(x => x.classList.toggle('on', x === b));
    $('#h-q').placeholder = TARGET[S.mode].ph;
    $('#h-q').value = '';
    renderSearch();
  };
  $('#h-q').oninput = renderSearch;
  $('#h-q').onkeydown = e => { if (e.key === 'Enter') { const a = $('#h-res .h-ri'); if (a) location.hash = a.getAttribute('href'); } };
  $$('[data-back]').forEach(b => b.onclick = () => { location.hash = ''; });

  $('#wk-prev').onclick = () => shiftWeek(-1);
  $('#wk-next').onclick = () => shiftWeek(1);
  $('#wk-today').onclick = () => { ({monday: S.monday, day: S.day} = currentWeek()); loadWeek(); };
  $('#dpills').onclick = e => { const b = e.target.closest('.dp'); if (b) setDay(+b.dataset.day); };
  $('#s-fav').onclick = toggleFav;
  $('#s-print').onclick = printSchedule;
  $('#s-share').onclick = async () => {
    try { await navigator.clipboard.writeText(location.href); toast('Ссылка скопирована'); }
    catch { toast(location.href); }
  };
  $('#s-body').addEventListener('click', e => {
    if (e.target.id === 'note-x') { store.set('rs_note_hidden', S.meta.settings.announcement); renderSchedule(); }
  });
  // свайп по дням на телефоне
  let sx = null, sy = null;
  $('#s-body').addEventListener('touchstart', e => { sx = e.touches[0].clientX; sy = e.touches[0].clientY; }, {passive: true});
  $('#s-body').addEventListener('touchend', e => {
    if (sx === null) return;
    const dx = e.changedTouches[0].clientX - sx, dy = e.changedTouches[0].clientY - sy;
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) {
      const n = S.day + (dx < 0 ? 1 : -1);
      if (n < 0) { S.day = 5; shiftWeek(-1); } else if (n > 5) { S.day = 0; shiftWeek(1); } else setDay(n);
    }
    sx = null;
  }, {passive: true});

  $('#a-tabs').onclick = e => { const b = e.target.closest('.a-tab'); if (b) { S.adminTab = b.dataset.tab; renderAdmin(); } };
  $('#a-logout').onclick = async () => {
    await adminCall(() => api('/api/logout', {method: 'POST', body: {}}));
    await refreshMeta().catch(() => { S.meta.admin = false; });
    renderAdmin(); toast('Вы вышли');
  };
  $('#a-body').addEventListener('click', async e => {
    const t = e.target.closest('button'); if (!t) return;
    const d = t.dataset;
    if (d.addLesson) editLesson(null, +d.addLesson, false);
    else if (d.addDated !== undefined) editLesson(null, 1, true);
    else if (d.togglePast !== undefined) { S.showPast = !S.showPast; adminLessons(); }
    else if (d.editLesson) editLesson(S.adminLessons.find(r => r.id === +d.editLesson));
    else if (d.delLesson) {
      const r = S.adminLessons.find(x => x.id === +d.delLesson);
      const when = r.date ? parseISO(r.date).toLocaleDateString('ru-RU') : WD[r.weekday - 1].toLowerCase();
      if (await confirmSheet('Удалить занятие?', `${r.subject}, ${when}, ${r.pair_no}-я пара`)) {
        if (!await adminCall(() => api(`/api/admin/lessons/${r.id}`, {method: 'DELETE'}))) return;
        toast('Удалено'); S.data = null; adminLessons();
      }
    } else if (d.editChange) editChange(S.adminChanges.find(r => r.id === +d.editChange));
    else if (d.delChange) {
      if (await confirmSheet('Удалить замену?', 'Расписание на эту дату вернётся к обычному.')) {
        if (!await adminCall(() => api(`/api/admin/changes/${d.delChange}`, {method: 'DELETE'}))) return;
        toast('Удалено'); S.data = null; adminChanges();
      }
    } else if (d.refAdd !== undefined) editRef(S.adminTab);
    else if (d.refEdit) editRef(S.adminTab, S.meta[REF[S.adminTab].list].find(x => x.id === +d.refEdit));
    else if (d.refDel) {
      const R = REF[S.adminTab], x = S.meta[R.list].find(v => v.id === +d.refDel);
      const warn = S.adminTab === 'groups' ? 'Вместе с группой удалятся все её занятия и замены.' : 'Если запись используется в расписании, удаление будет отклонено или ссылка очистится.';
      if (await confirmSheet(`Удалить ${R.noun}?`, `${R.title(x)}. ${warn}`)) {
        if (!await adminCall(() => api(`/api/admin/${S.adminTab}/${x.id}`, {method: 'DELETE'}))) return;
        await refreshMeta(); toast('Удалено'); renderAdmin();
      }
    }
  });

  $('#sheet-bg').onclick = closeSheet;
  addEventListener('keydown', e => {
    if (e.key === 'Escape') closeSheet();
    if (curScreen === 'ss' && !$('#sheet.show') && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) {
      if (e.key === 'ArrowLeft') shiftWeek(-1);
      if (e.key === 'ArrowRight') shiftWeek(1);
    }
  });
  addEventListener('hashchange', route);
  // обновление «идёт сейчас»
  setInterval(() => { if (curScreen === 'ss' && S.data && !$('#sheet.show')) { const st = $('#s-body').scrollTop; renderSchedule(); $('#s-body').scrollTop = st; } }, 30000);
}

async function boot() {
  const t0 = Date.now();
  // Панель администратора открывается по адресу /admin (кнопки на главной нет)
  if (location.pathname.replace(/\/+$/, '') === '/admin') history.replaceState(null, '', '/#/admin');
  bind();
  fetch('/logo.svg').then(r => r.text()).then(svg => { logoSvg = svg; $$('.logo-slot').forEach(el => el.innerHTML = svg); }).catch(() => {});
  try {
    S.meta = await api('/api/meta');
  } catch (e) {
    $('.sp-sub').textContent = 'Сервер недоступен: ' + e.message;
    return;
  }
  route();
  particles.toggle(curScreen === 'sh');
  setTimeout(() => $('#splash').classList.add('hide'), Math.max(0, 1100 - (Date.now() - t0)));
}
boot();
