"""
ПАТЧ 12 — фикс отсутствующего импорта threading
================================================
Патч 11 добавил в redcat_scraper.py глобальные структуры защиты от бана,
в которых используется threading.Lock(). Но сам модуль threading не был
импортирован — в исходном redcat_scraper.py его нет, а импорт api_guard,
aiohttp и requests его не подтягивают.

Результат: при первом же запуске — NameError: name 'threading' is not defined.

Что делает:

  • проверяет, что threading действительно не импортирован;
  • добавляет import threading рядом с другими stdlib-импортами;
  • прогоняет компиляцию и импорт.

Запуск:
    python apply_patch_12.py
"""
from __future__ import annotations

import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRAPER = HERE / "redcat_scraper.py"

# Куда вставить. Ставим рядом с остальными stdlib-импортами по алфавиту,
# после "import sys" и перед "import time".
ANCHOR = "import sys\nimport time\n"
NEW_IMPORT = "import sys\nimport threading\nimport time\n"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _write(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8", newline="")


def _backup(p: Path, stamp: str) -> Path:
    b = p.with_name(p.name + f".bak_{stamp}")
    shutil.copy2(p, b)
    return b


def _compile(p: Path) -> tuple[bool, str]:
    try:
        py_compile.compile(str(p), doraise=True, cfile=str(p) + ".pyc")
        return True, ""
    except py_compile.PyCompileError as e:
        return False, str(e)
    finally:
        Path(str(p) + ".pyc").unlink(missing_ok=True)


def apply() -> int:
    if not SCRAPER.exists():
        print(f"❌ Нет файла {SCRAPER.name}")
        return 1

    text = _read(SCRAPER)

    # Уже есть?
    if "import threading" in text:
        print("ℹ️  import threading уже есть в redcat_scraper.py — "
              "проверьте, действительно ли ошибка была из-за него.")
        return 0

    # Убеждаемся, что модуль и правда нужен — где-то используется threading.
    if "threading." not in text:
        print("ℹ️  threading не используется в файле — фикс не нужен.")
        return 0

    if ANCHOR not in text:
        print("❌ Не найден якорь 'import sys\\nimport time\\n'. "
              "Пришлите фрагмент блока импортов — вставлю вручную.")
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = _backup(SCRAPER, stamp)
    print(f"💾 Бэкап: {backup.name}")

    new_text = text.replace(ANCHOR, NEW_IMPORT, 1)
    _write(SCRAPER, new_text)

    ok, err = _compile(SCRAPER)
    if not ok:
        print(f"❌ После правки файл не компилируется: {err}")
        print("   Откатываю.")
        shutil.copy2(backup, SCRAPER)
        return 1

    print("   ✓ добавлен import threading")
    print("\n✅ Патч 12 применён.")
    return 0


def selftest() -> int:
    import importlib
    try:
        import redcat_scraper as rs
        importlib.reload(rs)
    except Exception as e:  # noqa: BLE001
        print(f"❌ redcat_scraper не импортируется: {type(e).__name__}: {e}")
        return 1

    failures = []
    if not hasattr(rs, "threading"):
        failures.append("threading не импортирован в модуль")
    if not hasattr(rs, "_RATE_LIMITERS"):
        failures.append("_RATE_LIMITERS не создан (проверьте патч 11)")
    if not hasattr(rs, "_CIRCUIT_BREAKERS"):
        failures.append("_CIRCUIT_BREAKERS не создан (проверьте патч 11)")

    if failures:
        print("❌ Провалились проверки:")
        for f in failures:
            print(f"   • {f}")
        return 1

    print("✅ Самопроверка: redcat_scraper импортируется, "
          "rate limiter и circuit breaker на месте.")
    return 0


if __name__ == "__main__":
    code = apply()
    if code == 0:
        code = selftest()
    sys.exit(code)  