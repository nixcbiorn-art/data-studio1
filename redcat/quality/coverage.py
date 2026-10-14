"""
Покрытие сверки «витрина ↔ внешний фид»
========================================
Отвечает на вопрос: какую долю записей витрины (и фида) сверка вообще
видит — то есть сколько строк сошлись по названию ЖК с учётом
hc_aliases.json и автосинонимов. Раньше эта величина была видна только
при ручном разборе (за период 10–14.10 она составила ~32%) и молча
просаживалась при деградации любого источника.

Два режима:
  • report_from_normalized(rows_by_key, specs) — чистая функция, считается
    прямо во время сбора (скрапер подмешивает результат в метрики запуска:
    в history появляется колонка <key>_coverage_right_pct на каждый источник).
  • python -m redcat.quality.coverage — по уже сохранённым базам через
    боевой матчер web_crosscheck (в его summary те же счётчики строк);
    пишет reports/coverage_latest.json.

Совпадение считается по ключу названия ЖК: canon (нормализация + словарь)
для обеих сторон, затем одиночные автосопоставления name_normalizer
(Скай Гарден ↔ Sky Garden). Ручные алиасы перекрывают авто.
"""
from __future__ import annotations

from redcat.core import paths
import argparse
import json

from redcat.sources import hc_aliases
from redcat.sources import name_normalizer as nn


# ── фильтры cross_check (то же семантика, что в dataops.build_where) ──
def _op_eq(a, b):
    return str(a).strip().casefold() == str(b).strip().casefold()


def _pass_filters(row, filters) -> bool:
    if not filters:
        return True
    items = filters if isinstance(filters, list) else [filters]
    for f in items:
        fld = f.get("field")
        op = (f.get("op") or "eq").lower()
        # «in» в конфигах использует ключ values (список); остальные — value
        val = f.get("value")
        if val is None:
            val = f.get("values")
        v = row.get(fld)
        if v is None or v == "":
            return False
        if op == "eq" and not _op_eq(v, val):
            return False
        if op in ("contains", "ilike") and str(val).casefold() not in str(v).casefold():
            return False
        if op == "in" and not any(_op_eq(v, x) for x in (val or [])):
            return False
        if op == "not_empty" and not str(v).strip():
            return False
    return True


def coverage_pair(left_rows, right_rows, cc) -> dict:
    """Покрытие одной пары (фид ↔ таблица-сосед) по правилам cross_check."""
    on_left, on_right = cc.get("on_left"), cc.get("on_right")
    filter_left, filter_right = cc.get("filter_left"), cc.get("filter_right")
    use_norm = bool(cc.get("normalize_key", True))

    aliases = {}
    for k, v in hc_aliases._load().items():
        if k and v:
            aliases[k] = v
    for k, v in (cc.get("key_aliases") or {}).items():
        nk, nv = (nn.canon(k), nn.canon(v)) if use_norm else (
            str(k).strip().lower(), str(v).strip().lower())
        if nk and nv:
            aliases[nk] = nv

    def key(row, col):
        raw = row.get(col)
        if raw is None or raw == "":
            return None
        return nn.canon(raw, aliases) if use_norm else str(raw).strip().lower()

    lset, rset = {}, {}
    for r in left_rows:
        if _pass_filters(r, filter_left):
            k = key(r, on_left)
            if k:
                lset[k] = lset.get(k, 0) + 1
    for r in right_rows:
        if _pass_filters(r, filter_right):
            k = key(r, on_right)
            if k:
                rset[k] = rset.get(k, 0) + 1

    # автосинонимы для кириллица↔латиницы — те же, что в боевом матчере:
    # только однозначные, с опорой на счётчики строк
    if use_norm:
        only_l = [k for k in lset if k not in rset]
        only_r = [k for k in rset if k not in lset]
        if only_l and only_r:
            found = nn.auto_aliases(only_l, only_r, existing=aliases,
                                    left_counts={k: lset[k] for k in only_l},
                                    right_counts={k: rset[k] for k in only_r})["aliases"]
            for lk, rk in found.items():
                if lk in lset and rk in rset:
                    lset[rk] = lset.pop(lk) + lset.get(rk, 0)

    r_total = sum(rset.values())
    l_total = sum(lset.values())
    r_matched = sum(n for k, n in rset.items() if k in lset)
    l_matched = sum(n for k, n in lset.items() if k in rset)
    return {
        "rows_right_total": r_total,
        "rows_right_matched": r_matched,
        "coverage_right_pct": round(100.0 * r_matched / r_total, 1) if r_total else None,
        "rows_left_total": l_total,
        "rows_left_matched": l_matched,
        "coverage_left_pct": round(100.0 * l_matched / l_total, 1) if l_total else None,
    }


