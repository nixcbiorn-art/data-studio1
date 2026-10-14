"""mail_bot — почтовые команды для Telegram-бота (с кнопками).

Список писем отдаётся inline-кнопками. Клик открывает карточку с
действиями (метки, ответ). Кеш последнего показанного списка хранится
в памяти процесса — после перезапуска кнопки станут неактуальны, что
нормально: пользователь просто отправит /mlist заново.

Команды:
    /mail <запрос>   — спросить (сводка, непрочитанные, от иван, ...)
    /mlist [N]       — последние N писем кнопками
"""
from __future__ import annotations

import sqlite3
from html import escape
from urllib.parse import quote

from redcat.web.web_data import db_for_table, load_specs
from redcat.web.web_mail_chat import answer, info
from redcat.web.web_mail_labels import (
    LABELS, add_label, labels_for, remove_label,
)


# ──────────────────────────────────────────────────────────────
#  Кеш списка
# ──────────────────────────────────────────────────────────────
# _LIST_CACHE[i] = {"message_id": ...}. Индекс — в callback_data.
# _LIST_TITLE — заголовок последнего списка, чтобы «◀ назад» умел
# перерисовать то же самое.
_LIST_CACHE: list = []
_LIST_TITLE: str = ""


def _mail_table() -> str:
    keys = [k for k in load_specs() if k.startswith("mail_")]
    if "mail_inbox" in keys:
        return "mail_inbox"
    return keys[0] if keys else "mail_inbox"


def _esc(s) -> str:
    return escape(str(s or ""))


def _load_message(message_id: str) -> dict | None:
    if not message_id:
        return None
    table = _mail_table()
    db = db_for_table(table)
    if not db.exists():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f'SELECT * FROM "{table}" WHERE message_id = ?',
            (message_id,)).fetchone()
        if row:
            return dict(row)
        row = conn.execute(
            f'SELECT * FROM "{table}" WHERE message_id LIKE ? '
            f'ORDER BY date DESC LIMIT 1',
            (message_id + "%",)).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _sender(row: dict) -> str:
    return row.get("from_name") or row.get("from_addr") or "?"


def _short(s: str, n: int) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[:n - 1] + "…"


# ──────────────────────────────────────────────────────────────
#  Формирование списка кнопками
# ──────────────────────────────────────────────────────────────
def _build_list(result: dict, title: str) -> tuple[str, dict | None]:
    """Возвращает (текст_заголовка, inline_keyboard | None)."""
    global _LIST_CACHE, _LIST_TITLE
    _LIST_CACHE = []
    _LIST_TITLE = title

    if not isinstance(result, dict):
        return _esc(result), None

    text = _esc(result.get("text") or "")
    rows = result.get("rows") or []
    if not rows:
        return text, None

    _LIST_CACHE = [{"message_id": r.get("message_id") or ""}
                   for r in rows]

    # Кнопки: по одной строке на письмо. Компактный номер + отправитель
    # + обрезанная тема.
    kb_rows = []
    for i, r in enumerate(rows):
        date = (r.get("date") or "")[:16]
        sender = _short(r.get("from") or r.get("from_addr") or "?", 22)
        subj = _short(r.get("subject") or "(без темы)", 50)
        icon = "📩" if "новое" in (r.get("flags") or "") else "📄"
        label = f"{icon} {i + 1}. {sender} — {subj}"
        if len(label) > 64:
            label = label[:63] + "…"
        kb_rows.append([{"text": label, "callback_data": f"mail:read:{i}"}])

    return text, {"inline_keyboard": kb_rows}


