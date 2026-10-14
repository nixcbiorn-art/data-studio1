"""web_runners — фоновый запуск сборщика и служебных инструментов из интерфейса."""
from __future__ import annotations

from redcat.core import runner
import os
import subprocess
import threading
from collections import deque
from datetime import datetime
from redcat.web.web_config import BASE_DIR
from redcat.web.web_telegram import _clean_token


# ──────────────────────────────────────────────────────────────
#  ЗАПУСК СБОРЩИКА
# ──────────────────────────────────────────────────────────────
class ScraperRunner:
    """Держит один фоновый запуск сборщика и копит его вывод для интерфейса."""

    def __init__(self):
        self.process = None
        self.lines = deque(maxlen=4000)
        self.counter = 0
        self.started_at = None
        self.finished_at = None
        self.exit_code = None
        self.command = []
        self.lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, args, token=None) -> dict:
        with self.lock:
            if self.running:
                return {"ok": False, "error": "Сбор уже идёт."}
            cmd = runner.script_cmd("redcat_scraper.py", *args, unbuffered=True)
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env.pop("REDCAT_TOKEN_ONESHOT", None)
            if token and _clean_token(token):
                env["REDCAT_TOKEN_ONESHOT"] = _clean_token(token)
            self.lines.clear()
            self.counter = 0
            self.command = cmd
            self.started_at = datetime.now().isoformat(timespec="seconds")
            self.finished_at = None
            self.exit_code = None
            self._append("$ python " + " ".join(["redcat_scraper.py"] + [str(a) for a in args]))
            try:
                self.process = subprocess.Popen(
                    cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env, text=True,
                    encoding="utf-8", errors="replace", bufsize=1)
            except OSError as e:
                self._append(f"❌ Не удалось запустить: {e}")
                return {"ok": False, "error": str(e)}
            threading.Thread(target=self._pump, daemon=True).start()
            return {"ok": True}

    def _pump(self):
        for line in self.process.stdout:
            self._append(line.rstrip("\n"))
        self.exit_code = self.process.wait()
        self.finished_at = datetime.now().isoformat(timespec="seconds")
        self._append(f"— процесс завершён, код {self.exit_code} —")

    def _append(self, line):
        self.lines.append({"n": self.counter, "text": line})
        self.counter += 1

    def stop(self) -> dict:
        with self.lock:
            if not self.running:
                return {"ok": False, "error": "Сбор не запущен."}
            self.process.terminate()
            self._append("⏹ Остановлено пользователем.")
            return {"ok": True}

    def state(self, since=0) -> dict:
        lines = [ln for ln in list(self.lines) if ln["n"] >= since]
        return {"running": self.running, "started_at": self.started_at,
                "finished_at": self.finished_at, "exit_code": self.exit_code,
                "lines": lines, "next": self.counter}


RUNNER = ScraperRunner()


