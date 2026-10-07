"""scrape_snapshot — снимки данных и их сравнение."""
from __future__ import annotations

import json
import logging
from redcat.collection.scrape_config import HISTORY_DIR


# ──────────────────────────────────────────────────────────────
#  СНАПШОТЫ И СРАВНЕНИЕ
# ──────────────────────────────────────────────────────────────
def load_snapshot(name):
    path = HISTORY_DIR / f"{name}.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logging.warning("Снапшот %s не прочитан: %s", name, e)
    return {}


def save_snapshot(name, snapshot):
    with open(HISTORY_DIR / f"{name}.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, default=str)


def compare_snapshots(old, new_rows, spec):
    label = spec.title or spec.key
    new_snap = {str(r[spec.id_field]): r for r in new_rows
                if r.get(spec.id_field) is not None}
    old_ids, new_ids = set(old), set(new_snap)
    changes = []

    for rid in sorted(new_ids - old_ids):
        changes.append({"Категория": f"{label}: Новая запись", "ID": rid,
                        "Название": new_snap[rid].get(spec.name_field),
                        "Поле": "", "Было": "", "Стало": ""})
    for rid in sorted(old_ids - new_ids):
        changes.append({"Категория": f"{label}: Запись пропала", "ID": rid,
                        "Название": old[rid].get(spec.name_field),
                        "Поле": "", "Было": "", "Стало": ""})
    for rid in sorted(old_ids & new_ids):
        for fld in spec.track_fields:
            o, n = old[rid].get(fld), new_snap[rid].get(fld)
            if str(o) != str(n):
                changes.append({"Категория": f"{label}: Изменение поля", "ID": rid,
                                "Название": new_snap[rid].get(spec.name_field),
                                "Поле": fld, "Было": o, "Стало": n})
    return changes, new_snap