# ──────────────────────────────────────────────────────────────
#  Карточка письма
# ──────────────────────────────────────────────────────────────
def _render_message(meta: dict, idx: int) -> tuple[str, dict]:
    """Возвращает (текст_карточки, inline_keyboard)."""
    mid = meta.get("message_id") or ""
    table = _mail_table()
    labels = labels_for(table, [mid]).get(mid, [])

    # Тело — через IMAP
    body_text = ""
    body_err = None
    try:
        from redcat.collection import scrape_imap
        body = scrape_imap.fetch_message_body(
            meta.get("folder") or "INBOX", int(meta["uid"]))
        body_text = (body.get("body") or "").strip()
    except Exception as e:  # noqa: BLE001
        body_err = f"{type(e).__name__}: {e}"

    if body_text and len(body_text) > 2800:
        body_text = body_text[:2800] + "\n…(сокращено)"

    sender = _sender(meta)
    date = (meta.get("date") or "")[:16].replace("T", " ")
    from_addr = meta.get("from_addr") or ""

    head = (
        f"<b>{_esc(meta.get('subject') or '(без темы)')}</b>\n"
        f"<b>От:</b> {_esc(sender)}"
        + (f" &lt;{_esc(from_addr)}&gt;" if from_addr else "")
        + f"\n<b>Дата:</b> {_esc(date)}\n"
    )
    if meta.get("attachments"):
        head += f"<b>Вложений:</b> {meta['attachments']}\n"

    # Индикация меток в шапке
    if labels:
        badges = " ".join(LABELS[l]["icon"]
                          for l in labels if l in LABELS)
        head += f"<b>Метки:</b> {badges}\n"

    if body_err:
        body_block = f"\n<i>Тело не прочитано: {_esc(body_err)}</i>"
    elif body_text:
        body_block = f"\n<pre>{_esc(body_text)}</pre>"
    else:
        body_block = "\n<i>(тело пустое)</i>"

    text = head + body_block

    # Кнопки действий
    def _lbl(key):
        icon = LABELS[key]["icon"]
        mark = " ✓" if key in labels else ""
        return f"{icon}{mark}"

    kb = [
        [
            {"text": _lbl("star"),  "callback_data": f"mail:star:{idx}"},
            {"text": _lbl("reply"), "callback_data": f"mail:replylab:{idx}"},
            {"text": _lbl("done"),  "callback_data": f"mail:done:{idx}"},
            {"text": _lbl("todo"),  "callback_data": f"mail:todo:{idx}"},
            {"text": _lbl("spam"),  "callback_data": f"mail:spam:{idx}"},
        ],
        [
            {"text": "✉️ Ответить в Яндекс.Почте",
             "callback_data": f"mail:reply:{idx}"},
        ],
        [
            {"text": "◀ назад к списку", "callback_data": "mail:back"},
        ],
    ]
    return text, {"inline_keyboard": kb}


# ──────────────────────────────────────────────────────────────
#  Команды
# ──────────────────────────────────────────────────────────────
HELP_TEXT = (
    "<b>Почта</b>\n\n"
    "Спросите словами:\n"
    "  • <i>сводка за неделю</i>\n"
    "  • <i>непрочитанные</i>\n"
    "  • <i>с вложениями за месяц</i>\n"
    "  • <i>от иван</i> · <i>тема заказ</i>\n"
    "  • <i>топ 5 отправителей</i>\n"
    "  • <i>последние 10</i>\n\n"
    "Или операторами:\n"
    "  <code>from:ivan subject:договор after:2025-01-01</code>\n"
    "  <code>unseen:true has:attach label:star</code>\n\n"
    "<b>Команды:</b>\n"
    "<code>/mlist [N]</code> — последние N (10 по умолчанию)\n"
    "<code>/mail запрос</code> — спросить"
)


def _cmd_mail(args: list) -> tuple[str, dict | None]:
    query = " ".join(args).strip()
    if not query:
        return HELP_TEXT, None
    try:
        result = answer(query)
    except Exception as e:  # noqa: BLE001
        return f"Ошибка: <code>{_esc(type(e).__name__)}: {_esc(e)}</code>", None
    title = f"Запрос: {query}"
    return _build_list(result, title)


def _cmd_mlist(args: list) -> tuple[str, dict | None]:
    n = 10
    if args:
        try:
            n = max(1, min(50, int(args[0])))
        except (ValueError, IndexError):
            pass
    try:
        result = answer(f"последние {n}")
    except Exception as e:  # noqa: BLE001
        return f"Ошибка: <code>{_esc(type(e).__name__)}: {_esc(e)}</code>", None
    title = f"Последние {n}"
    return _build_list(result, title)


HANDLERS = {
    "/mail": _cmd_mail,
    "/mlist": _cmd_mlist,
}

