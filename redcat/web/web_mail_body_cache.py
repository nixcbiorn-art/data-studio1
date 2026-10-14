"""web_mail_body_cache — кэш тел писем в studio.db.

Яндекс закрывает IMAP-соединения при частых запросах. Читаем тело
один раз, держим локально. Дальше — из кэша.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from redcat.web.web_config import STUDIO_DB


def _ensure_schema() -> None:
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS mail_bodies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            message_id TEXT NOT NULL,
            body TEXT,
            content_type TEXT,
            truncated INTEGER DEFAULT 0,
            ts TEXT NOT NULL,
            accessed_at TEXT NOT NULL,
            UNIQUE(table_name, message_id))""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_mail_bodies_msg
            ON mail_bodies(table_name, message_id)""")
        conn.commit()


def _get_cached(table_name: str, message_id: str):
    _ensure_schema()
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT body, content_type, truncated, ts "
            "FROM mail_bodies WHERE table_name=? AND message_id=?",
            (table_name, message_id)).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE mail_bodies SET accessed_at=? "
            "WHERE table_name=? AND message_id=?",
            (datetime.now().isoformat(timespec="seconds"),
             table_name, message_id))
        conn.commit()
        return {
            "body": row["body"],
            "content_type": row["content_type"],
            "truncated": bool(row["truncated"]),
            "cached_at": row["ts"],
        }


def _save_cache(table_name: str, message_id: str, body: dict) -> None:
    _ensure_schema()
    ts = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute(
            "INSERT INTO mail_bodies "
            "(table_name, message_id, body, content_type, truncated, "
            " ts, accessed_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(table_name, message_id) DO UPDATE SET "
            "body=excluded.body, content_type=excluded.content_type, "
            "truncated=excluded.truncated, ts=excluded.ts, "
            "accessed_at=excluded.accessed_at",
            (table_name, message_id,
             body.get("body") or "",
             body.get("content_type") or "",
             1 if body.get("truncated") else 0,
             ts, ts))
        conn.commit()


def get_body(table_name: str, message_id: str, folder: str, uid: int,
             force_refresh: bool = False) -> dict:
    """Возвращает тело. Сначала — из кэша; если нет или force_refresh,
    читает из IMAP. При сбое IMAP отдаёт старый кэш, если он есть."""
    if not force_refresh:
        cached = _get_cached(table_name, message_id)
        if cached is not None and cached["body"] is not None:
            cached["from_cache"] = True
            cached["error"] = None
            return cached

    try:
        from redcat.collection import scrape_imap
        result = scrape_imap.fetch_message_body(folder or "INBOX", int(uid))
    except Exception as e:  # noqa: BLE001
        cached = _get_cached(table_name, message_id)
        if cached is not None and cached["body"] is not None:
            cached["from_cache"] = True
            cached["error"] = f"{type(e).__name__}: {e} (старый кэш)"
            return cached
        return {
            "body": None, "content_type": None, "truncated": False,
            "from_cache": False, "cached_at": None,
            "error": f"{type(e).__name__}: {e}",
        }

    _save_cache(table_name, message_id, result)
    return {
        "body": result.get("body"),
        "content_type": result.get("content_type"),
        "truncated": bool(result.get("truncated")),
        "from_cache": False,
        "cached_at": datetime.now().isoformat(timespec="seconds"),
        "error": None,
    }


def stats() -> dict:
    _ensure_schema()
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT COUNT(*), SUM(LENGTH(body)), MIN(ts), MAX(ts) "
            "FROM mail_bodies").fetchone()
    if not row or not row[0]:
        return {"count": 0, "size": 0, "oldest": None, "newest": None}
    return {"count": row[0], "size": row[1] or 0,
            "oldest": row[2], "newest": row[3]}


def purge_older_than(days: int) -> int:
    cutoff = (datetime.now() - timedelta(days=days)
              ).isoformat(timespec="seconds")
    _ensure_schema()
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        cur = conn.execute(
            "DELETE FROM mail_bodies WHERE accessed_at < ?", (cutoff,))
        conn.commit()
        return cur.rowcount


def clear_all() -> int:
    _ensure_schema()
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        cur = conn.execute("DELETE FROM mail_bodies")
        conn.commit()
        return cur.rowcount