# ──────────────────────────────────────────────────────────────
#  РЕЕСТР ИНСТРУМЕНТОВ
# ──────────────────────────────────────────────────────────────
TOOLS = [
    {"key": "run_all_tests", "group": "Диагностика", "title": "Прогнать все тесты",
     "desc": "run_all_tests.py.", "script": "run_all_tests.py", "args": [],
     "timeout": 600, "icon": "🧪"},
    {"key": "selftest_readonly", "group": "Диагностика",
     "title": "Проверить режим «только чтение»",
     "desc": "selftest_readonly.py.", "script": "selftest_readonly.py",
     "args": [], "timeout": 120, "icon": "🔒"},
    {"key": "check_undefined", "group": "Диагностика",
     "title": "Найти неопределённые имена",
     "desc": "check_undefined_names.py.", "script": "check_undefined_names.py",
     "args": [], "timeout": 300, "icon": "🔍"},
    {"key": "check_runtime", "group": "Диагностика",
     "title": "Диагностика окружения", "desc": "check_runtime.py.",
     "script": "check_runtime.py", "args": [], "timeout": 60, "icon": "🩺"},
    {"key": "scan_sources", "group": "Диагностика",
     "title": "Скан spec'ов cross_check", "desc": "scan_sources.py.",
     "script": "scan_sources.py", "args": [], "timeout": 300, "icon": "🔬"},
    {"key": "dash_history", "group": "Дашборды",
     "title": "История по источникам", "desc": "history_dashboard.py.",
     "script": "history_dashboard.py", "args": ["--no-open"],
     "timeout": 300, "icon": "📈"},
    {"key": "dash_completeness", "group": "Дашборды",
     "title": "Заполненность данных", "desc": "completeness_dashboard.py.",
     "script": "completeness_dashboard.py", "args": ["--no-open"],
     "timeout": 300, "icon": "🧩"},
    {"key": "dash_stats", "group": "Дашборды",
     "title": "Статистика сверки источников", "desc": "source_stats.py.",
     "script": "source_stats.py",
     "args": ["--html", "reports/source_stats.html", "--no-open"],
     "timeout": 900, "icon": "📊"},
    {"key": "walk_keys", "group": "Разведка API",
     "title": "Схема ключей из samples", "desc": "walk_keys.py.",
     "script": "walk_keys.py", "args": [], "timeout": 300, "icon": "🔑"},
    {"key": "pipeline_specs", "group": "Разведка API",
     "title": "Сгенерировать черновики spec", "desc": "pipeline_specs.py.",
     "script": "pipeline_specs.py", "args": [], "timeout": 600, "icon": "⚙️"},
    {"key": "test_drafts", "group": "Разведка API",
     "title": "Проверить черновики spec", "desc": "test_draft_specs.py.",
     "script": "test_draft_specs.py", "args": [], "timeout": 900, "icon": "✔️"},
    {"key": "demo_data", "group": "Данные", "title": "Создать демо-данные",
     "desc": "demo_data.py.", "script": "demo_data.py", "args": [],
     "timeout": 120, "icon": "🎲"},
    {"key": "fill_aliases", "group": "Данные",
     "title": "Заполнить словарь синонимов ЖК", "desc": "fill_aliases.py.",
     "script": "fill_aliases.py", "args": [], "timeout": 300, "icon": "📚"},
    {"key": "match_names", "group": "Данные",
     "title": "Сопоставить названия ЖК", "desc": "match_complex_names.py.",
     "script": "match_complex_names.py", "args": [], "timeout": 300, "icon": "🔗"},
    {"key": "field_labels", "group": "Данные",
     "title": "Пересобрать русские подписи полей",
     "desc": "make_field_labels.py.", "script": "make_field_labels.py",
     "args": [], "timeout": 120, "icon": "🏷️"},
    {"key": "export_problem_jc", "group": "Проблемы",
     "title": "Собрать проблемные ЖК", "desc": "export_problem_jc.py.",
     "script": "export_problem_jc.py", "args": [], "timeout": 1800, "icon": "🚨"},
    {"key": "export_problem_jc_strict", "group": "Проблемы",
     "title": "Проблемные ЖК (порог 1%)",
     "desc": "export_problem_jc.py --threshold 1.",
     "script": "export_problem_jc.py", "args": ["--threshold", "1"],
     "timeout": 1800, "icon": "🎯"},
    {"key": "source_stats_run", "group": "Проблемы",
     "title": "Дашборд сверки источников",
     "desc": "source_stats.py — с окраской.", "script": "source_stats.py",
     "args": ["--html", "reports/source_stats.html", "--no-open"],
     "timeout": 900, "icon": "📊"},
    {"key": "tg_bot", "group": "Telegram",
     "title": "Запустить бота (long polling)", "desc": "tg_bot.py.",
     "script": "tg_bot.py", "args": [], "timeout": 0, "icon": "🤖"},
    {"key": "tg_bot_watch", "group": "Telegram",
     "title": "Бот + слежение за отчётами", "desc": "tg_bot.py --watch.",
     "script": "tg_bot.py", "args": ["--watch"], "timeout": 0, "icon": "📡"},
    {"key": "telegram_check", "group": "Telegram",
     "title": "Проверить настройки бота", "desc": "set_telegram.py --test.",
     "script": "set_telegram.py", "args": ["--test"], "timeout": 60, "icon": "🔧"},
    {"key": "make_release", "group": "Сборка",
     "title": "Собрать чистый архив поставки", "desc": "make_release.py.",
     "script": "make_release.py", "args": ["--dry-run"],
     "timeout": 300, "icon": "📦"},
    {"key": "fix_bat", "group": "Сборка", "title": "Привести .bat к cp866",
     "desc": "fix_bat_encoding.py.", "script": "fix_bat_encoding.py",
     "args": [], "timeout": 60, "icon": "🔤"},
]


