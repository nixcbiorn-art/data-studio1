"""ss_html_page — блоки источников и сборка HTML-дашборда."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from redcat.reporting.ss_config import DELTA_OK, DELTA_WARN
from redcat.reporting.ss_html_assets import _CSS
from redcat.reporting.ss_html_dev import _developer_block, _relevant_developers
from redcat.reporting.ss_html_parts import _cross_agg_table, _esc, _fmt, _metric_block
from redcat.reporting.ss_metrics import _class_for_pct


def _source_summary_pills(s: dict) -> str:
    """Короткая сводка-пилюли для шапки свёрнутого источника."""
    pills = []
    if s.get("ok"):
        pills.append(f'<span class="pill ok">ok {_fmt(s["ok"])}</span>')
    if s.get("warn"):
        pills.append(f'<span class="pill warn">вним. {_fmt(s["warn"])}</span>')
    if s.get("critical"):
        pills.append(f'<span class="pill crit">руками {_fmt(s["critical"])}</span>')
    if s.get("insufficient"):
        pills.append(f'<span class="pill info">мало {_fmt(s["insufficient"])}</span>')
    if s.get("left_only"):
        pills.append(f'<span class="pill gray">только RC {_fmt(s["left_only"])}</span>')
    if s.get("right_only"):
        pills.append(f'<span class="pill gray">только ист. {_fmt(s["right_only"])}</span>')
    return " ".join(pills)


def _source_body(st: dict) -> str:
    """Внутренности блока источника."""
    s = st["summary"]
    rows = st["rows"]
    kpis = [
        ("сопоставлено ЖК", s["matched"], ""),
        ("сходится", s["ok"], "ok"),
        ("внимание", s["warn"], "warn" if s["warn"] else ""),
        ("смотреть руками", s["critical"], "bad" if s["critical"] else ""),
        ("только у Redcat", s["left_only"], "warn" if s["left_only"] else ""),
        ("только у источника", s["right_only"], "warn" if s["right_only"] else ""),
        ("мало данных", s["insufficient"], ""),
        ("лотов Redcat", rows["left_total"], ""),
        ("лотов источника", rows["right_total"], ""),
    ]
    kpi_html = "".join(
        f'<div class="kpi {cls}"><div class="kl">{_esc(name)}</div>'
        f'<div class="kv">{_fmt(val)}</div></div>'
        for name, val, cls in kpis)

    mismatch_html = ""
    if rows["top_mismatches"]:
        def _mm_row(m):
            l, r, d = m["left"], m["right"], m["diff"]
            base = max(l, r) or 1
            pct = abs(d) / base * 100
            cls = _class_for_pct(pct)
            return (f'<tr class="mm-row mm-{cls}">'
                    f'<td>{_esc(m["jc"])}</td>'
                    f'<td class="num">{l}</td>'
                    f'<td class="num">{r}</td>'
                    f'<td class="num mm-delta mm-{cls}">{d:+}</td>'
                    f'<td class="num muted">{pct:.1f}%</td></tr>')
        mm_rows = "".join(_mm_row(m) for m in rows["top_mismatches"])
        mismatch_html = f'''
        <div class="card">
          <h3>Где перекос по количеству лотов</h3>
          <table><thead><tr><th>ЖК</th><th>Redcat</th><th>источник</th>
          <th>Δ</th></tr></thead><tbody>{mm_rows}</tbody></table>
        </div>
        '''

    metrics_html = "".join(
        _metric_block(name, detail)
        for name, detail in st["per_metric"].items())

    developer_html = _developer_block(st, relevant=_relevant_developers(st))

    return f'''
      <div class="kpis">{kpi_html}</div>
      {developer_html}
      {mismatch_html}
      {_cross_agg_table(st["cross_agg"])}
      {metrics_html}
    '''


def _source_block(st: dict) -> str:
    """Свёрнутый блок одного источника."""
    s = st["summary"]
    pills_html = _source_summary_pills(s)
    body_html = _source_body(st)

    return f'''
    <details class="src-block" id="src-{_esc(st["source"])}">
      <summary class="src-head">
        <span class="src-title">{_esc(st["source"])}</span>
        <span class="src-tag">Redcat «{_esc(st["left_label"])}»
          ↔ «{_esc(st["right_label"])}»</span>
        <span class="src-spacer"></span>
        <span class="src-pills">{pills_html}</span>
      </summary>
      <div class="src-body">{body_html}</div>
    </details>
    '''


def render_html_dashboard(stats: list[dict], out_path: Path) -> Path:
    if not stats:
        out_path.write_text("<html><body>нет данных</body></html>",
                            encoding="utf-8")
        return out_path

    tot = {
        "matched": sum(st["summary"]["matched"] for st in stats),
        "ok": sum(st["summary"]["ok"] for st in stats),
        "warn": sum(st["summary"]["warn"] for st in stats),
        "critical": sum(st["summary"]["critical"] for st in stats),
        "left_only": sum(st["summary"]["left_only"] for st in stats),
        "right_only": sum(st["summary"]["right_only"] for st in stats),
        "rows_left": sum(st["rows"]["left_total"] for st in stats),
        "rows_right": sum(st["rows"]["right_total"] for st in stats),
    }

    top_kpis = f'''
    <div class="kpis">
      <div class="kpi"><div class="kl">источников</div>
        <div class="kv">{len(stats)}</div></div>
      <div class="kpi ok"><div class="kl">сопоставлено ЖК</div>
        <div class="kv">{_fmt(tot["matched"])}</div></div>
      <div class="kpi ok"><div class="kl">сходится</div>
        <div class="kv">{_fmt(tot["ok"])}</div></div>
      <div class="kpi {'warn' if tot['warn'] else ''}"><div class="kl">внимание</div>
        <div class="kv">{_fmt(tot["warn"])}</div></div>
      <div class="kpi {'bad' if tot['critical'] else ''}">
        <div class="kl">смотреть руками</div>
        <div class="kv">{_fmt(tot["critical"])}</div></div>
      <div class="kpi {'warn' if tot['left_only'] else ''}">
        <div class="kl">только Redcat</div>
        <div class="kv">{_fmt(tot["left_only"])}</div></div>
      <div class="kpi {'warn' if tot['right_only'] else ''}">
        <div class="kl">только источник</div>
        <div class="kv">{_fmt(tot["right_only"])}</div></div>
      <div class="kpi"><div class="kl">лотов Redcat</div>
        <div class="kv">{_fmt(tot["rows_left"])}</div></div>
      <div class="kpi"><div class="kl">лотов источников</div>
        <div class="kv">{_fmt(tot["rows_right"])}</div></div>
    </div>
    '''

    head = ["источник", "сопоставлено", "сходится", "внимание",
            "руками", "только Redcat", "только источник", "мало данных"]
    rows_html = []
    for st in stats:
        s = st["summary"]
        rows_html.append(
            f'<tr><td><a href="#src-{_esc(st["source"])}" '
            f'class="src-link" data-target="src-{_esc(st["source"])}" '
            f'style="color:#9dc0ff;text-decoration:none">'
            f'{_esc(st["source"])}</a></td>'
            f'<td class="num">{_fmt(s["matched"])}</td>'
            f'<td class="num"><span class="pill ok">{_fmt(s["ok"])}</span></td>'
            f'<td class="num"><span class="pill warn">{_fmt(s["warn"])}</span></td>'
            f'<td class="num"><span class="pill crit">{_fmt(s["critical"])}</span></td>'
            f'<td class="num">{_fmt(s["left_only"])}</td>'
            f'<td class="num">{_fmt(s["right_only"])}</td>'
            f'<td class="num">{_fmt(s["insufficient"])}</td></tr>')

    summary_table = f'''
    <div class="card">
      <h3>Источники</h3>
      <div class="muted" style="margin-bottom:8px">
        Клик по названию — раскроет блок источника и прокрутит к нему.
        Пороги: ok ≤ {DELTA_OK:g}%, вним. ≤ {DELTA_WARN:g}%, выше — «руками».
      </div>
      <table>
        <thead><tr>{"".join(f"<th>{_esc(h)}</th>" for h in head)}</tr></thead>
        <tbody>{"".join(rows_html)}</tbody>
      </table>
    </div>
    '''

    controls = f'''
    <div class="controls">
      <button type="button" onclick="document.querySelectorAll('details.src-block').forEach(d => d.open = true)">
        Развернуть все
      </button>
      <button type="button" onclick="document.querySelectorAll('details.src-block').forEach(d => d.open = false)">
        Свернуть все
      </button>
      <button type="button" onclick="document.querySelectorAll('details.dev-row').forEach(d => d.open = false)">
        Свернуть застройщиков
      </button>
      <span class="hint">блоки источников свёрнуты по умолчанию</span>
    </div>
    '''

    blocks = "".join(_source_block(st) for st in stats)

    generated = datetime.now().strftime("%d.%m.%Y %H:%M")
    html_doc = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Сверка источников с Redcat</title>
<style>{_CSS}</style></head><body>
<h1>Сверка источников с Redcat</h1>
<p class="sub">Сформировано {generated}. Источников: {len(stats)}.
Redcat слева, проверяемый источник справа. Δ% = источник минус Redcat.
Пороги: ok ≤ {DELTA_OK:g}%, вним. ≤ {DELTA_WARN:g}%, выше — «руками».</p>
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
document.querySelectorAll('a.src-link').forEach(function(a) {{
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
