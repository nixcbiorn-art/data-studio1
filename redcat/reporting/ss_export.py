"""ss_export — выгрузка статистики в CSV и JSON."""
from __future__ import annotations

import csv
import json
from pathlib import Path


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
