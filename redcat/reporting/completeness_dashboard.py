"""
Дашборд заполненности Redcat.
=============================
Самодостаточный HTML-файл: заполненность по полям, по записям и по группам
для таблиц из reports/redcat_data.db. Ничего не отправляет в сеть, CSS и JS
инлайн, полоски — чистый CSS.

Что показывает:
  • сводную таблицу по всем Redcat-таблицам: сколько записей, взвешенная
    заполненность, сколько полей хуже 50%, сколько проблемных записей
    (ниже порога, по умолчанию 80%);
  • для каждой таблицы — свёрнутый блок:
      – поля: доля заполненных значений, худшие сверху, обязательные поля
        помечены звёздочкой;
      – записи ниже порога: id, название, % заполненности и список
        недостающих полей;
      – в разрезе групп (ЖК, застройщик и т.п.): средняя заполненность
        группы, топ худших.

Использование:
    python -m redcat.reporting.completeness_dashboard
    python -m redcat.reporting.completeness_dashboard --table apartments
    python -m redcat.reporting.completeness_dashboard --tables apartments housing_complexes
    python -m redcat.reporting.completeness_dashboard --html reports/completeness.html --no-open
    python -m redcat.reporting.completeness_dashboard --threshold 90
"""
from __future__ import annotations

from redcat.core import paths
import argparse
import html
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

BASE = paths.ROOT
sys.path.insert(0, str(BASE))

from redcat.quality import completeness
from redcat.data import dataops
from redcat.data import field_labels
from redcat.sources import registry as src


# ──────────────────────────────────────────────────────────────
#  СБОР ДАННЫХ
# ──────────────────────────────────────────────────────────────
def _load_specs() -> dict:
    """Описания источников. Нужны для весов (required/numeric) и подписей."""
    try:
        src.load_from_dir(paths.SOURCES_DIR)
        return {s.key: s for s in src.all_sources()}
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  sources не загружены ({type(e).__name__}: {e}). "
              f"Веса полей будут по умолчанию.")
        return {}


def _pick_group_field(field_names: list, spec) -> str | None:
    """Какое поле взять разрезом для групповой сводки.

    Приоритет — то, что описано в spec.group_fields, потом стандартные
    имена из мира недвижимости, потом уже ничего (без разрезов).
    """
    if spec and spec.group_fields:
        for f in spec.group_fields:
            if f in field_names:
                return f
    for candidate in ("housing_complex_name", "developer_name",
                      "provider_name", "district", "name"):
        if candidate in field_names:
            return candidate
    return None


def collect_tables(tables=None) -> list:
    """Собирает заполненность по всем (или выбранным) таблицам Redcat."""
    specs = _load_specs()
    try:
        all_tables = [t["name"] for t in dataops.list_tables(completeness.DATA_DB)]
    except dataops.DataError as e:
        print(f"❌ {e}")
        return []
    all_tables = [t for t in all_tables
                  if t != "comparison_vs_previous" and not t.startswith("_")]
    if tables:
        wanted = set(tables)
        all_tables = [t for t in all_tables if t in wanted]

    result = []
    for name in all_tables:
        spec = specs.get(name)
        try:
            fields = completeness.completeness_by_field(completeness.DATA_DB, name)
        except dataops.DataError as e:
            print(f"  ⚠️  {name}: {e}")
            continue

        for f in fields:
            f["kind"] = completeness._kind(f["field"], spec)
            f["label"] = field_labels.label_for(f["field"])

        overall = completeness.overall_score(fields, spec)

        records = completeness.completeness_by_record(
            completeness.DATA_DB, name, spec, precomputed_fields=fields)
        for r in records:
            r["missing_labels"] = [field_labels.label_for(f)
                                   for f in r.get("missing", [])]

        group_field = _pick_group_field([f["field"] for f in fields], spec)
        groups = []
        if group_field:
            try:
                groups = completeness.completeness_by_group(
                    completeness.DATA_DB, name, spec, group_field,
                    precomputed_fields=fields)
                for g in groups:
                    g["worst_fields_labels"] = [
                        field_labels.label_for(f)
                        for f in g.get("worst_fields", [])
                    ]
            except dataops.DataError as e:
                print(f"  ⚠️  {name}: групповая сводка пропущена ({e})")

        result.append({
            "table": name,
            "title": spec.title if spec and spec.title else name,
            "overall": overall,
            "fields": fields,
            "records": records,
            "records_total": len(records),
            "group_field": group_field,
            "groups": groups,
        })
    return result


