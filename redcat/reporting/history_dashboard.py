"""
Дашборд истории по источникам Redcat.
=====================================
Самодостаточный HTML-файл с динамикой метрик по каждому источнику
(квартиры, ЖК, регламенты, тарифы, внешние источники).

Что показывает:
  • сводку по всем запускам: сколько их, период, сколько неполных;
  • KPI последнего запуска: объём, полнота, длительность, изменения;
  • общий график объёма данных по источникам (multi-line);
  • по каждому источнику — свёрнутый блок:
      – последнее значение объёма и его дельта к прошлому запуску;
      – графики динамики каждой числовой метрики
        (число записей, цены, площади, уникальные значения групп);
      – таблица «все запуски» со значениями метрик этого источника.

Читает только history/run_stats.db. Ничего не пишет, в сеть не ходит.
CSS и JS инлайн, графики — inline-SVG.

Использование:
    python -m redcat.reporting.history_dashboard
    python -m redcat.reporting.history_dashboard --source apartments
    python -m redcat.reporting.history_dashboard --html reports/history.html --no-open
    python -m redcat.reporting.history_dashboard --db history/run_stats.db
"""
from __future__ import annotations

from redcat.core import paths
import argparse
import html
import sqlite3
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

BASE = paths.ROOT
sys.path.insert(0, str(BASE))


# ──────────────────────────────────────────────────────────────
#  НАСТРОЙКИ
# ──────────────────────────────────────────────────────────────
DEFAULT_DB = BASE / "history" / "run_stats.db"
DEFAULT_OUT = BASE / "reports" / "history.html"

# Общие метрики — не относятся к конкретному источнику
GLOBAL_METRICS = (
    "coverage_pct", "region_total_reported", "sources_count",
    "changes_total", "changes_new", "changes_gone", "changes_modified",
    "duration_sec", "hc_without_contracts",
)

# Специальные метрики, которые не имеют префикса источника,
# но относятся к apartments (обратная совместимость с run_stats.py)
APARTMENTS_UNPREFIXED = (
    "price_avg", "price_median", "price_min", "price_max",
    "price_per_sqm_avg", "price_per_sqm_median",
    "apartments_with_price", "developers_count",
)

# Спецслучай: ключ `hc_count` в runs соответствует housing_complexes
COUNT_ALIASES = {"hc_count": "housing_complexes"}


