"""
Проверка: приложение физически не может записать что-либо в API
===============================================================
Запустите:  python -m redcat.tools.selftest_readonly

Скрипт пытается отправить PUT / POST / PATCH / DELETE в сторонний адрес
всеми способами, которыми это вообще делается в проекте, и показывает,
что каждый из них блокируется до открытия сетевого соединения.

Интернет для проверки не нужен: запрос не доходит до сети — он падает
раньше. Если бы блокировка не работала, вы бы увидели сетевую ошибку
(таймаут, DNS), а не «ЗАБЛОКИРОВАНО».
"""

from __future__ import annotations

import sys

# Если вывод перенаправлен в файл (а не в реальную консоль), Python берёт
# кодировку локали ОС (на русской Windows — обычно cp1251), которая не
# умеет печатать эмодзи и часть символов из сообщений ниже — это роняло
# скрипт с UnicodeEncodeError вместо нормальной проверки.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

from redcat.core import api_guard

TARGET = "https://api.redcat.ai/api/v1/estate/regulations/1"

api_guard.install()


def _try(label, fn):
    try:
        fn()
    except api_guard.WriteRequestBlocked:
        print(f"  ✅ {label:<34} ЗАБЛОКИРОВАНО")
        return True
    except Exception as e:  # noqa: BLE001 — любой другой исход = защита не сработала
        print(f"  ❌ {label:<34} НЕ заблокировано: {type(e).__name__}: {e}")
        return False
    print(f"  ❌ {label:<34} НЕ заблокировано: запрос ушёл в сеть!")
    return False


def main() -> int:
    print("🔒 ПРОВЕРКА РЕЖИМА «ТОЛЬКО ЧТЕНИЕ»")
    print("=" * 56)
    print(api_guard.status_text())
    print(f"\nЦель проверки: {TARGET}\n")

    ok = True

    try:
        import requests
    except ImportError:
        print("  ⚠️ requests не установлен — пропуск блока requests")
    else:
        session = requests.Session()
        print("Через requests.Session:")
        for method in ("PUT", "POST", "PATCH", "DELETE"):
            ok &= _try(f"session.request({method})",
                       lambda m=method: session.request(m, TARGET, json={"x": 1}))
        print("\nЧерез функции-обёртки requests:")
        for name in ("put", "post", "patch", "delete"):
            ok &= _try(f"requests.{name}()",
                       lambda n=name: getattr(requests, n)(TARGET, json={"x": 1}))

    try:
        import asyncio

        import aiohttp
    except ImportError:
        print("\n  ⚠️ aiohttp не установлен — пропуск блока aiohttp")
    else:
        print("\nЧерез aiohttp (асинхронный сбор):")

        async def call(method):
            async with aiohttp.ClientSession() as s:
                await s.request(method, TARGET, json={"x": 1})

        for method in ("PUT", "POST", "PATCH", "DELETE"):
            ok &= _try(f"aiohttp {method}",
                       lambda m=method: asyncio.run(call(m)))

    print("\nКонтрольная проверка — GET разрешён:")
    allowed = api_guard.check_method("GET", TARGET) == "GET"
    print(f"  {'✅' if allowed else '❌'} GET проходит проверку метода "
          f"(сам запрос здесь не отправляется)")
    ok &= allowed

    print("\n" + "=" * 56)
    if ok:
        print(f"✅ ВСЁ В ПОРЯДКЕ. Заблокировано попыток записи: "
              f"{api_guard.blocked_count()}.")
        print("   Приложение умеет только читать данные из API.")
        return 0
    print("❌ ЗАЩИТА НЕ СРАБОТАЛА — не используйте сборщик, пока это не починено.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