# ──────────────────────────────────────────────────────────────
#  РЕНДЕР
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
.wrap-cell { white-space: normal; overflow: visible; text-overflow: clip; line-height: 1.9; }
.chip.miss {
  display: inline-block;
  background: rgba(239,74,90,.14); color: #ffb3ba;
  border: 1px solid rgba(239,74,90,.35);
  font-size: 10.5px; padding: 1px 7px; border-radius: 20px;
  margin: 1px 3px 1px 0;
}
td.pct-bad  { color: #ef4a5a; font-weight: 650; }
td.pct-warn { color: #e0ab1f; font-weight: 650; }
td.pct-soft { color: #8b93a7; }

/* полоски заполненности полей */
.bar-list { display: flex; flex-direction: column; gap: 5px; }
.bar-row {
  display: grid;
  grid-template-columns: minmax(180px, 320px) 1fr auto;
  gap: 10px; align-items: center; font-size: 12px;
}
.bar-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-track { background: #0f1115; border-radius: 4px; height: 14px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 4px; }
.bar-fill.fill-ok   { background: linear-gradient(90deg, #1f9d5c, #2fbf71); }
.bar-fill.fill-warn { background: linear-gradient(90deg, #b8850f, #e0ab1f); }
.bar-fill.fill-crit { background: linear-gradient(90deg, #c73847, #ef4a5a); }
.bar-val {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 11.5px; text-align: right; min-width: 55px; color: #8b93a7;
}

/* кнопки */
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

/* сворачивание таблицы */
details.table-block {
  background: #10131a; border: 1px solid #242835; border-radius: 12px;
  margin-bottom: 12px; padding: 0;
}
details.table-block > summary.table-head {
  display: flex; align-items: center; gap: 12px;
  padding: 14px 18px; cursor: pointer; list-style: none;
  border-radius: 12px; font-size: 14px;
}
details.table-block > summary.table-head::-webkit-details-marker { display: none; }
details.table-block > summary.table-head::before {
  content: "▸"; color: #8b93a7; font-size: 12px;
  transition: transform .15s; display: inline-block;
  width: 14px; flex: 0 0 14px;
}
details.table-block[open] > summary.table-head::before { transform: rotate(90deg); }
details.table-block > summary.table-head:hover { background: #161b25; }
details.table-block[open] > summary.table-head {
  border-bottom: 1px solid #242835; border-radius: 12px 12px 0 0;
}
.table-head .ttl { font-weight: 650; }
.table-head .sub-ttl {
  font-size: 12px; color: #8792a1;
  max-width: 40%; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap;
}
.table-head .pills { display: flex; gap: 5px; flex-wrap: wrap; }
.table-head .spacer { flex: 1; }
details.table-block > .table-body { padding: 16px 18px 4px; }
"""


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    return f"{v:.1f}%"


def _class_of(v) -> str:
    """Класс заполненности: ≥90 ok, ≥50 warn, ниже — crit."""
    if v is None:
        return "info"
    return "ok" if v >= 90 else "warn" if v >= 50 else "crit"


def _kpi(label: str, value, cls: str = "") -> str:
    return (f'<div class="kpi {cls}"><div class="kl">{_esc(label)}</div>'
            f'<div class="kv">{_esc(value)}</div></div>')


def _bar_list(items: list, max_items: int = 30) -> str:
    """items: [(label, value_0_100)] — худшие уже сверху."""
    if not items:
        return '<div class="empty">нет данных</div>'
    rows = []
    for label, value in items[:max_items]:
        v = value if value is not None else 0.0
        cls = _class_of(v)
        rows.append(
            f'<div class="bar-row">'
            f'  <div class="bar-label" title="{_esc(label)}">{_esc(label)}</div>'
            f'  <div class="bar-track">'
            f'    <div class="bar-fill fill-{cls}" style="width:{v:.1f}%"></div>'
            f'  </div>'
            f'  <div class="bar-val">{v:.1f}%</div>'
            f'</div>'
        )
    tail = ""
    if len(items) > max_items:
        tail = (f'<div class="muted" style="margin-top:8px">'
                f'…и ещё {len(items) - max_items} полей. '
                f'Полный список — во вкладке «Заполненность» приложения.</div>')
    return f'<div class="bar-list">{"".join(rows)}</div>{tail}'


def _fields_block(t: dict) -> str:
    """Секция «Поля: доля заполненных значений»."""
    fields = t["fields"]
    if not fields:
        return ""
    # худшие сверху; звёздочка у обязательных
    ordered = sorted(fields, key=lambda f: (f["fill_rate"] is None, f["fill_rate"]))
    items = []
    for f in ordered:
        label = f["label"]
        if f.get("kind") == "required":
            label = "* " + label
        items.append((label, f["fill_rate"]))
    return f'''
    <div class="card">
      <h3>Поля: доля заполненных значений
        <span class="muted" style="font-weight:400">
          — {len(fields)} полей, * — обязательное по описанию источника
        </span>
      </h3>
      <div class="muted" style="margin-bottom:10px">
        Порог окраски: <span class="pill ok">≥ 90%</span>
        <span class="pill warn">50–90%</span>
        <span class="pill crit">&lt; 50%</span>
      </div>
      {_bar_list(items)}
    </div>
    '''


def _records_block(t: dict, threshold: float) -> str:
    """Секция «Записи ниже порога»."""
    records = t["records"]
    if not records:
        return ""
    below = sorted((r for r in records if r["pct"] < threshold),
                   key=lambda r: r["pct"])
    if not below:
        return f'''
        <div class="card">
          <h3>Записи ниже порога</h3>
          <div class="empty">
            Все {len(records)} записей заполнены не хуже {threshold:g}%.
          </div>
        </div>
        '''
    cap = 300
    pct_cls = lambda p: ("pct-bad" if p < 50
                         else "pct-warn" if p < threshold else "pct-soft")
    rows = []
    for r in below[:cap]:
        miss = "".join(f'<span class="chip miss">{_esc(m)}</span>'
                       for m in (r.get("missing_labels") or [])[:12])
        if len(r.get("missing_labels") or []) > 12:
            miss += f'<span class="muted tiny">+{len(r["missing_labels"]) - 12}</span>'
        rows.append(
            f'<tr>'
            f'  <td><b>{_esc(r.get("name") or r.get("id"))}</b>'
            f'    <span class="muted" style="font-size:11px"> · id {_esc(r.get("id"))}</span></td>'
            f'  <td class="num {pct_cls(r["pct"])}">{r["pct"]:.1f}</td>'
            f'  <td class="wrap-cell">{miss or "—"}</td>'
            f'</tr>'
        )
    tail = ""
    if len(below) > cap:
        tail = (f'<div class="muted" style="margin-top:8px">'
                f'Показаны первые {cap} из {len(below)}. '
                f'Все — во вкладке «Заполненность» приложения.</div>')
    return f'''
    <div class="card">
      <h3>Записи ниже порога
        <span class="muted" style="font-weight:400">
          — {len(below)} из {len(records)}, порог {threshold:g}%
        </span>
      </h3>
      <div class="scroll" style="max-height:520px">
        <table>
          <thead><tr><th>запись</th><th>%</th><th>чего не хватает</th></tr></thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
      </div>
      {tail}
    </div>
    '''


def _groups_block(t: dict) -> str:
    """Секция «По группам» (ЖК, застройщик и т.п.)."""
    groups = t.get("groups") or []
    field = t.get("group_field")
    if not groups or not field:
        return ""
    # худшие сверху
    ordered = sorted(groups, key=lambda g: (g["avg_pct"] is None, g["avg_pct"]))
    items = [(g["group"] or "(пусто)", g["avg_pct"]) for g in ordered[:40]]
    label = field_labels.label_for(field)
    return f'''
    <div class="card">
      <h3>В разрезе «{_esc(label)}»
        <span class="muted" style="font-weight:400">
          — {len(groups)} групп, худшие сверху
        </span>
      </h3>
      <div class="muted" style="margin-bottom:10px">
        Средняя заполненность записей группы. Много пустых ЖК или
        застройщиков — типичный след неполного сбора данных.
      </div>
      {_bar_list(items, max_items=25)}
      <div class="scroll" style="max-height:340px;margin-top:12px">
        <table>
          <thead><tr><th>группа</th><th>записей</th><th>средний %</th>
          <th>хуже 80%</th><th>чаще всего пустые поля</th></tr></thead>
          <tbody>
          {"".join(
              f'<tr>'
              f'  <td>{_esc(g["group"] or "(пусто)")}</td>'
              f'  <td class="num">{g["records"]}</td>'
              f'  <td class="num">{g["avg_pct"]:.1f}</td>'
              f'  <td class="num">{g["below_80"]}</td>'
              f'  <td class="wrap-cell">'
              f'    {"".join(f"<span class=\'chip miss\'>{_esc(x)}</span>" for x in g.get("worst_fields_labels") or []) or "—"}'
              f'  </td>'
              f'</tr>'
              for g in ordered[:25]
          )}
          </tbody>
        </table>
      </div>
    </div>
    '''


def _table_summary_pills(t: dict, threshold: float) -> str:
    """Пилюли для шапки свёрнутого блока таблицы."""
    pills = []
    ov = t["overall"].get("pct")
    if ov is not None:
        cls = _class_of(ov)
        pills.append(f'<span class="pill {cls}">заполнено {ov:.1f}%</span>')
    low = sum(1 for f in t["fields"] if (f["fill_rate"] or 0) < 50)
    if low:
        pills.append(f'<span class="pill crit">полей &lt; 50%: {low}</span>')
    below = sum(1 for r in t["records"] if r["pct"] < threshold)
    if below:
        pills.append(f'<span class="pill warn">записей &lt; {threshold:g}%: {below}</span>')
    else:
        pills.append(f'<span class="pill ok">все записи ≥ {threshold:g}%</span>')
    return " ".join(pills)


def _table_block(t: dict, threshold: float) -> str:
    pills = _table_summary_pills(t, threshold)
    title = t["title"]
    name = t["table"]
    body = (
        _fields_block(t)
        + _records_block(t, threshold)
        + _groups_block(t)
    )
    return f'''
    <details class="table-block" id="tbl-{_esc(name)}">
      <summary class="table-head">
        <span class="ttl">{_esc(name)}</span>
        <span class="sub-ttl">{_esc(title)} · {len(t["records_total"] and t["records"] or [])} записей · {len(t["fields"])} полей</span>
        <span class="spacer"></span>
        <span class="pills">{pills}</span>
      </summary>
      <div class="table-body">{body}</div>
    </details>
    '''


def render(data: list, out_path: Path, threshold: float = 80.0) -> Path:
    if not data:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            "<html><body>нет данных</body></html>", encoding="utf-8")
        return out_path

    # сводные KPI
    total_tables = len(data)
    total_records = sum(t["records_total"] for t in data)
    overalls = [t["overall"].get("pct") for t in data
                if t["overall"].get("pct") is not None]
    avg_overall = (sum(overalls) / len(overalls)) if overalls else None
    total_low_fields = sum(1 for t in data for f in t["fields"]
                           if (f["fill_rate"] or 0) < 50)
    total_below = sum(1 for t in data for r in t["records"]
                      if r["pct"] < threshold)

    def kpi_cls(v):
        if v is None:
            return ""
        return "ok" if v >= 90 else "bad" if v < 50 else "warn"

    top_kpis = f'''
    <div class="kpis">
      {_kpi("Таблиц", total_tables)}
      {_kpi("Записей всего", f"{total_records:,}".replace(",", "\u202f"))}
      {_kpi("Средняя заполненность",
            f"{avg_overall:.1f}%" if avg_overall is not None else "—",
            kpi_cls(avg_overall))}
      {_kpi("Полей ниже 50%", total_low_fields,
            "bad" if total_low_fields else "ok")}
      {_kpi(f"Записей ниже {threshold:g}%", total_below,
            "warn" if total_below else "ok")}
    </div>
    '''

    # сводная таблица
    rows_html = []
    for t in data:
        ov = t["overall"].get("pct")
        low = sum(1 for f in t["fields"] if (f["fill_rate"] or 0) < 50)
        below = sum(1 for r in t["records"] if r["pct"] < threshold)
        rows_html.append(
            f'<tr>'
            f'  <td><a href="#tbl-{_esc(t["table"])}" '
            f'       class="tbl-link" data-target="tbl-{_esc(t["table"])}" '
            f'       style="color:#9dc0ff;text-decoration:none">'
            f'    {_esc(t["table"])}</a></td>'
            f'  <td>{_esc(t["title"])}</td>'
            f'  <td class="num">{t["records_total"]:,}</td>'.replace(",", "\u202f") +
            f'  <td class="num">{len(t["fields"])}</td>'
            f'  <td class="num"><span class="pill {_class_of(ov)}">'
            f'    {ov:.1f}%</span></td>'
            f'  <td class="num">{low}</td>'
            f'  <td class="num">{below}</td>'
            f'</tr>'
        )
    summary_table = f'''
    <div class="card">
      <h3>Сводка по таблицам</h3>
      <div class="muted" style="margin-bottom:8px">
        Клик по имени таблицы — раскроет блок и прокрутит к нему.
        Заполненность — взвешенная: обязательные поля из описания источника
        весят больше опциональных.
      </div>
      <table>
        <thead><tr>
          <th>таблица</th><th>название</th><th>записей</th><th>полей</th>
          <th>заполнено</th><th>полей &lt; 50%</th>
          <th>записей &lt; {threshold:g}%</th>
        </tr></thead>
        <tbody>{"".join(rows_html)}</tbody>
      </table>
    </div>
    '''

    controls = '''
    <div class="controls">
      <button type="button"
        onclick="document.querySelectorAll('details.table-block').forEach(d => d.open = true)">
        Развернуть все
      </button>
      <button type="button"
        onclick="document.querySelectorAll('details.table-block').forEach(d => d.open = false)">
        Свернуть все
      </button>
      <span class="hint">таблицы свёрнуты по умолчанию</span>
    </div>
    '''

    blocks = "".join(_table_block(t, threshold) for t in data)

    generated = datetime.now().strftime("%d.%m.%Y %H:%M")
    html_doc = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Заполненность данных Redcat</title>
<style>{_CSS}</style></head><body>
<h1>Заполненность данных Redcat</h1>
<p class="sub">Сформировано {generated}. Таблиц: {total_tables}.
Порог проблемной записи: {threshold:g}%.
Общая заполненность — взвешенная по важности полей, см. описание источника.</p>
{top_kpis}
{summary_table}
{controls}
{blocks}
<script>
function openFromHash() {{
  var id = location.hash.replace('#', '');
  if (!id) return;
  var el = document.getElementById(id);
  if (el && el.tagName === 'DETAILS') {{
    el.open = true;
    el.scrollIntoView({{behavior: 'smooth', block: 'start'}});
  }}
}}
window.addEventListener('hashchange', openFromHash);
document.addEventListener('DOMContentLoaded', openFromHash);
document.querySelectorAll('a.tbl-link').forEach(function(a) {{
  a.addEventListener('click', function() {{
    var t = document.getElementById(a.dataset.target);
    if (t) t.open = true;
  }});
}});
</script>
</body></html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")
    return out_path


# ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Дашборд заполненности Redcat (HTML, самодостаточный).")
    ap.add_argument("--table", default=None,
                    help="только одна таблица (быстрый режим)")
    ap.add_argument("--tables", nargs="*", default=None,
                    help="список таблиц; без флага — все, что есть в базе")
    ap.add_argument("--threshold", type=float, default=80.0,
                    help="порог «проблемной» записи в процентах (по умолчанию 80)")
    ap.add_argument("--html", default="reports/completeness.html",
                    help="куда сохранить HTML (по умолчанию reports/completeness.html)")
    ap.add_argument("--no-open", action="store_true",
                    help="не открывать HTML в браузере автоматически")
    args = ap.parse_args()

    if not completeness.DATA_DB.exists():
        print(f"❌ База не найдена: {completeness.DATA_DB}")
        print("   Сначала соберите данные: python -m redcat.collection.redcat_scraper")
        print("   Или создайте демо-набор:  python -m redcat.tools.demo_data")
        return 1

    tables = [args.table] if args.table else args.tables
    print(f"📖 База: {completeness.DATA_DB}")
    if tables:
        print(f"   Таблицы: {', '.join(tables)}")
    else:
        print("   Таблицы: все, что есть в базе")

    data = collect_tables(tables)
    if not data:
        print("❌ Нечего показывать — в базе нет ни одной таблицы.")
        return 1

    print()
    for t in data:
        ov = t["overall"].get("pct")
        below = sum(1 for r in t["records"] if r["pct"] < args.threshold)
        print(f"  • {t['table']:<24} "
              f"записей {t['records_total']:>7}  "
              f"полей {len(t['fields']):>3}  "
              f"заполнено {('—' if ov is None else f'{ov:5.1f}%')}  "
              f"проблемных {below:>5}"
              + (f"  · групп по {t['group_field']}: {len(t['groups'])}"
                 if t.get("group_field") and t["groups"] else ""))

    out = render(data, Path(args.html), threshold=args.threshold)
    print(f"\n🌐 HTML: {out}")

    if not args.no_open:
        try:
            webbrowser.open(out.resolve().as_uri())
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())