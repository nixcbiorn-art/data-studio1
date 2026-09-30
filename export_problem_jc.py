"""
export_problem_jc.py — проблемные ЖК.
========================================
В основной отчёт идут ТОЛЬКО реальные проблемы. Скидочные расхождения
отсеиваются и пишутся отдельным файлом.

Сравниваются цены СО СКИДКОЙ на обеих сторонах:
  • Redcat отдаёт цену со скидкой в поле `price`.
  • Внешний источник — в поле, указанном в cross_check.metrics.

Что считается проблемой (попадает в отчёт):
  • объём разошёлся   → проблема
  • площадь разошлась → проблема
  • цена разошлась И большинство лотов по цене НЕ совпадают → проблема

Что НЕ проблема (в отчёт не идёт):
  • цена разошлась, но у большинства лотов цена на обеих сторонах
    одинаковая (доля совпадения ≥ порога) — это скидка на части лотов

Файлы:
  reports/problem_jc_<ts>.csv           — только проблемы
  reports/problem_jc_<ts>.md            — то же, читаемо
  reports/problem_jc_<ts>_discounts.csv — отсеянные как скидочные
"""
from __future__ import annotations

# ── UTF-8 для stdout/stderr ───────────────────────────────────
# На русской Windows консоль по умолчанию cp1251. Символы вроде Δ, ≥, ₽
# в неё не влезают и роняют скрипт с UnicodeEncodeError ещё до начала
# работы. Переключаем потоки на UTF-8 с backslashreplace — тогда ничего
# не падает, даже если терминал не умеет UTF-8.
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

import argparse
import csv
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import dataops
import hc_aliases
import webapp


SOURCE_TO_DEVELOPER = {
    "jcat_lsr_apartments_normalized": "ЛСР",
    "samolet_apartments":             "Самолёт",
    "fsk_apartments":                 "ФСК/ДСК",
    "a101_apartments":                "А101",
    "rbi_apartments":                 "RBI",
    "dsk_apartments":                 "ДСК",
}

METRIC_LABELS = {
    "discounted_price": "Цена со скидкой",
    "sale_price":       "Цена распродажи",
    "promo_price":      "Акционная цена",
    "price":            "Цена",
    "base_price":       "Базовая цена",
    "list_price":       "Каталожная цена",
    "area":             "Площадь",
    "area_total":       "Площадь общая",
    "total_area":       "Площадь общая",
    "price_per_sqm":    "Цена за м²",
    "floor":            "Этаж",
    "rooms":            "Комнат",
}


# ── СЛОВАРЬ ФОРМУЛИРОВОК ──────────────────────────────────────
# Каждая фраза — один факт с числами. Никаких «похоже» и «может быть».
DIAG = {
    # объём
    "rows_less_big":  "в Redcat на {n} лотов меньше ({pct:.1f}%) — часть не собрана",
    "rows_less_sm":   "в Redcat на {n} лотов меньше ({pct:.1f}%) — в пределах шума",
    "rows_more_big":  "в Redcat на {n} лотов больше ({pct:.1f}%) — источник фильтрует часть",
    "rows_more_sm":   "в Redcat на {n} лотов больше ({pct:.1f}%) — в пределах шума",
    # площадь
    "area_diff_big":  "медиана площади расходится на {pct:+.1f}%",
    "area_diff_sm":   "медиана площади расходится на {pct:+.1f}% — в пределах округления",
    # цена
    "price_none":     "ни одна цена не совпала ({total} лотов) — проверить маппинг поля цены и фильтры",
    "price_tiny":     "цены совпадают только у {matched} из {total} лотов ({share:.1f}%) — проверить маппинг",
    "price_most":     "цены не совпали у {diff} из {total} лотов ({share:.0f}%) — расхождение сплошное",
    "price_part":     "цены не совпали у {diff} из {total} лотов ({share:.0f}%) — затрагивает большинство",
    "price_some":     "цены не совпали у {diff} из {total} лотов ({share:.0f}%) — затрагивает часть выборки",
    "price_nodata":   "цены в лотах не числовые — распределение не построить",
    # маппинг
    "map_mismatch":   "маппинг: слева «{left}» ({lk}), справа «{right}» ({rk}) — сравниваются разные виды цены",
}

