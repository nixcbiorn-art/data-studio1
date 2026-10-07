"""Собирает список всех ЖК ФСК по Москве через /api/v3/flats.

Список нужен один раз, чтобы потом фильтровать /flats по каждому ЖК
отдельно (project_slug). Результат — JSON вида:
    {"items": [{"slug": "...", "title": "...", "_id": "..."}, ...]}
Пишется рядом — можно сразу положить в reports/.
"""
import json
import time
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / "reports" / "fsk_complexes_list.json"
OUT.parent.mkdir(exist_ok=True)

BASE = "https://fsk.ru/api/v3/flats"
LIMIT = 100
MAX_PAGES = 50

seen = {}   # slug -> {slug, title, _id}
page = 1

while page <= MAX_PAGES:
    r = requests.get(
        BASE,
        params={"city": 1, "limit": LIMIT, "page": page,
                "sort": "price", "order": 1},
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    items = d.get("items") or []
    if not items:
        break

    for it in items:
        proj = it.get("project") or {}
        slug = proj.get("slug")
        if not slug:
            continue
        if slug not in seen:
            seen[slug] = {
                "slug": slug,
                "title": proj.get("title") or slug,
                "_id": proj.get("_id") or "",
            }

    total_pages = d.get("totalPages") or 0
    print(f"стр {page}/{total_pages}: собрано уникальных ЖК: {len(seen)}")

    if not d.get("hasNextPage"):
        break
    page += 1
    time.sleep(0.5)

result = {"items": sorted(seen.values(), key=lambda x: x["slug"])}
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2),
               encoding="utf-8")

print()
print(f"Всего ЖК: {len(result['items'])}")
print(f"Записано: {OUT}")
for x in result["items"][:20]:
    print(f"  {x['slug']:30} {x['title']}")
if len(result["items"]) > 20:
    print(f"  … ещё {len(result['items']) - 20}")