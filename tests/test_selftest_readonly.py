"""pytest-обвязка ручного selftest_readonly (инвариант «только чтение»).

Сам selftest патчит requests/aiohttp глобально (api_guard.install() на импорте),
поэтому запускаем его как отдельный процесс и проверяем код возврата —
так же, как это делает check_readonly.bat, но теперь внутри pytest-сьюта.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_selftest_readonly_exit_zero():
    r = subprocess.run(
        [sys.executable, "-m", "redcat.tools.selftest_readonly"],
        cwd=ROOT, capture_output=True, timeout=120,
    )
    tail = (r.stdout or b"").decode("utf-8", errors="replace").splitlines()[-3:]
    assert r.returncode == 0, (
        f"selftest_readonly вернул {r.returncode}; хвост вывода: {tail}"
    )
