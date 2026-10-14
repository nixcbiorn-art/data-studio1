"""web_mail_labels — локальные метки и шаблоны ответов.

Метки и шаблоны живут в studio.db, не трогают почтовый сервер:
  • метки работают даже когда IMAP только для чтения;
  • метки не видны в Яндексе/Gmail — это ваш личный рабочий слой;
  • действия не сломают ящик и не потребуют разрешений.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from redcat.web.web_config import STUDIO_DB

LABELS = {
    "star":  {"icon": "⭐", "title": "Важное"},
    "reply": {"icon": "📩", "title": "Ответить"},
    "done":  {"icon": "✅", "title": "Готово"},
    "spam":  {"icon": "🚫", "title": "Спам"},
    "todo":  {"icon": "📌", "title": "В работу"},
}


def _ensure_schema() -> None:
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS mail_labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            message_id TEXT NOT NULL,
            label TEXT NOT NULL,
            ts TEXT NOT NULL,
            UNIQUE(table_name, message_id, label))""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_mail_labels_msg
            ON mail_labels(table_name, message_id)""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_mail_labels_lbl
            ON mail_labels(table_name, label)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS mail_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            subject_prefix TEXT DEFAULT '',
            body TEXT NOT NULL,
            ts TEXT NOT NULL)""")
        conn.commit()


def add_label(table_name: str, message_id: str, label: str) -> bool:
    if label not in LABELS:
        raise ValueError(f"Неизвестная метка: {label}")
    _ensure_schema()
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO mail_labels "
            "(table_name, message_id, label, ts) VALUES (?,?,?,?)",
            (table_name, message_id, label,
             datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    # Хук: авто-создание задачи при определённых метках
    try:
        from redcat.web.web_mail_label_tasks import on_label_added
        on_label_added(table_name, message_id, label)
    except Exception:  # noqa: BLE001 — метка важнее, хук не должен ломать
        pass
    return True


def remove_label(table_name: str, message_id: str, label: str) -> bool:
    _ensure_schema()
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute(
            "DELETE FROM mail_labels WHERE table_name=? AND message_id=? "
            "AND label=?", (table_name, message_id, label))
        conn.commit()
    return True


def labels_for(table_name: str, message_ids: list) -> dict:
    if not message_ids:
        return {}
    _ensure_schema()
    out: dict = {}
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        placeholders = ",".join("?" for _ in message_ids)
        try:
            rows = conn.execute(
                f"SELECT message_id, label FROM mail_labels "
                f"WHERE table_name=? AND message_id IN ({placeholders})",
                [table_name] + list(message_ids)).fetchall()
        except sqlite3.Error:
            return {}
    for mid, lbl in rows:
        out.setdefault(mid, []).append(lbl)
    return out


def messages_with_label(table_name: str, label: str) -> list:
    _ensure_schema()
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        try:
            rows = conn.execute(
                "SELECT message_id FROM mail_labels "
                "WHERE table_name=? AND label=?",
                (table_name, label)).fetchall()
        except sqlite3.Error:
            return []
    return [r[0] for r in rows]


def counts_by_label(table_name: str) -> dict:
    _ensure_schema()
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        try:
            rows = conn.execute(
                "SELECT label, COUNT(*) FROM mail_labels "
                "WHERE table_name=? GROUP BY label",
                (table_name,)).fetchall()
        except sqlite3.Error:
            return {}
    return {lbl: n for lbl, n in rows}


def list_templates() -> list:
    _ensure_schema()
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, name, subject_prefix, body, ts "
                "FROM mail_templates ORDER BY name").fetchall()
        except sqlite3.Error:
            return []
    return [dict(r) for r in rows]


def save_template(name: str, body: str, subject_prefix: str = "") -> dict:
    name = (name or "").strip()
    body = (body or "").strip()
    if not name:
        raise ValueError("Укажите название шаблона.")
    if not body:
        raise ValueError("Тело шаблона пустое.")
    _ensure_schema()
    ts = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute(
            "INSERT INTO mail_templates (name, subject_prefix, body, ts) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "subject_prefix=excluded.subject_prefix, "
            "body=excluded.body, ts=excluded.ts",
            (name, subject_prefix or "", body, ts))
        conn.commit()
    return {"ok": True, "name": name}


def delete_template(name: str) -> dict:
    _ensure_schema()
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute("DELETE FROM mail_templates WHERE name=?", (name,))
        conn.commit()
    return {"ok": True}


def apply_template(name: str, row: dict) -> dict:
    _ensure_schema()
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        tpl = conn.execute(
            "SELECT * FROM mail_templates WHERE name=?",
            (name,)).fetchone()
    if not tpl:
        raise ValueError(f"Шаблон «{name}» не найден.")

    subj = tpl["subject_prefix"] or ""
    if "{subject}" in subj:
        subj = subj.replace("{subject}", row.get("subject") or "")
    if subj and not subj.lower().startswith("re:"):
        subj = "Re: " + subj

    text = tpl["body"] or ""
    for key in ("from_name", "from_addr", "subject", "date"):
        text = text.replace("{" + key + "}", str(row.get(key) or ""))

    return {"subject": subj, "body": text, "name": tpl["name"]}
