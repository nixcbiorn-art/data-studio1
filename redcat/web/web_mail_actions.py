"""web_mail_actions — действия с письмами и отправка через SMTP.

ВНИМАНИЕ: mark_read / mark_unread / delete ИЗМЕНЯЮТ ваш ящик на сервере.
Они выключены по умолчанию и включаются только явной галочкой в настройках.
Каждое действие пишется в studio.db (таблица mail_actions).

Ответ через SMTP — это новое исходящее письмо, оно ничего не меняет в
существующих. Тоже под галочкой, чтобы случайно не отправить.
"""
from __future__ import annotations

import imaplib
import json
import re
import smtplib
import ssl
import sqlite3
from datetime import datetime
from email.message import EmailMessage

from redcat.web.web_config import ENV_FILE, STUDIO_DB
from redcat.web.web_data import db_for_table, load_specs


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


def _upsert_env(updates: dict) -> None:
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    seen = set()
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k = s.split("=", 1)[0].strip()
        if k in updates:
            lines[i] = f"{k}={updates[k]}"
            seen.add(k)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clean(s: str) -> str:
    return re.sub(r"\s+", "", s or "").strip().strip('"').strip("'")


# ──────────────────────────────────────────────────────────────
#  Политика действий — хранится в .env, чтобы не терялась
# ──────────────────────────────────────────────────────────────
def read_policy() -> dict:
    env = _read_env()
    return {
        "allow_mark": env.get("REDCAT_MAIL_ALLOW_MARK", "0") == "1",
        "allow_delete": env.get("REDCAT_MAIL_ALLOW_DELETE", "0") == "1",
        "allow_send": env.get("REDCAT_MAIL_ALLOW_SEND", "0") == "1",
    }


def save_policy(body: dict) -> dict:
    updates = {}
    if "allow_mark" in body:
        updates["REDCAT_MAIL_ALLOW_MARK"] = "1" if body["allow_mark"] else "0"
    if "allow_delete" in body:
        updates["REDCAT_MAIL_ALLOW_DELETE"] = "1" if body["allow_delete"] else "0"
    if "allow_send" in body:
        updates["REDCAT_MAIL_ALLOW_SEND"] = "1" if body["allow_send"] else "0"
    if updates:
        _upsert_env(updates)
    return read_policy()


# ──────────────────────────────────────────────────────────────
#  SMTP-настройки
# ──────────────────────────────────────────────────────────────
SMTP_PRESETS = {
    "gmail": {"host": "smtp.gmail.com", "port": 587, "mode": "starttls"},
    "yandex": {"host": "smtp.yandex.ru", "port": 465, "mode": "ssl"},
    "mailru": {"host": "smtp.mail.ru", "port": 465, "mode": "ssl"},
    "custom": {"host": "", "port": 587, "mode": "starttls"},
}


def smtp_status() -> dict:
    env = _read_env()
    host = env.get("REDCAT_SMTP_HOST", "")
    port = env.get("REDCAT_SMTP_PORT", "587")
    user = env.get("REDCAT_SMTP_USER", "")
    password = env.get("REDCAT_SMTP_PASSWORD", "")
    mode = env.get("REDCAT_SMTP_MODE", "starttls")
    configured = bool(host and user and password
                      and "example.com" not in host
                      and "пароль_приложения" not in password)
    return {
        "host": host, "port": port, "user": user, "mode": mode,
        "password_set": bool(password) and "пароль_приложения" not in password,
        "password_preview": ("..." + password[-4:])
                            if password and len(password) > 4 else "",
        "configured": configured,
        "presets": SMTP_PRESETS,
    }


