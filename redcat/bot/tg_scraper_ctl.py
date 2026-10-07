"""tg_scraper_ctl — управление процессом сборщика (запуск, монитор, статус)."""
from __future__ import annotations

from redcat.core import runner
import json
import os
import subprocess
import threading
from datetime import datetime
from redcat.bot.tg_api import _html_escape, _reply, send_message
from redcat.bot.tg_common import HERE, LOG, bot_users
from redcat.bot.tg_reports import _subprocess_env
from redcat.bot.tg_ui import main_keyboard

# ──────────────────────────────────────────────────────────────
#  Запуск сбора (redcat_scraper.py)
# ──────────────────────────────────────────────────────────────
# Сбор — длинный процесс (минуты). Запускаем его в фоне: Popen пишет
# stdout/stderr в tg_run.log, отдельный поток ждёт завершения и потом
# шлёт инициатору короткий итог. Состояние запуска хранится в
# tg_run_state.json, чтобы /run_status отвечал даже между перезапусками
# бота. Кнопка «▶️ /run» добавляет аргументы по умолчанию: --only apartments,
# чтобы не гонять всю коллекцию. Передать свои аргументы — /run с текстом.
SCRAPER_SCRIPT = runner.script_path("redcat_scraper.py")

SCRAPER_LOG = HERE / "tg_run.log"

RUN_STATE = HERE / "tg_run_state.json"

_SCRAPER_PROC = None          # Popen текущего запуска (в этом процессе бота)

_SCRAPER_LOCK = threading.Lock()

