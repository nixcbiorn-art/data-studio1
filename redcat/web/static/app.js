/* RedCat Studio — интерфейс.
   Всё общение с сервером идёт на 127.0.0.1. Чтение — GET, сохранение личных
   правок — POST на локальный сервер (пишет в файл на вашем диске).
   В сторонний API приложение ходит только GET-ом. */

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(typeof c === 'object' ? c : document.createTextNode(String(c)));
  }
  return node;
}

const NUM = new Intl.NumberFormat('ru-RU');
const fmtNum = (v, digits = 0) => (v === null || v === undefined || v === '' || isNaN(v))
  ? '—' : new Intl.NumberFormat('ru-RU', { maximumFractionDigits: digits }).format(v);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const short = (v, n = 90) => {
  const s = v === null || v === undefined ? '' : String(v);
  return s.length > n ? s.slice(0, n) + '…' : s;
};

function toast(msg, isError = false) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'show' + (isError ? ' err' : '');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { t.className = ''; }, isError ? 6000 : 2600);
}

const api = {
  async get(route, params = {}) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') qs.append(k, typeof v === 'object' ? JSON.stringify(v) : v);
    }
    const r = await fetch(`/api/${route}?${qs}`);
    const data = await r.json();
    if (data && data.error) throw new Error(data.error);
    return data;
  },
  async post(route, body = {}) {
    const r = await fetch(`/api/${route}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (data && data.error) throw new Error(data.error);
    return data;
  },
};

/* ───────────────────────── графики (инлайн SVG, без библиотек) ───────────── */
function lineChart(title, labels, series, unit = '') {
  const W = 520, H = 210, P = { l: 56, r: 12, t: 10, b: 26 };
  const all = series.flatMap((s) => s.values).filter((v) => v !== null && !isNaN(v));
  if (!all.length) return el('div', { class: 'empty tiny' }, 'нет данных');
  let lo = Math.min(...all), hi = Math.max(...all);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const x = (i) => P.l + (i * (W - P.l - P.r)) / Math.max(1, labels.length - 1);
  const y = (v) => H - P.b - ((v - lo) / (hi - lo)) * (H - P.t - P.b);
  const colors = ['#4f8cff', '#3ecf8e', '#f0b429', '#b98cff'];
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}">`;
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4, yy = y(v);
    svg += `<line x1="${P.l}" x2="${W - P.r}" y1="${yy}" y2="${yy}" stroke="#2c384e" stroke-width="1"/>`;
    svg += `<text x="${P.l - 6}" y="${yy + 4}" fill="#8fa0ba" font-size="10" text-anchor="end">${fmtNum(v, Math.abs(hi) < 100 ? 1 : 0)}</text>`;
  }
  series.forEach((s, si) => {
    const pts = s.values.map((v, i) => (v === null || isNaN(v) ? null : `${x(i)},${y(v)}`)).filter(Boolean);
    if (pts.length) svg += `<polyline points="${pts.join(' ')}" fill="none" stroke="${colors[si % 4]}" stroke-width="2" stroke-linejoin="round"/>`;
    s.values.forEach((v, i) => { if (v !== null && !isNaN(v)) svg += `<circle cx="${x(i)}" cy="${y(v)}" r="2.4" fill="${colors[si % 4]}"/>`; });
  });
  const step = Math.ceil(labels.length / 6);
  labels.forEach((l, i) => {
    if (i % step === 0 || i === labels.length - 1) {
      svg += `<text x="${x(i)}" y="${H - 8}" fill="#8fa0ba" font-size="10" text-anchor="middle">${esc(l)}</text>`;
    }
  });
  svg += '</svg>';
  const box = el('div', {}, el('h3', {}, title), el('div', { html: svg }));
  if (series.length > 1 || unit) {
    box.appendChild(el('div', { class: 'legend' }, series.map((s, i) =>
      el('span', { html: `<i style="background:${colors[i % 4]}"></i>${esc(s.name)}${unit ? ' , ' + unit : ''}` }))));
  }
  return box;
}

function barList(rows, maxLabel = 40) {
  const max = Math.max(...rows.map((r) => Math.abs(r.value) || 0), 1);
  return el('div', {}, rows.map((r) => el('div', { class: 'bar-row' },
    el('div', { title: String(r.label) }, short(r.label, maxLabel) || '(пусто)'),
    el('div', { class: 'bar-track' }, el('div', {
      class: 'bar-fill' + (r.value < 0 ? ' neg' : ''),
      style: `width:${Math.max(1, (Math.abs(r.value) / max) * 100)}%`,
    })),
    el('div', { class: 'bar-val' }, fmtNum(r.value, r.digits ?? 0)))));
}

function boxPlot(rows, maxLabel = 40) {
  const valid = rows.filter((r) => r.min !== null && r.min !== undefined
    && r.max !== null && r.max !== undefined);
  if (!valid.length) return el('div', { class: 'empty tiny' }, 'нет числовых данных для графика');
  const lo0 = Math.min(...valid.map((r) => r.min));
  const hi0 = Math.max(...valid.map((r) => r.max));
  const span0 = hi0 - lo0 || 1;
  const lo = lo0 - span0 * 0.03, hi = hi0 + span0 * 0.03;
  const span = hi - lo;
  const pct = (v) => Math.max(0, Math.min(100, ((v - lo) / span) * 100));
  const box = el('div', { class: 'box-legend' },
    el('span', {}, el('b', {}, '│─│'), ' мин…макс'),
    el('span', {}, el('b', {}, '▮'), ' 25-й…75-й перцентиль (IQR)'),
    el('span', {}, el('b', {}, '│'), ' медиана'));
  const list = el('div', {}, rows.map((r) => {
    if (r.min === null || r.min === undefined || r.max === null || r.max === undefined) {
      return el('div', { class: 'box-row' },
        el('div', { title: String(r.label) }, short(r.label, maxLabel) || '(пусто)'),
        el('div', { class: 'box-track' }), el('div', { class: 'box-val muted' }, '—'));
    }
    const wl = pct(r.min), wr = pct(r.max);
    const p25 = r.p25 ?? r.min, p75 = r.p75 ?? r.max;
    const bl = pct(p25), br = pct(p75);
    const med = pct(r.median ?? p25);
    return el('div', { class: 'box-row' },
      el('div', { title: String(r.label) }, short(r.label, maxLabel) || '(пусто)'),
      el('div', { class: 'box-track' },
        el('div', { class: 'box-whisker', style: `left:${wl}%;width:${Math.max(0, wr - wl)}%` }),
        el('div', { class: 'box-body', style: `left:${bl}%;width:${Math.max(1, br - bl)}%` }),
        el('div', { class: 'box-median', style: `left:${med}%` })),
      el('div', { class: 'box-val' }, `медиана ${fmtNum(r.median, 2)}`));
  }));
  return el('div', {}, box, list);
}

function paretoChart(title, labels, values, cumulative) {
  const W = 640, H = 260, P = { l: 54, r: 48, t: 14, b: 34 };
  if (!labels.length) return el('div', { class: 'empty tiny' }, 'нет данных');
  const maxV = Math.max(...values, 1);
  const n = labels.length;
  const plotW = W - P.l - P.r;
  const x = (i) => P.l + (i + 0.5) * (plotW / n);
  const bw = Math.max(2, (plotW / n) * 0.62);
  const yV = (v) => H - P.b - (v / maxV) * (H - P.t - P.b);
  const yC = (v) => H - P.b - (v / 100) * (H - P.t - P.b);
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}">`;
  [0, 25, 50, 75, 100].forEach((p) => {
    const yy = yC(p);
    svg += `<line x1="${P.l}" x2="${W - P.r}" y1="${yy}" y2="${yy}" stroke="#2c384e" stroke-width="1"/>`;
    svg += `<text x="${W - P.r + 6}" y="${yy + 4}" fill="#8fa0ba" font-size="10">${p}%</text>`;
  });
  const y80 = yC(80);
  svg += `<line x1="${P.l}" x2="${W - P.r}" y1="${y80}" y2="${y80}" stroke="#f0b429" stroke-width="1" stroke-dasharray="4 3"/>`;
  values.forEach((v, i) => {
    const bx = x(i) - bw / 2, by = yV(v);
    svg += `<rect x="${bx.toFixed(1)}" y="${by.toFixed(1)}" width="${bw.toFixed(1)}" height="${(H - P.b - by).toFixed(1)}" fill="#4f8cff" fill-opacity="0.55" rx="2"/>`;
  });
  const pts = cumulative.map((v, i) => `${x(i).toFixed(1)},${yC(v).toFixed(1)}`).join(' ');
  svg += `<polyline points="${pts}" fill="none" stroke="#f0b429" stroke-width="2"/>`;
  cumulative.forEach((v, i) => { svg += `<circle cx="${x(i).toFixed(1)}" cy="${yC(v).toFixed(1)}" r="2.6" fill="#f0b429"/>`; });
  const step = Math.max(1, Math.ceil(n / 8));
  labels.forEach((l, i) => {
    if (i % step === 0 || i === n - 1) {
      svg += `<text x="${x(i).toFixed(1)}" y="${H - 10}" fill="#8fa0ba" font-size="10" text-anchor="middle">${esc(short(l, 10))}</text>`;
    }
  });
  svg += '</svg>';
  const box = el('div', {}, el('h3', {}, title), el('div', { html: svg }));
  box.appendChild(el('div', { class: 'legend' },
    el('span', { html: '<i style="background:#4f8cff"></i>значение по группе (левая ось)' }),
    el('span', { html: '<i style="background:#f0b429"></i>накоплено, % (правая ось, пунктир — 80%)' })));
  return box;
}

function heatTable(rowField, rows, cols, matrix) {
  const vals = matrix.flat().filter((v) => v !== null && v !== undefined);
  const lo = vals.length ? Math.min(...vals) : 0;
  const hi = vals.length ? Math.max(...vals) : 1;
  const span = hi - lo || 1;
  const table = el('table', { class: 'heat' });
  table.appendChild(el('thead', {}, el('tr', {},
    el('th', {}, rowField), cols.map((c) => el('th', {}, short(String(c ?? '—'), 16))))));
  const body = el('tbody');
  rows.forEach((r, i) => {
    const tr = el('tr', {}, el('td', { title: String(r) }, short(r, 30) || '(пусто)'));
    cols.forEach((c, j) => {
      const v = matrix[i][j];
      let style = '', cls = 'heat-cell';
      if (v === null || v === undefined) {
        cls += ' empty-cell';
      } else {
        const t = (v - lo) / span;
        style = `background:rgba(79,140,255,${(0.08 + t * 0.55).toFixed(2)})`;
      }
      tr.appendChild(el('td', { class: cls, style, title: v === null || v === undefined ? '' : String(v) },
        v === null || v === undefined ? '—' : fmtNum(v, Number.isInteger(v) ? 0 : 2)));
    });
    body.appendChild(tr);
  });
  table.appendChild(body);
  return el('div', { class: 'table-wrap' }, table);
}

function zStrip(items, threshold) {
  if (!items.length) return el('div', { class: 'empty tiny' }, 'нет данных');
  const W = 640, H = 100, P = { l: 24, r: 24, t: 16, b: 10 };
  const zs = items.map((it) => (it.direction === 'выше' ? it.z : -it.z));
  const maxAbs = Math.max(...zs.map(Math.abs), threshold * 1.15);
  const x = (z) => P.l + ((z + maxAbs) / (2 * maxAbs)) * (W - P.l - P.r);
  const midY = (H - P.t + P.b) / 2 + 6;
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}">`;
  svg += `<line x1="${x(0).toFixed(1)}" x2="${x(0).toFixed(1)}" y1="${P.t}" y2="${H - P.b}" stroke="#2c384e"/>`;
  [threshold, -threshold].forEach((t) => {
    svg += `<line x1="${x(t).toFixed(1)}" x2="${x(t).toFixed(1)}" y1="${P.t}" y2="${H - P.b}" stroke="#f0b429" stroke-width="1" stroke-dasharray="4 3"/>`;
  });
  items.forEach((it, i) => {
    const z = it.direction === 'выше' ? it.z : -it.z;
    const jitter = ((i % 7) - 3) * 7;
    const color = it.direction === 'выше' ? '#ff5f6d' : '#4f8cff';
    svg += `<circle cx="${x(z).toFixed(1)}" cy="${(midY + jitter).toFixed(1)}" r="3.6" fill="${color}" fill-opacity="0.78">`
      + `<title>${esc(fmtNum(it.value, 2))} (z=${it.z}${it.group ? ', ' + esc(String(it.group)) : ''})</title></circle>`;
  });
  svg += `<text x="${P.l}" y="${H - 2}" fill="#8fa0ba" font-size="10">ниже медианы</text>`;
  svg += `<text x="${W - P.r}" y="${H - 2}" fill="#8fa0ba" font-size="10" text-anchor="end">выше медианы</text>`;
  svg += '</svg>';
  return el('div', { class: 'zstrip-wrap' }, el('div', { html: svg }),
    el('div', { class: 'zstrip-legend' },
      el('span', { html: '<i style="background:#ff5f6d"></i>выше медианы' }),
      el('span', { html: '<i style="background:#4f8cff"></i>ниже медианы' }),
      el('span', {}, `пунктир — порог z=${threshold}`)));
}

function scatterChart(title, points, xLabel, yLabel, line) {
  const W = 540, H = 300, P = { l: 62, r: 14, t: 12, b: 38 };
  if (!points.length) return el('div', { class: 'empty tiny' }, 'нет данных');
  const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
  let x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (x0 === x1) { x0 -= 1; x1 += 1; }
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const X = (v) => P.l + ((v - x0) / (x1 - x0)) * (W - P.l - P.r);
  const Y = (v) => H - P.b - ((v - y0) / (y1 - y0)) * (H - P.t - P.b);
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}">`;
  for (let i = 0; i <= 4; i++) {
    const v = y0 + ((y1 - y0) * i) / 4, yy = Y(v);
    svg += `<line x1="${P.l}" x2="${W - P.r}" y1="${yy}" y2="${yy}" stroke="#2c384e"/>`;
    svg += `<text x="${P.l - 6}" y="${yy + 4}" fill="#8fa0ba" font-size="10" text-anchor="end">${fmtNum(v)}</text>`;
  }
  for (let i = 0; i <= 4; i++) {
    const v = x0 + ((x1 - x0) * i) / 4;
    svg += `<text x="${X(v)}" y="${H - 20}" fill="#8fa0ba" font-size="10" text-anchor="middle">${fmtNum(v)}</text>`;
  }
  points.forEach((p) => {
    svg += `<circle cx="${X(p.x)}" cy="${Y(p.y)}" r="2.6" fill="#4f8cff" fill-opacity="0.45"/>`;
  });
  if (line && line.slope !== null && line.slope !== undefined) {
    const ya = line.slope * x0 + line.intercept, yb = line.slope * x1 + line.intercept;
    svg += `<line x1="${X(x0)}" y1="${Y(Math.max(y0, Math.min(y1, ya)))}" x2="${X(x1)}" y2="${Y(Math.max(y0, Math.min(y1, yb)))}" stroke="#f0b429" stroke-width="2" stroke-dasharray="6 4"/>`;
  }
  svg += `<text x="${(W) / 2}" y="${H - 5}" fill="#8fa0ba" font-size="11" text-anchor="middle">${esc(xLabel)}</text>`;
  svg += `<text x="12" y="${H / 2}" fill="#8fa0ba" font-size="11" text-anchor="middle" transform="rotate(-90 12 ${H / 2})">${esc(yLabel)}</text>`;
  svg += '</svg>';
  return el('div', {}, el('h3', {}, title), el('div', { html: svg }));
}

