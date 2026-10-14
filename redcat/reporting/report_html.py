"""
HTML-дашборд с графиками динамики
=================================
Строит самодостаточный HTML-файл: графики рисуются как inline-SVG прямо на
Python, без matplotlib и без CDN. Файл открывается в браузере офлайн и не
виснет на больших объёмах, потому что содержит только агрегаты, а не сырые
десятки тысяч строк (они лежат в SQLite/Parquet).
"""

from __future__ import annotations

import html
from datetime import datetime

W, H = 780, 260           # размеры области графика
PAD_L, PAD_R = 70, 20
PAD_T, PAD_B = 20, 46


def _nice_num(v):
    """Компактное человекочитаемое число: 1 234 567 -> 1.23 млн."""
    if v is None:
        return "—"
    av = abs(v)
    if av >= 1_000_000_000:
        return f"{v / 1_000_000_000:.2f} млрд"
    if av >= 1_000_000:
        return f"{v / 1_000_000:.2f} млн"
    if av >= 1_000:
        return f"{v / 1_000:.1f} тыс."
    if isinstance(v, float) and not v.is_integer():
        return f"{v:.2f}"
    return f"{int(v)}"


def _axis_ticks(lo, hi, count=5):
    """Подбирает «круглые» отметки для оси Y."""
    if hi == lo:
        hi = lo + 1
    step = (hi - lo) / count
    magnitude = 10 ** (len(str(int(abs(step)))) - 1) if abs(step) >= 1 else 1
    step = max(round(step / magnitude) * magnitude, step)
    ticks, t = [], lo
    while t <= hi + step * 0.5 and len(ticks) <= count + 2:
        ticks.append(t)
        t += step
    return ticks


