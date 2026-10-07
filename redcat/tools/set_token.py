"""
Быстрое обновление токена в .env — без необходимости открывать файл руками.
Использование:
    python -m redcat.tools.set_token "eyJ...новый_токен"
или без аргумента — скрипт спросит токен в консоли (удобно, чтобы он не
оставался в истории команд терминала).
"""
from redcat.core import paths
import re
import sys

# Если вывод перенаправлен в файл (а не в реальную консоль), Python берёт
# кодировку локали ОС (на русской Windows — обычно cp1251), которая не
# умеет печатать часть символов из сообщений ниже — это роняло скрипт с
# UnicodeEncodeError вместо нормального обновления токена.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass
from pathlib import Path

ENV_PATH = paths.ROOT / ".env"


def _clean_token(raw: str) -> str:
    """JWT не содержит пробелов и переносов; убираем их (а также кавычки и
    префикс «Bearer»), чтобы .env не ломался от вставки с переносами строк."""
    t = re.sub(r"\s+", "", raw or "").strip("\"'")
    if t.lower().startswith("bearer"):
        t = t[6:]
    return t.strip("\"'")


def main():
    if len(sys.argv) > 1:
        token = sys.argv[1].strip()
    else:
        token = input("Вставьте новый REDCAT_TOKEN: ").strip()

    token = _clean_token(token)
    if not token:
        print("❌ Токен пустой — ничего не сохранено.")
        sys.exit(1)

    lines = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("REDCAT_TOKEN="):
            lines[i] = f"REDCAT_TOKEN={token}"
            found = True
            break
    if not found:
        lines.append(f"REDCAT_TOKEN={token}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✅ Токен обновлён в {ENV_PATH.name}")


if __name__ == "__main__":
    main()