function renderTable(columns, rows, opts = {}) {
  const table = el('table');
  table.appendChild(el('thead', {}, el('tr', {}, columns.map((c) => el('th', {}, c)))));
  const body = el('tbody');
  rows.forEach((row) => {
    const tr = el('tr');
    columns.forEach((c) => {
      const v = row[c];
      const isNum = typeof v === 'number';
      tr.appendChild(el('td', { class: isNum ? 'num' : '', title: String(v ?? '') },
        isNum ? fmtNum(v, Number.isInteger(v) ? 0 : 2) : short(v, opts.max || 90)));
    });
    if (opts.onClick) { tr.style.cursor = 'pointer'; tr.addEventListener('click', () => opts.onClick(row)); }
    body.appendChild(tr);
  });
  table.appendChild(body);
  return el('div', { class: 'table-wrap' }, table);
}

/* ───────────────────────────── приложение ────────────────────────────────── */
const App = {
  meta: null,
  table: null,
  columns: [],
  idField: 'id',
  nameField: 'id',
  visible: null,
  filters: [],
  sort: null,
  desc: false,
  page: 1,
  pageSize: 100,
  lastResult: null,
  anTab: 'pivot',
  runTimer: null,
  runSince: 0,
  extTable: null,
  extColumns: [],
  crossCheck: null,
  csvUpload: { file: null },

  async init() {
    document.querySelectorAll('#nav a').forEach((a) =>
      a.addEventListener('click', () => App.show(a.dataset.view)));
    $('sqlInput').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) App.runSql();
    });
    document.querySelectorAll('#anTabs button').forEach((b) =>
      b.addEventListener('click', () => {
        document.querySelectorAll('#anTabs button').forEach((x) => x.classList.remove('active'));
        b.classList.add('active');
        App.anTab = b.dataset.an;
        App.renderAnPanel();
      }));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') App.closeDrawer(); });
    await App.loadMeta();
    await App.loadOverview();
  },

  show(view) {
    document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
    document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.view === view));
    $('view-' + view).classList.add('active');
    const loaders = {
      overview: () => App.loadOverview(),
      data: () => App.table ? null : App.switchTable(App.firstTable()),
      search: () => App.initSearch(),
      analytics: () => App.initAnalytics(),
      quality: () => App.initQuality(),
      completeness: () => App.initCompleteness(),
      checks: () => App.loadChecks(),
      changes: () => App.loadChanges(),
      edits: () => App.loadEdits(),
      collect: () => App.initCollect(),
      sources: () => App.loadSources(),
      external: () => App.initExternal(),
      sql: () => App.initSql(),
      apilog: () => App.loadApiLog(),
      tools: () => App.loadTools(),
      tools: () => App.loadTools(),
    };
    (loaders[view] || (() => {}))();
  },

  firstTable() {
    const t = (App.meta?.tables || []).filter((x) => x.name !== 'comparison_vs_previous');
    return (t[0] || App.meta?.tables?.[0] || {}).name;
  },

  async loadMeta() {
    try {
      App.meta = await api.get('meta');
    } catch (e) { toast(e.message, true); return; }
    const total = Object.values(App.meta.edit_counts || {}).reduce((a, b) => a + b, 0);
    $('badgeEdits').textContent = total || '';
    const fill = (sel, extra, exclude = []) => {
      const node = $(sel);
      if (!node) return;
      const keep = node.value;
      node.innerHTML = '';
      if (extra) node.appendChild(el('option', { value: '' }, extra));
      (App.meta.tables || [])
        .filter((t) => !exclude.includes(t.name))
        .forEach((t) => node.appendChild(
          el('option', { value: t.name },
            `${t.title || t.name} (${NUM.format(t.rows)})${t.external ? ' 📦' : ''}`)));
      if (keep && !exclude.includes(keep)) node.value = keep;
    };
    fill('dataTable'); fill('anTable'); fill('qTable');
    fill('cTable', null, ['comparison_vs_previous']);
    const ss = $('searchSource');
    ss.innerHTML = '<option value="">везде</option>';
    (App.meta.tables || []).forEach((t) =>
      ss.appendChild(el('option', { value: t.name }, (t.title || t.name) + (t.external ? ' 📦' : ''))));
    const ro = $('runOnly');
    ro.innerHTML = '';
    (App.meta.sources || []).forEach((s) => ro.appendChild(
      el('option', { value: s.key },
        `${s.title || s.key}${s.split ? ' ⧉' : ''}${s.browser ? ' 🌐' : ''}${s.external ? ' [внешний]' : ''}`)));
  },

  async loadOverview() {
    await App.loadMeta();
    const m = App.meta;
    if (!m) return;
    $('overviewHint').textContent = m.data_ok
      ? `${m.tables.length} табл. · ${m.runs} запусков в истории`
      : 'данные ещё не собраны';

    let runs = [];
    try { runs = (await api.get('runs')).runs || []; } catch (e) { /* пусто */ }
    const last = runs[runs.length - 1] || {};
    const prev = runs[runs.length - 2] || {};
    const delta = (field) => {
      if (last[field] == null || prev[field] == null || !prev[field]) return null;
      return ((last[field] - prev[field]) / prev[field]) * 100;
    };
    const kpi = (label, value, field, digits = 0) => {
      const d = field ? delta(field) : null;
      return el('div', { class: 'kpi' },
        el('div', { class: 'label' }, label),
        el('div', { class: 'value' }, fmtNum(value, digits)),
        d === null ? el('div', { class: 'delta flat' }, '—')
          : el('div', { class: 'delta ' + (d > 0 ? 'up' : d < 0 ? 'down' : 'flat') },
            `${d > 0 ? '▲' : d < 0 ? '▼' : '='} ${Math.abs(d).toFixed(1)}% к прошлому`));
    };
    const rowsTotal = m.tables.reduce((a, t) => a + t.rows, 0);
    $('kpis').replaceChildren(
      kpi('Всего записей', rowsTotal),
      kpi('Главный источник', last.apartments_count ?? rowsTotal, 'apartments_count'),
      kpi('Полнота сбора, %', last.coverage_pct, 'coverage_pct', 1),
      kpi('Изменений', last.changes_total, 'changes_total'),
      kpi('Моих правок', (m.studio || {}).edits || 0),
      kpi('Избранное', (m.studio || {}).starred || 0));

    const charts = $('overviewCharts');
    charts.replaceChildren();
    if (runs.length > 1) {
      const labels = runs.map((r) => (r.started_at || '').slice(5, 10));
      const add = (title, fields, names, unit) => {
        const series = fields.map((f, i) => ({ name: names[i], values: runs.map((r) => r[f] ?? null) }))
          .filter((s) => s.values.some((v) => v !== null));
        if (series.length) charts.appendChild(el('div', { class: 'card' }, lineChart(title, labels, series, unit)));
      };
      add('Объём данных', ['apartments_count', 'hc_count'], ['записей', 'ЖК']);
      add('Цены', ['price_avg', 'price_median'], ['средняя', 'медиана'], '₽');
      add('Цена за м²', ['price_per_sqm_avg', 'price_per_sqm_median'], ['средняя', 'медиана'], '₽/м²');
      add('Оборот', ['changes_new', 'changes_gone', 'changes_modified'], ['новые', 'ушли', 'изменены']);
      add('Полнота сбора, %', ['coverage_pct'], ['полнота']);
      add('Длительность, сек', ['duration_sec'], ['секунд']);
    } else {
      charts.appendChild(el('div', { class: 'card empty' },
        el('span', { class: 'big' }, '📈'),
        'История появится после второго запуска сбора — тогда будет с чем сравнивать.'));
    }

    $('tableList').replaceChildren(...(m.tables.length ? m.tables.map((t) =>
      el('div', { class: 'bar-row' },
        el('div', {}, el('b', {}, t.title || t.name),
          t.external ? el('span', { class: 'pill info', style: 'margin-left:6px' }, 'внешний') : null,
          el('span', { class: 'muted tiny' }, ` · ${t.columns} колонок`)),
        el('div', { class: 'bar-track' }, el('div', {
          class: 'bar-fill',
          style: `width:${Math.max(2, (t.rows / Math.max(...m.tables.map((x) => x.rows), 1)) * 100)}%`,
        })),
        el('div', { class: 'row tight' },
          el('span', { class: 'bar-val' }, NUM.format(t.rows)),
          el('button', {
            class: 'sm',
            onclick: () => {
              if (t.external) { App.show('external'); App.switchExternal(t.name); }
              else { App.show('data'); App.switchTable(t.name); }
            },
          }, 'Открыть'))))
      : [el('div', { class: 'empty' }, el('span', { class: 'big' }, '🗂️'),
        'Данных пока нет. Запустите сбор или создайте демо-набор: python demo_data.py')]));

    try {
      const files = (await api.get('files')).items || [];
      $('fileList').replaceChildren(...files.slice(0, 14).map((f) => el('div', { class: 'row tight' },
        el('span', { class: 'mono' }, f.name),
        el('span', { class: 'muted' }, `${(f.size / 1024).toFixed(0)} КБ · ${f.modified.replace('T', ' ')}`))));
    } catch (e) { /* ничего */ }
  },

  async switchTable(name) {
    if (!name) return;
    App.table = name;
    $('dataTable').value = name;
    App.filters = []; App.sort = null; App.page = 1; App.visible = null;
    const info = await api.get('columns', { table: name });
    App.columns = info.columns;
    App.idField = info.id_field;
    App.nameField = info.name_field;
    const ff = $('facetField');
    ff.innerHTML = '';
    App.columns.forEach((c) => ff.appendChild(el('option', { value: c.name }, c.label || c.name)));
    ff.value = App.columns.find((c) => /name|развитие|status|type|developer|назв/i.test(c.name))?.name || App.columns[0].name;
    App.renderFilters();
    await App.loadViews();
    await App.reload();
    await App.loadFacets();
  },

  query() {
    return {
      filters: App.filters.filter((f) => f.field),
      match: $('filterMatch').value,
      sort: App.sort, desc: App.desc, page: App.page, page_size: App.pageSize,
      include_hidden: $('showHidden').checked,
      only: $('onlyMode').value,
    };
  },

  async reload() {
    if (!App.table) return;
    try {
      const res = await api.get('query', { table: App.table, q: App.query() });
      App.lastResult = res;
      App.renderGrid(res);
      $('dataCount').textContent =
        `${NUM.format(res.total)} строк${App.filters.length ? ' (отфильтровано)' : ''}`;
    } catch (e) { toast(e.message, true); }
  },

  renderGrid(res) {
    const cols = App.visible || res.columns;
    const thead = $('dataTableEl').querySelector('thead');
    const tbody = $('dataTableEl').querySelector('tbody');
    const labelOf = (name) => {
      const info = (App.columns || []).find((x) => x.name === name);
      return (info && info.label) || name;
    };

    thead.replaceChildren(el('tr', {},
      el('th', { style: 'width:78px' }, '·'),
      cols.map((c) => el('th', {
        title: `клик — сортировка (${c})`,
        onclick: () => { App.desc = App.sort === c ? !App.desc : false; App.sort = c; App.page = 1; App.reload(); },
      }, labelOf(c), App.sort === c ? el('span', { class: 'sort' }, App.desc ? '▼' : '▲') : null))));

    tbody.replaceChildren(...res.rows.map((row) => {
      const rid = row[res.id_field];
      const tr = el('tr', { class: row._hidden ? 'hidden-row' : '' });
      tr.appendChild(el('td', {}, el('div', { class: 'rowact' },
        el('button', {
          class: row._star ? 'star-on' : '', title: 'в избранное',
          onclick: () => App.flag(rid, 'star', !row._star),
        }, row._star ? '★' : '☆'),
        el('button', { title: 'скрыть из выдачи', onclick: () => App.flag(rid, 'hidden', !row._hidden) }, '🚫'),
        el('button', { title: 'карточка записи', onclick: () => App.openRecord(rid) }, '⋯'))));
      cols.forEach((c) => {
        const v = row[c];
        const edited = (row._edited || []).includes(c);
        const td = el('td', {
          class: [typeof v === 'number' ? 'num' : '', 'editable', edited ? 'edited' : ''].filter(Boolean).join(' '),
          title: edited ? `было: ${row._original[c]}` : String(v ?? ''),
        }, typeof v === 'number' ? fmtNum(v, Number.isInteger(v) ? 0 : 2) : short(v));
        td.addEventListener('dblclick', () => App.editCell(td, rid, c, v));
        tr.appendChild(td);
      });
      return tr;
    }));

    const pager = $('dataPager');
    pager.replaceChildren(
      el('button', { class: 'sm', disabled: res.page <= 1, onclick: () => { App.page = 1; App.reload(); } }, '«'),
      el('button', { class: 'sm', disabled: res.page <= 1, onclick: () => { App.page--; App.reload(); } }, '‹ назад'),
      el('span', {}, `стр. ${res.page} из ${res.pages}`),
      el('button', { class: 'sm', disabled: res.page >= res.pages, onclick: () => { App.page++; App.reload(); } }, 'вперёд ›'),
      el('button', { class: 'sm', disabled: res.page >= res.pages, onclick: () => { App.page = res.pages; App.reload(); } }, '»'),
      el('span', { class: 'muted' }, 'на странице:'),
      el('select', {
        onchange: (e) => { App.pageSize = +e.target.value; App.page = 1; App.reload(); },
      }, [50, 100, 250, 500].map((n) => el('option', { value: n, selected: n === App.pageSize ? '' : null }, n))));
  },

  editCell(td, rid, field, oldValue) {
    if (td.querySelector('input')) return;
    const input = el('input', { class: 'cell-input', value: oldValue ?? '' });
    td.replaceChildren(input);
    input.focus(); input.select();
    let done = false;
    const finish = async (save) => {
      if (done) return; done = true;
      const value = input.value;
      if (!save || String(value) === String(oldValue ?? '')) { App.reload(); return; }
      try {
        await api.post('edit', { table: App.table, id: rid, field, value, old_value: oldValue });
        toast('Правка сохранена локально');
        await App.loadMeta();
        App.reload();
      } catch (e) { toast(e.message, true); App.reload(); }
    };
    input.addEventListener('blur', () => finish(true));
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') finish(true);
      if (e.key === 'Escape') finish(false);
    });
  },

  async flag(rid, flag, on) {
    try {
      await api.post('flag', { table: App.table, id: rid, flag, on });
      App.reload();
    } catch (e) { toast(e.message, true); }
  },

  toggleFilters() {
    const p = $('filterPanel');
    p.style.display = p.style.display === 'none' ? 'block' : 'none';
  },

  addFilter(preset) {
    App.filters.push(preset || { field: App.columns[0]?.name, op: 'contains', value: '' });
    App.renderFilters();
    $('filterPanel').style.display = 'block';
  },

  clearFilters() { App.filters = []; App.renderFilters(); App.page = 1; App.reload(); },

  renderFilters() {
    const OPS = {
      eq: 'равно', ne: 'не равно', gt: 'больше', gte: '≥', lt: 'меньше', lte: '≤',
      between: 'в диапазоне', contains: 'содержит', notcontains: 'не содержит',
      starts: 'начинается с', ends: 'заканчивается', empty: 'пусто',
      notempty: 'не пусто', in: 'один из (через ;)', regex: 'рег. выражение',
    };
    $('filterRows').replaceChildren(...App.filters.map((f, i) => el('div', { class: 'filter-row' },
      el('select', { onchange: (e) => { f.field = e.target.value; } },
        App.columns.map((c) => el('option', { value: c.name, selected: c.name === f.field ? '' : null }, c.label || c.name))),
      el('select', { onchange: (e) => { f.op = e.target.value; App.renderFilters(); } },
        Object.entries(OPS).map(([k, v]) => el('option', { value: k, selected: k === f.op ? '' : null }, v))),
      ['empty', 'notempty'].includes(f.op) ? el('span', { class: 'muted tiny' }, '—')
        : f.op === 'between'
          ? el('div', { class: 'row tight' },
            el('input', { value: f.values?.[0] ?? '', placeholder: 'от', style: 'width:48%', onchange: (e) => { f.values = [e.target.value, f.values?.[1] ?? '']; } }),
            el('input', { value: f.values?.[1] ?? '', placeholder: 'до', style: 'width:48%', onchange: (e) => { f.values = [f.values?.[0] ?? '', e.target.value]; } }))
          : el('input', {
            value: f.value ?? '', placeholder: 'значение',
            onchange: (e) => {
              if (f.op === 'in') f.values = e.target.value.split(';').map((s) => s.trim()).filter(Boolean);
              else f.value = e.target.value;
            },
            onkeydown: (e) => { if (e.key === 'Enter') { e.target.blur(); App.page = 1; App.reload(); } },
          }),
      el('button', { class: 'sm ghost', onclick: () => { App.filters.splice(i, 1); App.renderFilters(); App.reload(); } }, '✕'))));
  },

  async loadFacets() {
    if (!App.table) return;
    const field = $('facetField').value;
    try {
      const res = await api.get('facets', {
        table: App.table, field, q: { filters: App.filters.filter((f) => f.field), match: $('filterMatch').value },
      });
      const max = Math.max(...res.values.map((v) => v.count), 1);
      $('facetList').replaceChildren(...res.values.map((v) => el('div', {
        class: 'bar-row', style: 'cursor:pointer;grid-template-columns:1fr auto',
        title: 'кликните, чтобы отфильтровать',
        onclick: () => { App.filters.push({ field, op: 'eq', value: v.value }); App.renderFilters(); App.page = 1; App.reload(); },
      },
        el('div', {},
          el('div', { style: 'overflow:hidden;text-overflow:ellipsis' }, short(v.value, 26) || '(пусто)'),
          el('div', { class: 'bar-track', style: 'height:5px;margin-top:2px' },
            el('div', { class: 'bar-fill', style: `width:${(v.count / max) * 100}%` }))),
        el('span', { class: 'bar-val' }, NUM.format(v.count)))));
    } catch (e) { $('facetList').textContent = e.message; }
  },

  async openColumns() {
    const cols = App.lastResult?.columns || App.columns.map((c) => c.name);
    const vis = new Set(App.visible || cols);
    let computed = [];
    try { computed = (await api.get('columns', { table: App.table })).computed || []; } catch (e) { /* пусто */ }
    const nameInput = el('input', { placeholder: 'название колонки', style: 'width:100%' });
    const exprInput = el('input', { placeholder: 'price / total_area', style: 'width:100%' });
    const labelOf = (name) => {
      const info = (App.columns || []).find((x) => x.name === name);
      return (info && info.label) || name;
    };

    App.drawer('Колонки таблицы',
      el('div', {},
        el('div', { class: 'row tight', style: 'margin-bottom:10px' },
          el('button', { class: 'sm', onclick: () => { App.visible = null; App.reload(); App.openColumns(); } }, 'Показать все'),
          el('button', { class: 'sm', onclick: () => { App.visible = [App.idField, App.nameField]; App.reload(); App.openColumns(); } }, 'Только ключевые')),
        ...cols.map((c) => el('label', { class: 'inline', style: 'display:flex;padding:3px 0' },
          el('input', {
            type: 'checkbox', checked: vis.has(c) ? '' : null,
            onchange: (e) => {
              if (e.target.checked) vis.add(c); else vis.delete(c);
              App.visible = cols.filter((x) => vis.has(x));
              App.reload();
            },
          }), labelOf(c))),
        el('h3', { style: 'margin-top:18px' }, 'Своя колонка-формула'),
        el('div', { class: 'tiny muted', style: 'margin-bottom:8px' },
          'Считается на лету из других колонок. Например: price / total_area.'),
        ...computed.map((c) => el('div', { class: 'row tight', style: 'padding:3px 0' },
          el('span', { class: 'chip' }, c.name), el('span', { class: 'tiny mono muted' }, c.expr),
          el('div', { class: 'spacer' }),
          el('button', {
            class: 'sm ghost',
            onclick: async () => {
              await api.post('computed_delete', { table: App.table, name: c.name });
              App.reload(); App.openColumns();
            },
          }, '✕'))),
        nameInput, el('div', { style: 'height:6px' }), exprInput,
        el('button', {
          class: 'primary sm', style: 'margin-top:8px',
          onclick: async () => {
            try {
              await api.post('computed_save', {
                table: App.table, name: nameInput.value.trim(), expr: exprInput.value.trim(),
              });
              toast('Колонка добавлена'); App.visible = null; App.reload(); App.openColumns();
            } catch (e) { toast(e.message, true); }
          },
        }, 'Добавить колонку')));
  },

  openBulk() {
    if (!App.table) return;
    const state = { field: App.columns[0].name, mode: 'set', value: '', find: '' };
    const modes = {
      set: 'установить значение', replace: 'заменить текст',
      regex: 'заменить по рег. выражению', trim: 'убрать лишние пробелы',
      formula: 'посчитать по формуле',
    };
    const body = el('div', {});
    const render = () => body.replaceChildren(
      el('p', { class: 'tiny muted' },
        `Правка применится ко всем строкам, подходящим под текущий фильтр
         (сейчас: ${App.filters.length ? App.filters.length + ' условие(й)' : 'фильтра нет — вся таблица'}).
         Данные в API не отправляются.`),
      el('label', { class: 'field' }, 'колонка',
        el('select', { onchange: (e) => { state.field = e.target.value; } },
          App.columns.map((c) => el('option', { value: c.name, selected: c.name === state.field ? '' : null }, c.label || c.name)))),
      el('label', { class: 'field', style: 'margin-top:9px' }, 'действие',
        el('select', { onchange: (e) => { state.mode = e.target.value; render(); } },
          Object.entries(modes).map(([k, v]) => el('option', { value: k, selected: k === state.mode ? '' : null }, v)))),
      ['replace', 'regex'].includes(state.mode)
        ? el('label', { class: 'field', style: 'margin-top:9px' }, 'что искать',
          el('input', { value: state.find, onchange: (e) => { state.find = e.target.value; } })) : null,
      state.mode !== 'trim'
        ? el('label', { class: 'field', style: 'margin-top:9px' },
          state.mode === 'formula' ? 'формула (например price / total_area)' : 'новое значение',
          el('input', { value: state.value, onchange: (e) => { state.value = e.target.value; } })) : null,
      el('div', { class: 'row', style: 'margin-top:13px' },
        el('button', {
          class: 'primary',
          onclick: async () => {
            try {
              const r = await api.post('edit_bulk', {
                table: App.table, field: state.field, mode: state.mode,
                value: state.value, find: state.find,
                filters: App.filters.filter((f) => f.field), match: $('filterMatch').value,
              });
              toast(`Изменено строк: ${r.updated}`);
              App.closeDrawer(); await App.loadMeta(); App.reload();
            } catch (e) { toast(e.message, true); }
          },
        }, 'Применить'),
        el('button', { onclick: () => App.closeDrawer() }, 'Отмена')));
    render();
    App.drawer('Массовая правка', body);
  },

  async saveView() {
    const name = prompt('Название сохранённого вида:');
    if (!name) return;
    try {
      await api.post('view_save', {
        name, table: App.table,
        payload: { filters: App.filters, match: $('filterMatch').value, sort: App.sort, desc: App.desc, visible: App.visible },
      });
      toast('Вид сохранён'); App.loadViews();
    } catch (e) { toast(e.message, true); }
  },

  async loadViews() {
    try {
      const res = await api.get('views', { source: App.table });
      $('savedViews').replaceChildren(...(res.items.length
        ? res.items.map((v) => el('span', { class: 'chip' },
          el('span', { style: 'cursor:pointer', onclick: () => App.applyView(v) }, v.name),
          el('button', { title: 'удалить', onclick: async () => { await api.post('view_delete', { id: v.id }); App.loadViews(); } }, '×')))
        : [el('span', { class: 'tiny muted' }, 'сохранённых видов пока нет')]));
    } catch (e) { /* ничего */ }
  },

  applyView(v) {
    const p = v.payload || {};
    App.filters = p.filters || [];
    App.sort = p.sort; App.desc = !!p.desc; App.visible = p.visible || null;
    $('filterMatch').value = p.match || 'AND';
    App.page = 1; App.renderFilters(); App.reload();
  },

  exportData(format) {
    const qs = new URLSearchParams({ table: App.table, format, what: 'table', q: JSON.stringify(App.query()) });
    window.location = `/api/export?${qs}`;
  },

  exportEdits(format) { window.location = `/api/export?what=edits&format=${format}`; },

  exportCompleteness(what, format) {
    const table = $('cTable').value;
    if (!table) { toast('Не выбрана таблица', true); return; }
    const threshold = +$('cThreshold').value || 80;
    const filters = (App.table === table)
      ? App.filters.filter((f) => f.field) : [];
    const qs = new URLSearchParams({
      what: what === 'fields' ? 'completeness_fields' : 'completeness_records',
      format, table, threshold,
      q: JSON.stringify({ filters, match: $('filterMatch').value }),
    });
    window.location = `/api/export?${qs}`;
  },

  exportExternal(format) {
    if (!App.extTable) { toast('Не выбрана таблица', true); return; }
    const qs = new URLSearchParams({ table: App.extTable, format, what: 'table' });
    window.location = `/api/export?${qs}`;
  },

  exportCrossCheck(format) {
    if (!App.extTable) { toast('Не выбрана таблица', true); return; }
    const qs = new URLSearchParams({
      what: 'cross_check', source: App.extTable, format,
    });
    window.location = `/api/export?${qs}`;
  },

  drawer(title, content) {
    const d = $('drawer');
    d.replaceChildren(
      el('div', { class: 'row', style: 'margin-bottom:13px' },
        el('h2', { style: 'margin:0;font-size:16px' }, title),
        el('div', { class: 'spacer' }),
        el('button', { class: 'ghost', onclick: () => App.closeDrawer() }, '✕')),
      content);
    d.classList.add('open');
  },
  closeDrawer() { $('drawer').classList.remove('open'); },

  async openRecord(rid) {
    try {
      const r = await api.get('record', { table: App.table, id: rid });
      const row = r.row;
      const labelOf = (name) => {
        const info = (App.columns || []).find((x) => x.name === name);
        return (info && info.label) || name;
      };
      const dl = el('dl', {});
      Object.entries(row).forEach(([k, v]) => {
        if (k.startsWith('_')) return;
        const edited = (row._edited || []).includes(k);
        dl.appendChild(el('dt', { title: k }, labelOf(k)));
        dl.appendChild(el('dd', { style: edited ? 'color:var(--edit)' : '' },
          String(v ?? '—'), edited ? el('span', { class: 'tiny muted' }, ` (было: ${row._original[k]})`) : null));
      });
      const noteBox = el('textarea', { rows: 2, placeholder: 'заметка к записи…' });
      noteBox.value = row._note || '';
      const tagBox = el('input', { placeholder: 'добавить тег и Enter' });
      tagBox.addEventListener('keydown', async (e) => {
        if (e.key !== 'Enter' || !e.target.value.trim()) return;
        await api.post('tag', { table: App.table, id: rid, tag: e.target.value.trim(), on: true });
        e.target.value = ''; App.openRecord(rid); App.reload();
      });

      const content = el('div', {},
        el('div', { class: 'row tight', style: 'margin-bottom:11px' },
          el('button', { class: 'sm', onclick: () => App.flag(rid, 'star', !row._star) }, row._star ? '★ в избранном' : '☆ в избранное'),
          el('button', { class: 'sm', onclick: () => App.flag(rid, 'hidden', !row._hidden) }, row._hidden ? 'вернуть' : 'скрыть'),
          r.edits.some((e) => e.active) ? el('button', {
            class: 'sm danger',
            onclick: async () => { await api.post('edit_revert', { table: App.table, id: rid }); toast('Правки записи отменены'); App.openRecord(rid); App.reload(); },
          }, '↺ откатить правки') : null),
        el('div', { class: 'row tight', style: 'margin-bottom:9px' },
          (row._tags || []).map((t) => el('span', { class: 'chip' }, t,
            el('button', {
              onclick: async () => { await api.post('tag', { table: App.table, id: rid, tag: t, on: false }); App.openRecord(rid); App.reload(); },
            }, '×')))),
        tagBox,
        el('div', { style: 'margin-top:9px' }, noteBox,
          el('button', {
            class: 'sm', style: 'margin-top:5px',
            onclick: async () => { await api.post('note', { table: App.table, id: rid, note: noteBox.value }); toast('Заметка сохранена'); App.reload(); },
          }, 'Сохранить заметку')),
        el('h3', { style: 'margin-top:16px' }, 'Значения'), dl,
        el('div', { id: 'relatedBox' }),
        r.numeric_fields.length ? el('div', {},
          el('h3', { style: 'margin-top:16px' }, 'История значения'),
          el('select', {
            onchange: (e) => App.recordSeries(rid, e.target.value),
          }, r.numeric_fields.map((f) => el('option', { value: f }, labelOf(f)))),
          el('div', { id: 'seriesBox', style: 'margin-top:9px' })) : null,
        r.edits.length ? el('div', {},
          el('h3', { style: 'margin-top:16px' }, 'История правок'),
          el('div', { class: 'tiny' }, r.edits.map((e) => el('div', { style: 'padding:3px 0;border-bottom:1px solid var(--line)' },
            el('span', { class: e.active ? '' : 'muted' }, `${labelOf(e.field)}: ${e.old_value ?? '—'} → ${e.new_value}`),
            el('span', { class: 'muted' }, ` · ${e.ts.replace('T', ' ')}${e.active ? '' : ' (отменена)'}`))))) : null);
      App.drawer(`Запись ${rid}`, content);
      if (r.numeric_fields.length) App.recordSeries(rid, r.numeric_fields[0]);
      App.loadRelated(rid);
    } catch (e) { toast(e.message, true); }
  },

  async loadRelated(rid) {
    const box = $('relatedBox');
    if (!box) return;
    try {
      const r = await api.get('related', { table: App.table, id: rid });
      if (!r.groups.length) { box.replaceChildren(); return; }
      box.replaceChildren(
        el('h3', { style: 'margin-top:16px' }, 'Связанные записи'),
        ...r.groups.map((g) => el('div', { style: 'margin-bottom:11px' },
          el('div', { class: 'row tight' },
            el('b', {}, g.table),
            el('span', { class: 'pill info' }, `${NUM.format(g.count)}`),
            el('div', { class: 'spacer' }),
            el('button', {
              class: 'sm',
              onclick: async () => {
                App.closeDrawer();
                await App.switchTable(g.table);
                App.filters = [{
                  field: g.direction === 'child' ? g.filter_field : g.id_field,
                  op: 'eq', value: g.link_value,
                }];
                App.renderFilters();
                $('filterPanel').style.display = 'block';
                App.page = 1;
                App.reload();
              },
            }, 'Показать все →')),
          el('div', { class: 'tiny muted' }, g.rows.slice(0, 3).map((row) =>
            el('div', { style: 'padding:2px 0;overflow:hidden;text-overflow:ellipsis' },
              short(Object.values(row).filter((v) =>
                v !== null && typeof v !== 'object').slice(0, 4).join(' · '), 70)))))));
    } catch (e) { box.replaceChildren(); }
  },

  async recordSeries(rid, field) {
    const box = $('seriesBox');
    if (!box) return;
    try {
      const r = await api.get('record_series', { source: App.table, id: rid, field });
      if (!r.series.length) {
        box.replaceChildren(el('div', { class: 'tiny muted' },
          'История по этой записи ещё не накоплена — она появляется после нескольких запусков сбора.'));
        return;
      }
      box.replaceChildren(lineChart(field, r.series.map((s) => (s.ts || '').slice(5, 10)),
        [{ name: field, values: r.series.map((s) => s.value) }]));
    } catch (e) { box.textContent = e.message; }
  },

  async initSearch() {
    const m = App.meta?.search || {};
    $('searchMeta').textContent = m.built_at
      ? `индекс: ${NUM.format(m.docs)} записей, собран ${m.built_at.replace('T', ' ')}`
      : 'индекс ещё не построен';
  },

  async rebuildIndex() {
    toast('Строю индекс, это может занять минуту…');
    try {
      const r = await api.post('search_rebuild');
      toast(`Индекс готов: ${NUM.format(r.documents)} записей`);
      await App.loadMeta(); App.initSearch();
    } catch (e) { toast(e.message, true); }
  },

  async runSearch() {
    const q = $('searchInput').value.trim();
    if (!q) return;
    try {
      const r = await api.get('search', { q, source: $('searchSource').value, limit: 120 });
      const box = $('searchResults');
      if (!r.results.length) {
        box.replaceChildren(el('div', { class: 'card empty' }, el('span', { class: 'big' }, '🔍'),
          r.meta.built_at ? 'Ничего не найдено.' : 'Индекс не построен — нажмите «Перестроить индекс».'));
        return;
      }
      if (r.results[0].error) { box.replaceChildren(el('div', { class: 'card empty' }, r.results[0].error)); return; }
      box.replaceChildren(el('div', { class: 'card' },
        el('h3', {}, `Найдено: ${r.results.length}`),
        ...r.results.map((x) => el('div', {
          style: 'padding:8px 0;border-bottom:1px solid var(--line);cursor:pointer',
          onclick: () => { App.show('data'); App.switchTable(x.source).then(() => App.openRecord(x.record_id)); },
        },
          el('div', {}, el('span', { class: 'pill info' }, x.source), ' ',
            el('b', {}, x.title || x.record_id),
            el('span', { class: 'muted tiny' }, ` · id ${x.record_id}`)),
          el('div', { class: 'tiny muted', html: esc(x.snippet).replace(/«/g, '<b style="color:var(--accent)">').replace(/»/g, '</b>') })))));
    } catch (e) { toast(e.message, true); }
  },

  initAnalytics() {
    if (!$('anTable').value) $('anTable').value = App.table || App.firstTable();
    App.analyticsTableChanged();
  },

  async analyticsTableChanged() {
    const table = $('anTable').value;
    if (!table) return;
    const info = await api.get('columns', { table });
    App.anColumns = info.columns;
    App.renderAnPanel();
  },

  anFilters() {
    return $('anUseFilters').checked && $('anTable').value === App.table
      ? { filters: App.filters.filter((f) => f.field), match: $('filterMatch').value } : {};
  },

  renderAnPanel() {
    const cols = App.anColumns || [];
    const names = cols.map((c) => c.name);
    const labels = Object.fromEntries(cols.map((c) => [c.name, c.label || c.name]));
    const numeric = cols.filter((c) => /INT|REAL|NUM|FLOA/.test(c.type)).map((c) => c.name);
    const sel = (id, list, extra) => el('select', { id },
      (extra ? [el('option', { value: '' }, extra)] : [])
        .concat(list.map((n) => el('option', { value: n }, labels[n] || n))));
    const panel = $('anPanel');
    $('anResult').replaceChildren();

    if (App.anTab === 'pivot') {
      panel.replaceChildren(el('div', { class: 'card' },
        el('div', { class: 'row' },
          el('label', { class: 'field' }, 'разрез (группировка)', sel('anDim', names)),
          el('label', { class: 'field' }, 'агрегация', el('select', { id: 'anAgg' },
            Object.entries({
              count: 'количество', count_distinct: 'уникальных', sum: 'сумма', avg: 'среднее',
              median: 'медиана', min: 'минимум', max: 'максимум', p25: '25-й перцентиль', p75: '75-й перцентиль',
            }).map(([k, v]) => el('option', { value: k }, v)))),
          el('label', { class: 'field' }, 'поле значения', sel('anMetric', numeric.length ? numeric : names, '— не нужно —')),
          el('button', { class: 'primary', onclick: () => App.runPivot() }, 'Построить'))));
    } else if (App.anTab === 'cross') {
      panel.replaceChildren(el('div', { class: 'card' }, el('div', { class: 'row' },
        el('label', { class: 'field' }, 'строки', sel('anRow', names)),
        el('label', { class: 'field' }, 'колонки', sel('anCol', names)),
        el('label', { class: 'field' }, 'агрегация', el('select', { id: 'anAgg2' },
          Object.entries({ count: 'количество', avg: 'среднее', median: 'медиана', sum: 'сумма' })
            .map(([k, v]) => el('option', { value: k }, v)))),
        el('label', { class: 'field' }, 'поле значения', sel('anMetric2', numeric.length ? numeric : names, '— не нужно —')),
        el('button', { class: 'primary', onclick: () => App.runCross() }, 'Построить'))));
    } else if (App.anTab === 'summary') {
      panel.replaceChildren(el('div', { class: 'card' },
        el('div', { class: 'row' },
          el('label', { class: 'field' }, 'разрез', sel('anDim', names)),
          el('label', { class: 'field' }, 'числовое поле', sel('anMetric', numeric.length ? numeric : names)),
          el('button', { class: 'primary', onclick: () => App.runSummary() }, 'Построить'))));
    } else if (App.anTab === 'trend') {
      const dateLike = names.filter((n) => /date|дата|created|updated|deadline|_at|from|to/i.test(n));
      panel.replaceChildren(el('div', { class: 'card' },
        el('div', { class: 'row' },
          el('label', { class: 'field' }, 'колонка с датой', sel('anDate', dateLike.length ? dateLike : names)),
          el('label', { class: 'field' }, 'детализация', el('select', { id: 'anGran' },
            Object.entries({ day: 'по дням', week: 'по неделям', month: 'по месяцам', year: 'по годам' })
              .map(([k, v]) => el('option', { value: k, selected: k === 'month' ? '' : null }, v)))),
          el('label', { class: 'field' }, 'агрегация', el('select', { id: 'anAgg3' },
            Object.entries({ count: 'количество', avg: 'среднее', median: 'медиана', sum: 'сумма', max: 'максимум' })
              .map(([k, v]) => el('option', { value: k }, v)))),
          el('label', { class: 'field' }, 'поле значения', sel('anMetric3', numeric.length ? numeric : names, '— не нужно —')),
          el('button', { class: 'primary', onclick: () => App.runTrend() }, 'Построить'))));
    } else if (App.anTab === 'corr') {
      panel.replaceChildren(el('div', { class: 'card' },
        el('div', { class: 'row' },
          el('label', { class: 'field' }, 'поле X', sel('anX', numeric.length ? numeric : names)),
          el('label', { class: 'field' }, 'поле Y', sel('anY', numeric.length ? numeric : names)),
          el('button', { class: 'primary', onclick: () => App.runCorr() }, 'Посчитать'))));
    } else if (App.anTab === 'pareto') {
      panel.replaceChildren(el('div', { class: 'card' },
        el('div', { class: 'row' },
          el('label', { class: 'field' }, 'разрез', sel('anDim', names)),
          el('label', { class: 'field' }, 'агрегация', el('select', { id: 'anAgg4' },
            Object.entries({ count: 'количество', sum: 'сумма' }).map(([k, v]) => el('option', { value: k }, v)))),
          el('label', { class: 'field' }, 'поле значения', sel('anMetric4', numeric.length ? numeric : names, '— не нужно —')),
          el('button', { class: 'primary', onclick: () => App.runPareto() }, 'Построить'))));
    } else if (App.anTab === 'hist') {
      panel.replaceChildren(el('div', { class: 'card' }, el('div', { class: 'row' },
        el('label', { class: 'field' }, 'числовое поле', sel('anField', numeric.length ? numeric : names)),
        el('label', { class: 'field' }, 'интервалов', el('input', { id: 'anBins', type: 'number', value: 20, min: 4, max: 60, style: 'width:90px' })),
        el('button', { class: 'primary', onclick: () => App.runHist() }, 'Построить'))));
    } else if (App.anTab === 'top') {
      panel.replaceChildren(el('div', { class: 'card' }, el('div', { class: 'row' },
        el('label', { class: 'field' }, 'поле', sel('anField', numeric.length ? numeric : names)),
        el('label', { class: 'field' }, 'подпись', sel('anLabel', names, '— id —')),
        el('label', { class: 'field' }, 'сколько', el('input', { id: 'anN', type: 'number', value: 10, min: 3, max: 50, style: 'width:80px' })),
        el('button', { class: 'primary', onclick: () => App.runTop() }, 'Показать'))));
    } else if (App.anTab === 'outliers') {
      panel.replaceChildren(el('div', { class: 'card' },
        el('div', { class: 'row' },
          el('label', { class: 'field' }, 'числовое поле', sel('anField', numeric.length ? numeric : names)),
          el('label', { class: 'field' }, 'искать внутри групп', sel('anGroup', names, '— по всей таблице —')),
          el('label', { class: 'field' }, 'порог', el('input', { id: 'anZ', type: 'number', value: 3.5, step: 0.5, min: 1.5, style: 'width:90px' })),
          el('button', { class: 'primary', onclick: () => App.runOutliers() }, 'Найти'))));
    } else {
      panel.replaceChildren(el('div', { class: 'card' },
        el('div', { class: 'row' },
          el('label', { class: 'field' }, 'ключ дубликата', sel('anField', names)),
          el('label', { class: 'field' }, 'и (необязательно)', sel('anField2', names, '— только первое —')),
          el('button', { class: 'primary', onclick: () => App.runDupes() }, 'Найти'))));
    }
  },

  async runPivot() {
    try {
      const r = await api.get('pivot', {
        table: $('anTable').value, dimensions: $('anDim').value,
        metric: $('anMetric').value, agg: $('anAgg').value, q: App.anFilters(), limit: 60,
      });
      const dim = r.dimensions[0];
      $('anResult').replaceChildren(el('div', { class: 'card' },
        el('h3', {}, `${r.agg_label}${r.metric ? ' · ' + r.metric : ''} по «${dim}»`),
        barList(r.data.map((d) => ({ label: d[dim], value: d.value, digits: 2 })))),
        renderTable([dim, 'value', 'rows_count'], r.data.map((d) => ({
          [dim]: d[dim], value: d.value, rows_count: d.rows_count,
        }))));
    } catch (e) { toast(e.message, true); }
  },

  async runCross() {
    try {
      const r = await api.get('crosstab', {
        table: $('anTable').value, row_field: $('anRow').value, col_field: $('anCol').value,
        metric: $('anMetric2').value, agg: $('anAgg2').value, q: App.anFilters(),
      });
      if (!r.rows.length) {
        $('anResult').replaceChildren(el('div', { class: 'card empty' }, 'Нет данных для этой пары полей.'));
        return;
      }
      $('anResult').replaceChildren(el('div', { class: 'card' },
        el('h3', {}, `${r.agg_label}: ${r.row_field} × ${r.col_field}`),
        heatTable(r.row_field, r.rows, r.cols, r.matrix)));
    } catch (e) { toast(e.message, true); }
  },

  async runSummary() {
    try {
      const r = await api.get('group_summary', {
        table: $('anTable').value, dimension: $('anDim').value,
        metric: $('anMetric').value, q: App.anFilters(),
      });
      if (!r.groups.length) {
        $('anResult').replaceChildren(el('div', { class: 'card empty' }, 'Нет данных для сводки.'));
        return;
      }
      const rows = r.groups.map((g) => ({
        [r.dimension]: g.group ?? '(пусто)', строк: g.rows_count,
        среднее: g.avg, медиана: g.median, 'разброс (IQR)': g.iqr,
        минимум: g.min, максимум: g.max, сумма: g.sum,
      }));
      $('anResult').replaceChildren(
        el('div', { class: 'card' },
          el('h3', {}, `«${r.metric}» по «${r.dimension}» · ${r.groups.length} групп`),
          boxPlot(r.groups.map((g) => ({
            label: g.group ?? '(пусто)', min: g.min, p25: g.p25, median: g.median,
            p75: g.p75, max: g.max,
          })))),
        el('div', { class: 'card' }, el('h3', {}, 'Полная таблица'), renderTable(Object.keys(rows[0] || {}), rows)));
    } catch (e) { toast(e.message, true); }
  },

  async runTrend() {
    try {
      const r = await api.get('timeseries', {
        table: $('anTable').value, date_field: $('anDate').value,
        granularity: $('anGran').value, agg: $('anAgg3').value,
        metric: $('anMetric3').value, q: App.anFilters(),
      });
      if (!r.data.length) {
        $('anResult').replaceChildren(el('div', { class: 'card empty' },
          `В колонке «${r.date_field}» не нашлось разбираемых дат.`));
        return;
      }
      $('anResult').replaceChildren(el('div', { class: 'card' },
        lineChart(`${r.agg_label}${r.metric ? ' · ' + r.metric : ''} ${r.granularity_label}`,
          r.data.map((d) => d.period), [{ name: r.metric || 'записей', values: r.data.map((d) => d.value) }])),
        el('div', { class: 'card' },
          el('h3', {}, 'Значения по периодам'),
          renderTable(['период', 'значение', 'строк'],
            r.data.map((d) => ({ 'период': d.period, 'значение': d.value, 'строк': d.rows_count })))));
    } catch (e) { toast(e.message, true); }
  },

  async runCorr() {
    try {
      const r = await api.get('correlation', {
        table: $('anTable').value, x: $('anX').value, y: $('anY').value, q: App.anFilters(),
      });
      if (r.message) { $('anResult').replaceChildren(el('div', { class: 'card empty' }, r.message)); return; }
      const strong = Math.abs(r.r) >= 0.6;
      $('anResult').replaceChildren(el('div', { class: 'card' },
        el('div', { class: 'row tight', style: 'margin-bottom:11px' },
          el('span', { class: 'pill ' + (strong ? 'ok' : 'info') }, `r = ${r.r}`),
          el('span', { class: 'pill info' }, r.strength),
          el('span', { class: 'muted tiny' }, `по ${NUM.format(r.n)} строкам, где заполнены оба поля`)),
        scatterChart(`${r.y_field} в зависимости от ${r.x_field}`, r.points, r.x_field, r.y_field, r)));
    } catch (e) { toast(e.message, true); }
  },

  async runPareto() {
    try {
      const r = await api.get('pareto', {
        table: $('anTable').value, dimension: $('anDim').value,
        agg: $('anAgg4').value, metric: $('anMetric4').value, q: App.anFilters(),
      });
      if (r.message) { $('anResult').replaceChildren(el('div', { class: 'card empty' }, r.message)); return; }
      const labels = r.data.map((d) => short(d[r.dimension], 14));
      $('anResult').replaceChildren(
        el('div', { class: 'card' },
          el('div', { class: 'row tight', style: 'margin-bottom:11px' },
            el('span', { class: 'pill ok' }, `A: ${r.a_count}`),
            el('span', { class: 'pill warn' }, `B: ${r.b_count}`),
            el('span', { class: 'pill info' }, `C: ${r.c_count}`),
            el('span', { class: 'muted tiny' }, r.summary)),
          paretoChart(`${r.agg_label}${r.metric ? ' · ' + r.metric : ''} по «${r.dimension}»`,
            labels, r.data.map((d) => d.value), r.data.map((d) => d.cumulative))),
        el('div', { class: 'card' },
          el('h3', {}, 'Вклад групп'),
          renderTable([r.dimension, 'значение', 'доля, %', 'накоплено, %', 'группа'],
            r.data.map((d) => ({
              [r.dimension]: d[r.dimension] ?? '(пусто)', 'значение': d.value,
              'доля, %': d.share, 'накоплено, %': d.cumulative, 'группа': d.abc,
            })))));
    } catch (e) { toast(e.message, true); }
  },

  async runHist() {
    try {
      const r = await api.get('histogram', {
        table: $('anTable').value, field: $('anField').value, bins: $('anBins').value, q: App.anFilters(),
      });
      if (r.message) { $('anResult').replaceChildren(el('div', { class: 'card empty' }, r.message)); return; }
      $('anResult').replaceChildren(el('div', { class: 'card' },
        el('h3', {}, `Распределение «${r.field}» · ${NUM.format(r.total)} значений`),
        barList(r.bins.map((b) => ({ label: `${fmtNum(b.from)} … ${fmtNum(b.to)}`, value: b.count })))));
    } catch (e) { toast(e.message, true); }
  },

  async runTop() {
    try {
      const r = await api.get('topbottom', {
        table: $('anTable').value, field: $('anField').value, n: $('anN').value,
        label_field: $('anLabel').value, q: App.anFilters(),
      });
      const labelField = $('anLabel').value;
      const labelOf = (row) => (labelField && row[labelField] != null) ? row[labelField]
        : (row[App.idField] ?? '');
      const cols = Object.keys(r.top[0] || { [r.field]: null }).filter((c) => c !== '_v');
      const toBars = (list) => list.map((row) => ({ label: labelOf(row), value: row[r.field] }));
      $('anResult').replaceChildren(
        el('div', { class: 'card' }, el('h3', {}, `Максимальные значения «${r.field}»`),
          r.top.length ? barList(toBars(r.top)) : el('div', { class: 'empty' }, 'Нет данных.'),
          r.top.length ? renderTable(cols, r.top) : null),
        el('div', { class: 'card' }, el('h3', {}, `Минимальные значения «${r.field}»`),
          r.bottom.length ? barList(toBars(r.bottom)) : el('div', { class: 'empty' }, 'Нет данных.'),
          r.bottom.length ? renderTable(cols, r.bottom) : null));
    } catch (e) { toast(e.message, true); }
  },

  async runOutliers() {
    try {
      const r = await api.get('outliers', {
        table: $('anTable').value, field: $('anField').value,
        group_by: $('anGroup').value, threshold: $('anZ').value, q: App.anFilters(),
      });
      $('anResult').replaceChildren(el('div', { class: 'card' },
        el('h3', {}, `Выбросы «${r.field}»: ${r.found} из ${NUM.format(r.checked)} проверенных`),
        r.items.length ? zStrip(r.items, r.threshold)
          : el('div', { class: 'empty' }, 'Выбросов не найдено — значения распределены ровно.'),
        r.items.length ? renderTable(['value', 'median', 'z', 'direction', 'group'],
          r.items.map((i) => ({ value: i.value, median: i.median, z: i.z, direction: i.direction, group: i.group ?? '—' })))
          : null));
    } catch (e) { toast(e.message, true); }
  },

  async runDupes() {
    try {
      const fields = [$('anField').value, $('anField2').value].filter(Boolean).join(',');
      const r = await api.get('duplicates', { table: $('anTable').value, fields, q: App.anFilters() });
      $('anResult').replaceChildren(el('div', { class: 'card' },
        el('h3', {}, `Групп с дублями: ${NUM.format(r.group_count)} · лишних строк: ${NUM.format(r.extra_rows)}`),
        r.groups.length ? renderTable([...r.fields, 'n'], r.groups)
          : el('div', { class: 'empty' }, 'Дубликатов нет.')));
    } catch (e) { toast(e.message, true); }
  },

  initQuality() {
    if (!$('qTable').value) $('qTable').value = App.table || App.firstTable();
    App.loadQuality(); App.loadAnomalies();
  },

  async loadQuality() {
    const table = $('qTable').value;
    if (!table) return;
    $('qualityTable').replaceChildren(el('div', { class: 'muted tiny' }, 'считаю…'));
    try {
      const r = await api.get('profile', { table });
      const rows = r.columns.map((c) => ({
        колонка: c.label || c.field, 'заполнено, %': c.fill_rate, пусто: c.empty,
        уникальных: c.unique, минимум: c.min, медиана: c.median, максимум: c.max,
      }));
      $('qualityTable').replaceChildren(renderTable(Object.keys(rows[0] || {}), rows));
    } catch (e) { $('qualityTable').textContent = e.message; }
  },

  async loadAnomalies() {
    try {
      const r = await api.get('anomalies', { severity: $('anomSeverity').value });
      const box = $('anomalyList');
      if (!r.items.length) {
        box.replaceChildren(el('div', { class: 'empty tiny' },
          'Аномалий не записано. Они появляются после запусков сбора.'));
        return;
      }
      box.replaceChildren(...r.items.slice(0, 200).map((a) => el('div', {
        style: 'padding:7px 0;border-bottom:1px solid var(--line)',
      },
        el('div', { class: 'row tight' },
          el('span', { class: 'pill ' + (a.severity === 'critical' ? 'crit' : a.severity === 'warning' ? 'warn' : 'info') },
            a.severity === 'critical' ? 'критично' : a.severity === 'warning' ? 'внимание' : 'инфо'),
          el('span', { class: 'muted tiny' }, `${a.source || '—'} · ${a.kind} · запуск #${a.run_id}`)),
        el('div', {}, a.message))));
    } catch (e) { $('anomalyList').textContent = e.message; }
  },

  initCompleteness() {
    if (!$('cTable').value) $('cTable').value = App.table || App.firstTable();
    App.loadCompleteness();
  },

  async loadCompleteness() {
    const table = $('cTable').value;
    if (!table) return;
    const threshold = +$('cThreshold').value || 80;
    const q = (App.table === table)
      ? { filters: App.filters.filter((f) => f.field), match: $('filterMatch').value } : {};
    $('cFields').replaceChildren(el('div', { class: 'muted tiny' }, 'считаю…'));
    $('cGroups').replaceChildren(el('div', { class: 'muted tiny' }, 'считаю…'));
    try {
      const [f, rec] = await Promise.all([
        api.get('completeness_fields', { table, q }),
        api.get('completeness_records', { table, threshold, q }),
      ]);

      const pct = f.overall.pct;
      $('cKpis').replaceChildren(
        el('div', { class: 'kpi' },
          el('div', { class: 'label' }, 'Взвешенная заполненность'),
          el('div', { class: 'value' }, pct === null ? '—' : fmtNum(pct, 1) + '%'),
          el('div', { class: 'delta ' + (pct >= 90 ? 'up' : pct >= 70 ? 'flat' : 'down') },
            pct >= 90 ? 'хорошо' : pct >= 70 ? 'приемлемо' : 'низко')),
        el('div', { class: 'kpi' },
          el('div', { class: 'label' }, 'Записей в таблице'),
          el('div', { class: 'value' }, NUM.format(rec.total))),
        el('div', { class: 'kpi' },
          el('div', { class: 'label' }, `Проблемных (< ${threshold}%)`),
          el('div', { class: 'value' }, NUM.format(rec.below)),
          el('div', { class: 'delta ' + (rec.below ? 'down' : 'up') },
            rec.total ? (100 * rec.below / rec.total).toFixed(1) + '% от всех записей' : '')));

      $('badgeComp').textContent = rec.below || '';

      const worst = [...f.fields].sort((a, b) => a.fill_rate - b.fill_rate);
      $('cFields').replaceChildren(barList(worst.map((c) => ({
        label: (c.label || c.field) + (c.kind === 'required' ? ' *' : ''),
        value: c.fill_rate, digits: 1,
      }))));
      [...$('cFields').querySelectorAll('.bar-row')].forEach((row) => {
        const raw = row.querySelector('.bar-val').textContent
          .replace(/\u00a0|\s/g, '').replace(',', '.');
        const v = parseFloat(raw);
        const fill = row.querySelector('.bar-fill');
        if (!fill || isNaN(v)) return;
        fill.classList.add(v < 50 ? 'fill-crit' : v < 90 ? 'fill-warn' : 'fill-ok');
      });

      if (rec.records.length) {
        const pctClass = (p) =>
          p < 50 ? 'num pct-bad' : p < threshold ? 'num pct-warn' : 'num pct-soft';

        const tableEl = el('table');
        tableEl.appendChild(el('thead', {}, el('tr', {},
          ['запись', '%', 'не хватает полей'].map((h) => el('th', {}, h)))));
        tableEl.appendChild(el('tbody', {}, rec.records.map((r) => el('tr', {
          style: 'cursor:pointer', title: 'открыть карточку записи',
          onclick: () => { App.show('data'); App.switchTable(table).then(() => App.openRecord(r.id)); },
        },
          el('td', {}, el('b', {}, short(r.name ?? r.id, 34)),
            el('span', { class: 'muted tiny' }, ` · id ${r.id}`)),
          el('td', { class: pctClass(r.pct) }, fmtNum(r.pct, 1)),
          el('td', { class: 'wrap-cell' },
            (r.missing_labels || r.missing).map((m) =>
              el('span', { class: 'chip miss' }, m)))))));

        const yellowCount = (rec.incomplete ?? rec.records.length) - rec.below;
        $('cRecords').replaceChildren(
          el('div', { class: 'tiny muted', style: 'margin-bottom:8px' },
            `Показаны ${NUM.format(rec.records.length)} из ${NUM.format(rec.incomplete ?? rec.records.length)} записей с пропусками ` +
            `— включая ${NUM.format(rec.below)} ниже ${threshold}% и ${NUM.format(yellowCount)} «жёлтых» (${threshold}–99%). ` +
            `Самые пустые первыми. Клик — карточка записи.`),
          el('div', { class: 'table-wrap', style: 'max-height:430px' }, tableEl));
      } else {
        $('cRecords').replaceChildren(el('div', { class: 'empty tiny' },
          `Все записи заполнены не хуже ${threshold}% — отлично.`));
      }

      const gf = $('cGroupField');
      gf.innerHTML = '';
      const cols = (await api.get('columns', { table })).columns;
      cols.forEach((c) => gf.appendChild(el('option', { value: c.name }, c.label || c.name)));
      const defGroup = cols.find((c) => /housing_complex|developer|group|district/i.test(c.name))?.name
        || cols[1]?.name || cols[0].name;
      gf.value = defGroup;
      App.loadCompletenessGroups();
    } catch (e) {
      $('cFields').replaceChildren(el('div', { class: 'empty tiny' }, e.message));
      toast(e.message, true);
    }
  },

  async loadCompletenessGroups() {
    const table = $('cTable').value;
    const groupField = $('cGroupField').value;
    if (!table || !groupField) return;
    const q = (App.table === table)
      ? { filters: App.filters.filter((f) => f.field), match: $('filterMatch').value } : {};
    try {
      const r = await api.get('completeness_groups', { table, group_field: groupField, q });
      if (!r.groups.length) {
        $('cGroups').replaceChildren(el('div', { class: 'empty tiny' },
          'Групп меньше трёх записей — сравнивать нечего.'));
        return;
      }
      const rows = r.groups.map((g) => ({
        группа: g.group ?? '(пусто)', записей: g.records,
        'средняя заполненность, %': g.avg_pct, 'хуже 80%': g.below_80,
        'чаще всего пустые поля': (g.worst_fields || [])
          .map((f) => {
            const info = (App.columns || []).find((x) => x.name === f);
            return (info && info.label) || f;
          }).join(', '),
      }));
      $('cGroups').replaceChildren(
        barList(r.groups.slice(0, 25).map((g) => ({
          label: g.group ?? '(пусто)', value: g.avg_pct, digits: 1,
        }))),
        el('div', { style: 'height:12px' }),
        renderTable(Object.keys(rows[0]), rows));
    } catch (e) { $('cGroups').textContent = e.message; }
  },

  async loadChecks() {
    $('checksVerdict').replaceChildren(el('div', { class: 'card muted tiny' }, 'проверяю…'));
    try {
      const r = await api.get('crosschecks');
      const [state, text] = r.verdict;
      const pill = { critical: 'crit', warning: 'warn', ok: 'ok' }[state] || 'info';
      $('checksVerdict').replaceChildren(el('div', { class: 'card' },
        el('div', { class: 'row tight' },
          el('span', { class: 'pill ' + pill },
            state === 'ok' ? 'всё сходится' : state === 'warning' ? 'есть замечания' : 'есть поломки'),
          el('span', {}, text)),
        el('div', { class: 'row tight', style: 'margin-top:10px' },
          el('span', { class: 'pill crit' }, `критично: ${r.counts.critical}`),
          el('span', { class: 'pill warn' }, `предупреждений: ${r.counts.warning}`),
          el('span', { class: 'pill info' }, `справочно: ${r.counts.info}`))));

      const bySeverity = { critical: 'crit', warning: 'warn', info: 'info' };
      $('checksList').replaceChildren(el('div', { class: 'card' },
        el('h3', {}, 'Результаты'),
        ...(r.issues.length ? r.issues.map((i) => el('div', {
          style: 'padding:10px 0;border-bottom:1px solid var(--line)',
        },
          el('div', { class: 'row tight' },
            el('span', { class: 'pill ' + bySeverity[i.level] },
              i.level === 'critical' ? 'критично' : i.level === 'warning' ? 'внимание' : 'инфо'),
            el('span', { class: 'pill info' }, i.area),
            el('b', {}, i.title)),
          i.detail ? el('div', { class: 'tiny muted', style: 'margin-top:4px' }, i.detail) : null,
          i.advice ? el('div', {
            class: 'tiny', style: 'margin-top:6px;white-space:pre-wrap;color:#9dc0ff',
          }, '→ ' + i.advice) : null))
          : [el('div', { class: 'empty' }, 'Замечаний нет.')])));

      $('checksRelations').replaceChildren(...(r.relations.length
        ? r.relations.map((rel) => el('div', { class: 'row tight', style: 'padding:4px 0' },
          el('span', { class: 'mono' }, `${rel.child}.${rel.child_field}`),
          el('span', { class: 'muted' }, '→'),
          el('span', { class: 'mono' }, `${rel.parent}.${rel.parent_field}`),
          el('span', { class: 'pill info' }, rel.source)))
        : [el('div', { class: 'tiny muted' }, 'Связей между таблицами не найдено.')]));

      const bad = r.counts.critical;
      $('badgeChecks').textContent = bad || '';
    } catch (e) {
      $('checksVerdict').replaceChildren(el('div', { class: 'card empty' }, e.message));
      $('checksList').replaceChildren();
    }
  },

  async loadChanges() {
    try {
      const res = await api.get('query', {
        table: 'comparison_vs_previous', q: { page_size: 500 },
      });
      App.changes = res.rows;
      App.renderChanges();
    } catch (e) {
      $('changesBox').replaceChildren(el('div', { class: 'card empty' },
        el('span', { class: 'big' }, '🔄'),
        'Таблица сравнения появится после второго запуска сбора.'));
    }
  },

  renderChanges() {
    if (!App.changes) return;
    const q = $('changeSearch').value.toLowerCase();
    const kind = $('changeKind').value;
    const rows = App.changes.filter((r) => {
      const cat = String(r['Категория'] ?? '');
      if (kind && !cat.includes(kind)) return false;
      if (!q) return true;
      return Object.values(r).some((v) => String(v ?? '').toLowerCase().includes(q));
    });
    const cols = Object.keys(App.changes[0] || {}).filter((c) => !c.startsWith('_'));
    $('changesBox').replaceChildren(el('div', { class: 'card' },
      el('h3', {}, `Показано ${rows.length} из ${App.changes.length}`),
      rows.length ? renderTable(cols, rows) : el('div', { class: 'empty' }, 'Ничего не подходит под фильтр.')));
  },

  async loadEdits() {
    try {
      const r = await api.get('edits');
      const box = $('editsBox');
      if (!r.items.length) {
        box.replaceChildren(el('div', { class: 'card empty' }, el('span', { class: 'big' }, '✏️'),
          'Правок пока нет. Откройте «Данные» и измените ячейку двойным кликом.'));
        return;
      }
      const table = el('table');
      table.appendChild(el('thead', {}, el('tr', {},
        ['таблица', 'запись', 'поле', 'было', 'стало', 'когда', ''].map((h) => el('th', {}, h)))));
      table.appendChild(el('tbody', {}, r.items.map((e) => el('tr', {},
        el('td', {}, e.source),
        el('td', {}, el('a', { style: 'cursor:pointer;color:var(--accent)',
          onclick: () => { App.show('data'); App.switchTable(e.source).then(() => App.openRecord(e.record_id)); } }, e.record_id)),
        el('td', {}, e.field),
        el('td', { class: 'muted' }, short(e.old_value, 30)),
        el('td', {}, short(e.new_value, 30)),
        el('td', { class: 'muted tiny' }, (e.ts || '').replace('T', ' ')),
        el('td', {}, el('button', {
          class: 'sm ghost', title: 'отменить',
          onclick: async () => { await api.post('edit_revert', { edit_id: e.id }); toast('Правка отменена'); App.loadEdits(); App.loadMeta(); },
        }, '↺'))))));
      box.replaceChildren(el('div', { class: 'card' },
        el('h3', {}, `Активных правок: ${r.items.length}`), el('div', { class: 'table-wrap' }, table)));
    } catch (e) { toast(e.message, true); }
  },

  initCollect() {
    const t = App.meta?.token || {};
    const cls = { ok: 'ok', soon: 'warn', expired: 'crit', missing: 'crit' }[t.state] || 'info';
    $('tokenPill').replaceChildren(el('span', { class: 'pill ' + cls }, t.text || '—'));
    App.pollRun();
  },

  async saveToken() {
    const token = $('tokenInput').value.trim();
    if (!token) { toast('Вставьте токен', true); return; }
    try {
      await api.post('token', { token });
      $('tokenInput').value = '';
      toast('Токен сохранён в .env');
      await App.loadMeta(); App.initCollect();
    } catch (e) { toast(e.message, true); }
  },

  async startRun() {
    const only = [...$('runOnly').selectedOptions].map((o) => o.value);
    try {
      const r = await api.post('run_start', {
        only, excel: $('runExcel').checked, no_anomalies: $('runNoAnom').checked,
        concurrency: $('runConc').value || null,
      });
      if (!r.ok) { toast(r.error, true); return; }
      App.runSince = 0;
      $('runLog').textContent = '';
      toast('Сбор запущен');
      App.pollRun();
    } catch (e) { toast(e.message, true); }
  },

  async stopRun() {
    try { await api.post('run_stop'); } catch (e) { toast(e.message, true); }
  },

  async pollRun() {
    clearTimeout(App.runTimer);
    try {
      const s = await api.get('run', { since: App.runSince });
      if (s.lines.length) {
        const log = $('runLog');
        const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
        if (log.textContent === 'Сбор ещё не запускался.') log.textContent = '';
        log.textContent += (log.textContent ? '\n' : '') + s.lines.map((l) => l.text).join('\n');
        App.runSince = s.next;
        if (atBottom) log.scrollTop = log.scrollHeight;
      }
      $('btnRun').disabled = s.running;
      $('btnStop').disabled = !s.running;
      $('runState').textContent = s.running
        ? `идёт с ${(s.started_at || '').replace('T', ' ')}`
        : (s.finished_at ? `завершено в ${s.finished_at.replace('T', ' ')} (код ${s.exit_code})` : '');
      if (s.running) App.runTimer = setTimeout(() => App.pollRun(), 900);
      else if (s.finished_at) { await App.loadMeta(); }
    } catch (e) { /* сервер мог перезапуститься */ }
  },

  async loadSources() {
    try {
      const r = await api.get('sources');
      $('sourceList').replaceChildren(...(r.items.length ? r.items.map((s) => el('div', {
        style: 'padding:9px 0;border-bottom:1px solid var(--line)',
      },
        el('div', { class: 'row tight' },
          el('b', {}, s.title || s.key),
          el('span', { class: 'pill info' }, s.key),
          s.external ? el('span', { class: 'pill warn' }, 'внешний') : null,
          s.has_cross_check ? el('span', { class: 'pill ok' }, 'сверка настроена') : null,
          el('div', { class: 'spacer' }),
          el('button', { class: 'sm', onclick: () => App.editSource(s.file, s.external) }, 'Правка'),
          el('button', { class: 'sm', onclick: () => App.runOne(s.key) }, '▶ Собрать только его'),
          el('button', {
            class: 'sm danger',
            onclick: async () => {
              if (!confirm(`Удалить файл ${s.file}?`)) return;
              await api.post('source_delete', { file: s.file }); toast('Удалено'); App.loadSources();
            },
          }, 'Удалить')),
        el('div', { class: 'tiny muted mono' }, short(s.url, 130))))
        : [el('div', { class: 'empty' },
            r.hint || 'Источников нет.',
            r.hint ? el('div', { class: 'tiny muted', style: 'margin-top:8px' },
              `Redcat: ${r.dir}`) : null,
            r.hint ? el('div', { class: 'tiny muted' },
              `Внешние: ${r.dir_ext}`) : null)]));
    } catch (e) { toast(e.message, true); }
  },

  async editSource(file, external) {
    try {
      const r = await api.get('source', { file });
      $('sourceFile').value = r.file;
      $('sourceEditor').value = r.content;
      $('sourceExternal').checked = !!r.external;
      $('sourceEditorCard').style.display = 'block';
      $('sourceEditorCard').scrollIntoView({ behavior: 'smooth' });
    } catch (e) { toast(e.message, true); }
  },

  newSource(external) {
    $('sourceFile').value = external ? 'external_new.json' : 'new_source.json';
    $('sourceExternal').checked = !!external;
    const spec = {
      key: external ? 'external_new' : 'new_source',
      title: external ? 'Новый внешний источник' : 'Новый источник',
      url: 'https://example.com/catalog?region={region_id}',
      id_field: 'id',
      name_field: 'title',
      numeric_fields: [],
      group_fields: [],
      track_fields: [],
    };
    if (external) {
      spec.external = true;
      spec.fetch_mode = 'http';
      spec.cross_check = {
        with_table: "apartments",
        on_left: "complex_name",
        on_right: "housing_complex_name",
        metrics: { price: "price" },
        agg: "median",
        normalize_key: true,
        min_group_size: 10,
        thresholds: { ok: 10, warn: 20 },
      };
    }
    $('sourceEditor').value = JSON.stringify(spec, null, 2);
    $('sourceEditorCard').style.display = 'block';
  },

  async saveSourceRaw() {
    try {
      await api.post('source_save_raw', {
        file: $('sourceFile').value,
        content: $('sourceEditor').value,
        external: $('sourceExternal').checked,
      });
      toast('Источник сохранён');
      App.loadSources(); App.loadMeta();
    } catch (e) { toast('Ошибка: ' + e.message, true); }
  },

  async runOne(key) {
    App.show('collect');
    try {
      const r = await api.post('run_start', { only: [key] });
      if (!r.ok) { toast(r.error, true); return; }
      App.runSince = 0; $('runLog').textContent = ''; App.pollRun();
    } catch (e) { toast(e.message, true); }
  },

  async probe() {
    const box = $('probeResult');
    box.replaceChildren(el('div', { class: 'muted tiny', style: 'margin-top:10px' }, 'пробую…'));
    try {
      const r = await api.post('probe', {
        url: $('probeUrl').value,
        key: $('probeKey').value,
        pagination_style: $('probeStyle').value,
        fetch_mode: $('probeMode').value,
        browser_wait_for: $('probeWaitFor').value,
        browser_wait_ms: parseInt($('probeWaitMs').value || '0', 10),
      });
      if (!r.ok && r.error) {
        box.replaceChildren(el('div', { class: 'card', style: 'margin-top:11px' },
          el('div', { class: 'pill crit' }, `Ошибка`),
          el('pre', { class: 'log', style: 'margin-top:9px' }, r.error + '\n\n' + (r.preview || ''))));
        return;
      }
      const suggestion = JSON.stringify(r.suggestion, null, 2);
      const children = [
        el('div', { class: 'row tight' },
          el('span', { class: 'pill ' + (r.ok ? 'ok' : 'warn') }, `HTTP ${r.status ?? '—'}`),
          el('span', { class: 'muted tiny' }, `${r.ms} мс`)),
        App._renderValidation(r.validation),
        el('h3', { style: 'margin-top:11px' }, 'Предлагаемое описание источника'),
        el('textarea', { rows: 12, id: 'probeSpec' }, suggestion),
        el('div', { class: 'row', style: 'margin-top:8px' },
          el('button', {
            class: 'primary',
            onclick: async () => {
              try {
                const spec = JSON.parse($('probeSpec').value);
                const isExternal = $('probeExternal').checked;
                if (isExternal) spec.external = true;
                await api.post('source_save', { spec, external: isExternal });
                toast('Источник добавлен');
                App.loadSources(); App.loadMeta();
              } catch (e) { toast(e.message, true); }
            },
          }, 'Сохранить как источник'),
          el('button', {
            onclick: async () => {
              try {
                const spec = JSON.parse($('probeSpec').value);
                const v = await api.post('probe_spec', { spec, url: spec.url });
                box.appendChild(App._renderValidation(v.validation));
              } catch (e) { toast(e.message, true); }
            },
          }, 'Проверить spec заново')),
        el('h3', { style: 'margin-top:13px' }, 'Ответ (фрагмент)'),
        el('pre', { class: 'log' }, r.preview),
      ];
      box.replaceChildren(el('div', { style: 'margin-top:11px' }, ...children));
    } catch (e) { box.replaceChildren(el('div', { class: 'card' }, e.message)); }
  },

  _renderValidation(v) {
    if (!v) return el('div');
    const rows = [];
    if (v.warnings && v.warnings.length) {
      for (const w of v.warnings) {
        const cls = w.level === 'critical' ? 'crit' : 'warn';
        rows.push(el('div', { style: 'padding:6px 0;border-bottom:1px solid var(--line)' },
          el('div', { class: 'row tight' },
            el('span', { class: 'pill ' + cls },
              w.level === 'critical' ? 'критично' : 'внимание'),
            el('span', { class: 'mono tiny muted' }, w.field || '')),
          el('div', {}, w.message || ''),
          w.advice ? el('div', { class: 'tiny', style: 'color:#9dc0ff;margin-top:3px' },
            '→ ' + w.advice) : null));
      }
    }
    if (v.checks && v.checks.length) {
      for (const c of v.checks) {
        rows.push(el('div', { style: 'padding:3px 0;font-size:11.5px;color:var(--muted)' },
          el('span', { class: 'pill ok' }, '✓'), ' ',
          el('span', { class: 'mono' }, c.field || ''), ' — ', c.message || ''));
      }
    }
    const head = el('div', { class: 'row tight', style: 'margin-bottom:8px;flex-wrap:wrap' },
      el('span', { class: 'pill ' + (v.parse_ok ? 'ok' : 'crit') },
        v.parse_ok ? 'парсер отработал' : 'парсер не отработал'),
      v.items_count ? el('span', { class: 'pill info' }, `записей: ${v.items_count}`) : null,
      v.total_reported ? el('span', { class: 'pill info' }, `total: ${v.total_reported}`) : null,
      (v.warnings && v.warnings.length)
        ? el('span', { class: 'pill crit' }, `замечаний: ${v.warnings.length}`) : null);
    const body = rows.length
      ? el('div', {}, ...rows)
      : el('div', { class: 'muted tiny' }, 'Замечаний нет.');
    return el('div', { class: 'card', style: 'margin-top:11px' }, head, body);
  },

  async loadExternalList() {
    await App.loadMeta();
    App.initExternal();
  },

  onCsvFilePicked(file) {
    App.csvUpload.file = file || null;
    if (file && !$('csvTableName').value) {
      const guess = file.name.replace(/\.csv$/i, '')
        .replace(/[^0-9A-Za-zА-Яа-яЁё_]+/g, '_').replace(/^_+|_+$/g, '');
      $('csvTableName').value = guess || 'csv_table';
    }
    $('csvUploadBtn').disabled = !file;
  },

  /* Простой, но не наивный разбор CSV: понимает кавычки, экранированные
     "" внутри поля и запятую/точку-с-запятой внутри кавычек — иначе
     адрес или описание с запятой внутри поломали бы все колонки после
     себя. Разделитель (, или ;) определяется автоматически по заголовку —
     Excel в русской локали часто экспортирует через ;. */
  parseCsv(text) {
    if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1); // BOM из Excel
    const headEnd = text.indexOf('\n');
    const head = text.slice(0, headEnd > -1 ? headEnd : 200);
    const commas = (head.match(/,/g) || []).length;
    const semis = (head.match(/;/g) || []).length;
    const delim = semis > commas ? ';' : ',';

    const rows = [];
    let row = [], field = '', inQuotes = false;
    const pushField = () => { row.push(field); field = ''; };
    const pushRow = () => { pushField(); rows.push(row); row = []; };
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (inQuotes) {
        if (c === '"') {
          if (text[i + 1] === '"') { field += '"'; i++; } else { inQuotes = false; }
        } else field += c;
        continue;
      }
      if (c === '"') { inQuotes = true; continue; }
      if (c === delim) { pushField(); continue; }
      if (c === '\r') continue;
      if (c === '\n') { pushRow(); continue; }
      field += c;
    }
    if (field !== '' || row.length) pushRow();
    while (rows.length && rows[rows.length - 1].every((v) => v === '')) rows.pop();
    return rows;
  },

  async uploadCsv() {
    const file = App.csvUpload.file;
    const table = ($('csvTableName').value || '').trim();
    if (!file) { toast('Выберите файл', true); return; }
    if (!table) { toast('Укажите имя таблицы', true); return; }
    const mode = $('csvMode').value;
    const btn = $('csvUploadBtn');
    const progress = $('csvUploadProgress');
    const fill = $('csvProgressFill');
    const text2 = $('csvProgressText');
    btn.disabled = true;
    progress.style.display = 'block';
    fill.style.width = '0%';
    text2.textContent = 'читаю файл…';
    try {
      const text = await file.text();
      const rows = App.parseCsv(text);
      if (rows.length < 2) throw new Error('В файле нет данных (нужен заголовок и хотя бы одна строка)');
      const header = rows[0].map((h) => h.trim());
      const dataRows = rows.slice(1);
      const BATCH = 1000;
      const total = Math.max(1, Math.ceil(dataRows.length / BATCH));
      let insertedSoFar = 0;
      for (let seq = 0; seq < total; seq++) {
        const chunk = dataRows.slice(seq * BATCH, (seq + 1) * BATCH);
        const body = { table, mode, seq, total, rows: chunk };
        if (seq === 0) body.columns = header;
        const r = await api.post('upload_csv', body);
        insertedSoFar = r.inserted_so_far;
        fill.style.width = Math.round(((seq + 1) / total) * 100) + '%';
        text2.textContent = `пачка ${seq + 1} из ${total} — загружено строк: ${insertedSoFar}`;
      }
      toast(`Готово: «${table}», строк ${insertedSoFar}`);
      $('csvFile').value = '';
      App.csvUpload.file = null;
      await App.loadMeta();
      App.loadSources();
    } catch (e) {
      toast('Ошибка загрузки CSV: ' + e.message, true);
    } finally {
      btn.disabled = !App.csvUpload.file;
    }
  },

  initExternal() {
    const list = (App.meta?.external_tables || [])
      .filter((t) => t.name !== 'comparison_vs_previous');
    const sel = $('extTable');
    sel.innerHTML = '';
    if (!list.length) {
      $('extEmpty').style.display = 'block';
      $('extContent').style.display = 'none';
      $('extEmpty').replaceChildren(el('div', { class: 'card empty' },
        el('span', { class: 'big' }, '📦'),
        'Внешних источников пока нет.',
        el('div', { class: 'tiny muted', style: 'margin-top:10px' },
          'Добавьте JSON-описание в папку ',
          el('code', {}, 'sources_external/'),
          ' — или нажмите «＋ Новый внешний» на вкладке «Источники». ',
          'После — запустите сбор: ',
          el('code', {}, 'python redcat_scraper.py --only <ключ>'),
          ' либо через вкладку «Сбор».')));
      return;
    }
    $('extEmpty').style.display = 'none';
    $('extContent').style.display = '';
    list.forEach((t) => sel.appendChild(
      el('option', { value: t.name },
        `${t.title || t.name} (${NUM.format(t.rows)})`)));
    const keep = App.extTable && list.some((t) => t.name === App.extTable)
      ? App.extTable : list[0].name;
    sel.value = keep;
    App.switchExternal(keep);
  },

  async switchExternal(table) {
    if (!table) return;
    App.extTable = table;
    App.crossCheck = null;
    $('extTable').value = table;
    $('extCrossCheck').replaceChildren(el('div', { class: 'muted tiny' },
      'Нажмите «⇄ Сверить с Redcat», чтобы построить отчёт.'));
    try {
      const info = await api.get('columns', { table });
      App.extColumns = info.columns;
      const cols = info.columns.map((c) => c.name);
      const labelOf = (name) => {
        const info2 = (App.extColumns || []).find((x) => x.name === name);
        return (info2 && info2.label) || name;
      };

      const res = await api.get('query', { table, q: { page: 1, page_size: 100 } });
      const thead = $('extTableEl').querySelector('thead');
      const tbody = $('extTableEl').querySelector('tbody');
      thead.replaceChildren(el('tr', {}, cols.map((c) => el('th', {}, labelOf(c)))));
      tbody.replaceChildren(...res.rows.map((row) => el('tr', {},
        cols.map((c) => {
          const v = row[c];
          return el('td', {
            class: typeof v === 'number' ? 'num' : '',
            title: String(v ?? ''),
          }, typeof v === 'number' ? fmtNum(v, Number.isInteger(v) ? 0 : 2) : short(v));
        }))));
      $('extCount').textContent = `${NUM.format(res.total)} строк`;
      $('extPager').replaceChildren(
        el('span', {}, `стр. ${res.page} из ${res.pages}`),
        el('span', { class: 'muted' }, 'показано'),
        el('span', {}, `${res.rows.length} из ${NUM.format(res.total)}`));

      try {
        const prof = await api.get('profile', { table });
        $('extProfile').replaceChildren(renderTable(
          ['колонка', 'заполнено, %', 'уникальных'],
          prof.columns.map((c) => ({
            колонка: c.label || c.field,
            'заполнено, %': c.fill_rate,
            уникальных: c.unique,
          }))));
      } catch (e) {
        $('extProfile').replaceChildren(el('div', { class: 'tiny muted' },
          'Профиль не посчитался: ' + e.message));
      }
    } catch (e) {
      $('extCount').textContent = e.message;
      toast(e.message, true);
    }
  },

  /* ── СВЕРКА С REDCAT ───────────────────────────────────────────────── */
  async runCrossCheck() {
    const table = App.extTable;
    if (!table) { toast('Не выбрана внешняя таблица', true); return; }
    const box = $('extCrossCheck');
    box.replaceChildren(el('div', { class: 'muted tiny' }, 'считаю…'));
    try {
      const r = await api.get('cross_check', { source: table });
      App.crossCheck = r;
      box.replaceChildren(App._renderCrossCheck(r));
    } catch (e) {
      box.replaceChildren(el('div', { class: 'empty tiny' }, e.message));
      toast(e.message, true);
    }
  },

  _renderCrossCheck(r) {
    const s = r.summary;
    const head = el('div', { class: 'row tight', style: 'margin-bottom:11px;flex-wrap:wrap' },
      el('span', { class: 'pill crit' }, `смотреть руками: ${s.critical}`),
      el('span', { class: 'pill warn' }, `внимание: ${s.warn}`),
      el('span', { class: 'pill ok' }, `сходится: ${s.ok}`),
      s.insufficient ? el('span', { class: 'pill info' }, `мало данных: ${s.insufficient}`) : null,
      s.left_only ? el('span', { class: 'pill info' }, `только слева: ${s.left_only}`) : null,
      s.right_only ? el('span', { class: 'pill info' }, `только справа: ${s.right_only}`) : null,
      el('div', { class: 'spacer' }),
      el('button', { class: 'sm', onclick: () => App.exportCrossCheck('csv') }, '⬇ CSV'),
      el('button', { class: 'sm', onclick: () => App.exportCrossCheck('json') }, '⬇ JSON'));

    const lLabel = r.left_label || r.source || 'внешний';
    const rLabel = r.right_label || r.with_table || 'внутренний';
    const lTag = r.left_external ? 'внешний' : 'внутренний';
    const rTag = r.right_external ? 'внешний' : 'внутренний';
    const meta = el('div', { class: 'tiny muted', style: 'margin-bottom:11px' },
      `Сопоставление: «${r.on_left}» (${lLabel}, ${lTag}) ↔ «${r.on_right}» (${rLabel}, ${rTag}), ` +
      `агрегация: ${r.agg}, ` +
      `порог: до ${r.thresholds.ok}% — сходится, до ${r.thresholds.warn}% — внимание, ` +
      `выше — смотреть руками. ` +
      (r.normalize_key ? 'Ключи нормализуются.' : 'Ключи сравниваются как есть.') +
      (r.filter_right ? ` Фильтр справа: ${r.filter_right.field} ${r.filter_right.op} ${r.filter_right.value}.` : ''));

    const blocks = [head, meta];

    const renderItems = (title, cls, items, clsLabel) => {
      if (!items.length) return null;
      const metricKeys = Object.keys(r.metrics);
      const cols = ['название', 'строк слева', 'строк справа'];
      for (const m of metricKeys) {
        cols.push(`левая: ${m}`);
        cols.push(`правая: ${r.metrics[m]}`);
        cols.push('Δ%');
      }
      const rows = items.map((it) => {
        const row = {
          'название': it.display,
          'строк слева': it.left_rows,
          'строк справа': it.right_rows,
        };
        for (const m of metricKeys) {
          const mm = (it.metrics || {})[m] || {};
          row[`левая: ${m}`] = mm.left;
          row[`правая: ${r.metrics[m]}`] = mm.right;
          row['Δ%'] = mm.diff_pct;
        }
        return row;
      });
      return el('div', { style: 'margin-bottom:14px' },
        el('div', { class: 'row tight', style: 'margin-bottom:6px' },
          el('span', { class: 'pill ' + cls }, clsLabel),
          el('span', { class: 'muted tiny' }, `${items.length} шт.`)),
        el('div', { class: 'table-wrap', style: 'max-height:380px' },
          renderTable(cols, rows, { max: 60 })));
    };

    const blocksAdd = [
      renderItems('Смотреть руками', 'crit', r.items_by_class.critical || [], 'Δ ≥ ' + r.thresholds.warn + '%'),
      renderItems('Внимание', 'warn', r.items_by_class.warn || [], 'Δ ' + r.thresholds.ok + '–' + r.thresholds.warn + '%'),
      renderItems('Мало данных', 'info', r.items_by_class.insufficient || [], 'меньше ' + r.min_group_size + ' строк'),
      renderItems('Только слева', 'info', r.items_by_class.left_only || [], 'есть у источника, нет у соседа'),
      renderItems('Только справа', 'info', r.items_by_class.right_only || [], 'есть у соседа, нет у источника'),
    ].filter(Boolean);

    if (!blocksAdd.length) {
      blocks.push(el('div', { class: 'card empty tiny' },
        `Нечего сравнивать: сопоставленных групп нет. Проверьте ключи on_left/on_right и фильтр filter_right.`));
    } else {
      blocks.push(...blocksAdd);
    }

    if ((r.items_by_class.ok || []).length) {
      const details = el('details', {}, el('summary', {
        style: 'cursor:pointer;color:var(--muted);padding:4px 0'
      }, `Сходится (${r.items_by_class.ok.length}) — развернуть`));
      details.appendChild(App._renderCrossCheckOkBlock(r));
      blocks.push(details);
    }
    return el('div', {}, ...blocks);
  },

  _renderCrossCheckOkBlock(r) {
    const metricKeys = Object.keys(r.metrics);
    const cols = ['название', 'строк слева', 'строк справа'];
    for (const m of metricKeys) {
      cols.push(`левая: ${m}`);
      cols.push(`правая: ${r.metrics[m]}`);
      cols.push('Δ%');
    }
    const rows = (r.items_by_class.ok || []).map((it) => {
      const row = {
        'название': it.display,
        'строк слева': it.left_rows,
        'строк справа': it.right_rows,
      };
      for (const m of metricKeys) {
        const mm = (it.metrics || {})[m] || {};
        row[`левая: ${m}`] = mm.left;
        row[`правая: ${r.metrics[m]}`] = mm.right;
        row['Δ%'] = mm.diff_pct;
      }
      return row;
    });
    return el('div', { class: 'table-wrap', style: 'max-height:380px;margin-top:6px' },
      renderTable(cols, rows, { max: 60 }));
  },



  async initSql() {
    await App.loadSqlDatabases();
    App.loadSqlSchema();
  },

  async loadSqlDatabases() {
    try {
      const r = await api.get('sql_databases');
      const sel = $('sqlDb');
      const keep = sel.value;
      sel.innerHTML = '';
      (r.items || []).forEach((db) => {
        const mb = (db.size / 1024 / 1024).toFixed(1);
        sel.appendChild(el('option', { value: db.id },
          `${db.title} · ${mb} МБ`));
      });
      if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
    } catch (e) {
      toast('Не удалось получить список баз: ' + e.message, true);
    }
  },

  async loadSqlSchema() {
    const dbId = $('sqlDb').value || 'redcat';
    const box = $('sqlSchema');
    box.replaceChildren(el('div', { class: 'muted tiny' }, 'загружаю схему…'));
    let items = [];
    try {
      const r = await api.get('sql_schema', { db: dbId });
      items = r.items || [];
    } catch (e) {
      box.replaceChildren(el('div', { class: 'empty tiny' }, e.message));
      return;
    }
    box.replaceChildren();
    if (!items.length) {
      box.appendChild(el('div', { class: 'muted tiny' }, 'в базе нет таблиц'));
      return;
    }
    for (const t of items) {
      const details = el('details', {}, el('summary', {
        style: 'cursor:pointer;padding:3px 0',
      }, `${t.name} (${NUM.format(t.rows)})`));
      (t.schema || []).forEach((c) => details.appendChild(el('div', {
        class: 'muted mono', style: 'padding-left:12px;cursor:pointer',
        title: c.type || '',
        onclick: () => { $('sqlInput').value += c.name; },
      }, `${c.name} ${c.type || ''}`)));
      box.appendChild(details);
    }
    // Примеры запросов — под выбранную базу.
    const samples = App.sqlSamplesFor(dbId, items);
    $('sqlSamples').replaceChildren(...samples.map(([label, sql]) =>
      el('div', {
        style: 'padding:4px 0;cursor:pointer;color:var(--accent)',
        onclick: () => { $('sqlInput').value = sql; },
      }, label)));
  },

  sqlSamplesFor(dbId, items) {
    const names = (items || []).map((t) => t.name);
    const byDb = {
      redcat: () => {
        const main = names.includes('apartments') ? 'apartments' : names[0];
        return [
          ['Сколько записей по каждой таблице',
           names.slice(0, 8).map((n) =>
             `SELECT '${n}' AS t, COUNT(*) AS n FROM "${n}"`).join('\nUNION ALL\n')],
          ['Топ значений колонки',
           `SELECT *, COUNT(*) AS n FROM "${main}" GROUP BY 1 ORDER BY n DESC LIMIT 20`],
        ];
      },
      external: () => [
        ['Все таблицы и размер',
         names.map((n) =>
           `SELECT '${n}' AS t, COUNT(*) AS n FROM "${n}"`).join('\nUNION ALL\n')],
      ],
      studio: () => [
        ['Активные правки по таблицам',
         `SELECT source, COUNT(*) AS n FROM edits WHERE active=1 GROUP BY source ORDER BY n DESC`],
        ['Все правки за последние 50',
         `SELECT id, source, record_id, field, old_value, new_value, ts
          FROM edits WHERE active=1 ORDER BY id DESC LIMIT 50`],
        ['Заметки',
         `SELECT source, record_id, substr(note, 1, 80) AS note, ts FROM notes ORDER BY ts DESC LIMIT 50`],
        ['Теги',
         `SELECT tag, COUNT(*) AS n FROM tags GROUP BY tag ORDER BY n DESC`],
        ['Обращения к API за последние 100',
         `SELECT ts, method, status, ms, substr(url, 1, 80) AS url FROM api_audit ORDER BY id DESC LIMIT 100`],
      ],
      stats: () => [
        ['Все запуски: объём и полнота',
         `SELECT run_id, started_at, apartments_count, hc_count,
                 region_total_reported, round(coverage_pct, 1) AS cov,
                 CASE
                   WHEN apartments_count >= 40000 THEN 'полный'
                   WHEN apartments_count < 10500 THEN 'срез 10к'
                   ELSE 'частичный'
                 END AS verdict
          FROM runs ORDER BY run_id`],
        ['Сводка по запускам',
         `SELECT COUNT(*) AS всего,
                 SUM(CASE WHEN apartments_count >= 40000 THEN 1 ELSE 0 END) AS полных,
                 SUM(CASE WHEN apartments_count <  10500 THEN 1 ELSE 0 END) AS срезов_10к,
                 MIN(started_at) AS первый,
                 MAX(started_at) AS последний
          FROM runs`],
        ['Аномалии по важности',
         `SELECT severity, COUNT(*) AS n FROM anomalies GROUP BY severity ORDER BY n DESC`],
        ['Последние 50 аномалий',
         `SELECT run_id, source, severity, kind, substr(message, 1, 100) AS msg
          FROM anomalies ORDER BY id DESC LIMIT 50`],
        ['История одной записи (пример)',
         `SELECT run_id, ts, field, value FROM record_history
          WHERE source='apartments'
          ORDER BY run_id DESC LIMIT 100`],
      ],
    };
    return (byDb[dbId] || (() => []))();
  },

  async runSql() {
    try {
      const r = await api.get('sql', {
        sql: $('sqlInput').value,
        db: $('sqlDb').value || 'redcat',
      });
      $('sqlResult').replaceChildren(el('div', { class: 'card' },
        el('h3', {}, `${r.rows.length} строк${r.truncated ? ` (показаны первые ${r.limit})` : ''}`),
        r.rows.length ? renderTable(r.columns, r.rows) : el('div', { class: 'empty' }, 'Пусто.')));
    } catch (e) {
      $('sqlResult').replaceChildren(el('div', { class: 'card' },
        el('span', { class: 'pill crit' }, 'ошибка'),
        el('pre', { class: 'log', style: 'margin-top:9px' }, e.message)));
    }
  },

  exportSql() {
    const qs = new URLSearchParams({
      what: 'sql', format: 'csv',
      sql: $('sqlInput').value,
      db: $('sqlDb').value || 'redcat',
    });
    window.location = `/api/export?${qs}`;
  },

  /* Инструменты */
  toolsCache: [], toolsTimer: null, toolsSince: 0,
  async loadTools() {
    try {
      const r = await api.get('tools');
      App.toolsCache = r.items || [];
      App.renderTools();
      App.loadTelegramStatus();
      App.pollTools();
    } catch (e) { toast(e.message, true); }
  },
  renderTools() {
    const byGroup = {};
    App.toolsCache.forEach((t) => { (byGroup[t.group] = byGroup[t.group] || []).push(t); });
    const box = $('toolsList'); if (!box) return;
    box.replaceChildren(...Object.entries(byGroup).map(([group, items]) => {
      const cards = items.map((t) => el('div', { class: 'tool-card', 'data-tool': t.key },
        el('div', { class: 'tc-head' },
          el('span', { class: 'tc-icon' }, t.icon || '🔧'),
          el('span', { class: 'tc-title' }, t.title)),
        el('div', { class: 'tc-desc' }, t.desc),
        el('div', { class: 'tc-foot' },
          el('span', { class: 'tc-key' }, t.key),
          el('button', { class: 'primary sm', onclick: () => App.runTool(t.key) }, '▶ Запустить'))));
      return el('div', { class: 'tool-group' }, el('h3', {}, group),
        el('div', { class: 'tool-grid' }, ...cards));
    }));
  },
  async runTool(key) {
    const tool = App.toolsCache.find((t) => t.key === key); if (!tool) return;
    if (tool.confirm && !window.confirm(tool.confirm)) return;
    try {
      const r = await api.post('tool_start', { key });
      if (!r.ok) { toast(r.error, true); return; }
      App.toolsSince = 0; const log = $('toolsLog'); if (log) log.textContent = '';
      App.markRunningTool(key, true); App.pollTools();
    } catch (e) { toast(e.message, true); }
  },
  async stopTool() {
    try { await api.post('tool_stop'); App.pollTools(); } catch (e) { toast(e.message, true); }
  },
  clearToolLog() { const log = $('toolsLog'); if (log) log.textContent = ''; },
  markRunningTool(key, running) {
    document.querySelectorAll('.tool-card').forEach((c) => {
      c.classList.toggle('running', running && c.dataset.tool === key);
    });
  },
  async pollTools() {
    clearTimeout(App.toolsTimer);
    if (document.hidden) { App.toolsTimer = null; return; }
    try {
      const s = await api.get('tool_state', { since: App.toolsSince });
      const log = $('toolsLog');
      if (log && s.lines.length) {
        const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
        if (log.textContent === 'Инструмент ещё не запускался.') log.textContent = '';
        log.textContent += (log.textContent ? '\n' : '') + s.lines.map((l) => l.text).join('\n');
        App.toolsSince = s.next;
        if (atBottom) log.scrollTop = log.scrollHeight;
      }
      const stopBtn = $('toolsStopBtn'); if (stopBtn) stopBtn.disabled = !s.running;
      if (s.running) {
        App.markRunningTool(s.key, true);
        const st = $('toolsState');
        if (st) st.textContent = '⏳ ' + s.key + ' с ' + (s.started_at||'').replace('T',' ');
        App.toolsTimer = setTimeout(() => App.pollTools(), 2000);
      } else {
        App.markRunningTool(null, false);
        const st = $('toolsState');
        if (st) st.textContent = s.finished_at ? 'готово (код ' + s.exit_code + ')' : '';
      }
    } catch (e) {}
  },
  async loadTelegramStatus() {
    try {
      const s = await api.get('telegram_status');
      const chat = $('tgChat'); if (chat && !chat.value) chat.value = s.chat_id || '';
      const st = $('tgStatusLine');
      if (st) {
        if (!s.env_exists) st.innerHTML = '<span class="tg-warn">.env не найден</span>';
        else if (!s.token_set) st.innerHTML = '<span class="tg-warn">токен не задан</span>';
        else st.innerHTML = 'сохранён: <code>' + (s.token_preview||'') + '</code>' +
          (s.chat_id ? ', chat <code>'+s.chat_id+'</code>' : ', chat не задан');
      }
    } catch (e) {}
  },
  toggleTgToken() {
    const f = $('tgToken'), b = $('tgEyeBtn'); if (!f) return;
    const show = f.type === 'password';
    f.type = show ? 'text' : 'password';
    if (b) b.textContent = show ? '🙈 Скрыть' : '👁 Показать';
  },
  async saveTelegram() {
    const token = ($('tgToken')?.value || '').trim();
    const chat_id = ($('tgChat')?.value || '').trim();
    const box = $('tgResult');
    if (!token) { if (box) box.innerHTML = '<span class="tg-crit">Введите токен</span>'; return; }
    try {
      const r = await api.post('telegram_save', { token, chat_id });
      if (r.ok) { toast('Токен сохранён'); if (box) box.innerHTML = '<span class="tg-ok">✅ сохранено</span>'; $('tgToken').value=''; App.loadTelegramStatus(); }
    } catch (e) { if (box) box.innerHTML = '<span class="tg-crit">'+esc(e.message)+'</span>'; }
  },
  async testTelegram() {
    const token = ($('tgToken')?.value || '').trim();
    const chat_id = ($('tgChat')?.value || '').trim();
    const box = $('tgResult'); if (box) box.innerHTML = '⏳ Проверяю…';
    try {
      const r = await api.post('telegram_test', { token, chat_id });
      App.renderTelegramResult(r);
    } catch (e) { if (box) box.innerHTML = '<span class="tg-crit">'+esc(e.message)+'</span>'; }
  },
  renderTelegramResult(r) {
    const box = $('tgResult'); if (!box) return;
    const parts = [];
    if (r.token_ok) parts.push('<div class="tg-ok">✅ Токен валиден — <b>@'+esc(r.bot_username)+'</b></div>');
    else parts.push('<div class="tg-crit">❌ '+esc(r.token_error||'')+'</div>');
    if (r.chat_ok) parts.push('<div class="tg-ok">✅ Chat ID — <b>'+esc(r.chat_title)+'</b></div>');
    else if (r.chat_error) parts.push('<div class="tg-warn">⚠️ '+esc(r.chat_error)+'</div>');
    if (r.updates && r.updates.length) {
      parts.push('<div style="margin-top:8px"><div class="muted tiny">Чаты, которые видел бот (кликните):</div>'
        + r.updates.map((u) => '<div class="tg-chat-row"><code onclick="App.pickTgChat(\''+esc(u.chat_id)+'\')">'
          +esc(u.chat_id)+'</code><span class="muted">— '+esc(u.title)+'</span></div>').join('') + '</div>');
    }
    box.innerHTML = parts.join('');
  },
  pickTgChat(id) { const f = $('tgChat'); if (f) f.value = id; },


  async loadApiLog() {
    try {
      const r = await api.get('apilog', { blocked: $('logBlocked').checked ? 1 : 0 });
      $('guardCard').replaceChildren(
        el('h3', {}, 'Состояние защиты'),
        el('div', { class: 'row tight' },
          el('span', { class: 'pill ok' }, r.guard),
          el('span', { class: 'pill info' }, 'разрешено: ' + r.allowed.join(', ')),
          el('span', { class: 'pill ' + (r.summary.blocked ? 'crit' : 'ok') },
            `заблокировано попыток записи: ${r.summary.blocked}`)),
        el('div', { class: 'tiny muted', style: 'margin-top:9px' },
          `Всего запросов в журнале: ${NUM.format(r.summary.total)}. По методам: `
          + Object.entries(r.summary.by_method).map(([m, n]) => `${m} — ${n}`).join(', ')
          + '. Для источников с режимом browser то же правило работает внутри Playwright.'));
      const rows = r.items.map((i) => ({
        время: (i.ts || '').replace('T', ' '), метод: i.method,
        код: i.status ?? '—', 'мс': i.ms, адрес: i.url, примечание: i.note || '',
      }));
      $('apiLogBox').replaceChildren(el('div', { class: 'card' },
        rows.length ? renderTable(Object.keys(rows[0]), rows, { max: 120 })
          : el('div', { class: 'empty' }, 'Обращений к API ещё не было.')));
    } catch (e) { toast(e.message, true); }
  },
};

window.App = App;
document.addEventListener('DOMContentLoaded', () => App.init());