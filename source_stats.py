"""
Расширенная статистика по источникам со сравнением с Redcat.
============================================================
Redcat показан СЛЕВА, проверяемый источник — СПРАВА. Оба источника
равнозначны, знак Δ% — «источник минус Redcat».

Что собирается:
  • сводка по классам (ok/warn/critical/only_left/only_right/insufficient)
  • суммы лотов слева (Redcat) и справа (источник) по сопоставленным ЖК
  • по каждой метрике: квартили |Δ%|, среднее, пороги, гистограмма,
    топ-5 худших ЖК
  • сравнение агрегаций median / mean / sum — видно, системный сдвиг
    или отдельные выбросы
  • сворачивание по застройщикам: одна строка — застройщик, клик
    раскрывает список его ЖК. Застройщик берётся С ОБЕИХ СТОРОН сверки
  • КАЖДЫЙ ИСТОЧНИК СВЁРНУТ: отчёт открывается компактным списком
    «источник + короткая сводка», раскрывается по клику
  • HTML-дашборд с inline-SVG, без CDN и зависимостей

Пороги Δ% задаются константами ниже.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import sqlite3
import statistics
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import webapp


# ──────────────────────────────────────────────────────────────
#  ПОРОГИ КЛАССОВ ПО |Δ%|
# ──────────────────────────────────────────────────────────────
# Сравнивается модуль расхождения между источником и Redcat:
#   |Δ| ≤ DELTA_OK                    → ok, «сходится»
#   DELTA_OK < |Δ| ≤ DELTA_WARN       → warn, «внимание»
#   |Δ| > DELTA_WARN                  → crit, «смотреть руками»
#
# DELTA_CRIT — отдельный, более грубый порог: используется в гистограмме
# (окрашивание «крайних» столбцов) и в подписи «≥N% у скольких ЖК».
DELTA_OK = 2.0
DELTA_WARN = 10.0
DELTA_CRIT = 50.0


# Колонки, в которых может лежать застройщик. Проверяем по порядку.
_DEVELOPER_COLUMN_CANDIDATES = (
    "developer_name", "developer.name", "developer",
    "Застройщик", "застройщик", "developer_title",
)


# ──────────────────────────────────────────────────────────────
#  Транспонирование отчёта: Redcat — слева, источник — справа
# ──────────────────────────────────────────────────────────────
def swap_report(r: dict) -> dict:
    """Меняет стороны отчёта местами: то, что было справа, становится слева.

    Знак Δ% меняется (было «левое минус правое», станет «правое минус левое»),
    чтобы Δ% по-прежнему означал «источник минус Redcat» уже в новой
    ориентации.

    ВАЖНО: ключи внутри item["metrics"] тоже переназываются. В отчёте
    webapp._cross_check_report они записаны именами ЛЕВЫХ (внешних) полей,
    а после swap стороны меняются — теперь слева Redcat, и метрики должны
    называться его именами. Иначе блоки «по метрике», «по застройщикам»
    и «системный сдвиг» не находят значения и показывают «—».
    """
    # {внешнее_поле: redcat_поле} — карта переименования ключей в items
    original_metrics = r.get("metrics") or {}

    def _swap_metric(m: dict) -> dict:
        return {
            "left": m.get("right"), "left_n": m.get("right_n"),
            "right": m.get("left"), "right_n": m.get("left_n"),
            "diff_abs": (-m["diff_abs"] if m.get("diff_abs") is not None else None),
            "diff_pct": (-m["diff_pct"] if m.get("diff_pct") is not None else None),
        }

    def _swap_item(it: dict) -> dict:
        old_m = it.get("metrics") or {}
        new_m = {
            original_metrics.get(k, k): _swap_metric(v)
            for k, v in old_m.items()
        }
        return {
            **it,
            "left_rows": it.get("right_rows", 0),
            "right_rows": it.get("left_rows", 0),
            "metrics": new_m,
        }

    src_items = r.get("items_by_class") or {}
    new_items = {}
    for cls in ("critical", "warn", "ok", "insufficient"):
        new_items[cls] = [_swap_item(it) for it in (src_items.get(cls) or [])]
    new_items["left_only"] = [_swap_item(it)
                              for it in (src_items.get("right_only") or [])]
    new_items["right_only"] = [_swap_item(it)
                               for it in (src_items.get("left_only") or [])]

    s = r.get("summary") or {}
    new_summary = {**s,
                   "left_only": s.get("right_only", 0),
                   "right_only": s.get("left_only", 0)}

    return {
        **r,
        "source": r.get("with_table"),
        "with_table": r.get("source"),
        "on_left": r.get("on_right"),
        "on_right": r.get("on_left"),
        "left_label": r.get("right_label"),
        "right_label": r.get("left_label"),
        "left_external": r.get("right_external"),
        "right_external": r.get("left_external"),
        # верхнеуровневые metrics — тоже переворачиваются; их ключи
        # (Redcat'овские имена полей) должны совпасть с ключами в items
        "metrics": {v: k for k, v in original_metrics.items()},
        "summary": new_summary,
        "items_by_class": new_items,
    }


# ──────────────────────────────────────────────────────────────
#  СВОРАЧИВАНИЕ ПО ЗАСТРОЙЩИКАМ
# ──────────────────────────────────────────────────────────────
def _aliases_for(report: dict) -> dict:
    """Собирает словарь алиасов так же, как webapp._cross_check_report:
    глобальный hc_aliases.json + key_aliases из spec ИСХОДНОГО источника
    (после swap_report он лежит в report["with_table"]).
    """
    aliases: dict = {}
    try:
        import hc_aliases
        for k, v in hc_aliases._load().items():
            if k and v:
                aliases[k] = v
    except Exception:
        pass
    spec = webapp.load_specs().get(report.get("with_table"))
    cc = getattr(spec, "cross_check", None) or {}
    for k, v in (cc.get("key_aliases") or {}).items():
        nk = webapp._normalize_key(k)
        nv = webapp._normalize_key(v)
        if nk and nv:
            aliases[nk] = nv
    return aliases


def _pick_developer_column(cols: set) -> str | None:
    """Ищет первую подходящую колонку-застройщика среди candidates."""
    for name in _DEVELOPER_COLUMN_CANDIDATES:
        if name in cols:
            return name
    return None


def _developer_map_for_table(db, table: str, name_field: str,
                             aliases: dict, normalize_key: bool) -> dict:
    """{нормализованный_ключ_ЖК: застройщик} из одной таблицы."""
    if not table or not name_field:
        return {}
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            if name_field not in cols:
                return {}
            dev_col = _pick_developer_column(cols)
            if not dev_col:
                return {}
            rows = conn.execute(
                f'SELECT DISTINCT "{name_field}", "{dev_col}" FROM "{table}" '
                f'WHERE "{name_field}" IS NOT NULL AND "{dev_col}" IS NOT NULL'
            ).fetchall()
    except sqlite3.Error:
        return {}

    out: dict = {}
    for nm, dev in rows:
        if not dev:
            continue
        k = (webapp._normalize_key(nm, aliases) if normalize_key
             else str(nm).strip().lower())
        if k and k not in out:
            out[k] = dev
    return out


def _developer_maps(report: dict) -> dict:
    """{нормализованный_ключ_ЖК: застройщик} — с ОБЕИХ сторон сверки."""
    aliases = _aliases_for(report)
    normalize_key = bool(report.get("normalize_key", True))
    out: dict = {}

    # 1. внешний источник (низкий приоритет)
    ext_table = report.get("with_table")
    ext_field = report.get("on_right")
    if ext_table and ext_field:
        try:
            ext_db = webapp.db_for_table(ext_table)
        except Exception:
            ext_db = webapp.EXTERNAL_DB
        for k, dev in _developer_map_for_table(
                ext_db, ext_table, ext_field, aliases, normalize_key).items():
            out[k] = dev

    # 2. Redcat (высокий приоритет — перекрывает внешний)
    rc_table = report.get("source")
    rc_field = report.get("on_left")
    if rc_table and rc_field:
        try:
            rc_db = webapp.db_for_table(rc_table)
        except Exception:
            rc_db = webapp.DATA_DB
        for k, dev in _developer_map_for_table(
                rc_db, rc_table, rc_field, aliases, normalize_key).items():
            out[k] = dev

    return out


def _attach_developers(report: dict) -> None:
    """Проставляет каждому ЖК в отчёте имя застройщика (in place)."""
    dev_map = _developer_maps(report)
    if not dev_map:
        return
    for cls in ("critical", "warn", "ok", "insufficient",
                "left_only", "right_only"):
        for item in report["items_by_class"].get(cls) or []:
            dev = dev_map.get(item.get("key"))
            if dev:
                item["developer"] = dev


def aggregate_by_developer(report: dict) -> list:
    """Сворачивает ЖК по застройщику."""
    metric_names = list((report.get("metrics") or {}).keys())
    groups: dict = {}

    classes = ("critical", "warn", "ok", "insufficient",
               "left_only", "right_only")
    for cls in classes:
        for item in report["items_by_class"].get(cls) or []:
            dev = item.get("developer") or "(застройщик неизвестен)"
            g = groups.setdefault(dev, {
                "developer": dev, "jc_count": 0,
                "critical": 0, "warn": 0, "ok": 0, "insufficient": 0,
                "left_only": 0, "right_only": 0,
                "left_rows": 0, "right_rows": 0, "worst_pct": 0.0,
                "_pcts": {m: [] for m in metric_names},
                "items": [],
            })
            g["jc_count"] += 1
            g[cls] += 1
            g["left_rows"] += item.get("left_rows", 0) or 0
            g["right_rows"] += item.get("right_rows", 0) or 0
            g["worst_pct"] = max(g["worst_pct"], item.get("worst_pct", 0) or 0)
            g["items"].append(item)
            for m in metric_names:
                mm = (item.get("metrics") or {}).get(m)
                if mm and mm.get("diff_pct") is not None:
                    g["_pcts"][m].append(mm["diff_pct"])

    out = []
    for g in groups.values():
        g["metrics"] = {
            m: {"median_pct": round(statistics.median(pcts), 2),
                "max_abs_pct": round(max(abs(p) for p in pcts), 2),
                "n": len(pcts)}
            for m, pcts in g["_pcts"].items() if pcts
        }
        del g["_pcts"]
        out.append(g)

    out.sort(key=lambda x: (-x["critical"], -x["warn"],
                            -x["worst_pct"], -x["jc_count"]))
    return out


# ──────────────────────────────────────────────────────────────
#  Сбор
# ──────────────────────────────────────────────────────────────
def all_sources_with_cross_check() -> list[str]:
    specs = webapp.load_specs()
    return sorted(k for k, s in specs.items()
                  if getattr(s, "cross_check", None))


def collect(source_key: str) -> dict | None:
    """Возвращает отчёт по источнику (уже с Redcat слева) или None."""
    try:
        r = webapp._cross_check_report(source_key)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  {source_key}: {type(e).__name__}: {e}")
        return None
    r = swap_report(r)
    _attach_developers(r)
    return r


# ──────────────────────────────────────────────────────────────
#  Агрегация метрик
# ──────────────────────────────────────────────────────────────
def _class_for_pct(pct) -> str:
    """Класс по |Δ%|. Пороги — константы DELTA_OK / DELTA_WARN."""
    if pct is None:
        return "info"
    p = abs(pct)
    if p <= DELTA_OK:
        return "ok"
    if p <= DELTA_WARN:
        return "warn"
    return "crit"


def _histogram(pcts: list[float],
               edges=(-50, -20, -10, -5, -2, 2, 5, 10, 20, 50)) -> list[dict]:
    if not pcts:
        return []
    buckets = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        n = sum(1 for p in pcts if lo <= p < hi)
        buckets.append({"from": lo, "to": hi, "count": n,
                        "share": round(100 * n / len(pcts), 1)})
    below = sum(1 for p in pcts if p < edges[0])
    above = sum(1 for p in pcts if p >= edges[-1])
    if below:
        buckets.insert(0, {"from": None, "to": edges[0], "count": below,
                           "share": round(100 * below / len(pcts), 1)})
    if above:
        buckets.append({"from": edges[-1], "to": None, "count": above,
                        "share": round(100 * above / len(pcts), 1)})
    return buckets


def _quartiles(vals: list[float]) -> dict:
    if not vals:
        return {}
    s = sorted(vals)
    n = len(s)

    def q(p):
        if n == 1:
            return s[0]
        k = (n - 1) * p
        f, c = int(k), min(int(k) + 1, n - 1)
        return s[f] + (s[c] - s[f]) * (k - f)

    return {"min": round(s[0], 2), "p25": round(q(0.25), 2),
            "median": round(q(0.5), 2), "p75": round(q(0.75), 2),
            "max": round(s[-1], 2)}


def metric_detail(report: dict, metric_name: str) -> dict:
    rows = []
    for cls in ("ok", "warn", "critical"):
        for item in report["items_by_class"].get(cls) or []:
            m = (item.get("metrics") or {}).get(metric_name)
            if not m or m.get("diff_pct") is None:
                continue
            rows.append({
                "jc": item["display"], "class": cls,
                "left": m.get("left"), "right": m.get("right"),
                "left_n": m.get("left_n"), "right_n": m.get("right_n"),
                "diff_abs": m.get("diff_abs"), "diff_pct": m["diff_pct"],
            })
    if not rows:
        return {"n": 0}

    pcts = [r["diff_pct"] for r in rows]
    abs_pcts = [abs(p) for p in pcts]
    rows_sorted = sorted(rows, key=lambda r: -abs(r["diff_pct"]))

    def _cnt(threshold):
        return sum(1 for p in abs_pcts if p >= threshold)

    return {
        "n": len(rows),
        "quartiles_abs": _quartiles(abs_pcts),
        "median_pct": round(statistics.median(pcts), 2),
        "mean_pct": round(statistics.fmean(pcts), 2),
        "over_ok":   _cnt(DELTA_OK),
        "over_warn": _cnt(DELTA_WARN),
        "over_crit": _cnt(DELTA_CRIT),
        "left_higher": sum(1 for p in pcts if p > 0),
        "right_higher": sum(1 for p in pcts if p < 0),
        "histogram": _histogram(pcts),
        "top_worst": rows_sorted[:5],
    }


def aggregate_across_jc(report: dict, metric_name: str, agg: str) -> dict:
    left_vals, right_vals = [], []
    for cls in ("ok", "warn", "critical"):
        for item in report["items_by_class"].get(cls) or []:
            m = (item.get("metrics") or {}).get(metric_name)
            if not m or m.get("left") is None or m.get("right") is None:
                continue
            left_vals.append(m["left"])
            right_vals.append(m["right"])
    if not left_vals:
        return {}

    def _agg(vals):
        if not vals:
            return None
        if agg == "sum":
            return round(sum(vals), 2)
        if agg == "mean":
            return round(statistics.fmean(vals), 2)
        return round(statistics.median(vals), 2)

    l, r = _agg(left_vals), _agg(right_vals)
    pct = round((l - r) / abs(r) * 100, 2) if r else None
    return {"left": l, "right": r, "diff_pct": pct, "n": len(left_vals)}


def rows_summary(report: dict) -> dict:
    left_total = right_total = 0
    mismatches = []
    for cls in ("ok", "warn", "critical"):
        for item in report["items_by_class"].get(cls) or []:
            l, r = item.get("left_rows", 0), item.get("right_rows", 0)
            left_total += l
            right_total += r
            if l != r:
                mismatches.append({"jc": item["display"],
                                   "left": l, "right": r,
                                   "diff": l - r, "class": cls})
    mismatches.sort(key=lambda x: -abs(x["diff"]))
    return {
        "left_total": left_total, "right_total": right_total,
        "diff": left_total - right_total,
        "diff_pct": round((left_total - right_total) / right_total * 100, 1)
                    if right_total else None,
        "top_mismatches": mismatches[:5],
    }


def summarize(report: dict) -> dict:
    s = report["summary"]
    metrics = report.get("metrics") or {}
    out = {
        "source": report["source"],
        "left_label": report.get("left_label"),
        "right_label": report.get("right_label"),
        "on_left": report.get("on_left"),
        "on_right": report.get("on_right"),
        "agg": report.get("agg"),
        "metrics_pairs": metrics,
        "summary": s,
        "auto_aliases": report.get("auto_aliases", 0),
        "aliases_total": report.get("aliases_total", 0),
        "rows": rows_summary(report),
        "per_metric": {},
        "cross_agg": {},
        "by_developer": aggregate_by_developer(report),
    }
    for metric_name in metrics.keys():
        out["per_metric"][metric_name] = metric_detail(report, metric_name)
        out["cross_agg"][metric_name] = {
            "median": aggregate_across_jc(report, metric_name, "median"),
            "mean":   aggregate_across_jc(report, metric_name, "mean"),
            "sum":    aggregate_across_jc(report, metric_name, "sum"),
        }
    return out


# ──────────────────────────────────────────────────────────────
#  Печать в консоль
# ──────────────────────────────────────────────────────────────
def print_short_table(stats: list[dict]) -> None:
    print()
    print("═" * 116)
    print(f"{'источник':<32}{'Redcat':>8}{'источник':>10}{'сопост.':>9}{'ok':>6}"
          f"{'вним.':>7}{'руками':>7}{'тл':>5}{'тп':>5}{'мало':>7}{'строкΔ':>9}")
    print("─" * 116)
    for st in stats:
        s = st["summary"]
        print(f"{st['source']:<32}"
              f"{s['matched'] + s['left_only']:>8}"
              f"{s['matched'] + s['right_only']:>10}"
              f"{s['matched']:>9}"
              f"{s['ok']:>6}"
              f"{s['warn']:>7}"
              f"{s['critical']:>7}"
              f"{s['left_only']:>5}"
              f"{s['right_only']:>5}"
              f"{s['insufficient']:>7}"
              f"{st['rows']['diff']:>9}")
    print("═" * 116)
    print("Redcat — слева, источник — справа; "
          "тл = только у Redcat, тп = только у источника; "
          "строкΔ = лоты источника минус лоты Redcat")
    print()


def print_source_detail(st: dict) -> None:
    print()
    print("━" * 116)
    print(f"  {st['source']}   —   "
          f"Redcat «{st['left_label']}»  ↔  источник «{st['right_label']}»")
    print("━" * 116)
    print(f"  ключи: {st['on_left']} (Redcat)  ↔  {st['on_right']} (источник); "
          f"агрегация {st['agg']}; "
          f"автосинонимов {st['auto_aliases']} / словарь {st['aliases_total']}; "
          f"пороги: ok ≤ {DELTA_OK:g}%, warn ≤ {DELTA_WARN:g}%")

    rows = st["rows"]
    print(f"\n  Лотов по сопоставленным ЖК: Redcat {rows['left_total']}, "
          f"источник {rows['right_total']}, разница {rows['diff']:+} "
          f"({rows['diff_pct']}%)")
    if rows["top_mismatches"]:
        print("  Где перекос по количеству:")
        for mm in rows["top_mismatches"]:
            print(f"    {mm['jc']:<32} RC {mm['left']:>5} / "
                  f"ист {mm['right']:<5} ({mm['diff']:+})")

    devs = st.get("by_developer") or []
    if devs:
        problem = sum(1 for d in devs if d["critical"] or d["warn"])
        print(f"\n  По застройщикам: {len(devs)} шт., "
              f"с проблемами {problem}")
        for d in devs[:12]:
            metrics_str = ", ".join(
                f"{m} {v['median_pct']:+.1f}%"
                for m, v in d["metrics"].items()
            ) or "—"
            extras = []
            if d.get("left_only"):
                extras.append(f"тл {d['left_only']}")
            if d.get("right_only"):
                extras.append(f"тп {d['right_only']}")
            extras_str = (" [" + ", ".join(extras) + "]") if extras else ""
            print(f"    {d['developer'][:30]:<30} "
                  f"ЖК: {d['jc_count']:>3}  "
                  f"руками {d['critical']:>2}  вним. {d['warn']:>2}  "
                  f"ok {d['ok']:>3}{extras_str}  {metrics_str}")
        if len(devs) > 12:
            print(f"    … ещё {len(devs) - 12}")

    for metric_name, detail in st["per_metric"].items():
        if detail["n"] == 0:
            continue
        q = detail["quartiles_abs"]
        print(f"\n  ▸ {metric_name}  (ЖК сравнено: {detail['n']})")
        print(f"      |Δ%|:  min {q['min']:>6}  p25 {q['p25']:>6}  "
              f"медиана {q['median']:>6}  p75 {q['p75']:>6}  max {q['max']:>6}")
        print(f"      среднее Δ%: {detail['mean_pct']:>7}   "
              f"источник>RC у {detail['left_higher']} ЖК, "
              f"RC>источник у {detail['right_higher']}")
        print(f"      пороги: ≥{DELTA_OK:g}% у {detail['over_ok']} ЖК, "
              f"≥{DELTA_WARN:g}% у {detail['over_warn']}, "
              f"≥{DELTA_CRIT:g}% у {detail['over_crit']}")
        print(f"      распределение Δ%:")
        for b in detail["histogram"]:
            lo = "−∞" if b["from"] is None else f"{b['from']:>4}"
            hi = "+∞" if b["to"] is None else f"{b['to']:>4}"
            bar = "█" * max(1, int(b["share"] / 3)) if b["count"] else ""
            print(f"        {lo} … {hi}%: {b['count']:>4} "
                  f"({b['share']:>4}%) {bar}")
        if detail["top_worst"]:
            print(f"      худшие:")
            for w in detail["top_worst"]:
                print(f"        {w['jc']:<32} "
                      f"RC {w['left']!s:>14} ист {w['right']!s:>14} "
                      f"Δ% {w['diff_pct']:>7}  [{w['class']}]")

    print(f"\n  Системный сдвиг (источник минус Redcat):")
    for metric_name, aggs in st["cross_agg"].items():
        parts = []
        for agg_name, v in aggs.items():
            if v and v.get("diff_pct") is not None:
                parts.append(f"{agg_name} {v['diff_pct']:+.1f}%")
        if parts:
            print(f"    {metric_name:<22} " + "   ".join(parts))
    print("    (совпадающий знак у median/mean/sum — системный сдвиг, "
          "разнобой — отдельные ЖК)")


# ──────────────────────────────────────────────────────────────
#  CSV / JSON
# ──────────────────────────────────────────────────────────────
def write_csv(stats: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow([
            "источник", "класс", "застройщик", "ЖК",
            "лотов Redcat", "лотов источника",
            "метрика", "метрика Redcat",
            "значение Redcat", "значение источника",
            "Δ абс", "Δ%",
            "n Redcat", "n источника",
        ])
        for st in stats:
            src = st["source"]
            rep = st.get("_report") or {}
            for cls in ("critical", "warn", "ok", "insufficient",
                        "left_only", "right_only"):
                for item in rep.get("items_by_class", {}).get(cls, []):
                    dev = item.get("developer") or ""
                    metrics = item.get("metrics") or {}
                    if not metrics:
                        w.writerow([src, cls, dev, item["display"],
                                    item["left_rows"], item["right_rows"],
                                    "", "", "", "", "", "", "", ""])
                        continue
                    for m_name, m in metrics.items():
                        w.writerow([
                            src, cls, dev, item["display"],
                            item["left_rows"], item["right_rows"],
                            st["metrics_pairs"].get(m_name, ""), m_name,
                            m.get("left") if m.get("left") is not None else "",
                            m.get("right") if m.get("right") is not None else "",
                            m.get("diff_abs") if m.get("diff_abs") is not None else "",
                            m.get("diff_pct") if m.get("diff_pct") is not None else "",
                            m.get("left_n") or "", m.get("right_n") or "",
                        ])
    print(f"📄 CSV: {path}")


def write_json(stats: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = []
    for st in stats:
        row = {k: v for k, v in st.items() if k != "_report"}
        if "by_developer" in row:
            row["by_developer"] = [
                {k: v for k, v in d.items() if k != "items"}
                for d in row["by_developer"]
            ]
        rep = st.get("_report") or {}
        row["items_by_class"] = {
            cls: [{"display": i["display"], "key": i["key"],
                   "developer": i.get("developer"),
                   "left_rows": i["left_rows"], "right_rows": i["right_rows"],
                   "metrics": i.get("metrics")}
                  for i in (rep.get("items_by_class", {}).get(cls) or [])[:200]]
            for cls in ("critical", "warn", "ok", "insufficient",
                        "left_only", "right_only")
        }
        clean.append(row)
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    print(f"📄 JSON: {path}")


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
"""


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


