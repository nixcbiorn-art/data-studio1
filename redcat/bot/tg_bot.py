"""tg_bot.py — Telegram-бот для отчётов по проблемным ЖК.
=========================================================
С кнопками:

  • Reply-клавиатура снизу — постоянные кнопки основных команд.
  • Inline-кнопки под /top, /find, /filter — каждая ЖК кликабельна,
    ведёт в карточку. Клик меняет текущее сообщение.

Запуск:
    python -m redcat.bot.tg_bot
    python -m redcat.bot.tg_bot --watch
    python -m redcat.bot.tg_bot --send-only "текст"

Требуется:
    pip install requests"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import requests
from redcat.bot.tg_api import _api_call, _get_conf, _html_escape, send_document, send_message
from redcat.bot.tg_callbacks import handle_callback
from redcat.bot.tg_commands import handle_command
from redcat.bot.tg_common import API, BOT_STARTED_AT, LOG, POLL_TIMEOUT, REPORTS, STATE, WATCH_BASELINE_MTIME_OFFSET, WATCH_INTERVAL, bot_users, log_startup_environment
from redcat.bot import tg_common
from redcat.bot.tg_reports import build_report_caption
from redcat.bot.tg_ui import main_keyboard

# ──────────────────────────────────────────────────────────────
#  Long polling
# ──────────────────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"offset": 0}

def _save_state(state: dict) -> None:
    try:
        STATE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass

def poll_loop(token: str, allowed_chats: set) -> None:
    state = _load_state()
    offset = int(state.get("offset") or 0)
    me = _api_call("getMe", token)
    if not me:
        print("❌ Токен не принят Telegram.")
        LOG.error("getMe отклонил токен")
        return
    username = me["result"].get("username") or "?"
    tg_common._BOT_USERNAME = username
    print(f"🤖 Бот @{username} запущен (long polling).")
    # Роли для тех, кто из окружения.
    # chat_id в poll_loop появляется только внутри цикла обработки
    # сообщений. Здесь берём его из окружения напрямую.
    #
    # TELEGRAM_CHAT_ID     → admin (владелец)
    # TELEGRAM_EXTRA_CHATS → viewer (гости, только чтение)
    _main_chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if bot_users is not None and _main_chat:
        if bot_users.get_role(_main_chat) is None:
            bot_users.ensure_admin(_main_chat)
            LOG.info("TELEGRAM_CHAT_ID добавлен как admin: %s",
                     _main_chat)
    # Гости из extra — viewer, если их ещё нет.
    _extra_raw = (os.environ.get("TELEGRAM_EXTRA_CHATS") or "").strip()
    if bot_users is not None and _extra_raw:
        for _cid in [x.strip() for x in _extra_raw.split(",") if x.strip()]:
            if _cid == _main_chat:
                continue
            if bot_users.get_role(_cid) is None:
                bot_users.add_user(_cid, "viewer")
                LOG.info("TELEGRAM_EXTRA_CHATS: %s → viewer", _cid)
    log_startup_environment()
    LOG.info("Бот @%s запущен", username)

    # Уведомление о запуске + устанавливаем клавиатуру снизу.
    try:
        msg = (f"🤖 <b>Бот @{_html_escape(username)} запущен</b>\n"
               f"Время: {BOT_STARTED_AT:%d.%m.%Y %H:%M:%S}\n"
               f"Снизу — кнопки основных команд.")
        for cid in allowed_chats:
            send_message(token, cid, msg, parse_mode="HTML",
                         reply_markup=main_keyboard(
                             bot_users.get_role(cid) or "viewer"
                             if bot_users else "viewer"))
    except Exception as e:
        LOG.warning("не удалось отправить уведомление о старте: %s", e)

    while True:
        try:
            r = requests.get(
                f"{API}/bot{token}/getUpdates",
                params={"offset": offset, "timeout": POLL_TIMEOUT,
                        "allowed_updates": json.dumps(
                            ["message", "edited_message", "callback_query"])},
                timeout=POLL_TIMEOUT + 10)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            LOG.warning("getUpdates: %s: %s", type(e).__name__, e)
            time.sleep(3)
            continue
        if not data.get("ok"):
            LOG.warning("getUpdates: %s", data.get("description"))
            time.sleep(3)
            continue

        for upd in data.get("result") or []:
            offset = upd["update_id"] + 1
            state["offset"] = offset
            _save_state(state)

            # 1. Inline-кнопки.
            cb = upd.get("callback_query")
            if cb:
                chat = (cb.get("message") or {}).get("chat") or {}
                cb_chat_id = str(chat.get("id") or "")
                if allowed_chats and cb_chat_id not in allowed_chats:
                    LOG.warning("callback от %s — не разрешён", cb_chat_id)
                    continue
                try:
                    handle_callback(token, cb)
                except Exception as e:
                    LOG.exception("ошибка обработки callback: %s", e)
                continue

            # 2. Обычные сообщения.
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            text = msg.get("text") or ""
            if not chat_id or not text:
                continue
            if allowed_chats and chat_id not in allowed_chats:
                LOG.warning("сообщение от %s — не в списке разрешённых",
                            chat_id)
                continue
            try:
                handle_command(token, chat_id, text,
                               role=(bot_users.get_role(chat_id)
                                     if bot_users else "viewer"))
            except Exception as e:
                LOG.exception("ошибка обработки команды: %s", e)

# ──────────────────────────────────────────────────────────────
#  Watch
# ──────────────────────────────────────────────────────────────
def watch_loop(token: str, chat_ids: list, baseline_ts: float) -> None:
    LOG.info("Следим за %s (каждые %d сек).", REPORTS, WATCH_INTERVAL)
    seen: set = set()
    while True:
        try:
            for md in REPORTS.glob("problem_jc_*.md"):
                if md.name in seen:
                    continue
                try:
                    mtime = md.stat().st_mtime
                except OSError:
                    continue
                if mtime <= baseline_ts:
                    seen.add(md.name)
                    continue
                seen.add(md.name)
                LOG.info("новый отчёт: %s", md.name)
                caption = build_report_caption(md)
                for cid in chat_ids:
                    send_document(token, cid, md, caption=caption)
                    csv = md.with_suffix(".csv")
                    if csv.exists():
                        send_document(token, cid, csv,
                                      caption="Проблемы (CSV)")
        except Exception as e:
            LOG.warning("watch: %s: %s", type(e).__name__, e)
        time.sleep(WATCH_INTERVAL)

# ──────────────────────────────────────────────────────────────
#  main
# ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Telegram-бот для проблемных ЖК")
    ap.add_argument("--send-only", metavar="TEXT")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--chat", default=None)
    ap.add_argument("--token", default=None)
    args = ap.parse_args()

    token, chat_id, extra = _get_conf()
    if args.token:
        token = args.token.strip()
    if args.chat:
        chat_id = args.chat.strip()

    if not token:
        print("❌ TELEGRAM_BOT_TOKEN не задан в .env или через --token.")
        return 1

    if args.send_only:
        if not chat_id:
            print("❌ TELEGRAM_CHAT_ID не задан.")
            return 1
        ok = send_message(token, chat_id, args.send_only, parse_mode=None)
        print("✅ отправлено" if ok else "❌ не отправлено")
        return 0 if ok else 1

    if not chat_id:
        print("❌ TELEGRAM_CHAT_ID не задан.")
        return 1

    allowed = {chat_id} | set(extra)
    baseline = time.time() - WATCH_BASELINE_MTIME_OFFSET

    if args.watch:
        threading.Thread(target=watch_loop,
                         args=(token, list(allowed), baseline),
                         daemon=True).start()

    try:
        poll_loop(token, allowed)
    except KeyboardInterrupt:
        print("\nОстановлено.")
        LOG.info("Остановлено пользователем (Ctrl+C)")
    return 0

if __name__ == "__main__":
    sys.exit(main())