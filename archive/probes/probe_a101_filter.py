"""Перебор возможных имён параметра фильтра по ЖК на a101.ru."""
import requests

BASE = "https://a101.ru/api/flats/"
TARGET = "desnarechie"
EXPECTED_TOTAL = 4566   # без фильтра

candidates = [
    {"project": TARGET},
    {"project__slug": TARGET},
    {"project_slug__in": TARGET},
    {"project_group": TARGET},
    {"project_group__slug": TARGET},
    {"project_id": TARGET},
    {"filter[project_slug]": TARGET},
    {"filter[project]": TARGET},
    {"project_slug[]": TARGET},
    {"project.name": TARGET},
    {"slug": TARGET},
]

for params in candidates:
    p = dict(params)
    p["limit"] = 1
    try:
        r = requests.get(BASE, params=p, timeout=20).json()
        total = r.get("count")
        marker = "✅" if total and total != EXPECTED_TOTAL else "  "
        print(f"{marker} {list(params.keys())[0]:30} → count={total}")
    except Exception as e:
        print(f"   {list(params.keys())[0]:30} → ошибка {e}")