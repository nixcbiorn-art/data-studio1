"""tg_api — обёртки над Telegram Bot API (отправка, правка, callback, документы)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import requests
from redcat.bot.tg_common import API, ENV, LOG, REQUEST_TIMEOUT

# ──────────────────────────────────────────────────────────────
#  .env и конфиг
# ──────────────────────────────────────────────────────────────
def _read_env() -> dict:
    values: dict = {}
    if not ENV.exists():
        return values
    for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values

def _get_conf():
    env = _read_env()
    token = (env.get("TELEGRAM_BOT_TOKEN")
             or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (env.get("TELEGRAM_CHAT_ID")
               or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    extra = (env.get("TELEGRAM_EXTRA_CHATS")
             or os.environ.get("TELEGRAM_EXTRA_CHATS") or "").strip()
    return token, chat_id, [c.strip() for c in extra.split(",") if c.strip()]

# ──────────────────────────────────────────────────────────────
#  Telegram API
# ──────────────────────────────────────────────────────────────
def _api_call(method: str, token: str, **payload) -> dict | None:
    url = f"{API}/bot{token}/{method}"
    try:
        r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        LOG.warning("%s: %s: %s", method, type(e).__name__, e)
        return None
    try:
        data = r.json()
    except ValueError:
        LOG.warning("%s: ответ не JSON (HTTP %s)", method, r.status_code)
        return None
    if not data.get("ok"):
        LOG.warning("%s: Telegram отклонил: %s",
                    method, data.get("description"))
        return None
    return data

def _split_text(s: str, limit: int) -> list[str]:
    if len(s) <= limit:
        return [s]
    parts, buf = [], s
    while buf:
        if len(buf) <= limit:
            parts.append(buf)
            break
        cut = buf.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(buf[:cut])
        buf = buf[cut:].lstrip("\n")
    return parts

def send_message(token: str, chat_id: str, text: str,
                 parse_mode: str | None = "HTML",
                 reply_markup: dict | None = None) -> bool:
    chunks = _split_text(text, 4000)
    ok_all = True
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk,
                   "disable_web_page_preview": "true"}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        # reply_markup — только на первой части, чтобы не дублировать.
        if reply_markup and i == 0:
            payload["reply_markup"] = json.dumps(reply_markup)
        ok_all &= bool(_api_call("sendMessage", token, **payload))
    return ok_all

def edit_message_text(token: str, chat_id: str, message_id: int, text: str,
                      reply_markup: dict | None = None) -> bool:
    payload = {"chat_id": chat_id, "message_id": message_id,
               "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": "true"}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    return bool(_api_call("editMessageText", token, **payload))

_SILENT_CALLBACK_ERRORS = (
    "query is too old",
    "response timeout expired",
    "query ID is invalid",
    "message is not modified",
)

def answer_callback(token: str, cb_id: str, text: str = "") -> None:
    """Отвечает на callback. Молча глотает «устаревшие» ошибки."""
    payload = {"callback_query_id": cb_id}
    if text:
        payload["text"] = text
    url = f"{API}/bot{token}/answerCallbackQuery"
    try:
        r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        LOG.warning("answerCallbackQuery: %s: %s", type(e).__name__, e)
        return
    try:
        data = r.json()
    except ValueError:
        return
    if data.get("ok"):
        return
    desc = str(data.get("description") or "")
    for frag in _SILENT_CALLBACK_ERRORS:
        if frag in desc:
            LOG.debug("answerCallbackQuery: %s (тихо)", desc)
            return
    LOG.warning("answerCallbackQuery: %s", desc)

def send_document(token: str, chat_id: str, path: Path,
                  caption: str = "") -> bool:
    if not path.exists():
        return False
    url = f"{API}/bot{token}/sendDocument"
    try:
        with path.open("rb") as fh:
            r = requests.post(url, data={
                "chat_id": chat_id, "caption": caption[:1000],
                "parse_mode": "HTML",
            }, files={"document": (path.name, fh)}, timeout=60)
    except requests.RequestException as e:
        LOG.warning("sendDocument: %s: %s", type(e).__name__, e)
        return False
    try:
        data = r.json()
    except ValueError:
        return False
    if not data.get("ok"):
        LOG.warning("sendDocument: %s", data.get("description"))
        return False
    return True

def _html_escape(s) -> str:
    return (str(s or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))

# ──────────────────────────────────────────────────────────────
#  Обработчик команд
# ──────────────────────────────────────────────────────────────
def _reply(token: str, chat_id: str, text: str,
           reply_markup: dict | None = None) -> None:
    send_message(token, chat_id, text, parse_mode="HTML",
                 reply_markup=reply_markup)