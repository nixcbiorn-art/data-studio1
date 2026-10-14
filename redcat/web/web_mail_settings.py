"""web_mail_settings — настройки IMAP для вкладки «Почта».

Отвечает за:
  • чтение/запись REDCAT_IMAP_* в .env;
  • пробный логин в ящик (readonly, только проверка);
  • автосоздание spec источника mail_inbox.json, если его ещё нет.
"""
from __future__ import annotations

import imaplib
import json
import re

from redcat.web.web_config import ENV_FILE, SOURCES_EXT_DIR

# Готовые настройки для популярных провайдеров. Только хост/порт/подсказка,
# пароль приложения всё равно нужен свой.
PRESETS = {
    "gmail": {
        "title": "Gmail",
        "host": "imap.gmail.com",
        "port": 993,
        "hint": "Нужен пароль приложения (обычный не пустит). "
                "Включите 2FA → https://myaccount.google.com/apppasswords",
    },
    "yandex": {
        "title": "Яндекс.Почта",
        "host": "imap.yandex.ru",
        "port": 993,
        "hint": "Включите IMAP в настройках почты и создайте пароль приложения.",
    },
    "mailru": {
        "title": "Mail.ru",
        "host": "imap.mail.ru",
        "port": 993,
        "hint": "Пароль для внешних приложений (не обычный пароль входа).",
    },
    "custom": {
        "title": "Свой сервер",
        "host": "",
        "port": 993,
        "hint": "imap.<ваш-домен> — порт 993 (SSL). Уточните у администратора.",
    },
}

MAIL_KEY = "mail_inbox"

MAIL_SPEC_DEFAULT = {
    "key": MAIL_KEY,
    "title": "Почта — входящие",
    "external": True,
    "url": "imap://{imap_host}:993/INBOX?since_days=30&limit=500",
    "fetch_strategy": "imap",
    "pagination_strategy": "stop",
    "page_size_param": "",
    "page_number_param": "",
    "data_path": ["items"],
    "total_path": ["total"],
    "id_field": "message_id",
    "name_field": "subject",
    "numeric_fields": ["size", "attachments", "uid"],
    "group_fields": ["from_addr", "folder"],
    "track_fields": ["seen"],
    "required_fields": ["message_id"],
}


# ──────────────────────────────────────────────────────────────
#  .env
# ──────────────────────────────────────────────────────────────
def _read_env() -> dict:
    out: dict = {}
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
    """Обновляет ключи в .env, не трогая остальные строки и комментарии."""
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


def _clean_password(s: str) -> str:
    """Пароль приложения не содержит пробелов; убираем переносы, кавычки."""
    return re.sub(r"\s+", "", s or "").strip().strip('"').strip("'")


# ──────────────────────────────────────────────────────────────
#  Статус для формы
# ──────────────────────────────────────────────────────────────
def mail_status() -> dict:
    env = _read_env()
    host = env.get("REDCAT_IMAP_HOST", "")
    port = env.get("REDCAT_IMAP_PORT", "993")
    user = env.get("REDCAT_IMAP_USER", "")
    password = env.get("REDCAT_IMAP_PASSWORD", "")
    configured = bool(host and user and password
                      and "example.com" not in host
                      and "пароль_приложения" not in password)

    source_path = SOURCES_EXT_DIR / f"{MAIL_KEY}.json"
    source_exists = source_path.exists()

    table_rows = None
    table_exists = False
    try:
        from redcat.web.web_config import EXTERNAL_DB
        from redcat.data import dataops
        import sqlite3
        if EXTERNAL_DB.exists():
            names = {t["name"] for t in dataops.list_tables(EXTERNAL_DB)}
            if MAIL_KEY in names:
                table_exists = True
                with sqlite3.connect(f"file:{EXTERNAL_DB}?mode=ro", uri=True) as c:
                    table_rows = c.execute(
                        f'SELECT COUNT(*) FROM "{MAIL_KEY}"').fetchone()[0]
    except Exception:  # noqa: BLE001 — статус не должен падать из-за мелочей
        pass

    return {
        "host": host,
        "port": port,
        "user": user,
        "password_set": bool(password)
                        and "пароль_приложения" not in password,
        "password_preview": ("..." + password[-4:])
                            if password and len(password) > 4 else "",
        "configured": configured,
        "source_exists": source_exists,
        "source_path": str(source_path),
        "table_exists": table_exists,
        "table_rows": table_rows,
        "presets": PRESETS,
        "env_file": str(ENV_FILE),
    }


# ──────────────────────────────────────────────────────────────
#  Сохранение
# ──────────────────────────────────────────────────────────────
def save_mail_settings(body: dict) -> dict:
    host = (body.get("host") or "").strip()
    port = str(body.get("port") or "993").strip()
    user = (body.get("user") or "").strip()
    password = _clean_password(body.get("password") or "")

    if not host:
        raise ValueError("Укажите IMAP-хост.")
    if not user:
        raise ValueError("Укажите логин (адрес почты).")

    env = _read_env()
    existing_password = env.get("REDCAT_IMAP_PASSWORD", "")

    updates = {
        "REDCAT_IMAP_HOST": host,
        "REDCAT_IMAP_PORT": port,
        "REDCAT_IMAP_USER": user,
    }
    if password:
        updates["REDCAT_IMAP_PASSWORD"] = password
    elif not existing_password or "пароль_приложения" in existing_password:
        raise ValueError("Укажите пароль приложения.")
    # если пароль не вводили и он уже сохранён — оставляем как есть

    _upsert_env(updates)

    # Автосоздание spec источника (только если его ещё нет)
    spec_path = SOURCES_EXT_DIR / f"{MAIL_KEY}.json"
    if not spec_path.exists():
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(
            json.dumps(MAIL_SPEC_DEFAULT, ensure_ascii=False, indent=2),
            encoding="utf-8")

    return mail_status()


# ──────────────────────────────────────────────────────────────
#  Проверка логина
# ──────────────────────────────────────────────────────────────
def probe_mail(host: str, port: int, user: str, password: str) -> dict:
    """Открывает INBOX readonly — только для проверки. Ничего не читает,
    ничего не помечает; closes сразу после handshake."""
    out = {"ok": False, "error": None, "folder": "INBOX", "total": None}
    if not (host and user and password):
        out["error"] = "Заполните хост, логин и пароль."
        return out
    try:
        conn = imaplib.IMAP4_SSL(host, int(port or 993), timeout=20)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    try:
        typ, _ = conn.login(user, password)
        if typ != "OK":
            out["error"] = "Сервер отклонил логин или пароль."
            return out
        typ, data = conn.select('"INBOX"', readonly=True)
        if typ != "OK":
            out["error"] = "INBOX не открылся (readonly)."
            return out
        try:
            out["total"] = int(data[0])
        except (ValueError, TypeError, IndexError):
            pass
        out["ok"] = True
    except imaplib.IMAP4.error as e:
        out["error"] = str(e)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


def test_mail_settings(body: dict) -> dict:
    """Тест с параметрами из формы, а если их нет — с сохранёнными в .env."""
    env = _read_env()
    host = (body.get("host") or env.get("REDCAT_IMAP_HOST") or "").strip()
    try:
        port = int(body.get("port") or env.get("REDCAT_IMAP_PORT") or 993)
    except (TypeError, ValueError):
        port = 993
    user = (body.get("user") or env.get("REDCAT_IMAP_USER") or "").strip()
    password = (_clean_password(body.get("password") or "")
                or env.get("REDCAT_IMAP_PASSWORD", ""))
    return probe_mail(host, port, user, password)