def save_smtp(body: dict) -> dict:
    host = (body.get("host") or "").strip()
    port = str(body.get("port") or "587").strip()
    user = (body.get("user") or "").strip()
    password = _clean(body.get("password") or "")
    mode = (body.get("mode") or "starttls").strip().lower()
    if mode not in ("ssl", "starttls", "plain"):
        mode = "starttls"

    if not host: raise ValueError("Укажите SMTP-хост.")
    if not user: raise ValueError("Укажите логин.")

    env = _read_env()
    existing = env.get("REDCAT_SMTP_PASSWORD", "")
    updates = {
        "REDCAT_SMTP_HOST": host,
        "REDCAT_SMTP_PORT": port,
        "REDCAT_SMTP_USER": user,
        "REDCAT_SMTP_MODE": mode,
    }
    if password:
        updates["REDCAT_SMTP_PASSWORD"] = password
    elif not existing or "пароль_приложения" in existing:
        raise ValueError("Укажите пароль приложения.")

    _upsert_env(updates)
    return smtp_status()


# ──────────────────────────────────────────────────────────────
#  Лог действий
# ──────────────────────────────────────────────────────────────
def _log_action(action: str, folder: str, uid: int, subject: str,
                ok: bool, error: str = "") -> None:
    try:
        with sqlite3.connect(str(STUDIO_DB)) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS mail_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, action TEXT NOT NULL,
                folder TEXT, uid INTEGER, subject TEXT,
                ok INTEGER, error TEXT)""")
            conn.execute(
                "INSERT INTO mail_actions (ts, action, folder, uid, subject, "
                "ok, error) VALUES (?,?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"),
                 action, folder, uid, (subject or "")[:200],
                 1 if ok else 0, error))
            conn.commit()
    except sqlite3.Error:
        pass


def list_actions(limit: int = 200) -> list[dict]:
    if not STUDIO_DB.exists():
        return []
    try:
        with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM mail_actions ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
            except sqlite3.Error:
                return []
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


# ──────────────────────────────────────────────────────────────
#  IMAP: пометить, удалить
# ──────────────────────────────────────────────────────────────
def _open_imap_writable(folder: str):
    """Открывает папку в режиме READ-WRITE. Это уже не readonly — действия
    меняют состояние ящика. Вызывается только если пользователь явно
    включил соответствующее разрешение."""
    host = _read_env().get("REDCAT_IMAP_HOST", "")
    port = int(_read_env().get("REDCAT_IMAP_PORT", "993") or 993)
    user = _read_env().get("REDCAT_IMAP_USER", "")
    password = _read_env().get("REDCAT_IMAP_PASSWORD", "")
    if not (host and user and password):
        raise RuntimeError("IMAP не настроен.")
    conn = imaplib.IMAP4_SSL(host, port, timeout=30)
    conn.login(user, password)
    typ, _ = conn.select(f'"{folder}"', readonly=False)
    if typ != "OK":
        try: conn.logout()
        except Exception: pass
        raise RuntimeError(f"Папка «{folder}» не открылась на запись.")
    return conn


def mark_message(folder: str, uid: int, subject: str, seen: bool) -> dict:
    policy = read_policy()
    if not policy["allow_mark"]:
        return {"ok": False,
                "error": "Действие выключено. Включите «Разрешить помечать "
                         "письма» в настройках почты."}
    try:
        conn = _open_imap_writable(folder)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        op = "+FLAGS" if seen else "-FLAGS"
        typ, _ = conn.uid("STORE", str(uid), op, "(\\Seen)")
        ok = typ == "OK"
        _log_action("mark_read" if seen else "mark_unread",
                    folder, uid, subject, ok,
                    "" if ok else "STORE вернул " + str(typ))
        return {"ok": ok, "error": None if ok else "IMAP отклонил STORE"}
    except Exception as e:  # noqa: BLE001
        _log_action("mark", folder, uid, subject, False, str(e))
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        try: conn.logout()
        except Exception: pass


def delete_message(folder: str, uid: int, subject: str,
                   expunge: bool = True) -> dict:
    policy = read_policy()
    if not policy["allow_delete"]:
        return {"ok": False,
                "error": "Удаление выключено. Включите «Разрешить удалять "
                         "письма» в настройках почты."}
    try:
        conn = _open_imap_writable(folder)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        typ, _ = conn.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
        if typ != "OK":
            _log_action("delete", folder, uid, subject, False,
                        "STORE \\Deleted вернул " + str(typ))
            return {"ok": False, "error": "STORE \\Deleted не сработал"}
        if expunge:
            typ2, _ = conn.expunge()
            if typ2 != "OK":
                _log_action("delete", folder, uid, subject, False,
                            "EXPUNGE вернул " + str(typ2))
                return {"ok": False,
                        "error": "Помечено удалённым, но EXPUNGE не прошёл."}
        _log_action("delete", folder, uid, subject, True)
        return {"ok": True, "error": None}
    except Exception as e:  # noqa: BLE001
        _log_action("delete", folder, uid, subject, False, str(e))
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        try: conn.logout()
        except Exception: pass


# ──────────────────────────────────────────────────────────────
#  SMTP: ответ / пересылка
# ──────────────────────────────────────────────────────────────
def _smtp_connect():
    env = _read_env()
    host = env.get("REDCAT_SMTP_HOST", "")
    port = int(env.get("REDCAT_SMTP_PORT", "587") or 587)
    user = env.get("REDCAT_SMTP_USER", "")
    password = env.get("REDCAT_SMTP_PASSWORD", "")
    mode = env.get("REDCAT_SMTP_MODE", "starttls").lower()
    if not (host and user and password):
        raise RuntimeError("SMTP не настроен. Заполните в настройках почты.")

    ctx = ssl.create_default_context()
    if mode == "ssl":
        conn = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
    else:
        conn = smtplib.SMTP(host, port, timeout=30)
        if mode == "starttls":
            conn.starttls(context=ctx)
    conn.login(user, password)
    return conn


def send_reply(to_addr: str, subject: str, body: str,
               in_reply_to: str = "", references: str = "") -> dict:
    policy = read_policy()
    if not policy["allow_send"]:
        return {"ok": False,
                "error": "Отправка выключена. Включите «Разрешить отправку "
                         "писем» в настройках почты."}
    to_addr = (to_addr or "").strip()
    if not to_addr:
        return {"ok": False, "error": "Не указан получатель."}
    if not body.strip():
        return {"ok": False, "error": "Пустое тело письма."}

    env = _read_env()
    from_addr = env.get("REDCAT_SMTP_USER", "")
    if not from_addr:
        return {"ok": False, "error": "Не задан SMTP-логин."}

    subj = subject or ""
    if subj and not subj.lower().startswith("re:"):
        subj = "Re: " + subj

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subj
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)

    try:
        conn = _smtp_connect()
    except Exception as e:  # noqa: BLE001
        _log_action("send", "", 0, subj, False, str(e))
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        conn.send_message(msg)
        _log_action("send", "", 0, subj, True)
        return {"ok": True, "error": None, "subject": subj, "to": to_addr}
    except Exception as e:  # noqa: BLE001
        _log_action("send", "", 0, subj, False, str(e))
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        try: conn.quit()
        except Exception: pass


def test_smtp() -> dict:
    """Подключается и логинится. Ничего не отправляет."""
    try:
        conn = _smtp_connect()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        conn.noop()
        return {"ok": True, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        try: conn.quit()
        except Exception: pass


# ──────────────────────────────────────────────────────────────
#  Период сбора: переписываем since_days в спеке
# ──────────────────────────────────────────────────────────────
def set_collect_period(days: int) -> dict:
    from redcat.web.web_config import SOURCES_EXT_DIR
    try:
        days = max(1, min(3650, int(days)))
    except (TypeError, ValueError):
        raise ValueError("Период должен быть числом дней.")
    path = SOURCES_EXT_DIR / "mail_inbox.json"
    if not path.exists():
        raise ValueError("Источник mail_inbox.json не найден. Сохраните "
                         "настройки почты заново.")
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"Не читается spec: {e}")
    url = spec.get("url") or ""
    if "since_days=" in url:
        url = re.sub(r"since_days=\d+", f"since_days={days}", url)
    else:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}since_days={days}"
    spec["url"] = url
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return {"ok": True, "days": days, "url": url}