class ToolsRunner:
    """Фоновый запуск скрипта из реестра TOOLS."""

    def __init__(self):
        self.process = None
        self.key = None
        self.lines = deque(maxlen=6000)
        self.counter = 0
        self.started_at = None
        self.finished_at = None
        self.exit_code = None
        self.command = []
        self.lock = threading.Lock()

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self, tool):
        with self.lock:
            if self.running:
                return {"ok": False, "error": f"Уже выполняется «{self.key}»"}
            cmd = runner.script_cmd(tool["script"], *(tool.get("args") or []),
                                    unbuffered=True)
            if cmd is None:
                return {"ok": False, "error": f"Нет файла {tool['script']}"}
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            self.lines.clear(); self.counter = 0; self.key = tool["key"]
            self.command = [tool["script"]] + list(tool.get("args") or [])
            self.started_at = datetime.now().isoformat(timespec="seconds")
            self.finished_at = None; self.exit_code = None
            self._append(f"$ python {' '.join(self.command)}")
            try:
                self.process = subprocess.Popen(
                    cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env, bufsize=0)
            except OSError as e:
                self._append(f"XX {e}")
                return {"ok": False, "error": str(e)}
            threading.Thread(target=self._pump, daemon=True).start()
            return {"ok": True, "key": self.key}

    def _pump(self):
        import codecs
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        tail = ""
        def proc(text):
            nonlocal tail
            for ch in text:
                if ch not in ("\n", "\r"):
                    tail += ch; continue
                if ch == "\n":
                    self._append(tail)
                else:
                    if self.lines: self.lines[-1]["text"] = tail
                    elif tail: self._append(tail)
                tail = ""
        try:
            while True:
                chunk = self.process.stdout.read(4096)
                if not chunk: break
                proc(decoder.decode(chunk, final=False))
            proc(decoder.decode(b"", final=True))
            if tail: self._append(tail)
            self.exit_code = self.process.wait()
        except Exception as e:
            self._append(f"!! {e}"); self.exit_code = -1
        self.finished_at = datetime.now().isoformat(timespec="seconds")
        status = "OK" if self.exit_code == 0 else f"код {self.exit_code}"
        if self.exit_code != 0 and self.counter <= 1:
            self._append(f"Процесс упал с кодом {self.exit_code} без вывода.")
        self._append(f"--- завершено ({status}) ---")

    def _append(self, line):
        if not line.strip(): return
        self.lines.append({"n": self.counter, "text": line}); self.counter += 1

    def stop(self):
        with self.lock:
            if not self.running:
                return {"ok": False, "error": "Ничего не запущено."}
            self.process.terminate()
            self._append("Остановлено.")
            return {"ok": True}

    def state(self, since=0):
        return {"running": self.running, "key": self.key,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "exit_code": self.exit_code, "command": self.command,
                "lines": [ln for ln in list(self.lines) if ln["n"] >= since],
                "next": self.counter}


TOOLS_RUNNER = ToolsRunner()


def tools_list():
    return [{"key": t["key"], "group": t["group"], "title": t["title"],
             "desc": t["desc"], "icon": t.get("icon", "🔧"),
             "confirm": t.get("confirm")} for t in TOOLS]
