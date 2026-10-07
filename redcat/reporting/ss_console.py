"""ss_console — вывод статистики в консоль."""
from __future__ import annotations

from redcat.reporting.ss_config import DELTA_CRIT, DELTA_OK, DELTA_WARN


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