# Скидочные формулировки — в отдельный файл.
DIAG_DISC = {
    "price_full":    "все {total} лотов с одинаковой ценой, но медиана расходится на {pct:+.1f}% — расхождение в агрегате",
    "price_high":    "скидка: цена отличается у {diff} из {total} лотов ({share:.0f}%) — совпадает у {matched}",
    "price_mid":     "скидка: цена отличается у {diff} из {total} лотов ({share:.0f}%)",
    "price_low":     "скидка: цена отличается у {diff} из {total} лотов ({share:.0f}%) — но часть совпадает",
}

# Пороги значимости: ниже — не считаем сигналом (шум/округление).
ROWS_PCT_NOISE = 2.0    # % расхождения объёма
ROWS_ABS_NOISE = 3      # лотов, минимальная разница
AREA_PCT_NOISE = 1.0    # % расхождения площади

# Границы для доли совпадения цен.
MATCH_NEAR_FULL = 0.9
MATCH_MOST      = 0.5
MATCH_FEW       = 0.1

# Порог доли совпадения цен, ниже которого расхождение цены считается
# проблемой, а не скидкой.
DISCOUNT_MATCH_MIN = 0.5


# ── вид цены по имени ─────────────────────────────────────────
_DISCOUNTED = ("discount", "sale", "promo", "act", "акци", "скидк")
_BASE = ("base", "list", "regular", "catalog", "каталож", "базов", "без_скидки")
_KIND_LABEL = {"discounted": "цена со скидкой", "base": "базовая цена",
               "unknown": "вид не определён"}


def _price_kind(name: str) -> str:
    n = (name or "").lower()
    if any(h in n for h in _DISCOUNTED):
        return "discounted"
    if any(h in n for h in _BASE):
        return "base"
    return "unknown"


def _is_price(name: str) -> bool:
    n = (name or "").lower()
    return "price" in n or "цена" in n or "стоимост" in n


def _price_pairs(metrics: dict) -> list[tuple]:
    out = []
    for lf, rf in (metrics or {}).items():
        if _is_price(lf) or _is_price(rf):
            out.append((lf, rf, _price_kind(lf), _price_kind(rf)))
    return out


def check_mapping(source_key: str, specs: dict) -> list[str]:
    spec = specs.get(source_key)
    cc = getattr(spec, "cross_check", None) or {}
    metrics = cc.get("metrics") or {}
    warns = []
    for lf, rf, lk, rk in _price_pairs(metrics):
        if lk == "unknown" or rk == "unknown" or lk == rk:
            continue
        warns.append(DIAG["map_mismatch"].format(
            left=lf, right=rf, lk=_KIND_LABEL[lk], rk=_KIND_LABEL[rk]))
    return warns


