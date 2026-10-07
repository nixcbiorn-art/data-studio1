"""
scan_sources.py — полная проверка всех источников с cross_check.
===================================================================
Проходит по каждому источнику и печатает:

  1. Спецификацию: что сравнивается, какие фильтры, агрегация, пороги.
  2. Объёмы: сколько лотов в источнике vs в Redcat по тому же фильтру.
  3. Отброшенные фильтры: если фильтр ссылается на несуществующую
     колонку (например developer_name в apartments) — увидим.
  4. Подозрительные поля: если в источнике есть колонка типа
     «category/type/commercial», а фильтра по ней нет — предупреждение.
  5. Маппинг цены: base_price vs discounted_price в metrics —
     предупреждение если сравниваются разные виды.
  6. Результаты сопоставления: сколько сопоставлено / left_only /
     right_only / critical.
  7. Топ-5 left_only ЖК (не сопоставились) — на что смотреть.
  8. Медианы метрик на обеих сторонах — даже при ok, чтобы видеть
     мелкие сдвиги.

Запуск:
    python scan_sources.py
    python scan_sources.py --source samolet_apartments
    python scan_sources.py --only-with-issues
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import webapp


# ──────────────────────────────────────────────────────────────
#  Вспомогательное
# ──────────────────────────────────────────────────────────────
_PRICE_DISCOUNTED = ("discount", "sale", "promo", "act", "акци", "скидк")
_PRICE_BASE = ("base", "list", "regular", "catalog", "каталож", "базов")
_TYPE_HINTS = ("category", "type", "kind", "commercial", "камерч",
               "property", "object", "deal_type")


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(" ", "").replace("\xa0", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _cols(db, table):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    finally:
        conn.close()


def _count(db, table, where=None, args=()):
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            sql = f'SELECT COUNT(*) FROM "{table}"'
            if where:
                sql += f" WHERE {where}"
            return conn.execute(sql, args).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _price_kind(col: str) -> str:
    n = col.lower()
    if any(h in n for h in _PRICE_DISCOUNTED):
        return "discounted"
    if any(h in n for h in _PRICE_BASE):
        return "base"
    return "unknown"


def _has_type_hints(cols: list) -> list:
    """Колонки, похожие на «тип объекта» или «категорию»."""
    out = []
    for c in cols:
        n = c.lower()
        for h in _TYPE_HINTS:
            if h in n:
                out.append(c)
                break
    return out


def _col_distribution(db, table, col, limit=15):
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                f'SELECT "{col}" AS v, COUNT(*) AS n FROM "{table}" '
                f'GROUP BY 1 ORDER BY n DESC LIMIT {limit}').fetchall()
            return [(r[0], r[1]) for r in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _median(db, table, col, where=None, args=()):
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            sql = (f'SELECT "{col}" FROM "{table}" WHERE '
                   f'"{col}" IS NOT NULL AND "{col}" != \'\'')
            if where:
                sql += f" AND ({where})"
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    vals = []
    for r in rows:
        v = _num(r[0])
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


# ──────────────────────────────────────────────────────────────
#  Основной анализ одного источника
# ──────────────────────────────────────────────────────────────
def scan_source(key: str, specs: dict) -> dict:
    """Возвращает словарь с результатами анализа."""
    out = {"source": key, "issues": [], "warnings": [], "info": {}}
    spec = specs.get(key)
    if not spec:
        out["issues"].append(f"spec {key!r} не найден")
        return out

    cc = getattr(spec, "cross_check", None) or {}
    if not cc:
        out["issues"].append("нет cross_check в spec")
        return out

    # ── 1. Спецификация ──
    metrics = cc.get("metrics") or {}
    filter_left = cc.get("filter_left")
    filter_right = cc.get("filter_right")
    agg = cc.get("agg") or "median"
    thresholds = cc.get("thresholds") or {}
    t_ok = float(thresholds.get("ok", 10))
    t_warn = float(thresholds.get("warn", 20))
    with_table = cc.get("with_table") or "apartments"
    on_left = cc.get("on_left")
    on_right = cc.get("on_right")

    out["info"]["metrics"] = metrics
    out["info"]["agg"] = agg
    out["info"]["thresholds"] = {"ok": t_ok, "warn": t_warn}
    out["info"]["with_table"] = with_table
    out["info"]["filter_left"] = filter_left
    out["info"]["filter_right"] = filter_right

    # ── 2. Маппинг цены: base vs discounted ──
    for left, right in metrics.items():
        lk = _price_kind(str(left))
        rk = _price_kind(str(right))
        if lk != "unknown" and rk != "unknown" and lk != rk:
            out["issues"].append(
                f"маппинг цены: слева «{left}» ({lk}), "
                f"справа «{right}» ({rk}) — сравниваются разные виды цены")

    # ── 3. Объёмы ──
    left_db = webapp.db_for_table(key)
    right_db = webapp.db_for_table(with_table)
    left_total = _count(left_db, key)
    right_total = _count(right_db, with_table)
    out["info"]["left_total"] = left_total
    out["info"]["right_total"] = right_total

    # ── 4. Отброшенные фильтры и подозрительные поля ──
    try:
        left_cols = _cols(left_db, key)
    except Exception:
        left_cols = []
    try:
        right_cols = _cols(right_db, with_table)
    except Exception:
        right_cols = []

    # filter_left колонки
    if filter_left:
        fl = filter_left if isinstance(filter_left, list) else [filter_left]
        for f in fl:
            if not isinstance(f, dict):
                continue
            fld = f.get("field")
            if fld and fld not in left_cols:
                out["issues"].append(
                    f"filter_left: колонка {fld!r} не найдена в {key}")
    if filter_right:
        fr = filter_right if isinstance(filter_right, list) else [filter_right]
        for f in fr:
            if not isinstance(f, dict):
                continue
            fld = f.get("field")
            if fld and fld not in right_cols:
                out["warnings"].append(
                    f"filter_right: колонка {fld!r} не найдена в {with_table} "
                    f"— фильтр будет отброшен (это норма для developer_name "
                    f"в apartments, но проверь)")

    # ── 5. Неиспользованные типовые поля ──
    type_left = _has_type_hints(left_cols)
    used_left_fields = set()
    for f in (filter_left if isinstance(filter_left, list)
              else [filter_left] if filter_left else []):
        if isinstance(f, dict):
            used_left_fields.add(f.get("field"))
    unused_type = [c for c in type_left if c not in used_left_fields]
    if unused_type:
        # Смотрим, что в этих полях есть.
        for c in unused_type:
            dist = _col_distribution(left_db, key, c)
            out["info"].setdefault("type_cols", {})[c] = dist
            # Если в распределении есть что-то кроме одного значения —
            # фильтр может быть нужен.
            if len(dist) >= 2:
                out["warnings"].append(
                    f"поле {c!r} в источнике не фильтруется, "
                    f"в нём {len(dist)} разных значений — возможно, "
                    f"нужен filter_left")
                break

    # ── 6. Сопоставление ──
    try:
        rep = webapp._cross_check_report(key)
        s = rep.get("summary") or {}
        out["info"]["matched"] = s.get("matched", 0)
        out["info"]["critical"] = s.get("critical", 0)
        out["info"]["warn"] = s.get("warn", 0)
        out["info"]["ok"] = s.get("ok", 0)
        out["info"]["insufficient"] = s.get("insufficient", 0)
        out["info"]["left_only"] = s.get("left_only", 0)
        out["info"]["right_only"] = s.get("right_only", 0)
        # Топ left_only
        lo = rep.get("items_by_class", {}).get("left_only") or []
        out["info"]["top_left_only"] = [
            (it.get("display"), it.get("left_rows"))
            for it in sorted(lo, key=lambda x: -(x.get("left_rows") or 0))[:5]
        ]
    except Exception as e:
        out["issues"].append(f"cross_check упал: {type(e).__name__}: {e}")
        return out

    # ── 7. Медианы метрик ──
    medians = {}
    for left_metric, right_metric in metrics.items():
        lm = _median(left_db, key, left_metric)
        rm = _median(right_db, with_table, right_metric)
        medians[left_metric] = {
            "left": lm, "right": rm,
            "diff_pct": ((lm - rm) / rm * 100) if (lm and rm) else None,
        }
    out["info"]["medians"] = medians

    return out


# ──────────────────────────────────────────────────────────────
#  Печать
# ──────────────────────────────────────────────────────────────
def print_report(results: list, only_issues: bool = False) -> int:
    total_issues = 0
    total_warnings = 0

    for r in results:
        has_issues = bool(r["issues"])
        has_warnings = bool(r["warnings"])
        if only_issues and not (has_issues or has_warnings):
            continue

        key = r["source"]
        info = r["info"]

        print()
        print("=" * 84)
        head = f"  {key}"
        if has_issues:
            head += "   ❌ есть проблемы"
        elif has_warnings:
            head += "   ⚠️  есть предупреждения"
        else:
            head += "   ✅"
        print(head)
        print("=" * 84)

        if has_issues:
            print("  ПРОБЛЕМЫ:")
            for x in r["issues"]:
                print(f"    ❌ {x}")
            total_issues += len(r["issues"])
        if has_warnings:
            print("  ПРЕДУПРЕЖДЕНИЯ:")
            for x in r["warnings"]:
                print(f"    ⚠️  {x}")
            total_warnings += len(r["warnings"])

        if not info:
            continue

        # Спека.
        print()
        print("  Спека:")
        print(f"    metrics     = {info.get('metrics')}")
        print(f"    agg         = {info.get('agg')}")
        print(f"    thresholds  = {info.get('thresholds')}")
        print(f"    with_table  = {info.get('with_table')}")
        if info.get("filter_left") is not None:
            print(f"    filter_left = {info['filter_left']}")
        if info.get("filter_right") is not None:
            print(f"    filter_right= {info['filter_right']}")

        # Объёмы.
        lt = info.get("left_total")
        rt = info.get("right_total")
        print()
        print(f"  Объёмы:")
        print(f"    источник: {lt} лотов")
        print(f"    Redcat:   {rt} лотов в {info.get('with_table')}")

        # Сопоставление.
        print()
        print("  Сопоставление:")
        print(f"    matched      = {info.get('matched')}   "
              f"(crit {info.get('critical')}, warn {info.get('warn')}, "
              f"ok {info.get('ok')}, мало {info.get('insufficient')})")
        print(f"    только слева = {info.get('left_only')}   "
              f"(есть у источника, нет у Redcat)")
        print(f"    только справа= {info.get('right_only')}   "
              f"(есть у Redcat, нет у источника)")

        # Медианы.
        med = info.get("medians") or {}
        if med:
            print()
            print("  Медианы метрик:")
            for m, v in med.items():
                lv = v["left"]
                rv = v["right"]
                d = v["diff_pct"]
                if lv is None or rv is None:
                    print(f"    {m:<20} — не посчитать")
                    continue
                lv_s = f"{lv:,.0f}".replace(",", " ") if lv >= 100 else f"{lv:.2f}"
                rv_s = f"{rv:,.0f}".replace(",", " ") if rv >= 100 else f"{rv:.2f}"
                d_s = f"{d:+.1f}%" if d is not None else "—"
                print(f"    {m:<20} источник {lv_s:>14}  "
                      f"Redcat {rv_s:>14}  Δ {d_s:>8}")

        # Топ left_only.
        top = info.get("top_left_only") or []
        if top:
            print()
            print("  Крупнейшие left_only (не сопоставились):")
            for name, rows in top:
                print(f"    {str(name)[:60]:<60}  {rows} лотов")

        # Типы в источнике.
        tc = info.get("type_cols") or {}
        for col, dist in tc.items():
            print()
            print(f"  Поле {col!r} в источнике (не фильтруется):")
            for v, n in dist[:8]:
                print(f"    {str(v)[:40]:<40}  {n}")

    print()
    print("=" * 84)
    print("  ИТОГО")
    print("=" * 84)
    print(f"  Источников проверено: {len(results)}")
    print(f"  Проблем:              {total_issues}")
    print(f"  Предупреждений:       {total_warnings}")
    return 0 if total_issues == 0 else 1


# ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=None,
                    help="один источник, а не все")
    ap.add_argument("--only-with-issues", action="store_true",
                    help="показать только те, где есть проблемы/предупреждения")
    args = ap.parse_args()

    specs = webapp.load_specs()
    targets = ([args.source] if args.source
               else sorted(k for k, s in specs.items()
                           if getattr(s, "cross_check", None)))

    if not targets:
        print("Нет источников с cross_check.")
        return 1

    print("=" * 84)
    print(f"  Проверка источников ({len(targets)})")
    print("=" * 84)

    results = []
    for key in targets:
        results.append(scan_source(key, specs))

    return print_report(results, only_issues=args.only_with_issues)


if __name__ == "__main__":
    sys.exit(main())