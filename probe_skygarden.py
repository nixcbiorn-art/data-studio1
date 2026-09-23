"""Проверка Скай Гардена через /flat-groups: уникальность и типы лотов."""
import requests
from collections import Counter

LIMIT = 100
PAGE_MAX = 20
slug = "skygarden"

seen_ids = set()
kinds = Counter()
total_groups = 0
total_flats = 0
page = 1

while True:
    d = requests.get(
        "https://fsk.ru/api/v3/flat-groups",
        params={"city": 1, "limit": LIMIT, "page": page, "complex.slug": slug},
        timeout=20,
    ).json()
    it = d.get("items") or []
    n_this = 0
    for g in it:
        for f in g.get("items") or []:
            seen_ids.add(f.get("externalId"))
            kinds[f.get("kind")] += 1
            n_this += 1
    total_groups += len(it)
    total_flats += n_this
    print(f"стр {page}: {len(it)} групп / {n_this} квартир "
          f"(hasNextPage={d.get('hasNextPage')})")

    page += 1
    if not d.get("hasNextPage") or page > PAGE_MAX:
        break

print()
print(f"ИТОГО строк:            {total_flats}")
print(f"Уникальных externalId:  {len(seen_ids)}")
print(f"Разбивка по kind:       {dict(kinds)}")