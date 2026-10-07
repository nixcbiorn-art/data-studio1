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

import sys as _sys
import argparse
import sys
from datetime import datetime
from pathlib import Path
from redcat.web import webapp
from redcat.reporting.ex_collect import collect
from redcat.reporting.ex_config import HERE
from redcat.reporting.ex_mapping import check_mapping
from redcat.reporting.ex_output import print_table, write_csv, write_markdown


for _stream in (_sys.stdout, _sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass


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
