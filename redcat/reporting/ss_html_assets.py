"""ss_html_assets — CSS и JS HTML-дашборда."""
from __future__ import annotations



# ──────────────────────────────────────────────────────────────
#  HTML-дашборд
# ──────────────────────────────────────────────────────────────
_CSS = """
* { box-sizing: border-box; }
body {
  margin: 0; padding: 28px; background: #0f1115; color: #e6e8ee;
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
}
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 24px 0 12px; font-weight: 600; }
h3 { font-size: 14px; margin: 0 0 10px; font-weight: 600; }
.sub { color: #8b93a7; margin: 0 0 22px; font-size: 13px; }
.card {
  background: #181b22; border: 1px solid #242835; border-radius: 10px;
  padding: 16px 18px; margin-bottom: 16px;
}
.kpis {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px; margin-bottom: 18px;
}
.kpi {
  background: #181b22; border: 1px solid #242835; border-radius: 10px;
  padding: 12px 14px;
}
.kpi .kl { color: #8b93a7; font-size: 11px; text-transform: uppercase; letter-spacing: .4px; }
.kpi .kv { font-size: 20px; font-weight: 650; margin-top: 3px; }
.kpi.warn { border-color: #7a4a1e; background: #1e1a15; }
.kpi.bad { border-color: #7a1e1e; background: #1f1414; }
.kpi.ok { border-color: #1e5c37; background: #141d19; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th, td {
  padding: 7px 10px; text-align: right; border-bottom: 1px solid #242835;
  white-space: nowrap;
}
th:first-child, td:first-child { text-align: left; }
th {
  color: #8b93a7; font-weight: 600; position: sticky; top: 0;
  background: #181b22; z-index: 2;
}
.scroll { overflow-x: auto; max-height: 520px; overflow-y: auto; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 20px;
        font-size: 11px; border: 1px solid #2c384e; }
.pill.ok { background: rgba(62,207,142,.15); color: #3ecf8e; border-color: rgba(62,207,142,.4); }
.pill.warn { background: rgba(240,180,41,.15); color: #f0b429; border-color: rgba(240,180,41,.4); }
.pill.crit { background: rgba(255,95,109,.15); color: #ff5f6d; border-color: rgba(255,95,109,.4); }
.pill.info { background: #24375c; color: #9dc0ff; }
.pill.gray { background: #202530; color: #8792a1; }
.muted { color: #8b93a7; font-size: 12px; }
.num { font-variant-numeric: tabular-nums; }
.empty { padding: 24px; text-align: center; color: #8b93a7; }
.hist svg { display: block; width: 100%; height: auto; }
.source-block { margin-bottom: 12px; }

/* ── кнопки «раскрыть все / свернуть все» ───────────────────── */
.controls {
  display: flex; gap: 8px; margin: 0 0 16px 0;
  align-items: center; flex-wrap: wrap;
}
.controls button {
  background: #181b22; color: #e6e8ee; border: 1px solid #2c384e;
  padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 12.5px;
  font-family: inherit;
}
.controls button:hover { background: #202530; border-color: #3a4a6a; }
.controls .hint { color: #8b93a7; font-size: 12px; }

/* ── сворачивание всего источника ────────────────────────────── */
details.src-block {
  background: #10131a; border: 1px solid #242835; border-radius: 12px;
  margin-bottom: 12px; padding: 0;
}
details.src-block > summary.src-head {
  display: flex; align-items: center; gap: 12px;
  padding: 14px 18px; cursor: pointer; list-style: none;
  border-radius: 12px; font-size: 14px;
}
details.src-block > summary.src-head::-webkit-details-marker { display: none; }
details.src-block > summary.src-head::before {
  content: "▸"; color: #8b93a7; font-size: 12px;
  transition: transform .15s; display: inline-block;
  width: 14px; flex: 0 0 14px;
}
details.src-block[open] > summary.src-head::before { transform: rotate(90deg); }
details.src-block > summary.src-head:hover { background: #161b25; }
details.src-block[open] > summary.src-head {
  border-bottom: 1px solid #242835; border-radius: 12px 12px 0 0;
}
.src-head .src-title { font-weight: 650; }
.src-head .src-tag {
  font-size: 12px; color: #8792a1;
  max-width: 40%; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap;
}
.src-head .src-pills { display: flex; gap: 5px; flex-wrap: wrap; }
.src-head .src-spacer { flex: 1; }
details.src-block > .src-body { padding: 16px 18px 4px; }
details.src-block .source-block { margin-bottom: 0; }

/* ── свернуть по застройщикам ───────────────────────────────── */
.dev-card .dev-list details {
  background: #161a21; border: 1px solid #242835;
  border-radius: 8px; margin-bottom: 6px;
}
.dev-list details summary {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 12px; cursor: pointer; list-style: none;
  font-size: 13px;
}
.dev-list details summary::-webkit-details-marker { display: none; }
.dev-list details summary::before {
  content: "▸"; color: #8b93a7; font-size: 11px;
  transition: transform .15s; display: inline-block;
  width: 12px; flex: 0 0 12px;
}
.dev-list details[open] summary::before { transform: rotate(90deg); }
.dev-list details summary:hover { background: #1c2027; }
.dev-list details summary .pills { display: flex; gap: 4px; flex-wrap: wrap; }
.dev-list details summary .msummary {
  margin-left: auto; font-size: 11.5px; color: #8b93a7;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  max-width: 40%;
}
.dev-list details > .scroll { padding: 0 12px 10px; }

.mm-row.mm-crit td { background: rgba(255, 95, 109, .10); }
.mm-row.mm-warn td { background: rgba(240, 180, 41, .08); }
.mm-row.mm-ok td { background: rgba(62, 207, 142, .06); }
.mm-row td.mm-delta { font-weight: 700; }
td.mm-delta.mm-crit { color: #ff5f6d; }
td.mm-delta.mm-warn { color: #f0b429; }
td.mm-delta.mm-ok { color: #3ecf8e; }
.mm-row.mm-crit td:first-child { border-left: 3px solid #ff5f6d; }
.mm-row.mm-warn td:first-child { border-left: 3px solid #f0b429; }
.mm-row.mm-ok td:first-child { border-left: 3px solid #3ecf8e; }
"""


# JS для фильтра по застройщику в отчёте source_stats.
# Вынесен отдельно, потому что внутри f-string фигурные скобки
# { } конфликтуют с плейсхолдерами Python.
_DEV_FILTER_JS = """
<script>
function devFilter(sel) {
  var card = sel.closest('.dev-card');
  if (!card) return;
  var name = sel.value;
  var rows = card.querySelectorAll('details.dev-row');
  for (var i = 0; i < rows.length; i++) {
    var d = rows[i];
    var rel = d.getAttribute('data-dev-rel') === '1';
    if (!name) {
      d.style.display = '';
    } else if (name === '__relevant__') {
      d.style.display = rel ? '' : 'none';
    } else {
      d.style.display = (d.getAttribute('data-dev') === name) ? '' : 'none';
    }
  }
}
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('select[data-dev-filter]').forEach(function (s) {
    devFilter(s);
  });
});
</script>
"""
