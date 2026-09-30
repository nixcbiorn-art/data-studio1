"""
show_preflight.py — показывает фактический код preflight_check_split.
======================================================================
Ничего не меняет. Печатает функцию как она есть в файле и явно
проверяет наличие ключевых подстрок (с точным указанием, какие именно
варианты написания найдены).
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    import redcat_scraper as rs
except Exception as e:
    print(f"❌ redcat_scraper не импортируется: {type(e).__name__}: {e}")
    sys.exit(1)

if not hasattr(rs, "preflight_check_split"):
    print("❌ Функции preflight_check_split нет в модуле.")
    sys.exit(1)

src = inspect.getsource(rs.preflight_check_split)
lines = src.splitlines()

print("=" * 72)
print("  ПОЛНЫЙ ИСХОДНИК preflight_check_split")
print("=" * 72)
for i, line in enumerate(lines, 1):
    print(f"{i:>4}| {line}")

print()
print("=" * 72)
print("  ПРОВЕРКИ ПОДСТРОК")
print("=" * 72)

checks = [
    ("data.get(\"items\")",  'data.get("items")' in src),
    ("data.get('items')",   "data.get('items')" in src),
    ("data.get( \"items\")", 'data.get( "items")' in src),
    ("data.get('items' )",  "data.get('items' )" in src),
    ("items = data",        "items = data" in src),
    (".get(\"items\")",      '.get("items")' in src),
    (".get('items')",       ".get('items')" in src),
    ("_total_of(",          "_total_of(" in src),
    ("async with aiohttp",  "async with aiohttp" in src),
]

for label, found in checks:
    mark = "✅" if found else "  "
    print(f"  {mark} {label!r}: {found}")

print()
print("=" * 72)
print("  СТРОКИ, ГДЕ УПОМИНАЕТСЯ 'items'")
print("=" * 72)
found_any = False
for i, line in enumerate(lines, 1):
    if "items" in line.lower():
        print(f"{i:>4}| {line!r}")
        found_any = True
if not found_any:
    print("  (нет ни одной строки с 'items' — функция items вообще не читает)")

print()
print("=" * 72)
print("  СТРОКИ, ГДЕ УПОМИНАЕТСЯ 'total'")
print("=" * 72)
for i, line in enumerate(lines, 1):
    if "total" in line.lower():
        print(f"{i:>4}| {line!r}")
        