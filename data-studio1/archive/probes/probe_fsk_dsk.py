"""Сравнение проектов и лотов FSK-канала и DSK-канала."""
import requests
import time
from collections import Counter

BASE = "https://fsk.ru/api/v3/flats"
LIMIT = 100


def collect_projects_and_ids(client):
    """Идёт по всему каталогу канала, собирает проекты и externalId."""
    projects = set()
    ids = set()
    page = 1
    while page <= 50:
        d = requests.get(BASE, params={
            "city": 1, "limit": LIMIT, "page": page, "client": client,
        }, timeout=30).json()
        items = d.get("items") or []
        if not items:
            break
        for it in items:
            proj = (it.get("project") or {}).get("slug")
            ext = it.get("externalId")
            if proj:
                projects.add(proj)
            if ext:
                ids.add(str(ext))
        if not d.get("hasNextPage"):
            break
        page += 1
        time.sleep(0.3)
    return projects, ids


fsk_projects, fsk_ids = collect_projects_and_ids("FSK")
dsk_projects, dsk_ids = collect_projects_and_ids("DSK")

print(f"FSK:  {len(fsk_projects)} проектов, {len(fsk_ids)} уникальных externalId")
print(f"DSK:  {len(dsk_projects)} проектов, {len(dsk_ids)} уникальных externalId")
print()
print(f"Проекты только в FSK: {sorted(fsk_projects - dsk_projects)}")
print(f"Проекты только в DSK: {sorted(dsk_projects - fsk_projects)}")
print(f"Проекты в обоих:      {sorted(fsk_projects & dsk_projects)}")
print()
print(f"externalId только FSK:  {len(fsk_ids - dsk_ids)}")
print(f"externalId только DSK:  {len(dsk_ids - fsk_ids)}")
print(f"externalId в обоих:     {len(fsk_ids & dsk_ids)}")
print()
print(f"ВСЕГО уникальных лотов: {len(fsk_ids | dsk_ids)}")