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
import sys
import webbrowser
from pathlib import Path
from redcat.reporting.ss_collect import all_sources_with_cross_check, collect
from redcat.reporting.ss_config import DELTA_OK, DELTA_WARN
from redcat.reporting.ss_console import print_short_table, print_source_detail
from redcat.reporting.ss_export import write_csv, write_json
from redcat.reporting.ss_html_page import render_html_dashboard
from redcat.reporting.ss_metrics import summarize


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
