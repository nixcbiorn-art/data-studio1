"""web_mail_notify_filters — фильтры «о каких письмах уведомлять».

Хранятся в studio.db, таблица mail_notify_filters. Логика:
  • письмо уведомляется, если проходит ХОТЯ БЫ одно из правил «включить»;
  • либо если НЕ подпадает ни под одно из «исключить».

Простой набор правил для типовой работы:
  from_contains  — подстрока в адресе отправителя
  subject_contains — подстрока в теме
  only_unread    — только непрочитанные
  only_attach    — только с вложениями
  skip_mailing   — не уведомлять про рассылки (no-reply, noreply, do-not-reply)
  mode           — "whitelist" (уведомлять только подходящие) или
                   "blacklist" (уведомлять все, кроме подходящих)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from redcat.web.web_config import STUDIO_DB


DEFAULT = {
    "enabled": True,
    "mode": "blacklist",
    "from_contains": "",
    "subject_contains": "",
    "only_unread": True,
    "only_attach": False,
    "skip_mailing": True,
    "browser_push": False,
}


def _ensure_schema() -> None:
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS mail_notify_filters (
            id INTEGER PRIMARY KEY CHECK (id=1),
            enabled INTEGER NOT NULL DEFAULT 1,
            mode TEXT NOT NULL DEFAULT 'blacklist',
            from_contains TEXT DEFAULT '',
            subject_contains TEXT DEFAULT '',
            only_unread INTEGER NOT NULL DEFAULT 1,
            only_attach INTEGER NOT NULL DEFAULT 0,
            skip_mailing INTEGER NOT NULL DEFAULT 1,
            browser_push INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT)""")
        conn.commit()
        row = conn.execute("SELECT id FROM mail_notify_filters").fetchone()
        if not row:
            conn.execute(
                "INSERT INTO mail_notify_filters "
                "(id, enabled, mode, from_contains, subject_contains, "
                " only_unread, only_attach, skip_mailing, browser_push, "
                " updated_at) VALUES (1,?,?,?,?,?,?,?,?,?)",
                (1, DEFAULT["mode"], "", "", 1, 0, 1, 0,
                 datetime.now().isoformat(timespec="seconds")))
            conn.commit()


def get() -> dict:
    _ensure_schema()
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM mail_notify_filters WHERE id=1"
                         ).fetchone()
    if not r:
        return dict(DEFAULT)
    return {
        "enabled": bool(r["enabled"]),
        "mode": r["mode"] or "blacklist",
        "from_contains": r["from_contains"] or "",
        "subject_contains": r["subject_contains"] or "",
        "only_unread": bool(r["only_unread"]),
        "only_attach": bool(r["only_attach"]),
        "skip_mailing": bool(r["skip_mailing"]),
        "browser_push": bool(r["browser_push"]),
    }


def set_fields(**fields) -> dict:
    _ensure_schema()
    allowed = {"enabled", "mode", "from_contains", "subject_contains",
               "only_unread", "only_attach", "skip_mailing", "browser_push"}
    updates = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("enabled", "only_unread", "only_attach",
                 "skip_mailing", "browser_push"):
            updates[k] = 1 if v else 0
        elif k == "mode":
            updates[k] = v if v in ("whitelist", "blacklist") else "blacklist"
        else:
            updates[k] = str(v or "")

    if not updates:
        return {"ok": False, "error": "нет полей"}

    updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
    sets = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values())
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute(f"UPDATE mail_notify_filters SET {sets} WHERE id=1", vals)
        conn.commit()
    return {"ok": True}


_MAILING_MARKERS = ("no-reply", "noreply", "do-not-reply", "donotreply",
                    "mailer-daemon", "notifications@", "info@",
                    "newsletter", "mailing", "marketing", "bounce",
                    "auto@", "robot@", "support+", "@mail.360.yandex")


def should_notify(row: dict, cfg: dict | None = None) -> tuple[bool, str]:
    """Письмо (dict со subject, from_addr, seen, attachments) —
    уведомлять или нет. Возвращает (ok, reason)."""
    cfg = cfg or get()
    if not cfg.get("enabled"):
        return False, "фильтры выключены"

    from_addr = (row.get("from_addr") or "").lower()
    subject = (row.get("subject") or "").lower()
    seen = bool(row.get("seen"))
    att = int(row.get("attachments") or 0)

    # Рассылки — отдельный быстрый отсев
    if cfg.get("skip_mailing"):
        for m in _MAILING_MARKERS:
            if m in from_addr:
                return False, f"рассылка ({m})"

    # Базовые условия
    if cfg.get("only_unread") and seen:
        return False, "уже прочитано"
    if cfg.get("only_attach") and att == 0:
        return False, "без вложений"

    # Матч по подстрокам
    f_needle = (cfg.get("from_contains") or "").lower().strip()
    s_needle = (cfg.get("subject_contains") or "").lower().strip()
    matches = []
    if f_needle and f_needle in from_addr:
        matches.append("from")
    if s_needle and s_needle in subject:
        matches.append("subject")
    has_needles = bool(f_needle or s_needle)

    mode = cfg.get("mode") or "blacklist"

    if mode == "whitelist":
        if not has_needles:
            # Условий нет — считаем, что уведомлять можно
            return True, "ок (без условий)"
        if matches:
            return True, "совпадает: " + ",".join(matches)
        return False, "не совпадает ни по одному условию"

    # blacklist
    if has_needles and matches:
        return False, "исключено: " + ",".join(matches)
    return True, "ок"
