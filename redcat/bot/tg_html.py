"""tg_html — генерация и выдача HTML-отчётов."""
from __future__ import annotations

from redcat.core import runner
import subprocess
from datetime import datetime
from pathlib import Path
from redcat.bot.tg_api import _html_escape, _reply, answer_callback, edit_message_text, send_document
from redcat.bot.tg_common import HERE, LOG, REPORTS
from redcat.bot.tg_reports import _subprocess_env

HTML_REPORTS = {
    "history": {
        "script": "history_dashboard.py",
        "output": REPORTS / "history.html",
        "title": "История по источникам",
        "caption": "📊 История по источникам — динамика метрик "
                   "по каждому запуску",
    },
    "completeness": {
        "script": "completeness_dashboard.py",
        "output": REPORTS / "completeness.html",
        "title": "Заполненность данных",
        "caption": "📋 Заполненность — по полям, записям и группам",
    },
    "dashboard": {
        "script": None,   # не генерируем, только отдаём готовый
        "output": REPORTS / "dashboard.html",
        "title": "Дашборд сборщика",
        "caption": "📈 Дашборд — создаётся автоматически после "
                   "каждого запуска сбора",
    },
    "stats": {
        "script": "source_stats.py",
        "output": REPORTS / "source_stats.html",
        "title": "Сверка с Redcat",
        "caption": "⇄ Сверка источников с Redcat — расхождения "
                   "по метрикам и объёмам",
        "extra_args": ["--html", str(REPORTS / "source_stats.html")],
    },
}

