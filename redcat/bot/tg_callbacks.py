"""tg_callbacks — обработчики inline-кнопок."""
from __future__ import annotations

from redcat.bot.tg_api import _reply, answer_callback, edit_message_text
from redcat.bot.tg_common import LOG
from redcat.bot.tg_fill import _handle_fill_callback
from redcat.bot.tg_html import _handle_html_callback
from redcat.bot.tg_reports import _format_card_full
from redcat.bot.tg_scraper_ctl import _scraper_start
from redcat.bot.tg_settings import CONCURRENCY_CYCLE, DEFAULT_SETTINGS, SENSITIVITY_CYCLE, _list_known_sources, _load_settings, _save_settings, _settings_keyboard, _settings_text, _settings_to_args
from redcat.bot import tg_ui

# ──────────────────────────────────────────────────────────────
#  Обработка inline-кнопок
# ──────────────────────────────────────────────────────────────
def _handle_settings_callback(token: str, chat_id: str, cb_id: str,
                              message_id, data: str) -> None:
    """Обрабатывает клики по /settings."""
    s = _load_settings()
    srcs = _list_known_sources()
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    page_offset = 0
    page_size = 10

    if action == "toggle_src":
        chosen = set(s.get("sources") or [])
        if arg in chosen:
            chosen.discard(arg)
        else:
            chosen.add(arg)
        s["sources"] = sorted(chosen)
        _save_settings(s)
        answer_callback(token, cb_id, "изменено")

    elif action == "toggle_opt":
        if arg in ("excel", "no_anomalies"):
            s[arg] = not bool(s.get(arg))
            _save_settings(s)
        answer_callback(token, cb_id, "изменено")

    elif action == "cycle_conc":
        cur = int(s.get("concurrency") or 0)
        try:
            idx = CONCURRENCY_CYCLE.index(cur)
        except ValueError:
            idx = 0
        s["concurrency"] = CONCURRENCY_CYCLE[
            (idx + 1) % len(CONCURRENCY_CYCLE)]
        _save_settings(s)
        answer_callback(token, cb_id, f"concurrency={s['concurrency']}")

    elif action == "cycle_sens":
        cur = float(s.get("sensitivity") or 0)
        try:
            idx = SENSITIVITY_CYCLE.index(cur)
        except ValueError:
            idx = 0
        s["sensitivity"] = SENSITIVITY_CYCLE[
            (idx + 1) % len(SENSITIVITY_CYCLE)]
        _save_settings(s)
        answer_callback(token, cb_id, f"sensitivity={s['sensitivity']}")

    elif action == "reset":
        s = dict(DEFAULT_SETTINGS)
        _save_settings(s)
        answer_callback(token, cb_id, "сброшено")

    elif action == "page":
        try:
            page_offset = max(0, int(arg))
        except ValueError:
            page_offset = 0
        answer_callback(token, cb_id)

    elif action == "run":
        answer_callback(token, cb_id, "запускаю…")
        args = _settings_to_args(s)
        ok, msg = _scraper_start(chat_id, token, args)
        try:
            edit_message_text(
                token, chat_id, message_id,
                _settings_text(s, srcs)
                + "\n\n" + ("▶️ запущено" if ok else "⚠️ не запущено"),
                reply_markup={"inline_keyboard": []})
        except Exception:
            pass
        _reply(token, chat_id, msg)
        return

    elif action == "noop":
        answer_callback(token, cb_id)
        return

    else:
        answer_callback(token, cb_id, "неизвестное действие")
        return

    edit_message_text(token, chat_id, message_id,
                      _settings_text(s, srcs),
                      reply_markup=_settings_keyboard(s, srcs,
                                                      offset=page_offset,
                                                      page=page_size))

def handle_callback(token: str, cb: dict) -> None:
    cb_id = cb.get("id") or ""
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    message_id = msg.get("message_id")

    # Быстро гасим часики в клиенте.
    answer_callback(token, cb_id)

    if not chat_id or not isinstance(message_id, int):
        return

    if data.startswith("jc:"):
        try:
            idx = int(data[3:])
        except ValueError:
            return
        if idx < 0 or idx >= len(tg_ui._CARDS_CACHE):
            # Кнопка из старого сообщения, кэш уже не тот.
            answer_callback(token, cb_id,
                            "Кнопка устарела. Отправьте /top заново.")
            return
        c = tg_ui._CARDS_CACHE[idx]
        new_text = _format_card_full(c)
        # Кнопка "◀ назад" — показать список заново.
        back_kb = {"inline_keyboard": [[
            {"text": "◀ назад к списку", "callback_data": "back"}
        ]]}
        edit_message_text(token, chat_id, message_id,
                          new_text, reply_markup=back_kb)
        LOG.info("callback jc:%d (%s) — карточка отправлена",
                 idx, c.get("jc", "?"))

    elif data.startswith("set:"):
        _handle_settings_callback(token, chat_id, cb_id,
                                  message_id, data)
        return

    elif data.startswith("html:"):
        _handle_html_callback(token, chat_id, cb_id,
                              message_id, data)
        return

    elif data.startswith("fill:"):
        _handle_fill_callback(token, chat_id, cb_id,
                              message_id, data)
        return

    elif data == "back":
        # Просто подсказка: список уже был выше.
        answer_callback(token, cb_id,
                        "Список — в сообщении выше. /top или /find заново.")
        LOG.info("callback back")

    else:
        LOG.warning("неизвестный callback: %r", data[:60])