"""
show_fields.py — показать все поля, которые API Redcat отдаёт в лоте.
======================================================================
Один GET на listing_apartments, разбор одной записи. Только читает,
ничего не меняет, в сеть ходит исключительно методом GET.

Что делает:
  1. Забирает 2 лота по конкретному ЖК (по умолчанию «Архитектор»).
  2. Печатает ВСЕ поля первой записи: имя, тип, значение.
  3. Отдельно печатает поля со словами price/discount/area/sale —
     именно они отвечают за цену и площадь.
  4. Считает, сколько раз каждое поле встречается в 100 записях —
     чтобы увидеть, что заполнено, а что приходит пустым.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
except ImportError:
    pass

TOKEN = os.environ.get("REDCAT_TOKEN", "").strip()
if not TOKEN:
    print("❌ REDCAT_TOKEN не найден в .env")
    sys.exit(1)

REGION_ID = os.environ.get("REDCAT_REGION_ID", "20003956")
COUNTRY_ID = os.environ.get("REDCAT_COUNTRY_ID", "2017370")

# ЖК «Архитектор» — 15399 или 535? Из ваших логов — 535, 15399, 15222.
# Берём все три как fallback, но основной — 535 (он стабильно давал данные).
HOUSING_COMPLEX_ID = os.environ.get("RC_TEST_HC_ID", "535")

URL = (
    "https://api.redcat.ai/api/v2/estate/housing_complexes/listing_apartments"
    f"?filter[country_id]={COUNTRY_ID}"
    f"&filter[region_id]={REGION_ID}"
    f"&filter[active_in_admin]=true"
    f"&sort=price_asc"
    f"&page[size]=100&page[number]=1"
)

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
})


# ──────────────────────────────────────────────────────────────
def fetch_all() -> list:
    """Забирает первую страницу лотов (100 записей) без фильтра по ЖК."""
    print(f"📡 GET {URL[:110]}...")
    try:
        r = session.get(URL, timeout=30)
    except Exception as e:
        print(f"❌ Запрос упал: {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"   HTTP {r.status_code}")
    if r.status_code != 200:
        print(f"   тело ответа: {r.text[:500]}")
        sys.exit(1)
    data = r.json()
    items = data.get("data") or data.get("items") or []
    if not isinstance(items, list):
        print(f"❌ в ответе нет массива записей.")
        print(f"   доступные ключи: {list(data.keys())}")
        sys.exit(1)
    return items


def describe(value):
    """Тип + значение для печати."""
    if value is None:
        return "None", "—"
    if isinstance(value, bool):
        return "bool", str(value)
    if isinstance(value, (int, float)):
        return type(value).__name__, str(value)
    if isinstance(value, str):
        s = value if len(value) < 80 else value[:77] + "…"
        return "str", repr(s)
    if isinstance(value, dict):
        keys = ", ".join(list(value.keys())[:6])
        return "dict", f"{{{keys}{'...' if len(value) > 6 else ''}}}"
    if isinstance(value, list):
        if not value:
            return "list", "[]"
        first_type = type(value[0]).__name__
        return "list", f"[{len(value)} × {first_type}]"
    return type(value).__name__, repr(value)


# ──────────────────────────────────────────────────────────────
def section_all_fields(items: list) -> None:
    print()
    print("=" * 78)
    print("  ВСЕ ПОЛЯ ПЕРВОЙ ЗАПИСИ")
    print("=" * 78)
    if not items:
        print("  нет записей.")
        return
    row = items[0]
    fields = list(row.keys())
    print(f"  Полей в записи: {len(fields)}\n")
    for k in fields:
        t, v = describe(row.get(k))
        print(f"  {k:<48}  {t:<10}  {v}")


def section_price_fields(items: list) -> None:
    print()
    print("=" * 78)
    print("  ПОЛЯ, СВЯЗАННЫЕ С ЦЕНОЙ / СКИДКОЙ")
    print("=" * 78)
    if not items:
        return

    keywords = ("price", "discount", "sale", "cost", "amount")
    found_keys = set()
    for row in items[:100]:
        for k in row.keys():
            if any(w in k.lower() for w in keywords):
                found_keys.add(k)

    if not found_keys:
        print("  (ни одного поля с price/discount/sale/cost/amount)")
        return

    print(f"  Найдено полей: {len(found_keys)}\n")

    # берём первую запись, но если поле пустое — ищем заполненное
    for k in sorted(found_keys):
        # ищем первую непустую запись
        val = None
        for row in items:
            if row.get(k) not in (None, ""):
                val = row.get(k)
                break
        t, v = describe(val)
        print(f"  {k:<48}  {t:<10}  {v}")


def section_area_fields(items: list) -> None:
    print()
    print("=" * 78)
    print("  ПОЛЯ, СВЯЗАННЫЕ С ПЛОЩАДЬЮ")
    print("=" * 78)
    if not items:
        return

    keywords = ("area", "space", "square", "sqm", "metr")
    found_keys = set()
    for row in items[:100]:
        for k in row.keys():
            if any(w in k.lower() for w in keywords):
                found_keys.add(k)

    if not found_keys:
        print("  (ни одного поля с area/space/square/sqm)")
        return

    print(f"  Найдено полей: {len(found_keys)}\n")
    for k in sorted(found_keys):
        val = None
        for row in items:
            if row.get(k) not in (None, ""):
                val = row.get(k)
                break
        t, v = describe(val)
        print(f"  {k:<48}  {t:<10}  {v}")


def section_stats(items: list) -> None:
    print()
    print("=" * 78)
    print("  ЗАПОЛНЕННОСТЬ ПОЛЕЙ (на выборке)")
    print("=" * 78)
    if not items:
        return
    n = len(items)
    counter = Counter()
    types = defaultdict(Counter)
    for row in items:
        for k, v in row.items():
            if v not in (None, "", [], {}):
                counter[k] += 1
            types[k][type(v).__name__] += 1

    # сортируем по заполненности
    ordered = sorted(types.keys(),
                     key=lambda k: -counter[k] / n)
    print(f"  Выборка: {n} записей\n")
    print(f"  {'поле':<48}  {'заполнено':>10}  {'осн. тип':>10}")
    for k in ordered:
        pct = 100.0 * counter[k] / n
        dom_type = types[k].most_common(1)[0][0] if types[k] else "—"
        print(f"  {k:<48}  {pct:>9.1f}%  {dom_type:>10}")


def main() -> int:
    print("=" * 78)
    print("  РАЗБОР ПОЛЕЙ API Redcat — listing_apartments")
    print("=" * 78)
    items = fetch_all()
    print(f"  Записей в ответе: {len(items)}")
    section_all_fields(items)
    section_price_fields(items)
    section_area_fields(items)
    section_stats(items)
    print()
    print("=" * 78)
    print("  ГОТОВО. Пришлите вывод — по нему видно, какие поля брать в spec.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())