def _load_run_state() -> dict:
    if not RUN_STATE.exists():
        return {}
    try:
        return json.loads(RUN_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def _save_run_state(state: dict) -> None:
    try:
        RUN_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    except OSError:
        pass

def _process_alive(pid: int) -> bool:
    """Проверяет, жив ли процесс с данным PID.

    На Windows os.kill(pid, 0) НЕ «проверяет» — он вызывает
    TerminateProcess и УБИВАЕТ процесс. Поэтому на Windows
    используем OpenProcess + GetExitCodeProcess через ctypes.
    На POSIX os.kill(pid, 0) — штатный способ.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(
                        handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # Если ctypes не сработал — считаем, что процесса нет,
            # чтобы не запускать второй сбор поверх.
            return False

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def _scraper_is_running() -> bool:
    """Живой ли сейчас запуск. Проверяем и объект, и state-файл."""
    with _SCRAPER_LOCK:
        if _SCRAPER_PROC is not None and _SCRAPER_PROC.poll() is None:
            return True
    st = _load_run_state()
    if not st.get("running"):
        return False
    if not _process_alive(st.get("pid")):
        st["running"] = False
        _save_run_state(st)
        return False
    return True

def _scraper_monitor(proc, chat_id: str, token: str) -> None:
    """Ждёт завершения subprocess, обновляет state, шлёт итог."""
    try:
        code = proc.wait()
    except Exception as e:
        LOG.exception("monitor: proc.wait упал: %s", e)
        code = -1

    st = _load_run_state()
    st["running"] = False
    st["finished_at"] = datetime.now().isoformat(timespec="seconds")
    st["exit_code"] = code
    _save_run_state(st)

    elapsed = ""
    try:
        if st.get("started_at"):
            t0 = datetime.fromisoformat(st["started_at"])
            dt = (datetime.now() - t0).total_seconds()
            elapsed = f" ({dt:.0f} сек)"
    except (ValueError, TypeError):
        pass

    # 3221225786 = 0xC000013A = STATUS_CONTROL_C_EXIT. Это Ctrl+C,
    # не реальная ошибка сбора.
    if code == 3221225786:
        head = f"⏹ <b>Сбор прерван</b> (Ctrl+C){elapsed}."
    elif code == 0:
        head = f"✅ <b>Сбор завершён</b>{elapsed}."
    else:
        head = f"❌ <b>Сбор упал</b> с кодом {code}{elapsed}."

    tail = ""
    if SCRAPER_LOG.exists():
        try:
            lines = SCRAPER_LOG.read_text(
                encoding="utf-8", errors="replace").splitlines()[-20:]
            tail = "\n".join(lines[-15:])
        except OSError:
            pass

    body = head
    if tail:
        body += f"\n\n<pre>{_html_escape(tail[-1500:])}</pre>"
    body += "\n\nНапишите /run_status для деталей."

    try:
        send_message(token, chat_id, body, parse_mode="HTML")
    except Exception as e:
        LOG.warning("monitor: не удалось отправить итог: %s", e)

def _scraper_start(chat_id: str, token: str, extra_args: list) -> tuple[bool, str]:
    """Запускает сбор. Возвращает (ok, текст_для_ответа)."""
    global _SCRAPER_PROC

    if not SCRAPER_SCRIPT.exists():
        return False, f"❌ Не найден <code>{SCRAPER_SCRIPT.name}</code>."

    if _scraper_is_running():
        st = _load_run_state()
        started = st.get("started_at") or "?"
        args = " ".join(st.get("args") or [])
        return False, (f"⏳ Сбор уже идёт.\n"
                       f"Начат: {_html_escape(started)}\n"
                       f"Аргументы: <code>{_html_escape(args or '—')}</code>\n"
                       f"Дождитесь завершения или посмотрите /run_status.")

    # Аргументы по умолчанию, если не передали ни одного: только квартиры.
    args = list(extra_args) if extra_args else ["--only", "apartments"]
    # Проверка admin-only команд.
    if bot_users is not None and cmd in bot_users.ADMIN_ONLY:
        if role != "admin":
            _reply(token, chat_id,
                   f"⛔ Команда <code>{cmd}</code> только для "
                   f"администраторов. Ваша роль: <b>{role}</b>.",
                   reply_markup=main_keyboard("viewer"))
            return

    # Пробуем писать stdout/stderr в файл, чтобы процесс не завис на PIPE.
    try:
        log_fh = open(SCRAPER_LOG, "w", encoding="utf-8", errors="replace")
    except OSError as e:
        return False, f"❌ Не могу открыть {SCRAPER_LOG.name}: {e}"

    try:
        proc = subprocess.Popen(
            runner.script_cmd("redcat_scraper.py", *args, unbuffered=True),
            cwd=str(HERE),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=_subprocess_env(),
            # Windows: своя консольная группа, чтобы Ctrl+C в окне
            # бота не убивал долгий сбор. На POSIX флаг отсутствует.
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt" else 0),
        
        )
    except OSError as e:
        log_fh.close()
        return False, f"❌ Не удалось запустить: {e}"

    with _SCRAPER_LOCK:
        _SCRAPER_PROC = proc

    st = {
        "running": True,
        "pid": proc.pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "exit_code": None,
        "args": args,
        "initiator": chat_id,
        "log_file": SCRAPER_LOG.name,
    }
    _save_run_state(st)

    threading.Thread(target=_scraper_monitor,
                     args=(proc, chat_id, token),
                     daemon=True).start()

    LOG.info("Запущен сбор: args=%s pid=%s", args, proc.pid)
    return True, (f"▶️ <b>Сбор запущен</b>\n"
                  f"PID: {proc.pid}\n"
                  f"Аргументы: <code>{_html_escape(' '.join(args))}</code>\n"
                  f"Лог: <code>{SCRAPER_LOG.name}</code>\n\n"
                  f"По завершении пришлю итог. Проверить вручную — "
                  f"/run_status.")

def _scraper_status_text() -> str:
    st = _load_run_state()
    if not st:
        return ("Сбор ещё не запускался. Запустить — /run "
                "(по умолчанию: только apartments).")
    running = bool(st.get("running")) and _scraper_is_running()
    lines = ["<b>Сбор</b>"]
    lines.append(f"Статус: " + ("⏳ идёт" if running else "✅ завершён"))
    if st.get("started_at"):
        lines.append(f"Начат: {_html_escape(st['started_at'])}")
    if st.get("finished_at") and not running:
        lines.append(f"Закончен: {_html_escape(st['finished_at'])}")
    if st.get("exit_code") is not None and not running:
        lines.append(f"Код возврата: {st['exit_code']}")
    if st.get("args"):
        lines.append(f"Аргументы: <code>{_html_escape(' '.join(st['args']))}</code>")
    if st.get("pid"):
        lines.append(f"PID: {st['pid']}")
    # Хвост лога.
    if SCRAPER_LOG.exists():
        try:
            tail = SCRAPER_LOG.read_text(
                encoding="utf-8", errors="replace").splitlines()[-15:]
            if tail:
                lines.append("")
                lines.append("<b>Хвост лога:</b>")
                lines.append(f"<pre>{_html_escape(chr(10).join(tail)[-1500:])}</pre>")
        except OSError:
            pass
    return "\n".join(lines)