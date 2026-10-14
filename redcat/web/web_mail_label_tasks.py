"""web_mail_label_tasks — авто-задачи при постановке меток.

Когда на письмо ставится определённая метка (например 📩 «Ответить»),
модуль автоматически создаёт задачу. Настройки хранятся в studio.db,
таблица mail_label_task_config:

    label       TEXT PRIMARY KEY   — какая метка
    enabled     INTEGER            — включено
    priority    TEXT               — приоритет задачи
    due_days    INTEGER            — срок в днях (NULL = без срока)
    assignee    TEXT               — кому назначить
    title_prefix TEXT              — что дописать к теме
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from redcat.web.web_config import STUDIO_DB

# Значения по умолчанию для известных меток. Пользователь может
# редактировать. Метки, которых здесь нет, задач не создают.
DEFAULT_CONFIG = {
    "reply": {"enabled": 1, "priority": "normal", "due_days": 1,
              "assignee": "", "title_prefix": ""},
    "todo":  {"enabled": 1, "priority": "high",   "due_days": 3,
              "assignee": "", "title_prefix": ""},
    "star":  {"enabled": 0, "priority": "normal", "due_days": None,
              "assignee": "", "title_prefix": ""},
    "done":  {"enabled": 0, "priority": "normal", "due_days": None,
              "assignee": "", "title_prefix": ""},
    "spam":  {"enabled": 0, "priority": "low",    "due_days": None,
              "assignee": "", "title_prefix": ""},
}


def _ensure_schema() -> None:
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS mail_label_task_config (
            label TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            priority TEXT NOT NULL DEFAULT 'normal',
            due_days INTEGER,
            assignee TEXT,
            title_prefix TEXT)""")
        conn.commit()
        # Досоздаём дефолты для отсутствующих
        for lbl, cfg in DEFAULT_CONFIG.items():
            row = conn.execute(
                "SELECT 1 FROM mail_label_task_config WHERE label=?",
                (lbl,)).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO mail_label_task_config "
                    "(label, enabled, priority, due_days, assignee, title_prefix) "
                    "VALUES (?,?,?,?,?,?)",
                    (lbl, cfg["enabled"], cfg["priority"],
                     cfg["due_days"], cfg["assignee"], cfg["title_prefix"]))
        conn.commit()


def get_config() -> dict:
    """{label: {enabled, priority, due_days, assignee, title_prefix}}."""
    _ensure_schema()
    out = {}
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM mail_label_task_config").fetchall()
    for r in rows:
        out[r["label"]] = {
            "enabled": bool(r["enabled"]),
            "priority": r["priority"] or "normal",
            "due_days": r["due_days"],
            "assignee": r["assignee"] or "",
            "title_prefix": r["title_prefix"] or "",
        }
    return out


def set_config(label: str, **fields) -> dict:
    _ensure_schema()
    allowed = {"enabled", "priority", "due_days", "assignee", "title_prefix"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return {"ok": False, "error": "нет полей"}
    # Нормализация
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
    if "due_days" in updates and updates["due_days"] in ("", None):
        updates["due_days"] = None
    sets = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [label]
    with sqlite3.connect(str(STUDIO_DB), timeout=10) as conn:
        # Убедимся, что строка есть
        r = conn.execute(
            "SELECT 1 FROM mail_label_task_config WHERE label=?",
            (label,)).fetchone()
        if not r:
            conn.execute(
                "INSERT INTO mail_label_task_config (label) VALUES (?)",
                (label,))
        conn.execute(
            f"UPDATE mail_label_task_config SET {sets} WHERE label=?", vals)
        conn.commit()
    return {"ok": True}


def on_label_added(table_name: str, message_id: str, label: str) -> dict:
    """Хук: вызывается из web_mail_labels.add_label.

    Если для метки включено авто-создание — создаёт задачу.
    Возвращает {created: bool, task_id: int|None, reason: str}.
    """
    cfg = get_config().get(label)
    if not cfg or not cfg.get("enabled"):
        return {"created": False, "reason": "выключено"}

    # Получаем данные письма
    try:
        from redcat.web.web_data import db_for_table
        import sqlite3 as _sq
        db = db_for_table(table_name)
        conn = _sq.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = _sq.Row
        try:
            row = conn.execute(
                f'SELECT subject, from_addr, from_name FROM "{table_name}" '
                f'WHERE message_id = ?', (message_id,)).fetchone()
        finally:
            conn.close()
    except Exception as e:
        return {"created": False,
                "reason": f"не прочитал письмо: {type(e).__name__}: {e}"}

    if not row:
        return {"created": False, "reason": "письмо не найдено"}

    subject = row["subject"] or "(без темы)"
    from_addr = row["from_addr"] or ""

    title = subject
    if cfg.get("title_prefix"):
        title = f"{cfg['title_prefix']} {subject}".strip()

    due_date = ""
    if cfg.get("due_days"):
        try:
            due_date = (datetime.now() + timedelta(
                days=int(cfg["due_days"]))).date().isoformat()
        except (TypeError, ValueError):
            pass

    try:
        from redcat.web.web_mail_tasks import create_task
        res = create_task(
            message_id=message_id,
            subject=subject,
            from_addr=from_addr,
            title=title,
            priority=cfg.get("priority") or "normal",
            due_date=due_date,
            assignee=cfg.get("assignee") or "",
            note=f"авто-задача по метке «{label}»",
        )
    except Exception as e:
        return {"created": False,
                "reason": f"{type(e).__name__}: {e}"}

    return {
        "created": bool(res.get("ok") and not res.get("existing")),
        "task_id": res.get("id"),
        "existing": bool(res.get("existing")),
        "reason": "ok" if res.get("ok") else "ошибка",
    }
