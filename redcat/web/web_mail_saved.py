"""web_mail_saved — сохранение писем как .eml.

Файлы в reports/saved/<date>_<short_hash>.eml
Каталог в studio.db, таблица saved_messages.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from redcat.web.web_config import BASE_DIR, STUDIO_DB


SAVED_DIR = BASE_DIR / "reports" / "saved"


def _ensure_schema() -> None:
    SAVED_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS saved_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            subject TEXT,
            from_addr TEXT,
            date TEXT,
            filename TEXT NOT NULL,
            size INTEGER,
            tags TEXT,
            note TEXT,
            saved_at TEXT NOT NULL)""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_saved_msg
            ON saved_messages(message_id)""")
        conn.commit()


def _safe_filename(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^\wА-Яа-яЁё .\-_]+", "_", s or "").strip()
    s = re.sub(r"[\s_]+", "_", s)
    return s[:maxlen] or "message"


def save_from_imap(table_name: str, message_id: str,
                   folder: str, uid: int,
                   subject: str = "", from_addr: str = "",
                   date: str = "", tags: str = "", note: str = "") -> dict:
    """Скачивает .eml целиком через IMAP и сохраняет в reports/saved/."""
    _ensure_schema()

    # Уже есть?
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, filename FROM saved_messages WHERE message_id=?",
            (message_id,)).fetchone()
    if row:
        return {"ok": True, "id": row["id"], "filename": row["filename"],
                "existing": True}

    try:
        from redcat.collection import scrape_imap
        raw = scrape_imap.fetch_message_raw(folder or "INBOX", int(uid))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if not raw:
        return {"ok": False, "error": "IMAP вернул пустое письмо"}

    # Имя файла: дата_тема_хэш.eml
    short_hash = hashlib.md5(str(message_id).encode("utf-8")).hexdigest()[:8]
    date_part = (date or "")[:10].replace("-", "") or \
                datetime.now().strftime("%Y%m%d")
    name_part = _safe_filename(subject, 40)
    filename = f"{date_part}_{name_part}_{short_hash}.eml"
    target = SAVED_DIR / filename

    try:
        target.write_bytes(raw)
    except OSError as e:
        return {"ok": False, "error": f"не записал файл: {e}"}

    size = len(raw)
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        cur = conn.execute(
            "INSERT INTO saved_messages "
            "(message_id, subject, from_addr, date, filename, size, "
            " tags, note, saved_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (message_id, subject, from_addr, date, filename, size,
             tags, note, now))
        conn.commit()
        sid = cur.lastrowid

    return {"ok": True, "id": sid, "filename": filename, "size": size}


def list_saved(query: str = "", limit: int = 500) -> list:
    _ensure_schema()
    where, args = ["1=1"], []
    if query:
        where.append(
            "(subject LIKE ? OR from_addr LIKE ? OR tags LIKE ? "
            "OR note LIKE ? OR filename LIKE ?)")
        args += [f"%{query}%"] * 5
    sql = (f"SELECT * FROM saved_messages WHERE {' AND '.join(where)} "
           f"ORDER BY saved_at DESC LIMIT ?")
    args.append(limit)
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_saved(save_id: int) -> dict | None:
    _ensure_schema()
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM saved_messages WHERE id=?",
                         (save_id,)).fetchone()
    return dict(r) if r else None


def update_meta(save_id: int, tags: str = None,
                note: str = None) -> dict:
    _ensure_schema()
    fields, vals = [], []
    if tags is not None:
        fields.append("tags=?"); vals.append(tags)
    if note is not None:
        fields.append("note=?"); vals.append(note)
    if not fields:
        return {"ok": False, "error": "нет полей"}
    vals.append(save_id)
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute(
            f"UPDATE saved_messages SET {', '.join(fields)} WHERE id=?",
            vals)
        conn.commit()
    return {"ok": True}


def delete_saved(save_id: int, remove_file: bool = True) -> dict:
    _ensure_schema()
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT filename FROM saved_messages WHERE id=?",
                           (save_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "не найдено"}
        filename = row["filename"]
        conn.execute("DELETE FROM saved_messages WHERE id=?", (save_id,))
        conn.commit()
    if remove_file:
        p = SAVED_DIR / filename
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}


def file_path(save_id: int) -> Path | None:
    s = get_saved(save_id)
    if not s:
        return None
    p = SAVED_DIR / s["filename"]
    return p if p.exists() else None


def stats() -> dict:
    _ensure_schema()
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        r = conn.execute(
            "SELECT COUNT(*), SUM(size), MAX(saved_at) FROM saved_messages"
        ).fetchone()
    return {"count": r[0] or 0, "size": r[1] or 0, "last": r[2]}