# ──────────────────────────────────────────────────────────────
#  ЧТЕНИЕ ИСТОРИИ
# ──────────────────────────────────────────────────────────────
def load_runs(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM runs ORDER BY run_id").fetchall()
    return [dict(r) for r in rows]


def load_anomaly_counts(db_path: Path) -> dict:
    """{run_id: {"critical": N, "warning": M, "info": K}}."""
    if not db_path.exists():
        return {}
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            rows = conn.execute("""
                SELECT run_id,
                       SUM(severity='critical') AS critical,
                       SUM(severity='warning')  AS warning,
                       SUM(severity='info')     AS info
                FROM anomalies GROUP BY run_id
            """).fetchall()
    except sqlite3.Error:
        return {}
    return {r[0]: {"critical": r[1] or 0, "warning": r[2] or 0, "info": r[3] or 0}
            for r in rows}


# ──────────────────────────────────────────────────────────────
#  ОПРЕДЕЛЕНИЕ ИСТОЧНИКОВ И ИХ МЕТРИК
# ──────────────────────────────────────────────────────────────
def discover_sources(runs: list[dict], specs: dict) -> dict:
    """{source_key: {"count_col": str, "metrics": [col, ...]}}.

    Источник определяется по колонке `<key>_count` в runs. Ключ `hc_count`
    переименовывается в `housing_complexes`. Для `apartments` дополнительно
    подхватываются метрики без префикса (price_avg и т.п.).
    """
    if not runs:
        return {}
    all_cols = set(runs[-1].keys())

    # 1. собираем ключи источников по `*_count`
    source_keys: dict[str, str] = {}   # {source_key: count_col}
    for col in all_cols:
        if not col.endswith("_count"):
            continue
        key = col[:-len("_count")]
        if key in ("sources",):        # не источник — общая метрика
            continue
        if col in COUNT_ALIASES:
            key = COUNT_ALIASES[col]
        source_keys[key] = col

    # 2. для каждого источника — его метрики
    out: dict[str, dict] = {}
    for key, count_col in source_keys.items():
        prefix = key + "_"
        metrics = []
        for col in all_cols:
            if col == count_col:
                continue
            if col in GLOBAL_METRICS:
                continue
            if col.startswith(prefix):
                metrics.append(col)
        # apartments — спец-случай: часть метрик без префикса
        if key == "apartments":
            for col in APARTMENTS_UNPREFIXED:
                if col in all_cols and col not in metrics:
                    metrics.append(col)
        # сортируем: сначала count-подобные, потом по алфавиту
        metrics.sort()
        out[key] = {"count_col": count_col, "metrics": metrics}
    return out


def is_numeric_col(runs: list[dict], col: str) -> bool:
    for r in runs:
        v = r.get(col)
        if v is None:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return True
        return False
    return False


# ──────────────────────────────────────────────────────────────
#  ПОДПИСИ МЕТРИК
# ──────────────────────────────────────────────────────────────
_SUFFIX_LABELS = {
    "avg": "среднее",
    "median": "медиана",
    "min": "минимум",
    "max": "максимум",
    "unique": "уникальных",
}

_SPECIAL_LABELS = {
    "price_avg": "Цена — среднее",
    "price_median": "Цена — медиана",
    "price_min": "Цена — минимум",
    "price_max": "Цена — максимум",
    "price_per_sqm_avg": "Цена за м² — среднее",
    "price_per_sqm_median": "Цена за м² — медиана",
    "apartments_with_price": "Записей с ценой",
    "developers_count": "Застройщиков",
    "region_total_reported": "Заявлено API",
    "coverage_pct": "Полнота сбора, %",
    "duration_sec": "Длительность сбора, сек",
    "sources_count": "Источников собрано",
    "changes_total": "Изменений всего",
    "changes_new": "Новых записей",
    "changes_gone": "Пропавших записей",
    "changes_modified": "Изменённых полей",
    "hc_without_contracts": "ЖК без договоров",
}


def _field_label(field: str) -> str:
    try:
        from redcat.data import field_labels
        return field_labels.label_for(field)
    except Exception:
        return field


def metric_label(col: str, source_key: str = "") -> str:
    if col in _SPECIAL_LABELS:
        return _SPECIAL_LABELS[col]
    base = col
    if source_key and col.startswith(source_key + "_"):
        base = col[len(source_key) + 1:]
    if base in _SPECIAL_LABELS:
        return _SPECIAL_LABELS[base]
    if base == "count":
        return "Количество записей"
    for sfx, label in _SUFFIX_LABELS.items():
        if base.endswith("_" + sfx):
            stem = base[:-len(sfx) - 1]
            return f"{_field_label(stem)} — {label}"
    return _field_label(base)


def group_metrics(metric_cols: list) -> list[dict]:
    """Группирует метрики в осмысленные графики.

    - «X_avg» + «X_median» → один график «X — среднее и медиана»
    - «X_min» + «X_max»    → один график «X — минимум и максимум»
    - остальные метрики (unique, count и т.п.) — по одному графику.
    """
    bases: dict[str, dict] = {}
    singles: list[str] = []
    for col in metric_cols:
        hit = None
        for sfx in ("_avg", "_median", "_min", "_max"):
            if col.endswith(sfx):
                hit = (col[:-len(sfx)], sfx)
                break
        if hit:
            base, sfx = hit
            bases.setdefault(base, {})[sfx] = col
        else:
            singles.append(col)

    groups: list[dict] = []
    for base in sorted(bases):
        variants = bases[base]
        avg_cols = [c for c in (variants.get("_avg"), variants.get("_median")) if c]
        mm_cols = [c for c in (variants.get("_min"), variants.get("_max")) if c]
        if avg_cols:
            groups.append({"title": f"{_field_label(base)} — среднее и медиана",
                           "columns": avg_cols})
        if mm_cols:
            groups.append({"title": f"{_field_label(base)} — минимум и максимум",
                           "columns": mm_cols})
    for col in singles:
        groups.append({"title": metric_label(col), "columns": [col]})
    return groups


# ──────────────────────────────────────────────────────────────
#  SVG-ГРАФИК
# ──────────────────────────────────────────────────────────────
_PALETTE = ("#4f8cff", "#28c076", "#f0b429", "#b98cff", "#ff7a45")


def line_svg(labels: list[str], series: list[tuple],
             *, width=780, height=220, unit="") -> str:
    """series: [(name, values, color?), ...]."""
    flat = [v for _, values, *_ in series for v in values if v is not None]
    if not flat:
        return '<div class="empty">нет данных для графика</div>'

    P = {"l": 66, "r": 18, "t": 12, "b": 30}
    lo, hi = min(flat), max(flat)
    if lo == hi:
        lo, hi = lo * 0.95 if lo else 0, hi * 1.05 if hi else 1
    else:
        pad = (hi - lo) * 0.08
        lo -= pad; hi += pad

    n = max(len(labels), 1)
    def x_at(i):
        if n == 1:
            return P["l"] + (width - P["l"] - P["r"]) / 2
        return P["l"] + i * (width - P["l"] - P["r"]) / (n - 1)

    def y_at(v):
        return height - P["b"] - (v - lo) / (hi - lo) * (height - P["t"] - P["b"])

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart">']

    # 5 горизонтальных уровней
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        yy = y_at(v)
        parts.append(f'<line x1="{P["l"]}" y1="{yy:.1f}" x2="{width - P["r"]}" '
                     f'y2="{yy:.1f}" stroke="#262b37"/>')
        parts.append(f'<text x="{P["l"] - 6}" y="{yy + 4:.1f}" fill="#7b869c" '
                     f'font-size="10" text-anchor="end">{_nice(v)}</text>')

    # подписи оси X, прореживаем
    step = max(1, n // 8)
    for i, lab in enumerate(labels):
        if i % step and i != n - 1:
            continue
        parts.append(f'<text x="{x_at(i):.1f}" y="{height - 8}" fill="#7b869c" '
                     f'font-size="10" text-anchor="middle">{html.escape(str(lab))}</text>')

    # линии
    for si, (name, values, *rest) in enumerate(series):
        color = rest[0] if rest else _PALETTE[si % len(_PALETTE)]
        segment = []
        for i, v in enumerate(values):
            if v is None:
                if len(segment) > 1:
                    parts.append(f'<polyline points="{" ".join(segment)}" '
                                 f'fill="none" stroke="{color}" stroke-width="2" '
                                 f'stroke-linejoin="round"/>')
                segment = []
                continue
            segment.append(f"{x_at(i):.1f},{y_at(v):.1f}")
        if len(segment) > 1:
            parts.append(f'<polyline points="{" ".join(segment)}" fill="none" '
                         f'stroke="{color}" stroke-width="2" stroke-linejoin="round"/>')
        for i, v in enumerate(values):
            if v is None:
                continue
            parts.append(
                f'<circle cx="{x_at(i):.1f}" cy="{y_at(v):.1f}" r="3" fill="{color}">'
                f'<title>{html.escape(str(name))} — {html.escape(str(labels[i]))}: '
                f'{_nice(v)}{html.escape(unit)}</title></circle>')

    parts.append("</svg>")

    legend = "".join(
        f'<span class="lg"><i style="background:'
        f'{series[i][2] if len(series[i]) > 2 else _PALETTE[i % len(_PALETTE)]}">'
        f'</i>{html.escape(str(series[i][0]))}</span>'
        for i in range(len(series))
    )
    return (f'<div class="legend">{legend}</div>{"".join(parts)}'
            if len(series) > 1 else "".join(parts))


def _nice(v):
    if v is None:
        return "—"
    av = abs(v)
    if av >= 1_000_000_000:
        return f"{v / 1_000_000_000:.2f} млрд"
    if av >= 1_000_000:
        return f"{v / 1_000_000:.1f} млн"
    if av >= 10_000:
        return f"{v / 1_000:.1f} тыс."
    if av >= 100:
        return f"{v:,.0f}".replace(",", "\u202f")
    if isinstance(v, float) and not v.is_integer():
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{int(v)}" if isinstance(v, (int, float)) else str(v)


def _fmt(v, digits=2):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, int):
        return f"{v:,}".replace(",", "\u202f")
    if isinstance(v, float):
        if abs(v) >= 1000:
            return f"{v:,.2f}".replace(",", "\u202f").rstrip("0").rstrip(".")
        return f"{v:.{digits}f}".rstrip("0").rstrip(".")
    return str(v)


def _esc(s):
    return html.escape(str(s if s is not None else ""))


# ──────────────────────────────────────────────────────────────
#  CSS
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
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px; margin-bottom: 18px;
}
.kpi {
  background: #181b22; border: 1px solid #242835; border-radius: 10px;
  padding: 12px 14px;
}
.kpi .kl { color: #8b93a7; font-size: 11px; text-transform: uppercase; letter-spacing: .4px; }
.kpi .kv { font-size: 20px; font-weight: 650; margin-top: 3px; }
.kpi .kd { font-size: 11px; color: #7b869c; margin-top: 2px; }
.kpi.warn { border-color: #7a4a1e; background: #1e1a15; }
.kpi.bad  { border-color: #7a1e1e; background: #1f1414; }
.kpi.ok   { border-color: #1e5c37; background: #141d19; }
.up   { color: #3ecf8e; } .down { color: #ff6b6b; } .flat { color: #7b869c; }

table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th, td {
  padding: 6px 10px; text-align: right; border-bottom: 1px solid #242835;
  white-space: nowrap;
}
th:first-child, td:first-child { text-align: left; }
th {
  color: #8b93a7; font-weight: 600; position: sticky; top: 0;
  background: #181b22; z-index: 2;
}
.scroll { overflow-x: auto; max-height: 420px; overflow-y: auto; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 20px;
        font-size: 11px; border: 1px solid #2c384e; }
.pill.ok { background: rgba(62,207,142,.15); color: #3ecf8e; border-color: rgba(62,207,142,.4); }
.pill.warn { background: rgba(240,180,41,.15); color: #f0b429; border-color: rgba(240,180,41,.4); }
.pill.crit { background: rgba(255,95,109,.15); color: #ff5f6d; border-color: rgba(255,95,109,.4); }
.pill.info { background: #24375c; color: #9dc0ff; }
.pill.gray { background: #202530; color: #8792a1; }
.muted { color: #8b93a7; font-size: 12px; }
.num { font-variant-numeric: tabular-nums; }
.empty { padding: 20px; text-align: center; color: #8b93a7; }
.chart { width: 100%; height: auto; display: block; margin-top: 6px; }
.legend { display: flex; gap: 14px; flex-wrap: wrap; margin: 4px 0 6px; }
.legend .lg { display: inline-flex; align-items: center; gap: 6px;
              font-size: 12px; color: #9aa3b6; }
.legend .lg i { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }

.controls { display: flex; gap: 8px; margin: 0 0 16px 0;
            align-items: center; flex-wrap: wrap; }
.controls button {
  background: #181b22; color: #e6e8ee; border: 1px solid #2c384e;
  padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 12.5px;
  font-family: inherit;
}
.controls button:hover { background: #202530; border-color: #3a4a6a; }
.controls .hint { color: #8b93a7; font-size: 12px; }

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
.src-head .ttl { font-weight: 650; }
.src-head .tag {
  font-size: 12px; color: #8792a1;
  max-width: 34%; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap;
}
.src-head .pills { display: flex; gap: 5px; flex-wrap: wrap; }
.src-head .spacer { flex: 1; }
details.src-block > .src-body { padding: 14px 18px 4px; }

.metric-card {
  background: #161a21; border: 1px solid #242835; border-radius: 8px;
  padding: 12px 14px; margin-bottom: 12px;
}
.metric-card h4 {
  margin: 0 0 8px; font-size: 12.5px; font-weight: 600; color: #c9d1e1;
}
.metric-card .stats {
  font-size: 11.5px; color: #8b93a7; margin-bottom: 6px;
  display: flex; gap: 14px; flex-wrap: wrap;
}
.metric-card .stats b { color: #e6e8ee; font-weight: 600; }
"""


# ──────────────────────────────────────────────────────────────
#  РЕНДЕР БЛОКОВ
# ──────────────────────────────────────────────────────────────
def _delta(cur, prev):
    if cur is None or prev is None:
        return None
    if prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100


def _kpi(label, value, delta=None, unit=""):
    """KPI-плитка. delta — уже в процентах, к прошлому запуску."""
    kd = ""
    cls = ""
    if delta is not None:
        if abs(delta) < 0.01:
            kd = "без изменений"; cls = "flat"
        else:
            sign = "+" if delta > 0 else "−"
            kd = f"{sign}{abs(delta):.1f}% к прошлому"
            cls = "up" if delta > 0 else "down"
    return (f'<div class="kpi">'
            f'  <div class="kl">{_esc(label)}</div>'
            f'  <div class="kv">{_esc(value)}{_esc(unit)}</div>'
            f'  <div class="kd {cls}">{_esc(kd)}</div>'
            f'</div>')


def _header_kpis(runs: list[dict], anomaly_counts: dict) -> str:
    if not runs:
        return ""
    last = runs[-1]
    prev = runs[-2] if len(runs) > 1 else {}
    incomplete_count = sum(1 for r in runs if r.get("incomplete"))

    kpi_items = [
        _kpi("Запусков", len(runs),
             delta=_delta(len(runs), len(runs) - 1) if False else None),
        _kpi("Неполных", incomplete_count,
             delta=None),
        _kpi("Полнота последнего, %",
             _fmt(last.get("coverage_pct")),
             delta=_delta(last.get("coverage_pct"), prev.get("coverage_pct"))),
        _kpi("Длительность, сек",
             _fmt(last.get("duration_sec")),
             delta=_delta(last.get("duration_sec"), prev.get("duration_sec"))),
        _kpi("Изменений за запуск",
             _fmt(last.get("changes_total")),
             delta=_delta(last.get("changes_total"), prev.get("changes_total"))),
        _kpi("Источников собрано",
             _fmt(last.get("sources_count")),
             delta=_delta(last.get("sources_count"), prev.get("sources_count"))),
    ]
    an_last = anomaly_counts.get(last.get("run_id") or 0) or {}
    an_all = sum((v.get("critical", 0) + v.get("warning", 0))
                 for v in anomaly_counts.values())
    kpi_items.append(
        _kpi("Аномалий (всего)", an_all,
             delta=None))
    return f'<div class="kpis">{"".join(kpi_items)}</div>'


def _runs_labels(runs: list[dict]) -> list[str]:
    labels = []
    for r in runs:
        raw = r.get("started_at") or ""
        try:
            labels.append(datetime.fromisoformat(raw).strftime("%d.%m %H:%M"))
        except (ValueError, TypeError):
            labels.append(f"#{r.get('run_id', '?')}")
    return labels


def _overview_chart(runs: list[dict], sources: dict) -> str:
    """Multi-line: объём каждой таблицы по запускам."""
    if not runs:
        return ""
    labels = _runs_labels(runs)
    series = []
    for i, (key, info) in enumerate(sorted(sources.items())):
        col = info["count_col"]
        values = [r.get(col) for r in runs]
        if not any(v is not None for v in values):
            continue
        series.append((key, values, _PALETTE[i % len(_PALETTE)]))
    if not series:
        return ""
    svg = line_svg(labels, series, unit=" шт.")
    return f'''
    <div class="card">
      <h3>Объём по источникам</h3>
      <div class="muted" style="margin-bottom:8px">
        Количество записей в каждом источнике по запускам.
      </div>
      {svg}
    </div>
    '''


def _global_metrics_block(runs: list[dict], anomaly_counts: dict) -> str:
    if not runs:
        return ""
    labels = _runs_labels(runs)
    cards = []

    # Полнота сбора
    series = [("coverage_pct", [r.get("coverage_pct") for r in runs], "#4f8cff")]
    if any(v is not None for _, v, _ in series):
        cards.append(_metric_card(
            "Полнота сбора, %", series, labels,
            note="Собрано от заявленного API. Ниже 80% — данные занижены.",
        ))

    # Заявлено API
    series = [("region_total_reported",
               [r.get("region_total_reported") for r in runs], "#b98cff")]
    if any(v is not None for _, v, _ in series):
        cards.append(_metric_card(
            "Заявлено API всего", series, labels,
            note="Оценка объёма источника со стороны API.",
        ))

    # Изменения
    series = [
        ("новые",     [r.get("changes_new") for r in runs], "#28c076"),
        ("пропали",   [r.get("changes_gone") for r in runs], "#ff6b6b"),
        ("изменения", [r.get("changes_modified") for r in runs], "#f0b429"),
    ]
    if any(any(v is not None for v in s[1]) for s in series):
        cards.append(_metric_card("Оборот записей", series, labels,
                                  note="Появившиеся, пропавшие и изменившиеся записи."))

    # Длительность
    series = [("секунд", [r.get("duration_sec") for r in runs], "#8792a1")]
    if any(v is not None for _, v, _ in series):
        cards.append(_metric_card("Длительность сбора", series, labels, unit=" сек"))

    # Аномалии
    an_crit = [anomaly_counts.get(r.get("run_id") or 0, {}).get("critical", 0)
               for r in runs]
    an_warn = [anomaly_counts.get(r.get("run_id") or 0, {}).get("warning", 0)
               for r in runs]
    if any(an_crit) or any(an_warn):
        series = [("критичные", an_crit, "#ff6b6b"),
                  ("предупреждения", an_warn, "#f0b429")]
        cards.append(_metric_card("Аномалии по запускам", series, labels))

    if not cards:
        return ""
    return f'''
    <div class="card">
      <h3>Общие метрики</h3>
      <div class="muted" style="margin-bottom:12px">
        Не привязаны к конкретному источнику — считаются по всему запуску.
      </div>
      {"".join(cards)}
    </div>
    '''


def _metric_card(title: str, series: list, labels: list,
                 note: str = "", unit: str = "") -> str:
    """Один график с подписью."""
    svg = line_svg(labels, series, unit=unit)
    stats = []
    # краткая сводка по последнему значению
    for name, values, *_ in series:
        last_v = next((v for v in reversed(values) if v is not None), None)
        prev_v = next((v for v in reversed(values[:-1]) if v is not None), None)
        d = _delta(last_v, prev_v) if last_v is not None and prev_v is not None else None
        tail = f' <span class="muted">({d:+.1f}%)</span>' if d is not None else ""
        stats.append(f'{_esc(name)}: <b>{_fmt(last_v)}</b>{tail}')
    note_html = f'<div class="muted" style="margin-bottom:6px">{_esc(note)}</div>' if note else ""
    return (f'<div class="metric-card">'
            f'  <h4>{_esc(title)}</h4>'
            f'  <div class="stats">{" · ".join(stats)}</div>'
            f'  {note_html}{svg}'
            f'</div>')


def _source_summary_pills(runs: list[dict], info: dict) -> str:
    last = runs[-1]
    prev = runs[-2] if len(runs) > 1 else {}
    cur = last.get(info["count_col"])
    prv = prev.get(info["count_col"]) if prev else None
    d = _delta(cur, prv) if cur is not None and prv is not None else None
    pills = [f'<span class="pill info">{_fmt(cur)}</span>']
    if d is not None:
        cls = "ok" if abs(d) < 0.5 else "warn" if abs(d) < 5 else "crit"
        sign = "+" if d > 0 else "−"
        pills.append(f'<span class="pill {cls}">{sign}{abs(d):.1f}%</span>')
    last_incomplete = bool(last.get("incomplete"))
    if last_incomplete:
        pills.append('<span class="pill crit">неполный сбор</span>')
    return " ".join(pills)


def _source_body(runs: list[dict], key: str, info: dict,
                 specs: dict) -> str:
    labels = _runs_labels(runs)
    count_col = info["count_col"]

    # 1. График count
    cards = []
    values = [r.get(count_col) for r in runs]
    if any(v is not None for v in values):
        cards.append(_metric_card(
            "Количество записей", [("записей", values, "#4f8cff")],
            labels,
            note="Объём источника в каждом запуске.",
        ))

    # 2. Остальные метрики — группируем в осмысленные пары
    numeric_metrics = [c for c in info["metrics"] if is_numeric_col(runs, c)]
    for grp in group_metrics(numeric_metrics):
        cols = grp["columns"]
        series = []
        for i, col in enumerate(cols):
            vals = [r.get(col) for r in runs]
            if not any(v is not None for v in vals):
                continue
            series.append((metric_label(col, key), vals,
                           _PALETTE[i % len(_PALETTE)]))
        if not series:
            continue
        cards.append(_metric_card(grp["title"], series, labels))

    if not cards:
        return '<div class="empty">метрик по этому источнику не нашлось</div>'

    # 3. Таблица — все метрики по всем запускам
    all_cols = [count_col] + numeric_metrics
    head = ["запуск"] + [metric_label(c, key) for c in all_cols]
    rows = []
    for r in runs:
        cells = [f'<td>{_esc(_runs_labels([r])[0])}</td>']
        for c in all_cols:
            v = r.get(c)
            cells.append(f'<td class="num">{_fmt(v)}</td>')
        cls = ' class="bad"' if r.get("incomplete") else ''
        rows.append(f'<tr{cls}>{"".join(cells)}</tr>')
    table_html = (
        f'<div class="scroll" style="max-height:340px;margin-top:12px">'
        f'  <table><thead><tr>'
        + "".join(f"<th>{_esc(h)}</th>" for h in head)
        + f'  </tr></thead><tbody>{"".join(rows)}</tbody></table>'
        f'</div>'
    )

    return "".join(cards) + f'''
    <div class="metric-card">
      <h4>Все запуски</h4>
      {table_html}
      <div class="muted" style="margin-top:8px">
        Красным отмечены запуски, помеченные как неполные — их значения
        занижены, в сравнении динамики они ненадёжны.
      </div>
    </div>
    '''


def _source_block(runs: list[dict], key: str, info: dict,
                  specs: dict) -> str:
    spec = specs.get(key)
    title = spec.title if spec and spec.title else key
    external = bool(getattr(spec, "external", False)) if spec else False
    pills = _source_summary_pills(runs, info)
    body = _source_body(runs, key, info, specs)
    tag_cls = "info"
    tag_text = "внешний" if external else "Redcat"
    return f'''
    <details class="src-block" id="src-{_esc(key)}">
      <summary class="src-head">
        <span class="ttl">{_esc(key)}</span>
        <span class="tag">{_esc(title)}</span>
        <span class="pill {tag_cls}">{tag_text}</span>
        <span class="spacer"></span>
        <span class="pills">{pills}</span>
      </summary>
      <div class="src-body">{body}</div>
    </details>
    '''


# ──────────────────────────────────────────────────────────────
#  ГЛАВНЫЙ РЕНДЕР
# ──────────────────────────────────────────────────────────────
def render(runs: list[dict], sources: dict, anomaly_counts: dict,
           specs: dict, out_path: Path, db_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not runs:
        out_path.write_text(
            "<html><body><h1>Истории нет</h1>"
            "<p>Файл run_stats.db пуст или отсутствует.</p>"
            "<p>Запустите сбор: <code>python -m redcat.collection.redcat_scraper</code></p>"
            "</body></html>", encoding="utf-8")
        return out_path

    first_ts = (runs[0].get("started_at") or "")[:16].replace("T", " ")
    last_ts = (runs[-1].get("started_at") or "")[:16].replace("T", " ")
    generated = datetime.now().strftime("%d.%m.%Y %H:%M")

    header = f'''
    <h1>История по источникам</h1>
    <p class="sub">
      Файл: <code>{_esc(str(db_path))}</code> · Запусков: {len(runs)} ·
      Период: {_esc(first_ts)} — {_esc(last_ts)} ·
      Сформировано: {generated}
    </p>
    '''

    kpis = _header_kpis(runs, anomaly_counts)

    overview = _overview_chart(runs, sources)

    global_block = _global_metrics_block(runs, anomaly_counts)

    controls = '''
    <div class="controls">
      <button type="button"
        onclick="document.querySelectorAll('details.src-block').forEach(d => d.open = true)">
        Развернуть все
      </button>
      <button type="button"
        onclick="document.querySelectorAll('details.src-block').forEach(d => d.open = false)">
        Свернуть все
      </button>
      <span class="hint">источники свёрнуты по умолчанию</span>
    </div>
    '''

    # Список источников — сортируем по убыванию объёма на последнем запуске
    last = runs[-1]
    ordered_keys = sorted(
        sources.keys(),
        key=lambda k: -(last.get(sources[k]["count_col"]) or 0),
    )
    blocks = "".join(_source_block(runs, k, sources[k], specs)
                     for k in ordered_keys)

    html_doc = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>История по источникам</title>
<style>{_CSS}</style></head><body>
{header}
{kpis}
{overview}
{global_block}
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
</script>
</body></html>"""

    out_path.write_text(html_doc, encoding="utf-8")
    return out_path


# ──────────────────────────────────────────────────────────────
def _load_specs() -> dict:
    try:
        from redcat.web import webapp
        return webapp.load_specs()
    except Exception:
        try:
            from redcat.sources import registry as src
            src.load_from_dir(paths.SOURCES_DIR)
            src.load_from_dir(paths.SOURCES_EXT_DIR)
            return {s.key: s for s in src.all_sources()}
        except Exception:
            return {}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Дашборд истории по источникам Redcat (HTML, самодостаточный).")
    ap.add_argument("--db", default=str(DEFAULT_DB),
                    help=f"путь к run_stats.db (по умолчанию {DEFAULT_DB})")
    ap.add_argument("--html", default=str(DEFAULT_OUT),
                    help=f"куда сохранить HTML (по умолчанию {DEFAULT_OUT})")
    ap.add_argument("--source", default=None,
                    help="показать только один источник (например apartments)")
    ap.add_argument("--no-open", action="store_true",
                    help="не открывать HTML в браузере автоматически")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"❌ База истории не найдена: {db_path}")
        print("   Запустите хотя бы один сбор: python -m redcat.collection.redcat_scraper")
        return 1

    runs = load_runs(db_path)
    if not runs:
        print(f"⚠️  В базе {db_path} нет ни одной строки runs — дашборд будет пустой.")

    specs = _load_specs()
    sources = discover_sources(runs, specs)
    if args.source:
        if args.source not in sources:
            print(f"❌ Источник «{args.source}» не найден в истории.")
            print(f"   Доступные: {', '.join(sorted(sources)) or '—'}")
            return 1
        sources = {args.source: sources[args.source]}

    anomaly_counts = load_anomaly_counts(db_path)

    print(f"📖 История: {db_path}")
    print(f"   Запусков: {len(runs)}")
    if runs:
        first_ts = (runs[0].get("started_at") or "")[:16].replace("T", " ")
        last_ts = (runs[-1].get("started_at") or "")[:16].replace("T", " ")
        print(f"   Период:   {first_ts} — {last_ts}")
    print(f"   Источников: {len(sources)}")
    for k, info in sorted(sources.items()):
        last_v = runs[-1].get(info["count_col"]) if runs else None
        n_metrics = len([c for c in info["metrics"] if is_numeric_col(runs, c)])
        print(f"     • {k:<28} записей {_fmt(last_v):>8}  "
              f"метрик: {n_metrics}")

    out = render(runs, sources, anomaly_counts, specs, Path(args.html), db_path)
    print(f"\n🌐 HTML: {out}")

    if not args.no_open:
        try:
            webbrowser.open(out.resolve().as_uri())
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())