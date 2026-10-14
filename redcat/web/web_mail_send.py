"""web_mail_send — отправка писем через SMTP из .env."""
from __future__ import annotations

import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from redcat.web.web_config import ENV_FILE


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


def _addrs(raw: str) -> list:
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[;,\s]+", raw)
            if p.strip() and "@" in p]


def smtp_ready() -> dict:
    env = _read_env()
    host = env.get("REDCAT_SMTP_HOST", "")
    user = env.get("REDCAT_SMTP_USER", "")
    pw = env.get("REDCAT_SMTP_PASSWORD", "")
    ok = bool(host and user and pw
              and "example.com" not in host
              and "пароль_приложения" not in pw)
    return {"configured": ok, "host": host,
            "port": env.get("REDCAT_SMTP_PORT", "465"),
            "user": user,
            "mode": env.get("REDCAT_SMTP_MODE", "ssl")}


def _connect():
    env = _read_env()
    host = env.get("REDCAT_SMTP_HOST", "")
    port = int(env.get("REDCAT_SMTP_PORT", "465") or 465)
    user = env.get("REDCAT_SMTP_USER", "")
    pw = env.get("REDCAT_SMTP_PASSWORD", "")
    mode = (env.get("REDCAT_SMTP_MODE", "ssl") or "ssl").lower()
    if not (host and user and pw):
        raise RuntimeError("SMTP не настроен (REDCAT_SMTP_* в .env)")
    ctx = ssl.create_default_context()
    if mode == "ssl":
        conn = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
    else:
        conn = smtplib.SMTP(host, port, timeout=30)
        if mode == "starttls":
            conn.ehlo()
            conn.starttls(context=ctx)
            conn.ehlo()
    conn.login(user, pw)
    return conn


def send(to: str, subject: str, body: str,
         cc: str = "", bcc: str = "",
         in_reply_to: str = "") -> dict:
    """Отправляет письмо. Параметры:
      to, cc, bcc    — адреса
      subject, body  — содержимое
      in_reply_to    — message_id письма, на которое отвечаем.
                       Если по этому письму есть задача — она
                       автоматически перейдёт в статус in_progress
                       (patch_reply_triggers_task.py).
    """
    extra = {"in_reply_to": in_reply_to}
    to_list = _addrs(to)
    if not to_list:
        return {"ok": False, "error": "Не указан получатель."}
    if not (body or "").strip() and not (subject or "").strip():
        return {"ok": False, "error": "Пустое письмо."}

    env = _read_env()
    from_addr = env.get("REDCAT_SMTP_USER", "")
    from_name = env.get("REDCAT_SMTP_FROM_NAME", "")

    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = ", ".join(to_list)
    cc_list = _addrs(cc)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    bcc_list = _addrs(bcc)
    if bcc_list:
        msg["Bcc"] = ", ".join(bcc_list)
    msg["Subject"] = subject or "(без темы)"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body or "")

    try:
        conn = _connect()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    try:
        all_rcpts = to_list + cc_list + bcc_list
        conn.send_message(msg, to_addrs=all_rcpts)
        return {"ok": True, "error": None,
                "message_id": msg["Message-ID"], "to": to_list}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        try: conn.quit()
        except Exception: pass
