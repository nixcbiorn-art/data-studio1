"""ss_html_parts — базовые HTML-блоки: экранирование, гистограмма, метрики, агрегаты."""
from __future__ import annotations

import html
from redcat.reporting.ss_config import DELTA_CRIT, DELTA_OK, DELTA_WARN
from redcat.reporting.ss_metrics import _class_for_pct


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}".replace(",", "\u202f").rstrip("0").rstrip(".")
    if isinstance(v, int):
        return f"{v:,}".replace(",", "\u202f")
    return str(v)


def _hist_svg(histogram: list[dict], width=560, height=140) -> str:
    if not histogram:
        return '<div class="empty">нет данных</div>'
    n = len(histogram)
    max_share = max((b["share"] for b in histogram), default=1) or 1
    slot = width / n
    bw = slot * 0.7
    parts = [f'<svg viewBox="0 0 {width} {height}">']
    parts.append(f'<line x1="0" y1="{height - 22}" x2="{width}" '
                 f'y2="{height - 22}" stroke="#2c384e"/>')
    for i, b in enumerate(histogram):
        h = (b["share"] / max_share) * (height - 40)
        x = i * slot + (slot - bw) / 2
        y = height - 22 - h
        # Цвет столбца — по тем же порогам, что и классы:
        #   |Δ| ≤ DELTA_OK      → ok (синий)
        #   DELTA_OK < |Δ| ≤ DELTA_WARN → warn (жёлтый)
        #   |Δ| > DELTA_WARN    → crit (красный)
        edge = max(abs(b["from"] or 0), abs(b["to"] or 0))
        if edge > DELTA_WARN:
            color = "#ff5f6d"
        elif edge > DELTA_OK:
            color = "#f0b429"
        else:
            color = "#4f8cff"
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                     f'height="{h:.1f}" fill="{color}" fill-opacity="0.75" rx="2">'
                     f'<title>{b["from"]}…{b["to"]}% : {b["count"]} '
                     f'({b["share"]}%)</title></rect>')
        if i == 0 or i == n - 1:
            lo = "−∞" if b["from"] is None else str(b["from"])
            hi = "+∞" if b["to"] is None else str(b["to"])
            parts.append(f'<text x="{x + bw/2:.1f}" y="{height - 8}" '
                         f'fill="#7b869c" font-size="10" text-anchor="middle">'
                         f'{lo}…{hi}%</text>')
    parts.append('</svg>')
    return "".join(parts)


def _metric_block(metric_name: str, detail: dict) -> str:
    if detail.get("n", 0) == 0:
        return ""
    q = detail["quartiles_abs"]
    worst = detail["top_worst"]
    hist = _hist_svg(detail["histogram"])

    rows = []
    for w in worst:
        cls = _class_for_pct(w["diff_pct"])
        cls_class = ("crit" if w["class"] == "critical"
                     else "warn" if w["class"] == "warn" else "ok")
        rows.append(
            f'<tr><td>{_esc(w["jc"])}</td>'
            f'<td class="num">{_fmt(w["left"])}</td>'
            f'<td class="num">{_fmt(w["right"])}</td>'
            f'<td class="num"><span class="pill {cls}">{w["diff_pct"]:+.1f}%</span></td>'
            f'<td><span class="pill {cls_class}">{_esc(w["class"])}</span></td></tr>')

    return f'''
    <div class="card">
      <h3>▸ {_esc(metric_name)}  <span class="muted">
        (ЖК сравнено: {detail["n"]})</span></h3>
      <div class="muted" style="margin-bottom:8px">
        |Δ%|: min {q["min"]} · p25 {q["p25"]} · медиана <b>{q["median"]}</b>
        · p75 {q["p75"]} · max {q["max"]} ·
        среднее Δ% {detail["mean_pct"]} ·
        источник&gt;Redcat у {detail["left_higher"]} ЖК,
        Redcat&gt;источник у {detail["right_higher"]} ·
        ≥{DELTA_OK:g}% у {detail["over_ok"]}, ≥{DELTA_WARN:g}% у {detail["over_warn"]},
        ≥{DELTA_CRIT:g}% у {detail["over_crit"]}
      </div>
      <div class="hist">{hist}</div>
      <div class="scroll" style="max-height:260px;margin-top:10px">
        <table>
          <thead><tr><th>ЖК</th><th>Redcat</th><th>источник</th>
          <th>Δ%</th><th>класс</th></tr></thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
      </div>
    </div>
    '''


def _cross_agg_table(cross_agg: dict) -> str:
    if not cross_agg:
        return ""
    rows = []
    for metric_name, aggs in cross_agg.items():
        cells = []
        for agg_name in ("median", "mean", "sum"):
            v = aggs.get(agg_name) or {}
            pct = v.get("diff_pct")
            if pct is None:
                cells.append('<td class="num">—</td>')
                continue
            cls = _class_for_pct(pct)
            cells.append(f'<td class="num"><span class="pill {cls}">'
                         f'{pct:+.1f}%</span></td>')
        rows.append(f'<tr><td>{_esc(metric_name)}</td>{"".join(cells)}</tr>')
    return f'''
    <div class="card">
      <h3>Системный сдвиг (источник минус Redcat)</h3>
      <div class="muted" style="margin-bottom:8px">
        Совпадающий знак у median / mean / sum — системный сдвиг.
        Разнобой — отдельные ЖК. Пороги: ok ≤ {DELTA_OK:g}%,
        вним. ≤ {DELTA_WARN:g}%, выше — «руками».
      </div>
      <table>
        <thead><tr><th>метрика</th><th>median</th><th>mean</th><th>sum</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    </div>
    '''
