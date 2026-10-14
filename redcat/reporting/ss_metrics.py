"""ss_metrics — классы, квартили, гистограммы и сводки по метрикам."""
from __future__ import annotations

import statistics
from redcat.reporting.ss_config import DELTA_CRIT, DELTA_OK, DELTA_WARN
from redcat.reporting.ss_developers import aggregate_by_developer


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