def _line_chart(title, labels, series, unit=""):
    """Линейный график: series = [(имя, [значения], цвет), ...].

    Значения None пропускаются (разрыв линии), чтобы отсутствующая метрика
    в старом запуске не обнуляла график и не врала о падении до нуля.
    """
    flat = [v for _, values, _ in series for v in values if v is not None]
    if not flat:
        return f'<div class="card"><h3>{html.escape(title)}</h3>' \
               f'<p class="empty">Недостаточно данных для графика.</p></div>'

    lo, hi = min(flat), max(flat)
    if lo == hi:
        lo, hi = lo * 0.95 if lo else 0, hi * 1.05 if hi else 1
    else:
        span = hi - lo
        lo, hi = lo - span * 0.08, hi + span * 0.08
    lo = min(lo, 0) if min(flat) >= 0 and lo < 0 else lo

    n = max(len(labels), 1)
    def x_at(i):
        if n == 1:
            return PAD_L + (W - PAD_L - PAD_R) / 2
        return PAD_L + i * (W - PAD_L - PAD_R) / (n - 1)

    def y_at(v):
        return H - PAD_B - (v - lo) / (hi - lo) * (H - PAD_T - PAD_B)

    parts = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" '
             f'aria-label="{html.escape(title)}">']

    # сетка и подписи оси Y
    for t in _axis_ticks(lo, hi):
        if t < lo or t > hi:
            continue
        y = y_at(t)
        parts.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                     f'class="grid"/>')
        parts.append(f'<text x="{PAD_L - 8}" y="{y + 4:.1f}" class="tick ty">'
                     f'{_nice_num(t)}</text>')

    # подписи оси X (прореживаем, чтобы не наслаивались)
    step = max(1, n // 8)
    for i, lab in enumerate(labels):
        if i % step and i != n - 1:
            continue
        parts.append(f'<text x="{x_at(i):.1f}" y="{H - PAD_B + 18}" class="tick tx">'
                     f'{html.escape(str(lab))}</text>')

    # линии и точки
    for name, values, color in series:
        segment = []
        for i, v in enumerate(values):
            if v is None:
                if len(segment) > 1:
                    parts.append(f'<polyline points="{" ".join(segment)}" '
                                 f'fill="none" stroke="{color}" stroke-width="2.5" '
                                 f'stroke-linejoin="round"/>')
                segment = []
                continue
            segment.append(f"{x_at(i):.1f},{y_at(v):.1f}")
        if len(segment) > 1:
            parts.append(f'<polyline points="{" ".join(segment)}" fill="none" '
                         f'stroke="{color}" stroke-width="2.5" stroke-linejoin="round"/>')
        for i, v in enumerate(values):
            if v is None:
                continue
            parts.append(
                f'<circle cx="{x_at(i):.1f}" cy="{y_at(v):.1f}" r="3.5" fill="{color}">'
                f'<title>{html.escape(name)} — {html.escape(str(labels[i]))}: '
                f'{_nice_num(v)} {html.escape(unit)}</title></circle>'
            )

    parts.append("</svg>")

    legend = "".join(
        f'<span class="lg"><i style="background:{c}"></i>{html.escape(nm)}</span>'
        for nm, _, c in series
    )
    return (f'<div class="card"><h3>{html.escape(title)}</h3>'
            f'<div class="legend">{legend}</div>{"".join(parts)}</div>')


def _bar_chart(title, labels, values, color="#4f7cff", unit=""):
    vals = [v or 0 for v in values]
    if not vals:
        return f'<div class="card"><h3>{html.escape(title)}</h3>' \
               f'<p class="empty">Недостаточно данных.</p></div>'

    hi = max(vals + [1])
    n = len(vals)
    slot = (W - PAD_L - PAD_R) / max(n, 1)
    bw = min(slot * 0.6, 44)

    parts = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" '
             f'aria-label="{html.escape(title)}">']
    for t in _axis_ticks(0, hi):
        y = H - PAD_B - (t / hi) * (H - PAD_T - PAD_B)
        parts.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{PAD_L - 8}" y="{y + 4:.1f}" class="tick ty">{_nice_num(t)}</text>')

    step = max(1, n // 8)
    for i, (lab, v) in enumerate(zip(labels, vals)):
        cx = PAD_L + slot * i + slot / 2
        h = (v / hi) * (H - PAD_T - PAD_B)
        parts.append(
            f'<rect x="{cx - bw / 2:.1f}" y="{H - PAD_B - h:.1f}" width="{bw:.1f}" '
            f'height="{h:.1f}" fill="{color}" rx="3">'
            f'<title>{html.escape(str(lab))}: {_nice_num(v)} {html.escape(unit)}</title></rect>'
        )
        if i % step == 0 or i == n - 1:
            parts.append(f'<text x="{cx:.1f}" y="{H - PAD_B + 18}" class="tick tx">'
                         f'{html.escape(str(lab))}</text>')
    parts.append("</svg>")
    return f'<div class="card"><h3>{html.escape(title)}</h3>{"".join(parts)}</div>'


def _kpi(label, value, delta="", warn=False):
    cls = "kpi warn" if warn else "kpi"
    return (f'<div class="{cls}"><span class="kl">{html.escape(label)}</span>'
            f'<span class="kv">{html.escape(str(value))}</span>'
            f'<span class="kd">{html.escape(delta)}</span></div>')


CSS = """
*{box-sizing:border-box}
body{margin:0;padding:28px;background:#0f1115;color:#e6e8ee;
 font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
h1{font-size:22px;margin:0 0 4px}
h3{font-size:15px;margin:0 0 12px;font-weight:600}
.sub{color:#8b93a7;margin:0 0 24px;font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:24px}
.kpi{background:#181b22;border:1px solid #242835;border-radius:10px;padding:14px 16px;
 display:flex;flex-direction:column;gap:3px}
.kpi.warn{border-color:#7a4a1e;background:#1e1a15}
.kl{color:#8b93a7;font-size:12px}
.kv{font-size:21px;font-weight:650;letter-spacing:-.3px}

.kd{font-size:12px;color:#7b869c}
.card{background:#181b22;border:1px solid #242835;border-radius:12px;padding:18px;margin-bottom:18px}
.chart{width:100%;height:auto;display:block}
.grid{stroke:#262b37;stroke-width:1}
.tick{fill:#7b869c;font-size:11px}
.ty{text-anchor:end}
.tx{text-anchor:middle}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:6px}
.lg{display:flex;align-items:center;gap:6px;font-size:12px;color:#9aa3b6}
.lg i{width:11px;height:11px;border-radius:3px;display:inline-block}
.empty{color:#7b869c;font-size:13px;margin:0}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid #242835;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:#8b93a7;font-weight:600;position:sticky;top:0;background:#181b22}
.scroll{overflow-x:auto;max-height:420px;overflow-y:auto}
.note{color:#7b869c;font-size:12px;margin-top:18px;line-height:1.7}
.bad{color:#ff8f6b}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.chip{font-size:12px;padding:4px 10px;border-radius:20px;background:#22262f;color:#c9d1e1}
.chip.critical{background:#3a1d1d;color:#ff8f6b}
.chip.warning{background:#3a2e17;color:#ffc470}
.chip.info{background:#1c2a3a;color:#8ab4f8}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:7px}
.dot.critical{background:#ff6b6b}.dot.warning{background:#ffb547}.dot.info{background:#5b9bd5}
td.msg{text-align:left;white-space:normal;line-height:1.5}
tr.an-critical td{background:#1e1516}
code{background:#22262f;padding:1px 5px;border-radius:4px;font-size:12px}
"""


def _anomaly_section(anomaly_summary):
    """Блок с найденными аномалиями, отсортированными по критичности."""
    if not anomaly_summary or not anomaly_summary.get("total"):
        return ('<div class="card"><h3>Аномалии</h3>'
                '<p class="empty">Аномалий не обнаружено — данные выглядят '
                'согласованно с историей.</p></div>')

    counts = anomaly_summary["counts"]
    chips = "".join(
        f'<span class="chip {sev}">{label}: {counts.get(sev, 0)}</span>'
        for sev, label in [("critical", "Критично"), ("warning", "Предупреждения"),
                           ("info", "Информация")]
        if counts.get(sev)
    )

    rows = []
    for a in anomaly_summary["items"][:300]:
        sev = a.get("severity", "info")
        rows.append(
            f'<tr class="an-{sev}"><td><span class="dot {sev}"></span>'
            f'{html.escape(str(a.get("source", "")))}</td>'
            f'<td>{html.escape(str(a.get("kind", "")))}</td>'
            f'<td class="msg">{html.escape(str(a.get("message", "")))}</td></tr>'
        )
    more = ""
    if anomaly_summary["total"] > 300:
        more = (f'<p class="note">Показаны первые 300 из '
                f'{anomaly_summary["total"]}. Полный список — в таблице '
                f'<code>anomalies</code> в history/run_stats.db.</p>')

    return (f'<div class="card"><h3>Аномалии</h3><div class="chips">{chips}</div>'
            f'<div class="scroll"><table><thead><tr><th>Источник</th><th>Тип</th>'
            f'<th>Описание</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            f'{more}</div>')


def build_dashboard(history, out_path, files_written=None, anomaly_summary=None,
                    anomaly_history=None):
    """Собирает HTML-дашборд из истории запусков."""
    if not history:
        return None

    labels = []
    for r in history:
        raw = r.get("started_at") or ""
        try:
            labels.append(datetime.fromisoformat(raw).strftime("%d.%m %H:%M"))
        except (ValueError, TypeError):
            labels.append(str(r.get("run_id", "?")))

    def col(field):
        return [r.get(field) for r in history]

    last = history[-1]

    from redcat.data.run_stats import format_delta  # локальный импорт, чтобы избежать цикла

    kpis = "".join([
        _kpi("Квартир собрано", _nice_num(last.get("apartments_count")),
             format_delta(history, "apartments_count")),
        _kpi("ЖК в справочнике", _nice_num(last.get("hc_count")),
             format_delta(history, "hc_count")),
        _kpi("Медианная цена", _nice_num(last.get("price_median")) + " ₽",
             format_delta(history, "price_median")),
        _kpi("Медиана за м²", _nice_num(last.get("price_per_sqm_median")) + " ₽",
             format_delta(history, "price_per_sqm_median")),
        _kpi("Изменений с прошлого раза", _nice_num(last.get("changes_total")), ""),
        _kpi("Аномалий найдено",
             str((anomaly_summary or {}).get("total", 0)),
             f'критичных: {(anomaly_summary or {}).get("counts", {}).get("critical", 0)}',
             warn=bool((anomaly_summary or {}).get("counts", {}).get("critical"))),
        _kpi("Полнота сбора",
             f'{last.get("coverage_pct")}%' if last.get("coverage_pct") is not None else "—",
             "", warn=bool(last.get("incomplete"))),
    ])

    charts = [
        _line_chart("Динамика объёма предложения", labels, [
            ("Квартир", col("apartments_count"), "#4f7cff"),
            ("Заявлено API по региону", col("region_total_reported"), "#6f7b91"),
        ], "шт."),
        _line_chart("Динамика цен", labels, [
            ("Медианная цена, ₽", col("price_median"), "#28c076"),
            ("Средняя цена, ₽", col("price_avg"), "#9b8cff"),
        ], "₽"),
        _line_chart("Цена за квадратный метр", labels, [
            ("Медиана, ₽/м²", col("price_per_sqm_median"), "#ffb547"),
            ("Среднее, ₽/м²", col("price_per_sqm_avg"), "#e0733d"),
        ], "₽/м²"),
        _bar_chart("Новые лоты за запуск", labels, col("changes_new"), "#28c076", "шт."),
        _bar_chart("Ушедшие лоты за запуск", labels, col("changes_gone"), "#ff6b6b", "шт."),
        _line_chart("Инфраструктура рынка", labels, [
            ("ЖК", col("hc_count"), "#4f7cff"),
            ("Застройщиков", col("developers_count"), "#28c076"),
            ("ЖК без договоров", col("hc_without_contracts"), "#ff8f6b"),
        ], "шт."),
        _line_chart("Длительность сбора", labels,
                    [("Секунд", col("duration_sec"), "#6f7b91")], "сек"),
    ]

    if anomaly_history:
        crit = [anomaly_history.get(r.get("run_id"), {}).get("critical", 0) for r in history]
        warn = [anomaly_history.get(r.get("run_id"), {}).get("warning", 0) for r in history]
        charts.append(_line_chart("Аномалии по запускам", labels, [
            ("Критичные", crit, "#ff6b6b"),
            ("Предупреждения", warn, "#ffb547"),
        ], "шт."))

    anomaly_block = _anomaly_section(anomaly_summary)

    # таблица истории
    head_map = [
        ("run_id", "#"), ("started_at", "Запуск"), ("apartments_count", "Квартир"),
        ("hc_count", "ЖК"), ("price_median", "Медиана ₽"),
        ("price_per_sqm_median", "₽/м² медиана"), ("changes_new", "Новых"),
        ("changes_gone", "Ушло"), ("changes_modified", "Изменено"),
        ("coverage_pct", "Полнота %"), ("duration_sec", "Сек"),
    ]
    thead = "".join(f"<th>{html.escape(t)}</th>" for _, t in head_map)
    body_rows = []
    for r in reversed(history):
        tds = []
        for key, _ in head_map:
            v = r.get(key)
            if key == "started_at" and v:
                try:
                    v = datetime.fromisoformat(v).strftime("%d.%m.%Y %H:%M")
                except (ValueError, TypeError):
                    pass
            elif isinstance(v, float):
                v = _nice_num(v)
            tds.append(f"<td>{html.escape(str(v if v is not None else '—'))}</td>")
        cls = ' class="bad"' if r.get("incomplete") else ""
        body_rows.append(f"<tr{cls}>{''.join(tds)}</tr>")

    files_note = ""
    if files_written:
        items = "".join(f"<li>{html.escape(str(p))}</li>" for p in files_written)
        files_note = f"<p class='note'>Полные данные этого запуска:</p><ul class='note'>{items}</ul>"

    generated = datetime.now().strftime("%d.%m.%Y %H:%M")
    doc = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RedCat — динамика рынка</title><style>{CSS}</style></head><body>
<h1>RedCat — динамика рынка</h1>
<p class="sub">Отчёт сформирован {generated}. Запусков в истории: {len(history)}.
Все показатели рассчитаны из фактически собранных данных.</p>
<div class="kpis">{kpis}</div>
{anomaly_block}
{"".join(charts)}
<div class="card"><h3>История запусков</h3>
<div class="scroll"><table><thead><tr>{thead}</tr></thead>
<tbody>{"".join(body_rows)}</tbody></table></div></div>
{files_note}
<p class="note">Красным отмечены запуски, где часть данных не догрузилась —
их показатели занижены и не годятся для сравнения динамики.</p>
</body></html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path