def _html_generate(key: str) -> tuple[bool, str, Path | None]:
    """Запускает скрипт генерации. Возвращает (ok, message, path)."""
    cfg = HTML_REPORTS.get(key)
    if not cfg:
        return False, f"❌ Неизвестный отчёт: {key}", None

    out_path = cfg["output"]
    script_name = cfg["script"]

    # dashboard.html не генерируется этой командой — только отдаётся.
    if script_name is None:
        if not out_path.exists():
            return False, (f"❌ <code>{out_path.name}</code> ещё нет. "
                           f"Запусти сбор: /run."), None
        return True, "готово", out_path

    script_path = runner.script_path(script_name)
    if not script_path.exists():
        return False, f"❌ Не найден <code>{script_name}</code>.", None

    LOG.info("HTML: запускаю %s", script_name)
    t_start = datetime.now().timestamp()
    try:
        cmd = runner.script_cmd(script_name, "--no-open")
        cmd += list(cfg.get("extra_args") or [])
        res = subprocess.run(
            cmd,
            cwd=str(HERE),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            env=_subprocess_env(),
            timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"❌ Не удалось запустить: {e}", None

    # Успех определяется не кодом возврата, а фактом: файл существует и
    # обновился после нашего запуска. source_stats.py, например, отдаёт
    # код 2, когда в данных есть критичные расхождения — это не ошибка
    # генерации, файл при этом создан.
    now = datetime.now().timestamp()
    fresh = False
    try:
        fresh = out_path.exists() and out_path.stat().st_mtime >= t_start - 1
    except OSError:
        fresh = False

    if res.returncode != 0 and not fresh:
        # Реальная ошибка: и код ненулевой, и файл не обновился.
        tail = (res.stderr or "").strip() or (res.stdout or "").strip()
        tail = tail[-1200:] if tail else f"код {res.returncode}"
        LOG.warning("HTML %s упал: %s", script_name, tail[-300:])
        return False, (f"❌ Генерация не удалась.\n\n"
                       f"<pre>{_html_escape(tail)}</pre>"), None

    if not out_path.exists():
        return False, (f"❌ Скрипт отработал, но файл "
                       f"<code>{out_path.name}</code> не найден."), None

    size = out_path.stat().st_size
    note = ""
    if res.returncode != 0:
        # Мягкий ненулевой код (например, 2 у source_stats.py) —
        # файл всё равно готов и корректен.
        note = f" (код возврата {res.returncode} — есть замечания в данных)"
        LOG.info("HTML %s готов с ненулевым кодом %d (файл обновлён)",
                 out_path.name, res.returncode)
    else:
        LOG.info("HTML %s готов: %d байт", out_path.name, size)
    return True, note or "готово", out_path

def _html_list_files() -> str:
    """Список всех .html файлов в reports/."""
    if not REPORTS.exists():
        return "Папка reports/ не найдена."
    items = sorted(REPORTS.glob("*.html"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not items:
        return ("В reports/ нет ни одного HTML-файла.\n"
                "Сгенерировать: /html history, /html completeness.\n"
                "dashboard.html появляется после запуска сбора.")
    lines = ["<b>HTML-отчёты в reports/</b>", ""]
    for p in items[:20]:
        sz = p.stat().st_size
        unit = "Б"
        if sz >= 1024 * 1024:
            sz, unit = sz // (1024 * 1024), "МБ"
        elif sz >= 1024:
            sz, unit = sz // 1024, "КБ"
        when = datetime.fromtimestamp(
            p.stat().st_mtime).strftime("%d.%m %H:%M")
        lines.append(f"• <code>{_html_escape(p.name)}</code> — "
                     f"{sz} {unit}, {when}")
    lines.append("")
    lines.append("Скачать один: /html history, /html completeness, "
                 "/html dashboard.")
    return "\n".join(lines)

def _html_menu_text() -> str:
    lines = ["<b>HTML-отчёты</b>", ""]
    for key, cfg in HTML_REPORTS.items():
        out = cfg["output"]
        mark = "✅" if out.exists() else "⬜"
        title = cfg["title"]
        if out.exists():
            when = datetime.fromtimestamp(
                out.stat().st_mtime).strftime("%d.%m %H:%M")
            lines.append(f"  {mark} <b>{title}</b> — <i>{when}</i>")
        else:
            lines.append(f"  {mark} <b>{title}</b> — ещё не создан")
    lines.append("")
    lines.append("Кликните под сообщением, чтобы сгенерировать и получить "
                 "файл. dashboard.html создаётся сборщиком после /run.")
    return "\n".join(lines)

def _html_menu_keyboard() -> dict:
    rows = []
    for key, cfg in HTML_REPORTS.items():
        label = cfg["title"]
        mark = "✅" if cfg["output"].exists() else "⬜"
        rows.append([{"text": f"{mark} {label}",
                      "callback_data": f"html:send:{key}"}])
    rows.append([
        {"text": "📂 Все файлы",
         "callback_data": "html:list"},
        {"text": "🔄 Обновить",
         "callback_data": "html:menu"},
    ])
    return {"inline_keyboard": rows}

def _handle_html_callback(token: str, chat_id: str, cb_id: str,
                          message_id, data: str) -> None:
    """Клики по /html."""
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""

    if action == "menu":
        answer_callback(token, cb_id, "обновляю")
        edit_message_text(token, chat_id, message_id,
                          _html_menu_text(),
                          reply_markup=_html_menu_keyboard())
        return

    if action == "list":
        answer_callback(token, cb_id)
        edit_message_text(token, chat_id, message_id,
                          _html_list_files(),
                          reply_markup={"inline_keyboard": [[
                              {"text": "◀ назад",
                               "callback_data": "html:menu"}]]
                          })
        return

    if action == "send" and len(parts) > 2:
        key = parts[2]
        cfg = HTML_REPORTS.get(key)
        if not cfg:
            answer_callback(token, cb_id, "неизвестный отчёт")
            return
        # Для dashboard.html — просто отправляем файл.
        if cfg["script"] is None:
            answer_callback(token, cb_id, "отправляю…")
            if not cfg["output"].exists():
                answer_callback(token, cb_id, "файла ещё нет")
                _reply(token, chat_id,
                       "❌ dashboard.html ещё не создан. "
                       "Запусти /run — сборщик его построит.")
                return
            send_document(token, chat_id, cfg["output"],
                          caption=cfg["caption"])
            return

        answer_callback(token, cb_id,
                        f"генерирую «{cfg['title']}», подожди…")
        # Редактируем сообщение — показываем, что идёт работа.
        edit_message_text(
            token, chat_id, message_id,
            f"⏳ Генерирую <b>{_html_escape(cfg['title'])}</b>…\n"
            f"Это может занять 10–30 секунд.",
            reply_markup={"inline_keyboard": []})

        ok, msg, path = _html_generate(key)
        if not ok:
            edit_message_text(token, chat_id, message_id, msg,
                              reply_markup={"inline_keyboard": [[
                                  {"text": "◀ назад к меню",
                                   "callback_data": "html:menu"}]]})
            return

        # Успех — отправляем файл и обновляем меню.
        send_document(token, chat_id, path, caption=cfg["caption"])
        edit_message_text(token, chat_id, message_id,
                          _html_menu_text(),
                          reply_markup=_html_menu_keyboard())
        return

    answer_callback(token, cb_id, "неизвестное действие")