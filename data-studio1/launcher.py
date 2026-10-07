"""
RedCat Estate Scraper — Launcher (панель управления)
======================================================
Короткий набор кнопок для тех, кому так привычнее. Полноценный интерфейс —
это приложение RedCat Studio: кнопка «🖥 Открыть приложение» наверху вкладки
«Сбор данных» (или двойной клик по `studio.bat`).

Из этой панели можно:
  - открыть приложение и проверить режим «только чтение»;
  - видеть статус токена (действителен / скоро истечёт / истёк) и обновлять его;
  - запускать сбор данных вручную и смотреть вывод в реальном времени;
  - ставить и снимать автозапуск по будням в 10:00 (Планировщик заданий Windows);
  - быстро открывать папку с отчётами, последний отчёт и лог.

Запуск:  python launcher.py   (или дважды кликнуть launcher.bat)
Зависимостей сверх стандартной библиотеки Python не требует (tkinter входит
в стандартную поставку Python для Windows).
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import queue
import subprocess
import sys

# Если вывод панели перенаправлен в файл (а не в реальную консоль), Python
# берёт кодировку локали ОС (на русской Windows — обычно cp1251), которая
# не умеет печатать эмодзи и часть символов из сообщений ниже — это
# роняло скрипт с UnicodeEncodeError вместо нормальной работы.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# Переиспользуем decode_jwt_payload из основного скрипта, чтобы логика
# разбора срока действия токена не дублировалась в двух местах.
try:
    import redcat_scraper as rs
except Exception:  # основной скрипт не найден рядом / не импортируется
    rs = None

try:
    import sources as srclib
except Exception:  # sources.py не найден рядом / не импортируется
    srclib = None

ENV_PATH = BASE_DIR / ".env"
SCRIPT_PATH = BASE_DIR / "redcat_scraper.py"
REPORTS_DIR = BASE_DIR / "reports"
HISTORY_DIR = BASE_DIR / "history"
SOURCES_DIR = BASE_DIR / "sources"
DASHBOARD_PATH = REPORTS_DIR / "dashboard.html"
LOG_PATH = BASE_DIR / "redcat_scraper.log"
INSTALL_BAT = BASE_DIR / "install_scheduled_task.bat"
UNINSTALL_BAT = BASE_DIR / "uninstall_scheduled_task.bat"
TASK_NAME = "RedCat Estate Scraper"

REPORTS_DIR.mkdir(exist_ok=True)
HISTORY_DIR.mkdir(exist_ok=True)
SOURCES_DIR.mkdir(exist_ok=True)


# ──────────────────────────────────────────────────────────────
#  Вспомогательные функции (без GUI — их можно проверить отдельно)
# ──────────────────────────────────────────────────────────────
def read_env_token() -> str:
    if not ENV_PATH.exists():
        return ""
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("REDCAT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def _clean_token(raw: str) -> str:
    """JWT не содержит пробелов и переносов; убираем их (а также кавычки и
    префикс «Bearer»), чтобы .env не ломался от вставки с переносами строк."""
    t = re.sub(r"\s+", "", raw or "").strip("\"'")
    if t.lower().startswith("bearer"):
        t = t[6:]
    return t.strip("\"'")


def write_env_token(token: str) -> None:
    token = _clean_token(token)
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


def token_status() -> tuple[str, str]:
    """Возвращает (текст_статуса, цвет_для_метки)."""
    token = read_env_token()
    if not token:
        return "Токен не задан — нажмите «Обновить токен».", "#c0392b"

    claims = rs.decode_jwt_payload(token) if rs else None
    if not claims or "exp" not in claims:
        return "Токен задан (срок действия не определён).", "#2c3e50"

    try:
        exp_dt = datetime.fromtimestamp(claims["exp"])
    except (OSError, OverflowError, ValueError):
        return "Токен задан (не удалось прочитать срок действия).", "#2c3e50"

    now = datetime.now()
    if exp_dt < now:
        return f"Токен ИСТЁК {exp_dt:%d.%m.%Y %H:%M} — обновите его.", "#c0392b"
    remaining = exp_dt - now
    if remaining.total_seconds() < 24 * 3600:
        h = int(remaining.total_seconds() // 3600)
        return f"Токен истекает {exp_dt:%d.%m.%Y %H:%M} (осталось ~{h} ч) — скоро обновите.", "#e67e22"
    return f"Токен действителен до {exp_dt:%d.%m.%Y %H:%M}.", "#27ae60"


def _decode_console_bytes(data: bytes) -> str:
    """Надёжно декодирует вывод консольных команд Windows: schtasks (и другие
    консольные утилиты) обычно пишут в кодовой странице консоли (часто cp866
    на русской Windows), а не в ANSI (cp1251) или UTF-8, поэтому нельзя просто
    полагаться на encoding по умолчанию — иначе будет UnicodeDecodeError на
    любом непопадающем в эту кодировку байте."""
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def query_task_status() -> str:
    if os.name != "nt":
        return "Планировщик заданий доступен только в Windows."
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST", "/v"],
            capture_output=True, timeout=10,
        )
    except FileNotFoundError:
        return "Не удалось обратиться к Планировщику заданий (schtasks не найден)."

    if result.returncode != 0:
        return "Автозапуск НЕ установлен."

    output = _decode_console_bytes(result.stdout)
    next_run, status = "?", "?"
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Next Run Time:"):
            next_run = line.split(":", 1)[1].strip()
        elif line.startswith("Status:"):
            status = line.split(":", 1)[1].strip()
    return f"Автозапуск установлен. Статус: {status}. Следующий запуск: {next_run}."


def run_elevated_bat(bat_path: Path) -> None:
    """Запускает .bat с запросом прав администратора через UAC."""
    if os.name != "nt":
        messagebox.showerror("Недоступно", "Эта функция работает только в Windows.")
        return
    if not bat_path.exists():
        messagebox.showerror("Файл не найден", f"Не найден файл:\n{bat_path}")
        return
    try:
        ctypes.windll.shell32.ShellExecuteW(None, "runas", str(bat_path), None, str(BASE_DIR), 1)
    except Exception as e:
        messagebox.showerror("Ошибка", f"Не удалось запустить {bat_path.name}:\n{e}")


def latest_report() -> Path | None:
    files = sorted(REPORTS_DIR.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def open_path(path: Path) -> None:
    if not path.exists():
        messagebox.showinfo("Нет файла", f"Файл/папка ещё не существует:\n{path}")
        return
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
    except Exception as e:
        messagebox.showerror("Ошибка", f"Не удалось открыть:\n{path}\n\n{e}")


# ──────────────────────────────────────────────────────────────
#  GUI
# ──────────────────────────────────────────────────────────────
class LauncherApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("RedCat Estate Scraper — панель управления")
        self.root.geometry("820x620")
        self.root.minsize(680, 520)

        self.output_queue: "queue.Queue[str]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.process: subprocess.Popen | None = None
        self.run_extra_args: list[str] = []

        self._build_ui()
        self.refresh_status()
        self.root.after(150, self._poll_output_queue)

    # ---------- построение интерфейса ----------
    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        run_tab = ttk.Frame(notebook)
        sources_tab = ttk.Frame(notebook)
        notebook.add(run_tab, text="▶ Сбор данных")
        notebook.add(sources_tab, text="🔌 Источники API")

        self._build_run_tab(run_tab)
        self._build_sources_tab(sources_tab)

    def _build_run_tab(self, root) -> None:
        pad = {"padx": 10, "pady": 6}

        # --- Приложение ---
        # Полноценный интерфейс со сбором, поиском, анализом и правкой данных.
        # Эта панель осталась для тех, кому привычнее короткий набор кнопок,
        # но всё интересное теперь в приложении.
        studio_frame = ttk.LabelFrame(root, text="Приложение RedCat Studio")
        studio_frame.pack(fill="x", **pad)
        ttk.Label(
            studio_frame,
            text="Таблицы с фильтрами и поиском, сводные и графики, правка данных "
                 "в локальной копии, запуск сбора с живым логом. Открывается в браузере, "
                 "работает офлайн. Запись в API заблокирована — только чтение.",
            wraplength=700, justify="left", foreground="#555",
        ).pack(anchor="w", padx=8, pady=(6, 4))
        studio_btns = ttk.Frame(studio_frame)
        studio_btns.pack(anchor="w", padx=8, pady=(0, 8))
        ttk.Button(studio_btns, text="🖥 Открыть приложение",
                   command=self.on_open_studio).pack(side="left")
        ttk.Button(studio_btns, text="🔒 Проверить режим «только чтение»",
                   command=self.on_check_readonly).pack(side="left", padx=(8, 0))

        # --- Токен ---
        token_frame = ttk.LabelFrame(root, text="Токен доступа к API")
        token_frame.pack(fill="x", **pad)

        self.token_label = ttk.Label(token_frame, text="…", wraplength=700, justify="left")
        self.token_label.pack(anchor="w", padx=8, pady=(6, 2))

        token_btns = ttk.Frame(token_frame)
        token_btns.pack(anchor="w", padx=8, pady=(0, 8))
        ttk.Button(token_btns, text="Обновить токен…", command=self.on_update_token).pack(side="left")
        ttk.Button(token_btns, text="Открыть .env", command=lambda: open_path(ENV_PATH)).pack(side="left", padx=(8, 0))

        # --- Запуск ---
        run_frame = ttk.LabelFrame(root, text="Сбор данных")
        run_frame.pack(fill="x", **pad)
        run_btns = ttk.Frame(run_frame)
        run_btns.pack(anchor="w", padx=8, pady=8)
        self.run_button = ttk.Button(run_btns, text="▶ Запустить сейчас", command=self.on_run_now)
        self.run_button.pack(side="left")
        ttk.Button(run_btns, text="Открыть папку отчётов", command=lambda: open_path(REPORTS_DIR)).pack(side="left", padx=(8, 0))
        ttk.Button(run_btns, text="Открыть последний отчёт", command=self.on_open_latest_report).pack(side="left", padx=(8, 0))
        ttk.Button(run_btns, text="📊 Открыть дашборд", command=self.on_open_dashboard).pack(side="left", padx=(8, 0))
        ttk.Button(run_btns, text="Открыть лог ошибок", command=lambda: open_path(LOG_PATH)).pack(side="left", padx=(8, 0))

        # --- Автозапуск ---
        sched_frame = ttk.LabelFrame(root, text="Автозапуск по будням в 10:00 (время этого ПК)")
        sched_frame.pack(fill="x", **pad)
        self.sched_label = ttk.Label(sched_frame, text="…", wraplength=700, justify="left")
        self.sched_label.pack(anchor="w", padx=8, pady=(6, 2))
        sched_btns = ttk.Frame(sched_frame)
        sched_btns.pack(anchor="w", padx=8, pady=(0, 8))
        ttk.Button(sched_btns, text="Установить автозапуск", command=self.on_install_task).pack(side="left")
        ttk.Button(sched_btns, text="Удалить автозапуск", command=self.on_uninstall_task).pack(side="left", padx=(8, 0))
        ttk.Label(sched_frame, text="(потребует подтверждения в окне UAC — это нормально)",
                  foreground="#7f8c8d").pack(anchor="w", padx=8, pady=(0, 6))

        # --- Общая кнопка обновления статусов ---
        top_btns = ttk.Frame(root)
        top_btns.pack(fill="x", padx=10)
        ttk.Button(top_btns, text="🔄 Обновить статус", command=self.refresh_status).pack(anchor="e")

        # --- Вывод ---
        out_frame = ttk.LabelFrame(root, text="Вывод последнего запуска")
        out_frame.pack(fill="both", expand=True, **pad)
        self.output_text = scrolledtext.ScrolledText(out_frame, height=14, state="disabled", wrap="word")
        self.output_text.pack(fill="both", expand=True, padx=6, pady=6)

    def _build_sources_tab(self, root) -> None:
        pad = {"padx": 10, "pady": 6}

        info = ttk.Label(
            root,
            text="Каждый источник — это один API-эндпоинт. Добавить новый можно без "
                 "программирования: укажите адрес и, если есть, вставьте пример ответа — "
                 "остальное (где искать записи, номер страницы, общее число) определится "
                 "само. Итоговое описание можно поправить вручную перед сохранением.",
            wraplength=700, justify="left", foreground="#555",
        )
        info.pack(anchor="w", **pad)

        list_frame = ttk.LabelFrame(root, text="Зарегистрированные источники")
        list_frame.pack(fill="both", expand=True, **pad)

        columns = ("key", "title", "url")
        self.sources_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=10)
        self.sources_tree.heading("key", text="Ключ")
        self.sources_tree.heading("title", text="Название")
        self.sources_tree.heading("url", text="URL")
        self.sources_tree.column("key", width=140, anchor="w")
        self.sources_tree.column("title", width=180, anchor="w")
        self.sources_tree.column("url", width=360, anchor="w")
        self.sources_tree.pack(fill="both", expand=True, padx=8, pady=8)

        src_btns = ttk.Frame(root)
        src_btns.pack(anchor="w", padx=10, pady=(0, 8))
        ttk.Button(src_btns, text="➕ Добавить источник…", command=self.on_add_source).pack(side="left")
        ttk.Button(src_btns, text="▶ Собрать только этот", command=self.on_run_selected_source).pack(side="left", padx=(8, 0))
        ttk.Button(src_btns, text="🗑 Удалить", command=self.on_delete_source).pack(side="left", padx=(8, 0))
        ttk.Button(src_btns, text="📂 Открыть папку sources", command=lambda: open_path(SOURCES_DIR)).pack(side="left", padx=(8, 0))
        ttk.Button(src_btns, text="🔄 Обновить список", command=self.refresh_sources_list).pack(side="left", padx=(8, 0))

        self.refresh_sources_list()

    # ---------- обновление статусов ----------
    def refresh_status(self) -> None:
        text, color = token_status()
        self.token_label.configure(text=text, foreground=color)
        self.sched_label.configure(text=query_task_status())

    # ---------- обработчики кнопок ----------
    def on_update_token(self) -> None:
        current = read_env_token()
        dialog = TokenDialog(self.root, current)
        self.root.wait_window(dialog.top)
        if dialog.result is not None:
            write_env_token(dialog.result.strip())
            self.refresh_status()
            messagebox.showinfo("Готово", "Токен сохранён в .env")

    def on_open_latest_report(self) -> None:
        report = latest_report()
        if report is None:
            messagebox.showinfo("Нет отчётов", "Отчётов пока нет — сначала запустите сбор данных.")
            return
        open_path(report)

    def on_open_dashboard(self) -> None:
        if not DASHBOARD_PATH.exists():
            messagebox.showinfo(
                "Дашборда пока нет",
                "Дашборд появится после первого запуска сбора данных.",
            )
            return
        open_path(DASHBOARD_PATH)

    def on_open_studio(self) -> None:
        """Поднимает локальный сервер приложения и открывает его в браузере.

        Сервер живёт отдельным процессом, чтобы закрытие этой панели его не
        убивало, а повторное нажатие кнопки не поднимало второй такой же.
        """
        studio_path = BASE_DIR / "studio.py"
        if not studio_path.exists():
            messagebox.showerror(
                "Файл не найден",
                "Не найден studio.py рядом с панелью. Проверьте, что все файлы "
                "приложения лежат в одной папке.",
            )
            return

        proc = getattr(self, "studio_process", None)
        if proc is not None and proc.poll() is None:
            webbrowser.open(getattr(self, "studio_url", "http://127.0.0.1:8765/"))
            return

        try:
            kwargs = {"cwd": str(BASE_DIR)}
            if os.name == "nt":
                # без своей консоли: окно приложения — это браузер
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.studio_process = subprocess.Popen(
                [sys.executable, str(studio_path), "--no-browser"], **kwargs)
        except OSError as e:
            messagebox.showerror("Не удалось запустить", str(e))
            return

        self.studio_url = "http://127.0.0.1:8765/"
        self.root.after(1500, lambda: webbrowser.open(self.studio_url))
        self._append_output(
            "🖥 Приложение запущено: http://127.0.0.1:8765/ "
            "(если браузер не открылся сам — откройте адрес вручную)\n")

    def on_check_readonly(self) -> None:
        """Прогоняет selftest_readonly.py и показывает результат."""
        test_path = BASE_DIR / "selftest_readonly.py"
        if not test_path.exists():
            messagebox.showerror("Файл не найден", "Не найден selftest_readonly.py.")
            return
        try:
            result = subprocess.run(
                [sys.executable, str(test_path)], cwd=str(BASE_DIR),
                capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            messagebox.showerror("Проверка не выполнена", str(e))
            return
        output = _decode_console_bytes(result.stdout or b"")
        self._append_output(output + "\n")
        if result.returncode == 0:
            messagebox.showinfo(
                "Режим «только чтение» подтверждён",
                "Попытки отправить PUT, POST, PATCH и DELETE в API заблокированы — "
                "запросы не доходят до сети.\n\nПодробности — в поле вывода внизу.")
        else:
            messagebox.showerror(
                "Проверка не пройдена",
                "Блокировка записи не сработала. Не запускайте сбор, пока это не "
                "исправлено. Подробности — в поле вывода внизу.")

    def on_install_task(self) -> None:
        run_elevated_bat(INSTALL_BAT)
        messagebox.showinfo(
            "Установка автозапуска",
            "Подтвердите запрос UAC в открывшемся окне.\n"
            "После этого нажмите «Обновить статус», чтобы увидеть результат.",
        )

    def on_uninstall_task(self) -> None:
        run_elevated_bat(UNINSTALL_BAT)
        messagebox.showinfo(
            "Удаление автозапуска",
            "Подтвердите запрос UAC в открывшемся окне.\n"
            "После этого нажмите «Обновить статус», чтобы увидеть результат.",
        )

    def on_run_now(self, extra_args: list[str] | None = None) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Уже выполняется", "Сбор данных уже запущен — дождитесь завершения.")
            return
        if not SCRIPT_PATH.exists():
            messagebox.showerror("Файл не найден", f"Не найден {SCRIPT_PATH}")
            return

        self.run_extra_args = extra_args or []
        self._clear_output()
        self.run_button.configure(state="disabled", text="⏳ Выполняется…")
        self.worker = threading.Thread(target=self._run_scraper_thread, daemon=True)
        self.worker.start()

    # ---------- вкладка «Источники API» ----------
    def refresh_sources_list(self) -> None:
        for row in self.sources_tree.get_children():
            self.sources_tree.delete(row)
        if srclib is None:
            return
        for path, key, title, url in srclib.list_source_files(SOURCES_DIR):
            self.sources_tree.insert("", "end", iid=f"{path}::{key}",
                                     values=(key, title, url), tags=(str(path),))

    def _selected_source(self):
        sel = self.sources_tree.selection()
        if not sel:
            return None, None
        item = self.sources_tree.item(sel[0])
        key = item["values"][0]
        path = Path(item["tags"][0]) if item["tags"] else None
        return key, path

    def on_add_source(self) -> None:
        if srclib is None:
            messagebox.showerror("Недоступно", "Модуль sources.py не найден рядом со скриптом.")
            return
        dialog = AddSourceDialog(self.root)
        self.root.wait_window(dialog.top)
        if dialog.saved:
            self.refresh_sources_list()

    def on_delete_source(self) -> None:
        key, path = self._selected_source()
        if not key:
            messagebox.showinfo("Ничего не выбрано", "Выберите источник в списке.")
            return
        if not messagebox.askyesno("Удалить источник",
                                    f"Удалить источник «{key}»?\nФайл: {path}"):
            return
        srclib.delete_source_file(path)
        self.refresh_sources_list()

    def on_run_selected_source(self) -> None:
        key, _ = self._selected_source()
        if not key:
            messagebox.showinfo("Ничего не выбрано", "Выберите источник в списке.")
            return
        self.on_run_now(extra_args=["--only", key])

    # ---------- фоновый запуск скрапера с потоковым выводом ----------
    def _run_scraper_thread(self) -> None:
        try:
            child_env = os.environ.copy()
            # Токен берём заново из .env перед КАЖДЫМ запуском. Иначе дочерний
            # процесс наследует REDCAT_TOKEN, который попал в os.environ при
            # старте лаунчера (import redcat_scraper -> load_dotenv), а
            # load_dotenv по умолчанию уже заданную переменную не перезаписывает —
            # и скрапер ходит со СТАРЫМ токеном, сколько бы вы его ни обновляли.
            fresh = _clean_token(read_env_token())
            if fresh:
                child_env["REDCAT_TOKEN"] = fresh
            child_env["PYTHONIOENCODING"] = "utf-8"  # чтобы вывод скрипта всегда был в UTF-8,
                                                       # независимо от системной кодовой страницы
            self.process = subprocess.Popen(
                [sys.executable, str(SCRIPT_PATH), *self.run_extra_args],
                cwd=str(BASE_DIR),
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.output_queue.put(line)
            self.process.wait()
            code = self.process.returncode
            self.output_queue.put(f"\n--- Завершено с кодом {code} ---\n")
        except Exception as e:
            self.output_queue.put(f"\n❌ Не удалось запустить скрипт: {e}\n")
        finally:
            self.output_queue.put("__DONE__")

    def _poll_output_queue(self) -> None:
        try:
            while True:
                line = self.output_queue.get_nowait()
                if line == "__DONE__":
                    self.run_button.configure(state="normal", text="▶ Запустить сейчас")
                    self.refresh_status()
                else:
                    self._append_output(line)
        except queue.Empty:
            pass
        self.root.after(150, self._poll_output_queue)

    def _append_output(self, text: str) -> None:
        self.output_text.configure(state="normal")
        self.output_text.insert("end", text)
        self.output_text.see("end")
        self.output_text.configure(state="disabled")

    def _clear_output(self) -> None:
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.configure(state="disabled")


class TokenDialog:
    """Простое модальное окно для ввода/вставки токена (с возможностью показать/скрыть)."""

    def __init__(self, parent: tk.Tk, current: str):
        self.result: str | None = None
        self.top = tk.Toplevel(parent)
        self.top.title("Обновить токен")
        self.top.geometry("620x180")
        self.top.transient(parent)
        self.top.grab_set()

        ttk.Label(self.top, text="Вставьте новый токен (REDCAT_TOKEN):").pack(anchor="w", padx=12, pady=(12, 4))

        self.show_var = tk.BooleanVar(value=False)
        entry_frame = ttk.Frame(self.top)
        entry_frame.pack(fill="x", padx=12)
        self.entry = ttk.Entry(entry_frame, show="•")
        self.entry.insert(0, current)
        self.entry.pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(entry_frame, text="показать", variable=self.show_var, command=self._toggle_show).pack(side="left", padx=(6, 0))

        # Контекстное меню правой кнопкой мыши — не все виджеты Tk дают его "из коробки" на Windows.
        self._menu = tk.Menu(self.entry, tearoff=0)
        self._menu.add_command(label="Вырезать", command=lambda: self.entry.event_generate("<<Cut>>"))
        self._menu.add_command(label="Копировать", command=lambda: self.entry.event_generate("<<Copy>>"))
        self._menu.add_command(label="Вставить", command=self._paste_from_clipboard)
        self._menu.add_separator()
        self._menu.add_command(label="Выделить всё", command=lambda: self.entry.selection_range(0, "end"))
        self.entry.bind("<Button-3>", self._show_context_menu)

        # Явные горячие клавиши — на случай, если стандартные <<Paste>>/<<Copy>> не срабатывают
        self.entry.bind("<Control-v>", self._on_ctrl_v)
        self.entry.bind("<Control-V>", self._on_ctrl_v)

        paste_row = ttk.Frame(self.top)
        paste_row.pack(anchor="w", padx=12, pady=(6, 0))
        ttk.Button(paste_row, text="📋 Вставить из буфера обмена", command=self._paste_from_clipboard).pack(side="left")
        ttk.Label(paste_row, text="(если Ctrl+V или правый клик не работают)", foreground="#7f8c8d").pack(side="left", padx=(8, 0))

        btns = ttk.Frame(self.top)
        btns.pack(pady=14)
        ttk.Button(btns, text="Сохранить", command=self._on_save).pack(side="left", padx=6)
        ttk.Button(btns, text="Отмена", command=self.top.destroy).pack(side="left", padx=6)

        self.entry.focus_set()

    def _toggle_show(self) -> None:
        self.entry.configure(show="" if self.show_var.get() else "•")

    def _show_context_menu(self, event) -> None:
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _on_ctrl_v(self, event) -> str:
        self._paste_from_clipboard()
        return "break"  # предотвращает повторную стандартную вставку тем же нажатием

    def _paste_from_clipboard(self) -> None:
        """Вставляет содержимое буфера обмена напрямую, в обход системных
        горячих клавиш/меню — гарантированно работает, даже если <<Paste>>
        по какой-то причине не сработал в этом окружении."""
        try:
            text = self.top.clipboard_get()
        except tk.TclError:
            messagebox.showwarning("Буфер обмена пуст", "В буфере обмена нет текста для вставки.")
            return
        text = text.strip()
        if not text:
            messagebox.showwarning("Буфер обмена пуст", "В буфере обмена нет текста для вставки.")
            return
        # Заменяем выделенный текст, если он есть, иначе вставляем в позицию курсора
        try:
            if self.entry.selection_present():
                self.entry.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        self.entry.insert("insert", text)

    def _on_save(self) -> None:
        value = self.entry.get().strip()
        if not value:
            messagebox.showwarning("Пусто", "Токен не может быть пустым.")
            return
        self.result = value
        self.top.destroy()


# ──────────────────────────────────────────────────────────────
#  Мастер добавления источника (вкладка «Источники API»)
# ──────────────────────────────────────────────────────────────
class AddSourceDialog:
    """Диалог добавления нового API-источника без программирования.

    Поток: пользователь вводит URL и (по желанию) вставляет пример ответа →
    нажимает «Определить автоматически» → видит сгенерированное описание в
    виде читаемого JSON, может поправить его руками → «Сохранить».
    """

    def __init__(self, parent: tk.Tk):
        self.saved = False
        self.top = tk.Toplevel(parent)
        self.top.title("Добавить источник API")
        self.top.geometry("640x680")
        self.top.transient(parent)
        self.top.grab_set()

        pad = {"padx": 10, "pady": 4}

        form = ttk.Frame(self.top)
        form.pack(fill="x", **pad)

        ttk.Label(form, text="Ключ (латиницей, без пробелов):").grid(row=0, column=0, sticky="w")
        self.key_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.key_var, width=30).grid(row=0, column=1, sticky="we", padx=(6, 0))

        ttk.Label(form, text="Название (для себя):").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.title_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.title_var, width=40).grid(row=1, column=1, sticky="we", padx=(6, 0), pady=(6, 0))

        ttk.Label(form, text="URL эндпоинта:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.url_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.url_var, width=60).grid(row=2, column=1, sticky="we", padx=(6, 0), pady=(6, 0))
        ttk.Label(
            self.top,
            text="Плейсхолдеры вида {region_id} подставляются из .env "
                 "(переменная REDCAT_REGION_ID и любые REDCAT_*).",
            foreground="#7f8c8d", wraplength=610, justify="left",
        ).pack(anchor="w", padx=10)

        form.columnconfigure(1, weight=1)

        style_frame = ttk.Frame(self.top)
        style_frame.pack(fill="x", **pad)
        ttk.Label(style_frame, text="Стиль пагинации:").pack(side="left")
        self.style_var = tk.StringVar(value="jsonapi")
        style_combo = ttk.Combobox(
            style_frame, textvariable=self.style_var, state="readonly", width=45,
            values=list(srclib.PAGINATION_STYLE_LABELS.values()) if srclib else [],
        )
        style_combo.current(0)
        style_combo.pack(side="left", padx=(6, 0))
        self._style_keys = list(srclib.PAGINATION_STYLES.keys()) if srclib else []

        ttk.Label(
            self.top,
            text="Пример ответа API (необязательно, но сильно помогает — вставьте JSON "
                 "одного запроса к этому эндпоинту, можно с одной-двумя записями):",
            wraplength=610, justify="left",
        ).pack(anchor="w", padx=10, pady=(8, 2))
        self.sample_text = scrolledtext.ScrolledText(self.top, height=8, wrap="word")
        self.sample_text.pack(fill="both", padx=10, pady=(0, 6))

        ttk.Button(self.top, text="🔍 Определить автоматически", command=self._on_detect).pack(padx=10, anchor="w")

        ttk.Label(
            self.top, text="Итоговое описание (можно поправить перед сохранением):",
            wraplength=610, justify="left",
        ).pack(anchor="w", padx=10, pady=(10, 2))
        self.spec_text = scrolledtext.ScrolledText(self.top, height=12, wrap="none")
        self.spec_text.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        btns = ttk.Frame(self.top)
        btns.pack(fill="x", padx=10, pady=8)
        ttk.Button(btns, text="Сохранить", command=self._on_save).pack(side="left")
        ttk.Button(btns, text="Отмена", command=self.top.destroy).pack(side="left", padx=6)

    def _selected_style_key(self) -> str:
        idx = list(srclib.PAGINATION_STYLE_LABELS.values()).index(self.style_var.get()) \
            if self.style_var.get() in srclib.PAGINATION_STYLE_LABELS.values() else 0
        return self._style_keys[idx] if self._style_keys else "jsonapi"

    def _on_detect(self) -> None:
        key = self.key_var.get().strip()
        url = self.url_var.get().strip()
        if not key or not url:
            messagebox.showwarning("Заполните поля", "Укажите хотя бы ключ и URL.")
            return

        sample_raw = self.sample_text.get("1.0", "end").strip()
        sample_json = None
        if sample_raw:
            try:
                sample_json = json.loads(sample_raw)
            except json.JSONDecodeError as e:
                messagebox.showerror(
                    "Некорректный JSON",
                    f"Не удалось разобрать пример ответа: {e}\n\n"
                    f"Можно оставить поле пустым и описать источник вручную ниже.",
                )
                return

        spec = srclib.suggest_spec(
            url, sample_json, key=key, title=self.title_var.get().strip(),
            pagination_style=self._selected_style_key(),
        )
        self.spec_text.delete("1.0", "end")
        self.spec_text.insert("1.0", json.dumps(spec, ensure_ascii=False, indent=2))

        if sample_json is None:
            messagebox.showinfo(
                "Черновик готов",
                "Пример ответа не указан, поэтому определены только URL и стиль "
                "пагинации. Остальные поля (data_path, id_field и т.п.) можно "
                "дописать вручную по образцу в sources/README.md.",
            )

    def _on_save(self) -> None:
        raw = self.spec_text.get("1.0", "end").strip()
        if not raw:
            # если пользователь не нажал «Определить автоматически» — соберём минимум сами
            key = self.key_var.get().strip()
            url = self.url_var.get().strip()
            if not key or not url:
                messagebox.showwarning("Заполните поля", "Укажите хотя бы ключ и URL.")
                return
            spec = srclib.suggest_spec(url, None, key=key, title=self.title_var.get().strip(),
                                       pagination_style=self._selected_style_key())
        else:
            try:
                spec = json.loads(raw)
            except json.JSONDecodeError as e:
                messagebox.showerror("Некорректный JSON", f"Описание источника не читается как JSON: {e}")
                return

        try:
            path = srclib.save_source_file(SOURCES_DIR, spec)
        except (TypeError, ValueError) as e:
            messagebox.showerror("Ошибка описания", f"Источник не сохранён: {e}")
            return

        self.saved = True
        messagebox.showinfo("Готово", f"Источник сохранён: {path.name}\n"
                                       f"Он появится в общем списке и будет собираться при следующем запуске.")
        self.top.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
