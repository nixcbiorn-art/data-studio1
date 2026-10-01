"""Сколько лотов из 542 скрыл бы фильтр 'не Сдана'."""
import requests
from collections import Counter

slug = "skygarden"
total = 0
sold = 0
by_label = Counter()

page = 1
while True:
    d = requests.get(
        "https://fsk.ru/api/v3/flat-groups",
        params={"city": 1, "limit": 100, "page": page, "complex.slug": slug},
        timeout=20,
    ).json()
    for g in d.get("items") or []:
        for f in g.get("items") or []:
            total += 1
            labels = [l.get("title") for l in (f.get("labels") or [])]
            for t in labels:
                by_label[t] += 1
            if "Сдана" in labels:
                sold += 1
    page += 1
    if not d.get("hasNextPage"):
        break

print(f"всего лотов:        {total}")
print(f"с ярлыком Сдана:    {sold}")
print(f"без ярлыка Сдана:   {total - sold}")
print(f"top labels:         {by_label.most_common(10)}")