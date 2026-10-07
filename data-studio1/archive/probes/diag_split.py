"""
diag_split.py — разбор сплита по developer_id из журнала API.
==============================================================
Только читает studio.db. Ничего не меняет.

Разбирает URL'ы запросов listing_apartments, вытаскивает значение
filter[developer_id] и считает, сколько запросов пришлось на каждого
застройщика. По числу запросов можно оценить, у кого сколько лотов
и есть ли «пустые» значения, ради которых сбор гоняет API впустую.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

BASE = Path(__file__).resolve().parent
STUDIO_DB = BASE / "studio.db"
SEP = "─" * 74


def open_ro(path: Path):
    if not path.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def fmt(n):
    if n is None:
        return "—"
    return f"{n:,}".replace(",", " ")


def extract_dev_id(url: str):
    """
    Из URL вида:
      ...listing_apartments?filter[country_id]=..&filter[developer_id]=224&...
      ...listing_apartments?filter[developer_id]=596&page[number]=2
      ...listing_apartments?filter%5Bdeveloper_id%5D=763&...
    Достать значение 224/596/763.
    """
    # декодируем %5B → [ чтобы не писать два регулярных выражения
    decoded = unquote(url)
    m = re.search(r"filter\[developer_id\]=([^&]+)", decoded)
    if m:
        return m.group(1)
    return None


def main() -> int:
    conn = open_ro(STUDIO_DB)
    if conn is None:
        print("❌ studio.db не найден.")
        return 1

    rows = conn.execute("""
        SELECT url, ms, status
        FROM api_audit
        WHERE url LIKE '%listing_apartments%'
          AND url LIKE '%filter[developer_id]%'
    """).fetchall()
    # также попробуем закодированный вариант
    rows2 = conn.execute("""
        SELECT url, ms, status
        FROM api_audit
        WHERE url LIKE '%listing_apartments%'
          AND url LIKE '%filter%5Bdeveloper_id%5D%'
    """).fetchall()

    all_rows = {r["url"]: r for r in rows}
    for r in rows2:
        all_rows.setdefault(r["url"], r)

    if not all_rows:
        print("⚠️  В журнале нет запросов с filter[developer_id].")
        conn.close()
        return 0

    # группируем по значению developer_id
    per_dev = defaultdict(lambda: {"requests": 0, "ms_total": 0, "errors": 0})
    others = 0
    for r in all_rows.values():
        dev_id = extract_dev_id(r["url"])
        if dev_id is None:
            others += 1
            continue
        key = dev_id
        per_dev[key]["requests"] += 1
        per_dev[key]["ms_total"] += r["ms"] or 0
        if r["status"] is None or r["status"] >= 400:
            per_dev[key]["errors"] += 1

    total_devs = len(per_dev)
    total_requests = sum(v["requests"] for v in per_dev.values())

    print(f"\n{SEP}\n  РАЗБОР СПЛИТА ПО developer_id\n{SEP}")
    print(f"  Уникальных developer_id в журнале: {total_devs}")
    print(f"  Всего запросов с фильтром:        {fmt(total_requests)}")
    if others:
        print(f"  Запросов без разобранного фильтра: {fmt(others)}")
    print(f"  Среднее запросов на застройщика:  "
          f"{total_requests / max(total_devs, 1):.1f}")

    # сортируем по числу запросов: сверху — «жирные»
    ordered = sorted(per_dev.items(), key=lambda kv: -kv[1]["requests"])

    print(f"\n  Топ-20 застройщиков по числу запросов "
          f"(≈ сотни записей = десятки страниц):")
    print(f"  {'dev_id':>10}  {'запросов':>9}  {'ср. мс':>8}  {'ошибок':>7}")
    for dev_id, v in ordered[:20]:
        avg_ms = v["ms_total"] // max(v["requests"], 1)
        print(f"  {str(dev_id):>10}  {v['requests']:>9}  {avg_ms:>8}  "
              f"{v['errors']:>7}")

    # распределение по «размеру»
    print(f"\n  Распределение значений по числу запросов:")
    buckets = [
        ("1 запрос (пустые?)", 1, 1),
        ("2 запроса (1 страница + пагинация)", 2, 2),
        ("3–5 запросов", 3, 5),
        ("6–20 запросов", 6, 20),
        ("21–100 запросов", 21, 100),
        ("более 100 запросов", 101, 10**9),
    ]
    for label, lo, hi in buckets:
        n = sum(1 for v in per_dev.values() if lo <= v["requests"] <= hi)
        if n:
            bar = "█" * min(40, n)
            print(f"    {label:<38}  {n:>5}  {bar}")

    # самое подозрительное — значения с 1 запросом (пустые)
    empties = [(k, v) for k, v in ordered if v["requests"] <= 2]
    if empties:
        print(f"\n  ⚠️  Значения с 1–2 запросами "
              f"(вероятно, у застройщика нет лотов или фильтр не находит):")
        for dev_id, v in empties[:30]:
            print(f"    dev_id={dev_id}  запросов={v['requests']}  "
                  f"ошибок={v['errors']}")
        if len(empties) > 30:
            print(f"    … и ещё {len(empties) - 30}")

    # суммарное число страниц ≈ число запросов минус пагинационные промахи
    print(f"\n  Оценка:")
    print(f"    Если API заявляет 58 159 квартир, а запросов всего "
          f"{fmt(total_requests)},")
    print(f"    то в среднем это {58159 / max(total_requests, 1):.1f} записей "
          f"на запрос.")

    # сколько записей реально в apartments
    redcat_db = BASE / "reports" / "redcat_data.db"
    if redcat_db.exists():
        try:
            rc = sqlite3.connect(f"file:{redcat_db}?mode=ro", uri=True)
            n = rc.execute("SELECT COUNT(*) FROM apartments").fetchone()[0]
            rc.close()
            print(f"\n    Фактически в apartments сейчас: {fmt(n)}")
            print(f"    Разница с API: {fmt(58159 - n)}")
        except sqlite3.Error as e:
            print(f"    Не смог прочитать apartments: {e}")

    conn.close()
    print(f"\n{SEP}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())