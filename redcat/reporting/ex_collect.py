"""ex_collect — сбор проблемных ЖК по источнику."""
from __future__ import annotations

from redcat.web import webapp
from redcat.reporting.ex_classify import classify
from redcat.reporting.ex_diagnose import diagnose
from redcat.reporting.ex_format import developer_for, metric_label
from redcat.reporting.ex_mapping import _is_price
from redcat.reporting.ex_prices import _build_price_map, _match_ratio


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
                kind, cls = classify(item, rows_tol, area_tol, threshold, specs.get(src_key))
                if kind is None:
                    continue
                item["class"] = cls
                diagnostics = diagnose(item, specs.get(src_key))
                if not diagnostics:
                    wf = item.get("worst_field")
                    wp = item.get("worst_pct")
                    lf, rf = item.get("left_rows", 0), item.get("right_rows", 0)
                    rd = (lf or 0) - (rf or 0)
                    if wf and wp is not None:
                        diagnostics.append(
                            f"расхождение по «{metric_label(wf)}»: {wp:+.1f}%")
                    if rd:
                        base = max(lf or 0, rf or 0) or 1
                        diagnostics.append(
                            f"разница лотов: {rd:+d} ({rd / base * 100:+.1f}%)")
                    if not diagnostics:
                        diagnostics.append("попал по совокупности метрик")
                item["diagnostics"] = diagnostics
                if kind == "problem":
                    problems.append(item)
                else:
                    discounts.append(item)

    order = {"crit": 0, "medium": 1, "small": 2, "ok": 3}
    problems.sort(key=lambda r: (
        order.get(r["class"], 9), r["developer"],
        -abs(r["worst_pct"] or 0)))
    discounts.sort(key=lambda r: (
        order.get(r["class"], 9), r["developer"],
        -abs(r["worst_pct"] or 0)))
    return problems, discounts