def _developer_block(st: dict) -> str:
    """Свёрнутая секция «по застройщикам»."""
    devs = st.get("by_developer") or []
    if not devs:
        return ""

    metric_names = list((st.get("metrics_pairs") or {}).keys())
    n_devs = len(devs)
    problem = sum(1 for d in devs if d["critical"] or d["warn"])

    def _worst(item):
        worst = 0.0
        for m in metric_names:
            mm = (item.get("metrics") or {}).get(m) or {}
            if mm.get("diff_pct") is not None:
                worst = max(worst, abs(mm["diff_pct"]))
        return worst

    head_metric_cols = "".join(f"<th>{_esc(m)} Δ%</th>" for m in metric_names)

    details_html = []
    for d in devs:
        pills = []
        if d["critical"]:
            pills.append(f'<span class="pill crit">руками {d["critical"]}</span>')
        if d["warn"]:
            pills.append(f'<span class="pill warn">вним. {d["warn"]}</span>')
        if d["ok"]:
            pills.append(f'<span class="pill ok">ok {d["ok"]}</span>')
        if d["insufficient"]:
            pills.append(f'<span class="pill info">мало {d["insufficient"]}</span>')
        if d.get("left_only"):
            pills.append(f'<span class="pill gray">только RC {d["left_only"]}</span>')
        if d.get("right_only"):
            pills.append(f'<span class="pill gray">только ист. {d["right_only"]}</span>')
        pills_html = " ".join(pills)

        m_summary = " · ".join(
            f'{_esc(m)}: медиана {v["median_pct"]:+.1f}%'
            for m, v in d["metrics"].items()
        ) or "—"

        jc_rows = []
        for it in sorted(d["items"], key=_worst, reverse=True):
            cells = []
            for m in metric_names:
                mm = (it.get("metrics") or {}).get(m) or {}
                pct = mm.get("diff_pct")
                if pct is None:
                    cells.append('<td class="num">—</td>')
                else:
                    cls = _class_for_pct(pct)
                    cells.append(
                        f'<td class="num"><span class="pill {cls}">'
                        f'{pct:+.1f}%</span></td>')
            jc_rows.append(
                f'<tr><td>{_esc(it.get("display"))}</td>'
                f'<td class="num">{it.get("left_rows", 0)}</td>'
                f'<td class="num">{it.get("right_rows", 0)}</td>'
                f'{"".join(cells)}</tr>')

        details_html.append(f'''
        <details class="dev-row">
          <summary>
            <b>{_esc(d["developer"])}</b>
            <span class="muted">· ЖК: {d["jc_count"]}</span>
            <span class="pills">{pills_html}</span>
            <span class="msummary">{m_summary}</span>
          </summary>
          <div class="scroll" style="max-height:400px">
            <table>
              <thead><tr><th>ЖК</th><th>лотов слева</th>
              <th>лотов справа</th>{head_metric_cols}</tr></thead>
              <tbody>{"".join(jc_rows)}</tbody>
            </table>
          </div>
        </details>''')

    return f'''
    <div class="card dev-card">
      <h3>По застройщикам
        <span class="muted" style="font-weight:400">
          — {n_devs} застройщиков, из них с проблемами: {problem}
        </span>
      </h3>
      <div class="muted" style="margin-bottom:10px">
        Одна строка — застройщик; кликните, чтобы раскрыть список его ЖК
        и увидеть, у каких именно расхождение. Сверху — те, у кого больше ЖК
        «смотреть руками». Пороги Δ%: ok ≤ {DELTA_OK:g}%,
        вним. ≤ {DELTA_WARN:g}%, выше — «руками».
      </div>
      <div class="dev-list">{"".join(details_html)}</div>
    </div>
    '''


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
        mm_rows = "".join(
            f'<tr><td>{_esc(m["jc"])}</td>'
            f'<td class="num">{m["left"]}</td>'
            f'<td class="num">{m["right"]}</td>'
            f'<td class="num">{m["diff"]:+}</td></tr>'
            for m in rows["top_mismatches"])
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

    developer_html = _developer_block(st)

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


# ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Статистика сверки источников с Redcat")
    ap.add_argument("--sources", nargs="*", default=None)
    ap.add_argument("--exclude", nargs="*", default=None)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--html", default=None,
                    help="сохранить HTML-дашборд, например "
                         "reports/source_stats.html")
    ap.add_argument("--no-open", action="store_true",
                    help="не открывать HTML в браузере автоматически")
    ap.add_argument("--only-short", action="store_true",
                    help="только сводная таблица, без деталей")
    args = ap.parse_args()

    targets = args.sources or all_sources_with_cross_check()
    if args.exclude:
        targets = [t for t in targets if t not in set(args.exclude)]
    if not targets:
        print("Нет источников с cross_check.")
        return 1

    print(f"Источников к обработке: {len(targets)}")
    print(f"  {', '.join(targets)}")
    print(f"Пороги Δ%: ok ≤ {DELTA_OK:g}%, warn ≤ {DELTA_WARN:g}%, "
          f"crit > {DELTA_WARN:g}%")

    stats = []
    for key in targets:
        print(f"\n▶ {key}")
        rep = collect(key)
        if rep is None:
            continue
        st = summarize(rep)
        st["_report"] = rep
        stats.append(st)
        s = rep["summary"]
        n_devs = len(st.get("by_developer") or [])
        unknown = sum(1 for d in (st.get("by_developer") or [])
                      if d["developer"] == "(застройщик неизвестен)")
        print(f"   сопоставлено {s['matched']}, "
              f"ok={s['ok']} warn={s['warn']} crit={s['critical']}, "
              f"тл={s['left_only']} тп={s['right_only']} "
              f"мало={s['insufficient']}, "
              f"застройщиков={n_devs}"
              + (f" (без застройщика: {unknown})" if unknown else ""))

    if not stats:
        print("\nНет данных для отчёта.")
        return 1

    print_short_table(stats)

    if not args.only_short:
        for st in stats:
            print_source_detail(st)

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
    print()
    print("═" * 116)
    print(f"Итого: сопоставлено {tot['matched']}, "
          f"ok={tot['ok']} warn={tot['warn']} crit={tot['critical']}, "
          f"тл={tot['left_only']} тп={tot['right_only']}")
    print(f"       лотов в Redcat {tot['rows_left']}, "
          f"в источниках {tot['rows_right']}, "
          f"разница {tot['rows_left'] - tot['rows_right']:+}")
    print("═" * 116)

    if args.csv:
        write_csv(stats, Path(args.csv))
    if args.json:
        write_json(stats, Path(args.json))
    if args.html:
        p = render_html_dashboard(stats, Path(args.html))
        print(f"🌐 HTML: {p}")
        if not args.no_open:
            try:
                webbrowser.open(p.resolve().as_uri())
            except Exception:
                pass

    return 0 if not tot["critical"] else 2


if __name__ == "__main__":
    sys.exit(main())