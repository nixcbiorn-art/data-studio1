"""
RedCat Studio — точка входа
===========================
Запускает локальное приложение и открывает его в браузере.

    python studio.py                 обычный запуск
    python studio.py --port 9000     другой порт
    python studio.py --no-browser    не открывать браузер сам

Ставить ничего не нужно: приложение собрано на стандартной библиотеке
Python. Интернет требуется только самому сбору данных, интерфейс работает
полностью офлайн.

Режим работы с API — строго только чтение (GET). Проверить:
    python selftest_readonly.py
"""

from __future__ import annotations

import argparse
import sys

# Если вывод приложения перенаправлен в файл (а не в реальную консоль),
# Python берёт кодировку локали ОС (на русской Windows — обычно cp1251),
# которая не умеет печатать эмодзи и часть символов из сообщений ниже —
# это роняло скрипт с UnicodeEncodeError вместо нормального запуска.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description="RedCat Studio")
    ap.add_argument("--port", type=int, default=8765, help="порт (по умолчанию 8765)")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    ap.add_argument("--demo", action="store_true",
                    help="создать демо-данные, если база пуста, и открыть приложение")
    args = ap.parse_args()

    if args.demo and not (BASE_DIR / "reports" / "redcat_data.db").exists():
        import demo_data
        demo_data.build(force=False)

    try:
        import webapp
    except ImportError as e:
        print(f"❌ Не удалось загрузить приложение: {e}")
        print("   Проверьте, что все файлы лежат в одной папке.")
        return 1

    webapp.serve(port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