def report_from_normalized(rows_by_key, specs) -> dict:
    """Покрытие всех пар cross_check по свежим строкам сбора.

    Возвращает {source_key: {...}}; источники, чьих соседних таблиц ещё нет
    в этом запуске, пропускаются (не с чем сверять)."""
    out = {}
    for s in specs:
        cc = getattr(s, "cross_check", None) or {}
        if not (cc.get("with_table") and cc.get("on_left") and cc.get("on_right")):
            continue
        left = rows_by_key.get(s.key)
        right = rows_by_key.get(cc["with_table"])
        if not left or not right:
            continue
        out[s.key] = coverage_pair(left, right, cc)
    return out


# ──────────────────────────────────────────────────────────────
#  CLI: по сохранённым базам через боевой матчер
# ──────────────────────────────────────────────────────────────
def collect_from_dbs() -> list[dict]:
    from redcat.sources import registry as src
    from redcat.web.web_crosscheck import _cross_check_report
    src.load_from_dir(paths.SOURCES_DIR)
    src.load_from_dir(paths.SOURCES_EXT_DIR)
    rows = []
    for s in src.all_sources():
        cc = getattr(s, "cross_check", None) or {}
        if not (cc.get("with_table") and cc.get("on_left") and cc.get("on_right")):
            continue
        try:
            rep = _cross_check_report(s.key)
        except Exception as e:      # база не собрана и т.п.
            rows.append({"source": s.key, "error": f"{type(e).__name__}: {e}"})
            continue
        summ = rep.get("summary", {})
        rows.append({
            "source": s.key, "title": s.title or s.key,
            "with_table": cc.get("with_table"),
            "coverage_right_pct": summ.get("coverage_right_pct"),
            "rows_right_total": summ.get("rows_right_total"),
            "rows_right_matched": summ.get("rows_right_matched"),
            "coverage_left_pct": summ.get("coverage_left_pct"),
            "matched_keys": summ.get("matched"),
            "total_keys": summ.get("total_keys"),
            "auto_aliases": rep.get("auto_aliases"),
        })
    return rows


def main(argv=None) -> int:
    from redcat.core import safe_stdout
    safe_stdout.enable()
    ap = argparse.ArgumentParser(description="Покрытие сверки витрина↔источники")
    ap.add_argument("--json", action="store_true",
                    help="записать reports/coverage_latest.json")
    args = ap.parse_args(argv)

    rows = collect_from_dbs()
    if not rows:
        print("Нет источников с cross_check — покрыть нечего.")
        return 1
    print(f"{'источник':24} {'покрытие витрины':>16} {'покрытие фида':>14} {'ключей':>10}")
    for r in rows:
        if "error" in r:
            print(f"{r['source']:24}   ⚠️ {r['error']}")
            continue
        print(f"{r['source']:24} {str(r['coverage_right_pct']) + '%':>16} "
              f"{str(r['coverage_left_pct']) + '%':>14} "
              f"{str(r['matched_keys']) + '/' + str(r['total_keys']):>10}")
    if args.json:
        out = paths.REPORTS_DIR / "coverage_latest.json"
        out.parent.mkdir(exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        print(f"Записан {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