MAIL_COMMANDS = frozenset(HANDLERS.keys())


def handle_mail_command(cmd: str, args: list,
                        chat_id: str = ""):
    """Если cmd — почтовая команда, вернёт (text, keyboard) или (text, None).
    Иначе None."""
    fn = HANDLERS.get(cmd)
    if not fn:
        return None
    try:
        return fn(args)
    except Exception as e:  # noqa: BLE001
        return f"Ошибка: <code>{_esc(type(e).__name__)}: {_esc(e)}</code>", None


# ──────────────────────────────────────────────────────────────
#  Обработчик inline-кнопок
# ──────────────────────────────────────────────────────────────
def handle_mail_callback(token: str, chat_id: str, cb_id: str,
                         message_id, data: str) -> None:
    """Клики по mail:* — обрабатывает tg_callbacks."""
    from redcat.bot.tg_api import (
        answer_callback, edit_message_text, send_message,
    )
    from redcat.bot.tg_common import LOG

    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""

    if action == "back":
        # Перерисовать заголовок списка без кнопок писем (их уже не вернуть
        # — сообщение отредактировано на карточку). Отправляем подсказку.
        answer_callback(token, cb_id, "отправьте /mlist заново")
        edit_message_text(
            token, chat_id, message_id,
            "<i>Список можно открыть заново — отправьте</i> "
            "<code>/mlist 10</code> <i>или ваш запрос ещё раз.</i>",
            reply_markup={"inline_keyboard": []})
        return

    if not arg.isdigit():
        answer_callback(token, cb_id, "плохой индекс")
        return
    idx = int(arg)
    if idx < 0 or idx >= len(_LIST_CACHE):
        answer_callback(token, cb_id,
                        "Кнопка устарела. Отправьте /mlist заново.")
        return

    mid = _LIST_CACHE[idx].get("message_id")
    meta = _load_message(mid)
    if not meta:
        answer_callback(token, cb_id, "письмо не найдено")
        return

    table = _mail_table()

    if action == "read":
        answer_callback(token, cb_id, "загружаю…")
        text, kb = _render_message(meta, idx)
        edit_message_text(token, chat_id, message_id, text,
                          reply_markup=kb)
        return

    if action in ("star", "done", "todo", "reply", "spam"):
        label = action
        current = labels_for(table, [mid]).get(mid, [])
        if label in current:
            remove_label(table, mid, label)
            on = False
        else:
            add_label(table, mid, label)
            on = True
        icon = LABELS[label]["icon"]
        answer_callback(token, cb_id,
                        f"{icon} {'поставлено' if on else 'снято'}")
        # Перерисовать карточку с обновлёнными метками
        text, kb = _render_message(meta, idx)
        edit_message_text(token, chat_id, message_id, text,
                          reply_markup=kb)
        return

    if action == "replylab":
        # Синоним: метка reply (для симметрии с остальными).
        label = "reply"
        current = labels_for(table, [mid]).get(mid, [])
        if label in current:
            remove_label(table, mid, label)
            on = False
        else:
            add_label(table, mid, label)
            on = True
        answer_callback(token, cb_id,
                        f"{LABELS[label]['icon']} "
                        f"{'поставлено' if on else 'снято'}")
        text, kb = _render_message(meta, idx)
        edit_message_text(token, chat_id, message_id, text,
                          reply_markup=kb)
        return

    if action == "reply":
        answer_callback(token, cb_id, "открываю…")
        to_addr = meta.get("from_addr") or ""
        subj = meta.get("subject") or ""
        if not to_addr:
            send_message(token, chat_id,
                         "<i>У письма нет обратного адреса.</i>")
            return
        url = ("https://mail.yandex.ru/compose?to=" + quote(to_addr)
               + ("&subject=" + quote("Re: " + subj) if subj else ""))
        send_message(
            token, chat_id,
            f"<b>Ответ на письмо</b>\n"
            f"<i>{_esc(subj)}</i>\n\n"
            f"Кому: <code>{_esc(to_addr)}</code>\n\n"
            f'<a href="{_esc(url)}">Открыть в Яндекс.Почте →</a>')
        return

    answer_callback(token, cb_id, "неизвестное действие")
