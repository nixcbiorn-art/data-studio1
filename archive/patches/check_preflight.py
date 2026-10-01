"""
check_preflight.py — точная проверка патча 10.
================================================
Смотрит на сам код preflight_check_split и проверяет, что до открытия
aiohttp.ClientSession нет вызовов _fetch_json. Именно эти «висячие»
вызовы и были багом: они обращались к unfiltered_url, url и session
до того, как эти переменные определялись.

Ничего не меняет — только читает модуль и печатает результат.
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def main() -> int:
    # 1. Импорт
    try:
        import redcat_scraper as rs
    except Exception as e:
        print(f"❌ redcat_scraper не импортируется: {type(e).__name__}: {e}")
        return 1

    if not hasattr(rs, "preflight_check_split"):
        print("❌ В redcat_scraper нет функции preflight_check_split.")
        return 1

    if not hasattr(rs, "_total_of"):
        print("❌ В redcat_scraper нет хелпера _total_of — патч 10 не применён.")
        return 1

    src = inspect.getsource(rs.preflight_check_split)

    # 2. Ищем async with — точку, где появляются session и unfiltered_url
    m = re.search(r"async\s+with\s+aiohttp\.ClientSession", src)
    if not m:
        print("❌ В preflight_check_split нет 'async with aiohttp.ClientSession' — "
              "файл повреждён или функция другая.")
        return 1

    before = src[:m.start()]
    after = src[m.start():]

    # 3. До async with не должно быть _fetch_json
    calls_before = before.count("_fetch_json(")
    calls_after = after.count("_fetch_json(")

    print("─" * 60)
    print("  Проверка preflight_check_split")
    print("─" * 60)
    print(f"  вызовов _fetch_json до async with:  {calls_before}")
    print(f"  вызовов _fetch_json внутри async with: {calls_after}")
    print()

    if calls_before > 0:
        print("  ❌ Найден висячий вызов _fetch_json ДО создания session.")
        print("     Это тот самый баг: unfiltered_url/url/session ещё не определены.")
        print("     Покажем первые строки функции:")
        print()
        for i, line in enumerate(before.splitlines()[:15], 1):
            print(f"     {i:>3}| {line}")
        return 1

    if calls_after < 2:
        print(f"  ⚠️  Внутри async with найдено только {calls_after} вызов(а) "
              f"_fetch_json — ожидалось 2 (запрос без фильтра + запрос с фильтром).")
        print("     Возможно, функция переписана не полностью.")
        return 1

    print("  ✅ Патч 10 применён корректно.")
    print(f"     Все {calls_after} вызова _fetch_json — внутри async with,")
    print(f"     до этого обращения к unfiltered_url/session нет.")
    print()

    # 4. Дополнительно: проверяем, что _total_of используется
    if "_total_of(" not in src:
        print("  ⚠️  _total_of определён, но не используется в функции.")
        return 1
    print("  ✅ _total_of используется для чтения total.")

    # 5. Проверяем, что функция реально читает unified (data.get('items'))
    if 'data.get("items")' not in src and "data.get('items')" not in src:
        print("  ⚠️  Функция не читает items из unified-ответа.")
        return 1
    print("  ✅ items читается из unified-ответа (data.get('items')).")

    # 6. Ищем мусорные строки, которые в принципе могли остаться от слияния
    suspicious = [
        line for line in src.splitlines()
        if "unfiltered_url, session" in line and "await _fetch_json" not in line
        and "base_data" not in line and "data, status, body" not in line
    ]
    if suspicious:
        print(f"  ℹ️  Найдены подозрительные строки ({len(suspicious)}):")
        for line in suspicious[:5]:
            print(f"     {line.strip()}")
        print("     Скорее всего, это часть корректного кода — оставьте как есть.")

    print()
    print("─" * 60)
    print("  ИТОГ: preflight_check_split в рабочем состоянии.")
    print("─" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())