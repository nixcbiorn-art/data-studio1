"""ex_output — вывод: таблица в консоль, CSV и Markdown."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from redcat.reporting.ex_format import _class_label, _fmt_abs, _fmt_pct, _fmt_val, _md_icon, metric_label


# ── таблица в консоль ─────────────────────────────────────────
def _truncate(s: str, width: int) -> str:
    s = str(s or "")
    if len(s) <= width:
        return s
    return s[:width - 1] + "…"


def print_table(rows: list, top: int = 40, title: str = "") -> None:
    if not rows:
        return
    W = {"dev": 12, "jc": 26, "rows": 13, "drow": 7,
         "dpct": 10, "match": 12, "diag": 60}
    if title:
        print(f"  {title}")
        print()
    header = (f"  {'застройщик':<{W['dev']}}  {'ЖК':<{W['jc']}}  "
              f"{'строки RC/ист':>{W['rows']}}  "
              f"{'Δ стр':>{W['drow']}}  "
              f"{'Δ% метрики':>{W['dpct']}}  "
              f"{'совпало цен':>{W['match']}}  "
              f"{'что странно':<{W['diag']}}")
    sep = (f"  {'-' * W['dev']}  {'-' * W['jc']}  "
           f"{'-' * W['rows']}  {'-' * W['drow']}  "
           f"{'-' * W['dpct']}  "
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
        drow_s = f"{(r['rows_rc'] or 0) - (r['rows_src'] or 0):+d}"
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
              f"{drow_s:>{W['drow']}}  "
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
