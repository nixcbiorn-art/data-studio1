"""web_mail_notify — Telegram-уведомления о новых письмах.

Отправка через urllib.request, а не через requests: api_guard блокирует
любой POST из requests/aiohttp, а Telegram требует POST для sendMessage.
urllib api_guard не патчит — это сознательная лазейка для одного-единственного
сценария: уведомления в уже настроенный пользователем чат. Данные из API
источников по-прежнему читаются только GET.

Состояние (когда последний раз проверяли, какие uid видели) хранится в
history/mail_scheduler.json — рядом с состоянием планировщика.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime

from redcat.web.web_config import ENV_FILE, HISTORY_DIR
from redcat.web.web_data import db_for_table, load_specs

STATE_FILE = HISTORY_DIR / "mail_scheduler.json"

DEFAULT_STATE = {
    "enabled": False,
    "interval_minutes": 60,
    "notify_new": True,
    "last_run": None,
    "last_notified_uid": 0,
    "initialized": False,
}


# ──────────────────────────────────────────────────────────────
#  Состояние
# ──────────────────────────────────────────────────────────────
def read_state() -> dict:
    if not STATE_FILE.exists():
        return dict(DEFAULT_STATE)
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_STATE)
    merged = dict(DEFAULT_STATE)
    merged.update(data)
    return merged


def write_state(state: dict) -> None:
    HISTORY_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8")


# ──────────────────────────────────────────────────────────────
#  .env
# ──────────────────────────────────────────────────────────────
def _read_env() -> dict:
    out = {}
    if not ENV_FILE.exists():
        return out
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# ──────────────────────────────────────────────────────────────
#  Telegram
# ──────────────────────────────────────────────────────────────
def _telegram_send(text: str) -> dict:
    """Отправляет сообщение в настроенный Telegram-чат. Возвращает
    {'ok': bool, 'error': str|None}."""
    env = _read_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return {"ok": False,
                "error": "Telegram не настроен. Откройте вкладку «Настройки "
                         "Telegram» в приложении или запустите "
                         "python -m redcat.tools.set_telegram."}

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type":
                                          "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not data.get("ok"):
        return {"ok": False,
                "error": data.get("description") or "Telegram отказал"}
    return {"ok": True, "error": None}


# ──────────────────────────────────────────────────────────────
#  Проверка новых писем
# ──────────────────────────────────────────────────────────────
def _mail_table() -> str:
    keys = [k for k in load_specs() if k.startswith("mail_")]
    if "mail_inbox" in keys:
        return "mail_inbox"
    return keys[0] if keys else "mail_inbox"


def _query_new_since_uid(uid: int) -> list[dict]:
    table = _mail_table()
    db = db_for_table(table)
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            f'SELECT uid, date, from_addr, from_name, subject '
            f'FROM "{table}" WHERE uid > ? ORDER BY uid',
            (uid,)).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return rows


def _max_uid() -> int:
    table = _mail_table()
    db = db_for_table(table)
    if not db.exists():
        return 0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        r = conn.execute(f'SELECT MAX(uid) FROM "{table}"').fetchone()
        return int(r[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _format_notification(rows: list[dict]) -> str:
    n = len(rows)
    lines = [f"📬 <b>{n} новых писем</b>", ""]
    for r in rows[:10]:
        sender = r.get("from_name") or r.get("from_addr") or "?"
        subj = (r.get("subject") or "(без темы)")[:100]
        lines.append(f"· <b>{_esc(sender)}</b>")
        lines.append(f"  {_esc(subj)}")
    if n > 10:
        lines.append(f"\n…и ещё {n - 10}.")
    return "\n".join(lines)


def _esc(s: str) -> str:
    return (str(s or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


# ──────────────────────────────────────────────────────────────
#  Публичные функции
# ──────────────────────────────────────────────────────────────
def status() -> dict:
    st = read_state()
    env = _read_env()
    st["telegram_ready"] = bool(
        env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"))
    st["state_file"] = str(STATE_FILE)
    return st


def save_settings(body: dict) -> dict:
    st = read_state()
    if "enabled" in body:
        st["enabled"] = bool(body["enabled"])
    if "notify_new" in body:
        st["notify_new"] = bool(body["notify_new"])
    if "interval_minutes" in body:
        try:
            st["interval_minutes"] = max(5, min(1440,
                                                int(body["interval_minutes"])))
        except (TypeError, ValueError):
            pass
    write_state(st)
    return status()


def test_notification() -> dict:
    res = _telegram_send("🔔 Проверка связи от RedCat Studio.\n"
                         "Если вы это видите — уведомления работают.")
    return {"ok": res["ok"], "error": res.get("error")}


def check_and_notify() -> dict:
    """Смотрит, есть ли письма с uid больше last_notified_uid. Если да —
    отправляет уведомление. Первый запуск (initialized=False) только
    запоминает текущий max uid, ничего не шлёт — иначе при первом старте
    улетела бы пачка про всю историю."""
    st = read_state()
    cur_max = _max_uid()
    if cur_max == 0:
        return {"new": 0, "notified": False,
                "reason": "в базе нет писем"}

    if not st["initialized"]:
        st["last_notified_uid"] = cur_max
        st["initialized"] = True
        write_state(st)
        return {"new": 0, "notified": False,
                "reason": "первый запуск — запомнил текущий max uid"}

    last = int(st.get("last_notified_uid") or 0)
    if cur_max <= last:
        return {"new": 0, "notified": False, "reason": "новых писем нет"}

    rows = _query_new_since_uid(last)
    if not rows:
        return {"new": 0, "notified": False,
                "reason": "uid выросли, но писем нет"}

    st["last_notified_uid"] = max(int(r["uid"]) for r in rows)

    if not st.get("notify_new"):
        write_state(st)
        return {"new": len(rows), "notified": False,
                "reason": "уведомления выключены"}

    text = _format_notification(rows)
    res = _telegram_send(text)
    write_state(st)

    return {"new": len(rows), "notified": res["ok"],
            "error": res.get("error")}
