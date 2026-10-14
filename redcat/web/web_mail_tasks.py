"""web_mail_tasks — задачи из писем.

Таблицы в studio.db:
  mail_tasks       — задачи (id, message_id, title, status, priority,
                     due_date, assignee, note, created_at, updated_at)
  mail_task_rules  — правила (id, name, match_from, match_subject,
                     match_body, priority, due_days, enabled, created_at)

Задача привязана к message_id (тема письма). Одна задача на письмо.
Если нужно несколько — вручную из UI, через /mail_tasks.html.
"""
from __future__ import annotations

import sqlite3
import re
from datetime import datetime, timedelta

from redcat.web.web_config import STUDIO_DB

STATUSES = ("open", "in_progress", "done", "cancelled")
PRIORITIES = ("low", "normal", "high", "urgent")


def _ensure_schema() -> None:
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS mail_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT,
            subject TEXT,
            from_addr TEXT,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            priority TEXT NOT NULL DEFAULT 'normal',
            due_date TEXT,
            assignee TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(message_id))""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_tasks_status
            ON mail_tasks(status)""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_tasks_due
            ON mail_tasks(due_date)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS mail_task_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            match_from TEXT,
            match_subject TEXT,
            match_body TEXT,
            priority TEXT NOT NULL DEFAULT 'normal',
            due_days INTEGER,
            assignee TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL)""")
        conn.commit()


# ──────────────────────────────────────────────────────────────
#  Задачи
# ──────────────────────────────────────────────────────────────
def create_task(message_id: str, subject: str, from_addr: str,
                title: str = "", priority: str = "normal",
                due_date: str = "", assignee: str = "",
                note: str = "") -> dict:
    _ensure_schema()
    if not title:
        title = subject or "(без темы)"
    if priority not in PRIORITIES:
        priority = "normal"
    now = datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        try:
            cur = conn.execute(
                "INSERT INTO mail_tasks "
                "(message_id, subject, from_addr, title, status, priority, "
                " due_date, assignee, note, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (message_id or None, subject, from_addr, title,
                 "open", priority, due_date, assignee, note, now, now))
            conn.commit()
            return {"ok": True, "id": cur.lastrowid}
        except sqlite3.IntegrityError:
            # Уже есть задача по этому письму
            row = conn.execute(
                "SELECT id FROM mail_tasks WHERE message_id=?",
                (message_id,)).fetchone()
            return {"ok": True, "id": row[0] if row else None,
                    "existing": True}


def update_task(task_id: int, **fields) -> dict:
    _ensure_schema()
    allowed = {"title", "status", "priority", "due_date",
               "assignee", "note"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return {"ok": False, "error": "нет полей для обновления"}
    if "status" in updates and updates["status"] not in STATUSES:
        return {"ok": False, "error": f"неверный статус: {updates['status']}"}
    if "priority" in updates and updates["priority"] not in PRIORITIES:
        return {"ok": False, "error": f"неверный приоритет: {updates['priority']}"}

    updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
    sets = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [task_id]
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute(f"UPDATE mail_tasks SET {sets} WHERE id=?", vals)
        conn.commit()
    return {"ok": True}


def delete_task(task_id: int) -> dict:
    _ensure_schema()
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute("DELETE FROM mail_tasks WHERE id=?", (task_id,))
        conn.commit()
    return {"ok": True}


def get_task(task_id: int) -> dict | None:
    _ensure_schema()
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM mail_tasks WHERE id=?",
                           (task_id,)).fetchone()
    return dict(row) if row else None


def list_tasks(status: str = "", assignee: str = "",
               priority: str = "", due_before: str = "",
               query: str = "", limit: int = 500) -> list:
    _ensure_schema()
    where, args = ["1=1"], []
    if status:
        if status == "active":
            where.append("status IN ('open','in_progress')")
        else:
            where.append("status = ?"); args.append(status)
    if assignee:
        where.append("assignee = ?"); args.append(assignee)
    if priority:
        where.append("priority = ?"); args.append(priority)
    if due_before:
        where.append("due_date <= ? AND due_date <> ''")
        args.append(due_before)
    if query:
        where.append("(title LIKE ? OR subject LIKE ? OR from_addr LIKE ? "
                     "OR note LIKE ?)")
        args += [f"%{query}%"] * 4
    sql = (f"SELECT * FROM mail_tasks WHERE {' AND '.join(where)} "
           f"ORDER BY CASE status WHEN 'open' THEN 0 "
           f"WHEN 'in_progress' THEN 1 WHEN 'done' THEN 2 ELSE 3 END, "
           f"CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
           f"WHEN 'normal' THEN 2 ELSE 3 END, "
           f"due_date, id DESC LIMIT ?")
    args.append(limit)
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def counts() -> dict:
    _ensure_schema()
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM mail_tasks GROUP BY status"
        ).fetchall()
        overdue = conn.execute(
            "SELECT COUNT(*) FROM mail_tasks "
            "WHERE status IN ('open','in_progress') "
            "AND due_date <> '' AND due_date < ?",
            (datetime.now().date().isoformat(),)).fetchone()[0]
    out = {s: 0 for s in STATUSES}
    for s, n in rows:
        out[s] = n
    out["overdue"] = overdue
    out["total"] = sum(n for _, n in rows)
    return out


# ──────────────────────────────────────────────────────────────
#  Правила
# ──────────────────────────────────────────────────────────────
def create_rule(name: str, match_from: str = "", match_subject: str = "",
                match_body: str = "", priority: str = "normal",
                due_days: int | None = None, assignee: str = "",
                enabled: bool = True) -> dict:
    _ensure_schema()
    if priority not in PRIORITIES:
        priority = "normal"
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        cur = conn.execute(
            "INSERT INTO mail_task_rules "
            "(name, match_from, match_subject, match_body, priority, "
            " due_days, assignee, enabled, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name or "правило", match_from, match_subject, match_body,
             priority, due_days, assignee, 1 if enabled else 0, now))
        conn.commit()
    return {"ok": True, "id": cur.lastrowid}


def update_rule(rule_id: int, **fields) -> dict:
    _ensure_schema()
    allowed = {"name", "match_from", "match_subject", "match_body",
               "priority", "due_days", "assignee", "enabled"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return {"ok": False, "error": "нет полей"}
    sets = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [rule_id]
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute(f"UPDATE mail_task_rules SET {sets} WHERE id=?", vals)
        conn.commit()
    return {"ok": True}


def delete_rule(rule_id: int) -> dict:
    _ensure_schema()
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute("DELETE FROM mail_task_rules WHERE id=?", (rule_id,))
        conn.commit()
    return {"ok": True}


def list_rules() -> list:
    _ensure_schema()
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM mail_task_rules ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────────────────────
#  Триггер: отправлен ответ → задача в работе (patch_reply_triggers_task.py)
# ──────────────────────────────────────────────────────────────
def task_in_progress_from_reply(message_id: str,
                                 to: str = "") -> dict:
    """Переводит задачу по message_id в статус in_progress.

    Вызывается после успешной отправки письма. Возвращает:
      {ok: bool, task_id, prev_status, new_status}
    Если задачи нет — ok=False, reason='no_task'.
    Если уже in_progress / done / cancelled — не трогаем.
    """
    if not message_id:
        return {"ok": False, "reason": "нет message_id"}

    _ensure_schema()
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, status, note FROM mail_tasks WHERE message_id = ?",
            (message_id,)).fetchone()
        if not row:
            return {"ok": False, "reason": "no_task"}

        prev = row["status"]
        if prev not in ("open",):
            return {"ok": False, "reason": f"already_{prev}",
                    "task_id": row["id"], "prev_status": prev}

        # Обновляем статус и дописываем заметку
        extra = f"\n[{now[:16].replace('T', ' ')}] → в работе: "
        extra += f"отправлен ответ" + (f" на {to}" if to else "")
        new_note = (row["note"] or "") + extra

        conn.execute(
            "UPDATE mail_tasks SET status='in_progress', "
            "note=?, updated_at=? WHERE id=?",
            (new_note, now, row["id"]))
        conn.commit()
        task_id = row["id"]

    # Публикуем в SSE
    try:
        from redcat.web.web_stream import publish
        publish("task:status_changed", {
            "task_id": task_id,
            "message_id": message_id,
            "from": prev, "to": "in_progress",
        })
    except Exception:
        pass

    return {"ok": True, "task_id": task_id,
            "prev_status": prev, "new_status": "in_progress"}

