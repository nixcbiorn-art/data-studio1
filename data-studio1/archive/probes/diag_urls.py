"""
diag_urls.py — проверка: повторяются ли URL'ы у жирных застройщиков.
=====================================================================
Только читает studio.db. Ничего не меняет.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

BASE = Path(__file__).resolve().parent
STUDIO_DB = BASE / "studio.db"


def open_ro(p):
    if not p.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def page_of(url: str):
    d = unquote(url)
    m = re.search(r"page\[number\]=(\d+)", d)
    if m:
        return int(m.group(1))
    m = re.search(r"offset=(\d+)", d)
    if m:
        return int(m.group(1))
    return None


def main():
    conn = open_ro(STUDIO_DB)
    if conn is None:
        print("❌ studio.db не найден.")
        return 1

    for dev_id in ("209", "261", "936"):
        rows = conn.execute("""
            SELECT url FROM api_audit
            WHERE (url LIKE ? OR url LIKE ?)
        """, (f"%developer_id]={dev_id}%",
              f"%developer_id%5D={dev_id}%")).fetchall()

        if not rows:
            print(f"\ndev_id={dev_id}: записей нет")
            continue

        pages = Counter()
        full_urls = Counter()
        for r in rows:
            p = page_of(r["url"])
            if p is not None:
                pages[p] += 1
            full_urls[r["url"]] += 1

        total = len(rows)
        unique_urls = len(full_urls)
        max_page = max(pages) if pages else None
        repeats = [(u, n) for u, n in full_urls.items() if n > 1]
        max_repeat = max(n for _, n in repeats) if repeats else 1

        print(f"\n── dev_id={dev_id} ──")
        print(f"  Запросов всего:        {total}")
        print(f"  Уникальных URL:        {unique_urls}")
        print(f"  Диапазон страниц:      1 … {max_page}")
        print(f"  Максимальный повтор одного URL: {max_repeat}")

        if max_page is not None and unique_urls < 50:
            print(f"  ⚠️  Похоже на зацикливание: страниц "
                  f"только {max_page}, но запросов {total}.")
        elif max_repeat > 10:
            print(f"  ⚠️  Один URL запрашивался {max_repeat} раз — "
                  f"вероятно, retry в цикле.")
        else:
            print(f"  ✅ URL'ы разные, реальные страницы.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())