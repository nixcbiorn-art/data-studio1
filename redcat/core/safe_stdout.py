"""Принудительный UTF-8 для консольного вывода.

Windows: при перенаправлении stdout/stderr в файл или pipe (>> run_log.txt,
`... > out.txt`, запуск по планировщику) поток получает кодировку ANSI
(cp1251/cp866). Печать любого не-ANSI символа — ✅ ⚠ ✗ ↔ → — роняет процесс
с UnicodeEncodeError уже ПОСЛЕ того, как полезная работа сделана, и запуск
по расписанию помечается упавшим.

В интерактивной консоли проблемы нет (Python пишет через WriteConsole),
поэтому патч нужен именно для сценариев с перенаправлением.

Использование (в каждой точке входа, внутри `if __name__ == "__main__":`):

    from redcat.core import safe_stdout
    safe_stdout.enable()
"""
from __future__ import annotations

import sys


def enable() -> None:
    """UTF-8 + errors='replace' для stdout/stderr, если поток это поддерживает.

    Идемпотентно, тихо для окружений без reconfigure (stdout заглушён,
    zipped-интерпретатор и т.п.)."""
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