# ── утилиты ───────────────────────────────────────────────────
def _to_num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(" ", "").replace("\xa0", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _clean_title(t: str) -> str:
    if not t:
        return ""
    return re.split(r"\s+[—–-]\s+|\(|\[|:", t, maxsplit=1)[0].strip()


def developer_for(source_key: str, specs: dict) -> str:
    if source_key in SOURCE_TO_DEVELOPER:
        return SOURCE_TO_DEVELOPER[source_key]
    spec = specs.get(source_key)
    if spec and getattr(spec, "title", ""):
        c = _clean_title(spec.title)
        if c:
            return c
    return source_key


def metric_label(name: str) -> str:
    return METRIC_LABELS.get(name, name)


def _fmt_val(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}".replace(".", ",")
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    return str(v)


def _fmt_pct(p) -> str:
    if p is None:
        return ""
    return f"{p:+.1f}%".replace(".", ",")


def _fmt_abs(a) -> str:
    if a is None:
        return ""
    if isinstance(a, float):
        return f"{a:+.2f}".replace(".", ",")
    if isinstance(a, int):
        return f"{a:+,}".replace(",", " ")
    return str(a)


def _class_for_pct(pct, threshold):
    if pct is None:
        return "ok"
    a = abs(pct)
    if a < threshold:
        return "ok"
    if a < 5:
        return "small"
    if a < 10:
        return "medium"
    return "crit"


def _class_label(cls: str) -> str:
    return {"ok": "ok", "small": "мелкое",
            "medium": "среднее", "crit": "крупное"}.get(cls, cls)


def _md_icon(cls: str) -> str:
    return {"crit": "🔴", "medium": "⚠️", "small": "·", "ok": "✅"}.get(cls, "")


# ── диагностика одного ЖК ─────────────────────────────────────
def diagnose(item: dict) -> list[str]:
    out = []

    rows_rc = item.get("rows_rc") or 0
    rows_src = item.get("rows_src") or 0
    if rows_rc != rows_src:
        n = abs(rows_rc - rows_src)
        pct = (n / max(rows_src, 1)) * 100 if rows_src else 0.0
        big = pct >= ROWS_PCT_NOISE and n >= ROWS_ABS_NOISE
        if rows_rc < rows_src:
            key = "rows_less_big" if big else "rows_less_sm"
        else:
            key = "rows_more_big" if big else "rows_more_sm"
        out.append(DIAG[key].format(n=n, pct=pct))

    area_pct = _find_area_diff(item)
    if area_pct is not None and abs(area_pct) > 0.001:
        key = ("area_diff_big" if abs(area_pct) >= AREA_PCT_NOISE
               else "area_diff_sm")
        out.append(DIAG[key].format(pct=area_pct))

    matches = item.get("price_matches") or {}
    if not matches:
        if any(_is_price(m["metric_left"]) for m in item["metrics"]):
            out.append(DIAG["price_nodata"])
    else:
        for _lf, info in matches.items():
            ratio = info["ratio"]
            matched = info["matched"]
            total = info["total"]
            diff = total - matched
            share = diff / total * 100 if total else 0.0

            if matched == 0:
                out.append(DIAG["price_none"].format(total=total))
            elif ratio < MATCH_FEW:
                out.append(DIAG["price_tiny"].format(
                    matched=matched, total=total,
                    share=matched / total * 100))
            elif ratio < MATCH_MOST:
                out.append(DIAG["price_most"].format(
                    diff=diff, total=total, share=share))
            elif ratio < MATCH_NEAR_FULL:
                out.append(DIAG["price_part"].format(
                    diff=diff, total=total, share=share))
            elif ratio < 1.0:
                out.append(DIAG["price_some"].format(
                    diff=diff, total=total, share=share))
    return out


def diagnose_discount(item: dict) -> list[str]:
    out = []
    matches = item.get("price_matches") or {}
    price_pct = _find_price_diff(item) or 0.0
    for _lf, info in matches.items():
        ratio = info["ratio"]
        matched = info["matched"]
        total = info["total"]
        diff = total - matched
        share = diff / total * 100 if total else 0.0
        if ratio >= 0.999:
            out.append(DIAG_DISC["price_full"].format(
                total=total, pct=price_pct))
        elif ratio >= MATCH_NEAR_FULL:
            out.append(DIAG_DISC["price_high"].format(
                diff=diff, total=total, share=share, matched=matched))
        elif ratio >= MATCH_MOST:
            out.append(DIAG_DISC["price_mid"].format(
                diff=diff, total=total, share=share))
        else:
            out.append(DIAG_DISC["price_low"].format(
                diff=diff, total=total, share=share))
    return out


# ── распределение цен по лотам ────────────────────────────────
def _aliases_for(source_key: str, specs: dict) -> dict:
    aliases: dict = {}
    try:
        for k, v in hc_aliases._load().items():
            if k and v:
                aliases[k] = v
    except Exception:
        pass
    spec = specs.get(source_key)
    cc = getattr(spec, "cross_check", None) or {}
    for k, v in (cc.get("key_aliases") or {}).items():
        nk = webapp._normalize_key(k)
        nv = webapp._normalize_key(v)
        if nk and nv:
            aliases[nk] = nv
    return aliases


def _build_price_map(source_key: str, specs: dict) -> dict:
    spec = specs.get(source_key)
    cc = getattr(spec, "cross_check", None) or {}
    if not cc:
        return {}
    on_left = cc.get("on_left")
    on_right = cc.get("on_right")
    right_table = cc.get("with_table")
    metrics = cc.get("metrics") or {}
    if not (on_left and on_right and right_table and metrics):
        return {}

    price_pairs = [(lf, rf) for lf, rf, _, _ in _price_pairs(metrics)]
    if not price_pairs:
        return {}

    normalize_key = bool(cc.get("normalize_key", True))
    aliases = _aliases_for(source_key, specs)

    def _key(raw):
        if raw in (None, ""):
            return None
        if normalize_key:
            return webapp._normalize_key(raw, aliases)
        return str(raw).strip().lower()

    left_fields = list({lf for lf, _ in price_pairs})
    right_fields = list({rf for _, rf in price_pairs})

    left_db = webapp.db_for_table(source_key)
    right_db = webapp.db_for_table(right_table)

    out: dict = {}

    try:
        stream = dataops.iter_all(left_db, source_key,
                                  select=list({on_left} | set(left_fields)))
        next(stream, None)
        for row in stream:
            k = _key(row.get(on_left))
            if not k:
                continue
            entry = out.setdefault(k, {})
            for lf in left_fields:
                v = _to_num(row.get(lf))
                if v is None:
                    continue
                slot = entry.setdefault(lf, {"left": Counter(), "right": Counter()})
                slot["left"][v] += 1
    except Exception as e:
        print(f"  ⚠️  {source_key}.{on_left}: {type(e).__name__}: {e}")

    try:
        stream = dataops.iter_all(right_db, right_table,
                                  select=list({on_right} | set(right_fields)))
        next(stream, None)
        for row in stream:
            k = _key(row.get(on_right))
            if not k:
                continue
            entry = out.setdefault(k, {})
            for lf, rf in price_pairs:
                v = _to_num(row.get(rf))
                if v is None:
                    continue
                slot = entry.setdefault(lf, {"left": Counter(), "right": Counter()})
                slot["right"][v] += 1
    except Exception as e:
        print(f"  ⚠️  {right_table}.{on_right}: {type(e).__name__}: {e}")

    return out


def _match_ratio(left_dist: Counter, right_dist: Counter):
    if not left_dist or not right_dist:
        return None, 0, 0
    common = set(left_dist) & set(right_dist)
    matched = sum(min(left_dist[p], right_dist[p]) for p in common)
    total = max(sum(left_dist.values()), sum(right_dist.values()))
    if total == 0:
        return None, 0, 0
    return matched / total, matched, total


# ── поиск расхождений ─────────────────────────────────────────
def _find_area_diff(item):
    out = None
    for m in item["metrics"]:
        ln = m["metric_left"].lower()
        rn = m["metric_right"].lower()
        if "area" in ln or "area" in rn or "площад" in ln or "площад" in rn:
            pct = m["diff_pct"]
            if pct is None:
                continue
            if out is None or abs(pct) > abs(out):
                out = pct
    return out


def _find_price_diff(item):
    out = None
    for m in item["metrics"]:
        ln = m["metric_left"].lower()
        rn = m["metric_right"].lower()
        if _is_price(ln) or _is_price(rn):
            pct = m["diff_pct"]
            if pct is None:
                continue
            if out is None or abs(pct) > abs(out):
                out = pct
    return out


def classify(item, rows_tol, area_tol, price_threshold):
    """Возвращает (kind, cls).

    kind:
      "problem"  — идёт в основной отчёт
      "discount" — отсеивается в отдельный файл
      None       — ничего интересного
    """
    rows_rc = item.get("rows_rc") or 0
    rows_src = item.get("rows_src") or 0
    rows_diff = abs(rows_rc - rows_src)
    area_pct = _find_area_diff(item)
    price_pct = _find_price_diff(item)

    rows_bad = rows_diff > rows_tol
    area_bad = area_pct is not None and abs(area_pct) > area_tol
    price_bad = price_pct is not None and abs(price_pct) >= price_threshold

    # Ничего значимого.
    if not (rows_bad or area_bad or price_bad):
        return None, None

    # Худший модуль для класса.
    worst = 0.0
    for v in (price_pct, area_pct):
        if v is not None and abs(v) > worst:
            worst = abs(v)
    if rows_bad and rows_src:
        rows_pct = abs(rows_rc - rows_src) / max(rows_src, 1) * 100
        worst = max(worst, rows_pct)
    cls = _class_for_pct(worst, price_threshold)

    # Объём или площадь разошлись — всегда проблема.
    if rows_bad or area_bad:
        return "problem", cls

    # Разошлась только цена. Если доля совпавших лотов высокая —
    # считаем это скидкой на части лотов. Иначе — проблемой.
    matches = item.get("price_matches") or {}
    for info in matches.values():
        if info["ratio"] >= DISCOUNT_MATCH_MIN:
            return "discount", cls
    return "problem", cls


def collect(sources, threshold, specs, rows_tol, area_tol):
    problems, discounts = [], []
    for src_key in sources:
        try:
            rep = webapp._cross_check_report(src_key)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  {src_key}: {type(e).__name__}: {e}")
            continue

        developer = developer_for(src_key, specs)
        metrics = rep.get("metrics") or {}
        price_maps = _build_price_map(src_key, specs)

        for cls_css in ("critical", "warn", "ok", "insufficient"):
            for item_raw in rep["items_by_class"].get(cls_css) or []:
                item_metrics = item_raw.get("metrics") or {}
                per_metric = []
                worst_pct = None
                worst_field = None
                for left_field, right_field in metrics.items():
                    m = item_metrics.get(left_field)
                    if not m:
                        continue
                    pct = m.get("diff_pct")
                    per_metric.append({
                        "metric_left": left_field,
                        "metric_right": right_field,
                        "label": metric_label(left_field),
                        "rc_value": m.get("right"),
                        "src_value": m.get("left"),
                        "diff_abs": m.get("diff_abs"),
                        "diff_pct": pct,
                        "n_rc": m.get("right_n"),
                        "n_src": m.get("left_n"),
                    })
                    if pct is None:
                        continue
                    if worst_pct is None or abs(pct) > abs(worst_pct):
                        worst_pct = pct
                        worst_field = left_field

                jc_key = item_raw.get("key")
                pm = price_maps.get(jc_key, {})
                price_matches = {}
                for left_field, _right_field in metrics.items():
                    if not _is_price(left_field):
                        continue
                    slot = pm.get(left_field)
                    if not slot:
                        continue
                    ratio, matched, total = _match_ratio(
                        slot["left"], slot["right"])
                    if ratio is None:
                        continue
                    price_matches[left_field] = {
                        "ratio": ratio, "matched": matched, "total": total,
                        "label": metric_label(left_field),
                    }

                item = {
                    "source_key": src_key,
                    "developer": developer,
                    "jc_key": jc_key,
                    "jc": item_raw.get("display"),
                    "rows_rc": item_raw.get("right_rows"),
                    "rows_src": item_raw.get("left_rows"),
                    "worst_field": worst_field,
                    "worst_pct": worst_pct,
                    "metrics": per_metric,
                    "price_matches": price_matches,
                }
                kind, cls = classify(item, rows_tol, area_tol, threshold)
                if kind is None:
                    continue
                item["class"] = cls
                if kind == "problem":
                    item["diagnostics"] = diagnose(item)
                    problems.append(item)
                else:
                    item["diagnostics"] = diagnose_discount(item)
                    discounts.append(item)

    order = {"crit": 0, "medium": 1, "small": 2, "ok": 3}
    problems.sort(key=lambda r: (
        order.get(r["class"], 9), r["developer"],
        -abs(r["worst_pct"] or 0)))
    discounts.sort(key=lambda r: (
        order.get(r["class"], 9), r["developer"],
        -abs(r["worst_pct"] or 0)))
    return problems, discounts


# ── таблица в консоль ─────────────────────────────────────────
def _truncate(s: str, width: int) -> str:
    s = str(s or "")
    if len(s) <= width:
        return s
    return s[:width - 1] + "…"


def print_table(rows: list, top: int = 40, title: str = "") -> None:
    if not rows:
        return
    W = {"dev": 12, "jc": 26, "rows": 13, "dpct": 8,
         "match": 12, "diag": 70}
    if title:
        print(f"  {title}")
        print()
    header = (f"  {'застройщик':<{W['dev']}}  {'ЖК':<{W['jc']}}  "
              f"{'строки RC/ист':>{W['rows']}}  {'Δ%':>{W['dpct']}}  "
              f"{'совпало цен':>{W['match']}}  {'что странно':<{W['diag']}}")
    sep = (f"  {'-' * W['dev']}  {'-' * W['jc']}  "
           f"{'-' * W['rows']}  {'-' * W['dpct']}  "
           f"{'-' * W['match']}  {'-' * W['diag']}")
    print(header)
    print(sep)
    shown = 0
    for r in rows:
        if shown >= top:
            print(f"  … ещё {len(rows) - top} (см. CSV для полного списка)")
            break
        shown += 1
        rows_s = f"{r['rows_rc']} / {r['rows_src']}"
        dpct_s = _fmt_pct(r.get("worst_pct")) if r.get("worst_pct") is not None else "—"
        matches = r.get("price_matches") or {}
        match_s = "—"
        if matches:
            first = next(iter(matches.values()))
            match_s = f"{first['matched']} / {first['total']}"
        diag_s = "; ".join(r.get("diagnostics") or []) or "—"
        print(f"  {_truncate(r['developer'], W['dev']):<{W['dev']}}  "
              f"{_truncate(r['jc'], W['jc']):<{W['jc']}}  "
              f"{rows_s:>{W['rows']}}  "
              f"{dpct_s:>{W['dpct']}}  "
              f"{match_s:>{W['match']}}  "
              f"{_truncate(diag_s, W['diag']):<{W['diag']}}")
    print(sep)
    print()


# ── CSV ───────────────────────────────────────────────────────
def write_csv(rows, path: Path, keep_source_key: bool) -> None:
    header = ["застройщик"]
    if keep_source_key:
        header.append("ключ источника")
    header += [
        "ЖК", "класс",
        "строк RC", "строк источника", "Δ строк",
        "худший Δ%", "худшая метрика",
        "совпало цен (лотов)", "всего цен (лотов)", "доля совпадения, %",
        "что странно",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(header)
        for r in rows:
            rows_rc = r.get("rows_rc") or 0
            rows_src = r.get("rows_src") or 0
            matches = r.get("price_matches") or {}
            if matches:
                first = next(iter(matches.values()))
                matched = first["matched"]
                total = first["total"]
                share = f"{first['ratio'] * 100:.1f}".replace(".", ",")
            else:
                matched = total = share = ""
            diag = "; ".join(r.get("diagnostics") or [])
            line = [r["developer"]]
            if keep_source_key:
                line.append(r["source_key"])
            line += [
                r["jc"],
                _class_label(r["class"]),
                rows_rc, rows_src, f"{rows_rc - rows_src:+}",
                _fmt_pct(r["worst_pct"]),
                metric_label(r["worst_field"] or ""),
                matched, total, share,
                diag,
            ]
            w.writerow(line)


# ── Markdown ──────────────────────────────────────────────────
def write_markdown(problems, discounts, path, threshold, rows_tol, area_tol,
                   sources, mapping_warns) -> None:
    lines = []
    lines.append("# Проблемные ЖК")
    lines.append("")
    lines.append(f"Порог по цене: |Δ%| ≥ {threshold:g}")
    lines.append(f"Допуск по строкам: {rows_tol}")
    lines.append(f"Допуск по площади: {area_tol:g}%")
    lines.append(f"Сформировано: {datetime.now():%d.%m.%Y %H:%M}")
    lines.append("")
    lines.append("## Что считается проблемой")
    lines.append("")
    lines.append("- расхождение по количеству лотов;")
    lines.append("- расхождение по площади;")
    lines.append("- расхождение по цене со скидкой, при котором совпадает "
                 "меньше половины лотов.")
    lines.append("")
    lines.append("Если объём и площадь совпали, а цены не совпали лишь у "
                 "части лотов (совпало ≥ 50%) — это скидка на части "
                 "выборки, а не проблема. Такие ЖК идут в отдельный файл.")
    lines.append("")

    bad_maps = {k: v for k, v in mapping_warns.items() if v}
    if bad_maps:
        lines.append("## ⚠️ Маппинг цен подозрительный")
        lines.append("")
        for src, warns in bad_maps.items():
            lines.append(f"**{src}**")
            for w in warns:
                lines.append(f"- {w}")
            lines.append("")

    if not problems:
        lines.append("## Результат")
        lines.append("")
        lines.append("✅ Проблем не найдено.")
    else:
        lines.append(f"## Проблемы ({len(problems)} ЖК)")
        lines.append("")
        lines.append("| застройщик | ЖК | строки RC/ист | Δ% | совпало цен | что странно |")
        lines.append("|---|---|---:|---:|---:|---|")
        for r in problems:
            matches = r.get("price_matches") or {}
            if matches:
                first = next(iter(matches.values()))
                match_s = f"{first['matched']} / {first['total']}"
            else:
                match_s = "—"
            dpct = _fmt_pct(r.get("worst_pct")) if r.get("worst_pct") is not None else "—"
            diag = "; ".join(r.get("diagnostics") or []) or "—"
            rows_s = f"{r['rows_rc']} / {r['rows_src']}"
            lines.append(
                f"| {r['developer']} | {r['jc']} | {rows_s} | {dpct} | "
                f"{match_s} | {diag} |")
        lines.append("")
        lines.append("## Детали")
        lines.append("")
        for r in problems:
            icon = _md_icon(r["class"])
            lines.append(f"### {icon} [{r['developer']}] {r['jc']}")
            lines.append("")
            lines.append(f"- строк: RC {r['rows_rc']} / источник {r['rows_src']}")
            for d in (r.get("diagnostics") or []):
                lines.append(f"- {d}")
            lines.append("")
            lines.append("| метрика | RC | источник | Δ абс | Δ% |")
            lines.append("|---|---:|---:|---:|---:|")
            for m in r["metrics"]:
                pct = m["diff_pct"]
                pct_s = _fmt_pct(pct) if pct is not None else "—"
                lines.append(
                    f"| {m['label']} | {_fmt_val(m['rc_value'])} | "
                    f"{_fmt_val(m['src_value'])} | "
                    f"{_fmt_abs(m['diff_abs'])} | {pct_s} |")
            lines.append("")

    if discounts:
        lines.append(f"## Скидочные расхождения ({len(discounts)} ЖК)")
        lines.append("")
        lines.append("Объём и площадь совпали, цены не совпали лишь у части "
                     "лотов. Это скидка на части выборки, в отчёт проблем "
                     "не идёт. Полный список — в `*_discounts.csv`.")
        lines.append("")
        lines.append("| застройщик | ЖК | строки RC/ист | Δ% | совпало цен |")
        lines.append("|---|---|---:|---:|---:|")
        for r in discounts:
            matches = r.get("price_matches") or {}
            if matches:
                first = next(iter(matches.values()))
                match_s = f"{first['matched']} / {first['total']}"
            else:
                match_s = "—"
            dpct = _fmt_pct(r.get("worst_pct")) if r.get("worst_pct") is not None else "—"
            rows_s = f"{r['rows_rc']} / {r['rows_src']}"
            lines.append(
                f"| {r['developer']} | {r['jc']} | {rows_s} | {dpct} | {match_s} |")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Проблемные ЖК: цены со скидкой на обеих сторонах.")
    ap.add_argument("--threshold", type=float, default=2.0,
                    help="порог |Δ%%| по цене (по умолчанию 2)")
    ap.add_argument("--rows-tolerance", type=int, default=1,
                    help="допуск по числу строк (по умолчанию 1)")
    ap.add_argument("--area-tolerance", type=float, default=1.0,
                    help="допуск по площади, %% (по умолчанию 1)")
    ap.add_argument("--source", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--keep-source-key", action="store_true")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--strict-mapping", action="store_true",
                    help="выход с кодом 2 при подозрительном маппинге цен")
    ap.add_argument("--no-price-match", action="store_true",
                    help="не считать распределение цен по лотам")
    args = ap.parse_args()

    specs = webapp.load_specs()
    all_with_cc = sorted(k for k, s in specs.items()
                         if getattr(s, "cross_check", None))

    if args.source:
        if args.source not in all_with_cc:
            print(f"❌ Источник {args.source!r} не найден или без cross_check.")
            print(f"   Доступные: {', '.join(all_with_cc) or '—'}")
            return 1
        sources = [args.source]
    else:
        sources = all_with_cc

    if not sources:
        print("❌ Нет источников с cross_check.")
        return 1

    mapping_warns = {k: check_mapping(k, specs) for k in sources}
    bad_maps = {k: v for k, v in mapping_warns.items() if v}

    print("=" * 78)
    print("  ПРОБЛЕМНЫЕ ЖК — цены со скидкой на обеих сторонах")
    print("=" * 78)
    print(f"  Источников:              {len(sources)}")
    print(f"  Порог по цене:           |Δ%| ≥ {args.threshold:g}")
    print(f"  Допуск по строкам:       {args.rows_tolerance}")
    print(f"  Допуск по площади:       {args.area_tolerance:g}%")
    print(f"  Совпадение цен по лотам: "
          f"{'выключено' if args.no_price_match else 'считается'}")
    print()

    if bad_maps:
        print("  ⚠️  МАППИНГ ЦЕН ПОДОЗРИТЕЛЬНЫЙ:")
        for src, warns in bad_maps.items():
            print(f"    • {src}")
            for w in warns:
                print(f"        {w}")
        print()
        if args.strict_mapping:
            print("  --strict-mapping: выход с кодом 2.")
            return 2
        print("  Продолжаю. Цифры выше могут быть фиктивными.")
        print()
    else:
        print("  ✅ Маппинг цен согласован (или вид определить не удалось).")
        print()

    problems, discounts = collect(
        sources, args.threshold, specs,
        args.rows_tolerance, args.area_tolerance)

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    base = Path(args.out) if args.out else (HERE / "reports" / f"problem_jc_{ts}")
    base.parent.mkdir(parents=True, exist_ok=True)
    csv_path = base.with_suffix(".csv")
    md_path = base.with_suffix(".md")
    disc_path = base.with_name(base.name + "_discounts").with_suffix(".csv")

    write_csv(problems, csv_path, args.keep_source_key)
    write_csv(discounts, disc_path, args.keep_source_key)
    write_markdown(problems, discounts, md_path, args.threshold,
                   args.rows_tolerance, args.area_tolerance,
                   sources, mapping_warns)

    print(f"  📄 CSV проблем:      {csv_path}")
    print(f"  📝 Markdown:         {md_path}")
    print(f"  📄 CSV скидок:       {disc_path}")
    print()
    print(f"  Всего проблем:       {len(problems)}")
    print(f"  Всего скидочных:     {len(discounts)}")
    print()

    if problems:
        print_table(problems, top=args.top, title="🔧 ПРОБЛЕМЫ:")
    else:
        print("  ✅ Проблем не найдено.")
    print()

    if discounts:
        print_table(discounts, top=min(args.top, 15),
                    title="💸 СКИДКИ (в отчёт проблем не идут):")
    print()
    print("✅ Готово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())